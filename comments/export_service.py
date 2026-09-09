"""
Xuất dữ liệu ra các định dạng chuẩn của ngành NLP.

Nguyên tắc:
1. STREAMING: bộ nhớ không phụ thuộc kích thước dữ liệu.
2. BIO/IOB2 chính xác: dựa trên `Token.span_group`, không suy đoán từ việc các
   nhãn liền kề có giống nhau hay không.
3. Không bịa dữ liệu: `is_meaningful = NULL` (chưa xác định) phải xuất ra khác
   với `False` (đã xác định là vô nghĩa).
4. Lọc theo trạng thái duyệt: dữ liệu công bố chỉ nên gồm nhãn đã chốt.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import NamedTuple
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr as xml_quoteattr

from django.db.models import Q
from django.http import HttpResponse, StreamingHttpResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import Comment, ExportRecord

logger = logging.getLogger(__name__)

CHUNK_SIZE = 500
OUTSIDE = 'O'


# ---------------------------------------------------------------------------
# Tiện ích
# ---------------------------------------------------------------------------
def safe_filename(value: str, max_length: int = 60) -> str:
    """Chuẩn hoá chuỗi thành tên file an toàn cho header Content-Disposition."""
    value = unicodedata.normalize('NFKD', str(value or 'export'))
    value = value.encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^A-Za-z0-9._-]+', '-', value).strip('-.')
    return (value or 'export')[:max_length]


def tri_state(value) -> str:
    """
    Xuất giá trị ba trạng thái đúng ngữ nghĩa.

    None nghĩa là chưa xác định, và phải ghi thành 'unknown'. Ghi thành 'false'
    là khẳng định sai rằng bình luận đã được xét và bị coi là vô nghĩa.
    """
    if value is None:
        return 'unknown'
    return 'true' if value else 'false'


def _label_filter(label_name):
    return (
        Q(gold_label__label__name=label_name)
        | Q(manual_label__label__name=label_name)
        | Q(ai_label__label__name=label_name)
    )


def _comments_queryset(project, youtube_link, filter_label='all', *,
                       review_filter='all'):
    """
    QuerySet bình luận để xuất.

    `review_filter`:
      - 'all'      : mọi bình luận (kể cả chưa gán nhãn), dùng để sao lưu.
      - 'labelled' : có ít nhất một nhãn (AI hoặc người).
      - 'human'    : có nhãn do người gán.
      - 'gold'     : chỉ nhãn đã chốt (đồng thuận hoặc đã phân xử), dùng để
                     công bố dataset và huấn luyện mô hình.
    """
    if youtube_link is not None:
        queryset = Comment.objects.filter(youtube_link=youtube_link)
    else:
        queryset = Comment.objects.filter(youtube_link__project=project)

    if filter_label and filter_label != 'all':
        queryset = queryset.filter(_label_filter(filter_label))

    if review_filter == 'labelled':
        queryset = queryset.filter(
            Q(gold_label__isnull=False) | Q(manual_label__isnull=False)
            | Q(ai_label__isnull=False) | Q(is_meaningful=False)
        )
    elif review_filter == 'human':
        queryset = queryset.filter(
            Q(manual_label__isnull=False) | Q(gold_label__isnull=False)
            | Q(is_meaningful=False)
        )
    elif review_filter == 'gold':
        queryset = queryset.filter(
            Q(review_status__in=('agreed', 'adjudicated')) | Q(is_meaningful=False)
        )

    return queryset.select_related(
        'ai_label__label', 'manual_label__label', 'gold_label__label', 'youtube_link'
    ).prefetch_related(
        'tokens__ai_label__label',
        'tokens__manual_label__label',
        'tokens__gold_label__label',
        'annotations__annotator',
        'annotations__project_label__label',
    ).order_by('fetched_at')


class _ProgressQuerySet:
    """
    Bọc queryset để đếm số bình luận đã xử lý trong lúc sinh file.

    Mọi builder đều lấy dữ liệu qua `_iter_comments()`, và hàm đó chỉ gọi đúng
    một phương thức là `.iterator()`, nên chỉ cần proxy phương thức này là đủ
    cho tất cả định dạng. Nhờ vậy tiến độ phản ánh công việc thật đang chạy chứ
    không phải con số phỏng đoán theo thời gian.
    """

    def __init__(self, queryset, on_item):
        self._queryset = queryset
        self._on_item = on_item

    def iterator(self, chunk_size=None):
        for index, comment in enumerate(
            self._queryset.iterator(chunk_size=chunk_size or CHUNK_SIZE), start=1
        ):
            self._on_item(index)
            yield comment

    def __getattr__(self, name):
        return getattr(self._queryset, name)


def _iter_comments(queryset):
    return queryset.iterator(chunk_size=CHUNK_SIZE)


def _comment_label(comment) -> str:
    effective = comment.effective_label
    return effective.display_name if effective else OUTSIDE


def _token_label(token_dict) -> str:
    effective = token_dict.get('effective_label')
    return effective.get('name', OUTSIDE) if effective else OUTSIDE


# ---------------------------------------------------------------------------
# BIO / IOB2
# ---------------------------------------------------------------------------
def bio_tags(tokens) -> list[str]:
    """
    Chuyển nhãn token phẳng thành thẻ BIO (IOB2).

    Ranh giới cụm lấy từ `span_group`: hai token liền kề cùng nhãn nhưng khác
    span_group là HAI cụm riêng biệt (`B-x`, `B-x`), cùng span_group là một cụm
    (`B-x`, `I-x`). Đây là thông tin không thể khôi phục nếu chỉ có nhãn phẳng.

    Token cũ chưa có span_group (dữ liệu trước khi bổ sung trường này) sẽ được
    gộp theo chuỗi liền kề cùng nhãn: cách diễn giải hợp lý nhất còn lại.
    """
    tags: list[str] = []
    previous_label = None
    previous_group = None

    for token in tokens:
        label = _token_label(token)
        if label == OUTSIDE:
            tags.append(OUTSIDE)
            previous_label = None
            previous_group = None
            continue

        group = token.get('span_group')
        if previous_label != label:
            continues = False
        elif group is None and previous_group is None:
            # Dữ liệu cũ: gộp chuỗi liền kề cùng nhãn.
            continues = True
        else:
            continues = group is not None and group == previous_group

        tags.append(f'{"I" if continues else "B"}-{label}')
        previous_label = label
        previous_group = group

    return tags


def entity_spans(comment) -> list[dict]:
    """
    Gom token đã gán nhãn thành danh sách cụm theo offset ký tự.

    Trả về [{'start', 'end', 'label', 'text'}]: dạng mà spaCy, Doccano và
    Label Studio đều dùng.
    """
    tokens = comment.display_tokens
    tags = bio_tags(tokens)
    spans: list[dict] = []

    for token, tag in zip(tokens, tags, strict=False):
        if tag == OUTSIDE:
            continue
        prefix, _, label = tag.partition('-')
        if prefix == 'B' or not spans or spans[-1]['label'] != label:
            spans.append({
                'start': token['start_offset'],
                'end': token['end_offset'],
                'label': label,
            })
        else:
            spans[-1]['end'] = token['end_offset']

    text = comment.text or ''
    for span in spans:
        span['text'] = text[span['start']:span['end']]
    return spans


def _annotator_rows(comment):
    """Ai đã gán nhãn gì: dữ liệu cần để tính lại IAA từ chính file export."""
    return [{
        'annotator': annotation.annotator.username if annotation.annotator else 'AI',
        'source': annotation.source,
        'label': annotation.label_name,
    } for annotation in comment.annotations.all()]


def _base_entry(comment) -> dict:
    return {
        'id': str(comment.youtube_comment_id),
        'text': comment.text,
        'source_text': comment.source_text or comment.text,
        'label': _comment_label(comment),
        'ai_label': comment.ai_label.display_name if comment.ai_label else None,
        'manual_label': comment.manual_label.display_name if comment.manual_label else None,
        'gold_label': comment.gold_label.display_name if comment.gold_label else None,
        'review_status': comment.review_status,
        # None = chưa xác định; giữ nguyên None thay vì ép thành false.
        'is_meaningful': comment.is_meaningful,
        'annotators': _annotator_rows(comment),
    }


# ---------------------------------------------------------------------------
# Định dạng: JSON
# ---------------------------------------------------------------------------
def stream_json_sentence(queryset, **_kwargs):
    yield '[\n'
    first = True
    for comment in _iter_comments(queryset):
        entry = _base_entry(comment)
        entry.update({'author': comment.author, 'like_count': comment.like_count})
        if not first:
            yield ',\n'
        first = False
        yield json.dumps(entry, ensure_ascii=False, indent=2)
    yield '\n]\n'


def stream_json_token(queryset, **_kwargs):
    yield '[\n'
    first = True
    for comment in _iter_comments(queryset):
        entry = _base_entry(comment)
        tokens = comment.display_tokens
        tags = bio_tags(tokens)
        entry['tokens'] = [{
            'text': token['text'],
            'label': _token_label(token),
            'bio': tag,
            'ai_label': (token.get('ai_label') or {}).get('name'),
            'manual_label': (token.get('manual_label') or {}).get('name'),
            'position': token['position'],
            'start_offset': token['start_offset'],
            'end_offset': token['end_offset'],
        } for token, tag in zip(tokens, tags, strict=False)]
        entry['spans'] = entity_spans(comment)
        if not first:
            yield ',\n'
        first = False
        yield json.dumps(entry, ensure_ascii=False, indent=2)
    yield '\n]\n'


def stream_jsonl(queryset, **_kwargs):
    """JSON Lines: mỗi dòng một bản ghi, nạp trực tiếp vào pipeline."""
    for comment in _iter_comments(queryset):
        entry = _base_entry(comment)
        tokens = comment.display_tokens
        tags = bio_tags(tokens)
        entry['tokens'] = [{
            'text': token['text'],
            'label': _token_label(token),
            'bio': tag,
            'start': token['start_offset'],
            'end': token['end_offset'],
        } for token, tag in zip(tokens, tags, strict=False)]
        entry['spans'] = entity_spans(comment)
        yield json.dumps(entry, ensure_ascii=False) + '\n'


# ---------------------------------------------------------------------------
# Định dạng: CoNLL-2003 (text thuần), chuẩn của bài toán gán nhãn chuỗi
# ---------------------------------------------------------------------------
def stream_conll(queryset, **_kwargs):
    """
    CoNLL-2003: mỗi dòng một token, phân tách bằng TAB, dòng trống ngăn câu.

    Cột: TOKEN <TAB> nhãn_BIO
    Đây là định dạng mà spaCy, Flair, HuggingFace, CRFsuite đều đọc được.
    Khoá "xml_conll" cũ trỏ về exporter XML, không phải về đây.
    """
    for comment in _iter_comments(queryset):
        tokens = comment.display_tokens
        if not tokens:
            continue
        tags = bio_tags(tokens)
        yield f'# id = {comment.youtube_comment_id}\n'
        yield f'# label = {_comment_label(comment)}\n'
        for token, tag in zip(tokens, tags, strict=False):
            # Token không được chứa TAB hay xuống dòng, nếu không sẽ hỏng cột.
            text = token['text'].replace('\t', ' ').replace('\n', ' ')
            yield f'{text}\t{tag}\n'
        yield '\n'


def stream_conll_full(queryset, **_kwargs):
    """CoNLL mở rộng: TOKEN, offset đầu, offset cuối, nhãn BIO, nhãn cấp câu."""
    yield '# token\tstart\tend\tbio\tsentence_label\n'
    for comment in _iter_comments(queryset):
        tokens = comment.display_tokens
        if not tokens:
            continue
        tags = bio_tags(tokens)
        sentence_label = _comment_label(comment)
        yield f'# id = {comment.youtube_comment_id}\n'
        for token, tag in zip(tokens, tags, strict=False):
            text = token['text'].replace('\t', ' ').replace('\n', ' ')
            yield (
                f'{text}\t{token["start_offset"]}\t{token["end_offset"]}'
                f'\t{tag}\t{sentence_label}\n'
            )
        yield '\n'


# ---------------------------------------------------------------------------
# Định dạng: HuggingFace datasets
# ---------------------------------------------------------------------------
def stream_hf_jsonl(queryset, project=None, **_kwargs):
    """
    JSONL theo bố cục thư viện `datasets` của HuggingFace:
        {"id", "tokens": [...], "ner_tags": [int...], "sentence_label": ...}

    Dòng đầu tiên là bản ghi metadata chứa danh sách nhãn, để ánh xạ ner_tags
    (số nguyên) trở lại tên nhãn:
        {"__meta__": true, "label_names": ["O", "B-toxic", "I-toxic", ...]}
    """
    label_names = _bio_label_names(project)
    index_of = {name: index for index, name in enumerate(label_names)}

    yield json.dumps({
        '__meta__': True,
        'label_names': label_names,
        'project': project.name if project else None,
        'exported_at': timezone.now().isoformat(),
    }, ensure_ascii=False) + '\n'

    for comment in _iter_comments(queryset):
        tokens = comment.display_tokens
        if not tokens:
            continue
        tags = bio_tags(tokens)
        yield json.dumps({
            'id': str(comment.youtube_comment_id),
            'tokens': [token['text'] for token in tokens],
            'ner_tags': [index_of.get(tag, 0) for tag in tags],
            'bio_tags': tags,
            'sentence_label': _comment_label(comment),
            'review_status': comment.review_status,
        }, ensure_ascii=False) + '\n'


def _bio_label_names(project) -> list[str]:
    """Danh sách nhãn BIO theo thứ tự ổn định: O, B-x, I-x, B-y, I-y..."""
    names = [OUTSIDE]
    if project is None:
        return names
    from .models import ProjectLabel

    labels = sorted(
        {
            pl.display_name
            for pl in ProjectLabel.objects.filter(project=project).select_related('label')
        }
    )
    for name in labels:
        if name == OUTSIDE:
            continue
        names.extend([f'B-{name}', f'I-{name}'])
    return names


# ---------------------------------------------------------------------------
# Định dạng: spaCy
# ---------------------------------------------------------------------------
def stream_spacy_json(queryset, **_kwargs):
    """
    Định dạng huấn luyện của spaCy:
        [["văn bản", {"entities": [[start, end, "nhãn"], ...]}], ...]
    Nạp bằng `spacy.training.Example.from_dict` hoặc convert sang .spacy DocBin.
    """
    yield '[\n'
    first = True
    for comment in _iter_comments(queryset):
        spans = entity_spans(comment)
        record = [
            comment.text or '',
            {
                'entities': [[s['start'], s['end'], s['label']] for s in spans],
                'cats': {_comment_label(comment): 1.0},
            },
        ]
        if not first:
            yield ',\n'
        first = False
        yield json.dumps(record, ensure_ascii=False)
    yield '\n]\n'


# ---------------------------------------------------------------------------
# Định dạng: công cụ gán nhãn khác (nhập/xuất qua lại)
# ---------------------------------------------------------------------------
def stream_doccano_jsonl(queryset, **_kwargs):
    """
    JSONL của Doccano: công cụ gán nhãn mã nguồn mở phổ biến nhất:
        {"text": ..., "label": [[start, end, "nhãn"], ...], "cats": [...]}
    Cho phép đưa dữ liệu sang Doccano để đối chiếu hoặc gán nhãn bổ sung.
    """
    for comment in _iter_comments(queryset):
        sentence_label = _comment_label(comment)
        yield json.dumps({
            'id': str(comment.youtube_comment_id),
            'text': comment.text or '',
            'label': [
                [span['start'], span['end'], span['label']]
                for span in entity_spans(comment)
            ],
            'cats': [] if sentence_label == OUTSIDE else [sentence_label],
        }, ensure_ascii=False) + '\n'


def stream_label_studio_json(queryset, **_kwargs):
    """
    JSON nhập liệu của Label Studio (dạng pre-annotation):
        [{"data": {"text": ...}, "predictions": [{"result": [...]}]}]
    """
    yield '[\n'
    first = True
    for comment in _iter_comments(queryset):
        results = []
        for index, span in enumerate(entity_spans(comment)):
            results.append({
                'id': f'{comment.youtube_comment_id}-{index}',
                'from_name': 'label',
                'to_name': 'text',
                'type': 'labels',
                'value': {
                    'start': span['start'],
                    'end': span['end'],
                    'text': span['text'],
                    'labels': [span['label']],
                },
            })
        sentence_label = _comment_label(comment)
        if sentence_label != OUTSIDE:
            results.append({
                'from_name': 'sentiment',
                'to_name': 'text',
                'type': 'choices',
                'value': {'choices': [sentence_label]},
            })

        record = {
            'data': {
                'text': comment.text or '',
                'ref_id': str(comment.youtube_comment_id),
            },
            'predictions': [{'model_version': 'annotahub', 'result': results}],
        }
        if not first:
            yield ',\n'
        first = False
        yield json.dumps(record, ensure_ascii=False)
    yield '\n]\n'


# ---------------------------------------------------------------------------
# Định dạng: huấn luyện LLM
# ---------------------------------------------------------------------------
def stream_json_llm(queryset, **_kwargs):
    """
    JSONL dạng hội thoại để fine-tune LLM.

    Chỉ nên dùng với bộ lọc `review=gold`. Xuất cả bình luận chưa ai gán
    nhãn sẽ dạy mô hình rằng "chưa gán nhãn" nghĩa là "O": làm hỏng dữ liệu
    huấn luyện. Hàm generate_export cảnh báo nếu chọn sai bộ lọc.
    """
    for comment in _iter_comments(queryset):
        spans = entity_spans(comment)
        answer = {
            'label': _comment_label(comment),
            'is_meaningful': bool(comment.is_meaningful),
            'spans': [
                {'text': span['text'], 'label': span['label']} for span in spans
            ],
        }
        yield json.dumps({
            'messages': [
                {
                    'role': 'system',
                    'content': 'Bạn là hệ thống gán nhãn nội dung bình luận tiếng Việt. '
                               'Trả về JSON gồm nhãn cấp câu và các cụm từ mang nhãn.',
                },
                {'role': 'user', 'content': comment.text or ''},
                {'role': 'assistant',
                 'content': json.dumps(answer, ensure_ascii=False)},
            ],
            'metadata': {
                'id': str(comment.youtube_comment_id),
                'review_status': comment.review_status,
                'annotator_count': comment.manual_annotation_count,
            },
        }, ensure_ascii=False) + '\n'


# ---------------------------------------------------------------------------
# Định dạng: XML
# ---------------------------------------------------------------------------
def stream_xml(queryset, project=None, **_kwargs):
    """XML có cấu trúc, kèm thẻ BIO và danh sách cụm."""
    project_name = project.name if project else 'export'
    yield '<?xml version="1.0" encoding="UTF-8"?>\n'
    yield (f'<corpus name={xml_quoteattr(str(project_name))} '
           f'exported={xml_quoteattr(timezone.now().isoformat())}>\n')
    for comment in _iter_comments(queryset):
        tokens = comment.display_tokens
        tags = bio_tags(tokens)
        yield (
            f'  <sentence id={xml_quoteattr(str(comment.youtube_comment_id))} '
            f'label={xml_quoteattr(_comment_label(comment))} '
            f'review_status={xml_quoteattr(comment.review_status)} '
            f'is_meaningful={xml_quoteattr(tri_state(comment.is_meaningful))}>\n'
        )
        yield f'    <text>{xml_escape(comment.text or "")}</text>\n'
        yield '    <tokens>\n'
        for token, tag in zip(tokens, tags, strict=False):
            yield (
                f'      <token position="{token["position"]}" '
                f'start="{token["start_offset"]}" end="{token["end_offset"]}" '
                f'label={xml_quoteattr(_token_label(token))} '
                f'bio={xml_quoteattr(tag)}>'
                f'{xml_escape(token["text"])}</token>\n'
            )
        yield '    </tokens>\n'
        spans = entity_spans(comment)
        if spans:
            yield '    <spans>\n'
            for span in spans:
                yield (
                    f'      <span start="{span["start"]}" end="{span["end"]}" '
                    f'label={xml_quoteattr(span["label"])}>'
                    f'{xml_escape(span["text"])}</span>\n'
                )
            yield '    </spans>\n'
        yield '  </sentence>\n'
    yield '</corpus>\n'


# ---------------------------------------------------------------------------
# Định dạng: CSV
# ---------------------------------------------------------------------------
class _Echo:
    """File giả cho csv.writer khi streaming."""

    def write(self, value):
        return value


def stream_csv_sentence(queryset, **_kwargs):
    writer = csv.writer(_Echo())
    yield '﻿'  # BOM để Excel mở đúng tiếng Việt
    yield writer.writerow([
        'id', 'text', 'source_text', 'label', 'ai_label', 'manual_label',
        'gold_label', 'review_status', 'annotator_count', 'is_meaningful',
        'author', 'like_count',
    ])
    for comment in _iter_comments(queryset):
        yield writer.writerow([
            comment.youtube_comment_id,
            comment.text,
            comment.source_text or comment.text,
            _comment_label(comment),
            comment.ai_label.display_name if comment.ai_label else '',
            comment.manual_label.display_name if comment.manual_label else '',
            comment.gold_label.display_name if comment.gold_label else '',
            comment.review_status,
            comment.manual_annotation_count,
            tri_state(comment.is_meaningful),
            comment.author,
            comment.like_count,
        ])


def stream_csv_token(queryset, **_kwargs):
    writer = csv.writer(_Echo())
    yield '﻿'
    yield writer.writerow([
        'comment_id', 'comment_label', 'review_status', 'is_meaningful',
        'token_text', 'token_label', 'bio', 'token_ai_label', 'token_manual_label',
        'position', 'start_offset', 'end_offset', 'span_group',
    ])
    for comment in _iter_comments(queryset):
        comment_label = _comment_label(comment)
        meaningful = tri_state(comment.is_meaningful)
        tokens = comment.display_tokens
        tags = bio_tags(tokens)
        for token, tag in zip(tokens, tags, strict=False):
            yield writer.writerow([
                comment.youtube_comment_id,
                comment_label,
                comment.review_status,
                meaningful,
                token['text'],
                _token_label(token),
                tag,
                (token.get('ai_label') or {}).get('name', ''),
                (token.get('manual_label') or {}).get('name', ''),
                token['position'],
                token['start_offset'],
                token['end_offset'],
                token.get('span_group') or '',
            ])


def stream_csv_spans(queryset, **_kwargs):
    """Mỗi dòng một cụm đã gán nhãn: gọn nhất để rà soát thủ công."""
    writer = csv.writer(_Echo())
    yield '﻿'
    yield writer.writerow([
        'comment_id', 'comment_text', 'comment_label',
        'span_text', 'span_label', 'start', 'end',
    ])
    for comment in _iter_comments(queryset):
        comment_label = _comment_label(comment)
        for span in entity_spans(comment):
            yield writer.writerow([
                comment.youtube_comment_id,
                comment.text,
                comment_label,
                span['text'],
                span['label'],
                span['start'],
                span['end'],
            ])


def stream_csv_annotations(queryset, **_kwargs):
    """Xuất từng annotation riêng lẻ: dữ liệu thô để tính lại IAA độc lập."""
    writer = csv.writer(_Echo())
    yield '﻿'
    yield writer.writerow([
        'comment_id', 'text', 'annotator', 'source', 'label',
        'review_status', 'created_at',
    ])
    for comment in _iter_comments(queryset):
        for annotation in comment.annotations.all():
            yield writer.writerow([
                comment.youtube_comment_id,
                comment.text,
                annotation.annotator.username if annotation.annotator else 'AI',
                annotation.source,
                annotation.label_name,
                comment.review_status,
                annotation.created_at.isoformat(),
            ])


# ---------------------------------------------------------------------------
# Định dạng: Excel
# ---------------------------------------------------------------------------
def build_xlsx(queryset, project=None, **_kwargs) -> bytes:
    """
    Sổ Excel nhiều sheet cho người rà soát không dùng công cụ kỹ thuật.

    Không streaming được (định dạng xlsx là zip, phải dựng trọn gói), nên có
    giới hạn số dòng để không làm cạn bộ nhớ.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        raise RuntimeError(
            'Xuất Excel cần thư viện openpyxl. Cài bằng: pip install openpyxl'
        ) from exc

    max_rows = 100_000
    workbook = Workbook()

    header_font = Font(bold=True, color='FFFFFF')
    header_fill = PatternFill('solid', fgColor='1A1A2E')

    def write_header(sheet, columns):
        sheet.append(columns)
        for index, _column in enumerate(columns, start=1):
            cell = sheet.cell(row=1, column=index)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(vertical='center')
        sheet.freeze_panes = 'A2'

    sentences = workbook.active
    sentences.title = 'Câu'
    write_header(sentences, [
        'id', 'text', 'label', 'ai_label', 'manual_label', 'gold_label',
        'review_status', 'annotator_count', 'is_meaningful', 'author', 'like_count',
    ])

    spans_sheet = workbook.create_sheet('Cụm đã gán nhãn')
    write_header(spans_sheet, [
        'comment_id', 'span_text', 'span_label', 'start', 'end', 'comment_text',
    ])

    count = 0
    for comment in _iter_comments(queryset):
        if count >= max_rows:
            break
        count += 1
        sentences.append([
            str(comment.youtube_comment_id),
            comment.text,
            _comment_label(comment),
            comment.ai_label.display_name if comment.ai_label else '',
            comment.manual_label.display_name if comment.manual_label else '',
            comment.gold_label.display_name if comment.gold_label else '',
            comment.review_status,
            comment.manual_annotation_count,
            tri_state(comment.is_meaningful),
            comment.author,
            comment.like_count,
        ])
        for span in entity_spans(comment):
            spans_sheet.append([
                str(comment.youtube_comment_id),
                span['text'], span['label'], span['start'], span['end'],
                comment.text,
            ])

    for sheet, widths in (
        (sentences, [26, 70, 14, 14, 14, 14, 14, 10, 14, 20, 10]),
        (spans_sheet, [26, 28, 16, 8, 8, 70]),
    ):
        for index, width in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = width

    info = workbook.create_sheet('Thông tin')
    info.append(['Dự án', project.name if project else ''])
    info.append(['Xuất lúc', timezone.now().strftime('%Y-%m-%d %H:%M:%S')])
    info.append(['Số bình luận', count])
    if count >= max_rows:
        info.append(['CẢNH BÁO', f'Đã cắt ở {max_rows} dòng. Dùng CSV cho dữ liệu lớn hơn.'])
    info.column_dimensions['A'].width = 18
    info.column_dimensions['B'].width = 60

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Đăng ký định dạng
# ---------------------------------------------------------------------------
class ExportFormat(NamedTuple):
    """
    Metadata của một định dạng xuất.

    `label`, `group`, `description` dùng gettext_lazy nên được dịch theo ngôn
    ngữ đang hoạt động tại thời điểm render, không phải lúc nạp module.
    """

    builder: object
    content_type: str
    extension: str
    label: object
    group: object
    streaming: bool
    description: object
    sample: str


# Tên nhóm: khai báo riêng để tái dùng và dịch một lần.
GROUP_NLP = _('Chuẩn NLP')
GROUP_TOOLS = _('Công cụ gán nhãn khác')
GROUP_GENERAL = _('Tổng quát')
GROUP_SHEET = _('Bảng tính')
GROUP_TRAINING = _('Huấn luyện mô hình')


EXPORT_FORMATS = {
    # --- Chuẩn gán nhãn chuỗi ---
    'conll': ExportFormat(
        stream_conll, 'text/plain; charset=utf-8', '.conll',
        _('CoNLL-2003 — token + nhãn BIO'), GROUP_NLP, True,
        _('Định dạng chuẩn của bài toán gán nhãn chuỗi (NER, chunking): mỗi dòng '
          'một token, phân tách bằng TAB, dòng trống ngăn câu. spaCy, Flair, '
          'HuggingFace và CRFsuite đều đọc trực tiếp được.'),
        'thằng\tB-toxic\n'
        'ngu\tI-toxic\n'
        'này\tO\n'
        '\n'
        '# câu tiếp theo…',
    ),
    'conll_full': ExportFormat(
        stream_conll_full, 'text/plain; charset=utf-8', '.tsv',
        _('CoNLL mở rộng — kèm offset và nhãn câu'), GROUP_NLP, True,
        _('Như CoNLL nhưng thêm vị trí ký tự đầu/cuối của token và nhãn cấp câu. '
          'Dùng khi pipeline cần ánh xạ ngược về văn bản gốc.'),
        '# token\tstart\tend\tbio\tsentence_label\n'
        'thằng\t0\t5\tB-toxic\ttoxic\n'
        'ngu\t6\t9\tI-toxic\ttoxic',
    ),
    'hf_jsonl': ExportFormat(
        stream_hf_jsonl, 'application/jsonl', '.jsonl',
        _('HuggingFace datasets — tokens + ner_tags'), GROUP_NLP, True,
        _('Nạp trực tiếp bằng datasets.load_dataset("json", ...). Dòng đầu tiên là '
          'metadata chứa label_names để ánh xạ ner_tags (số) sang tên nhãn.'),
        '{"__meta__": true, "label_names": ["O", "B-toxic", "I-toxic"]}\n'
        '{"id": "...", "tokens": ["thằng", "ngu", "này"],\n'
        ' "ner_tags": [1, 2, 0], "bio_tags": ["B-toxic", "I-toxic", "O"]}',
    ),
    'spacy_json': ExportFormat(
        stream_spacy_json, 'application/json', '.json',
        _('spaCy training — text + entities'), GROUP_NLP, True,
        _('Định dạng huấn luyện của spaCy. Nạp bằng Example.from_dict hoặc chuyển '
          'sang DocBin (.spacy). Cụm từ được mô tả bằng offset ký tự.'),
        '[\n'
        '  ["thằng ngu này nói linh tinh",\n'
        '   {"entities": [[0, 9, "toxic"]], "cats": {"toxic": 1.0}}]\n'
        ']',
    ),

    # --- Công cụ gán nhãn khác ---
    'doccano_jsonl': ExportFormat(
        stream_doccano_jsonl, 'application/jsonl', '.jsonl',
        _('Doccano JSONL'), GROUP_TOOLS, True,
        _('Nhập ngược vào Doccano để đối chiếu chéo hoặc gán nhãn bổ sung. '
          'Nhãn cụm ở dạng [start, end, "tên nhãn"].'),
        '{"id": "...", "text": "thằng ngu này",\n'
        ' "label": [[0, 9, "toxic"]], "cats": ["toxic"]}',
    ),
    'label_studio_json': ExportFormat(
        stream_label_studio_json, 'application/json', '.json',
        _('Label Studio — pre-annotation'), GROUP_TOOLS, True,
        _('Nạp vào Label Studio dưới dạng dự đoán sẵn (predictions) để người gán '
          'nhãn chỉ cần xác nhận hoặc sửa.'),
        '[{"data": {"text": "..."},\n'
        '  "predictions": [{"result": [\n'
        '    {"type": "labels", "value": {"start": 0, "end": 9,\n'
        '     "labels": ["toxic"]}}]}]}]',
    ),

    # --- Dạng tổng quát ---
    'json_sentence': ExportFormat(
        stream_json_sentence, 'application/json', '.json',
        _('JSON — cấp câu'), GROUP_GENERAL, True,
        _('Mỗi bình luận một đối tượng, không có dữ liệu token. Phù hợp cho bài '
          'toán phân loại cả câu.'),
        '[{\n'
        '  "id": "...", "text": "thằng ngu này",\n'
        '  "label": "toxic", "ai_label": "toxic", "gold_label": "toxic",\n'
        '  "review_status": "agreed", "is_meaningful": true,\n'
        '  "annotators": [{"annotator": "alice", "label": "toxic"}]\n'
        '}]',
    ),
    'json_token': ExportFormat(
        stream_json_token, 'application/json', '.json',
        _('JSON — cấp token (kèm BIO và cụm)'), GROUP_GENERAL, True,
        _('Đầy đủ nhất: nhãn câu, từng token kèm thẻ BIO và offset, cùng danh sách '
          'cụm đã gán nhãn.'),
        '[{\n'
        '  "id": "...", "text": "thằng ngu này", "label": "toxic",\n'
        '  "tokens": [{"text": "thằng", "label": "toxic", "bio": "B-toxic",\n'
        '              "start_offset": 0, "end_offset": 5}],\n'
        '  "spans": [{"start": 0, "end": 9, "text": "thằng ngu",\n'
        '             "label": "toxic"}]\n'
        '}]',
    ),
    'jsonl': ExportFormat(
        stream_jsonl, 'application/jsonl', '.jsonl',
        _('JSONL — cấp token (mỗi dòng một bản ghi)'), GROUP_GENERAL, True,
        _('Cùng nội dung với JSON cấp token nhưng mỗi dòng là một bản ghi độc lập '
          '— đọc theo luồng được, không cần nạp cả tệp vào bộ nhớ.'),
        '{"id": "...", "text": "...", "label": "toxic",\n'
        ' "tokens": [...], "spans": [...]}\n'
        '{"id": "...", ...}',
    ),
    'xml': ExportFormat(
        stream_xml, 'application/xml', '.xml',
        _('XML — cấu trúc đầy đủ'), GROUP_GENERAL, True,
        _('Cây XML gồm văn bản, token kèm thẻ BIO và danh sách cụm. Tên cũ '
          '"xml_conll" vẫn dùng được nhưng đây KHÔNG phải định dạng CoNLL — '
          'chọn "CoNLL-2003" nếu cần chuẩn đó.'),
        '<corpus name="..." exported="...">\n'
        '  <sentence id="..." label="toxic" is_meaningful="true">\n'
        '    <text>thằng ngu này</text>\n'
        '    <tokens>\n'
        '      <token position="0" start="0" end="5"\n'
        '             label="toxic" bio="B-toxic">thằng</token>\n'
        '    </tokens>\n'
        '    <spans><span start="0" end="9" label="toxic">thằng ngu</span></spans>\n'
        '  </sentence>\n'
        '</corpus>',
    ),

    # --- Bảng tính ---
    'csv_sentence': ExportFormat(
        stream_csv_sentence, 'text/csv; charset=utf-8', '.csv',
        _('CSV — cấp câu'), GROUP_SHEET, True,
        _('Một dòng một bình luận. Có BOM UTF-8 nên Excel mở đúng tiếng Việt.'),
        'id,text,label,ai_label,manual_label,gold_label,review_status,\n'
        'annotator_count,is_meaningful,author,like_count\n'
        '...,thằng ngu này,toxic,toxic,toxic,toxic,agreed,1,true,@user,3',
    ),
    'csv_token': ExportFormat(
        stream_csv_token, 'text/csv; charset=utf-8', '.csv',
        _('CSV — cấp token (kèm BIO)'), GROUP_SHEET, True,
        _('Một dòng một token, có cột bio và span_group để biết token nào thuộc '
          'cùng một cụm.'),
        'comment_id,comment_label,token_text,token_label,bio,\n'
        'position,start_offset,end_offset,span_group\n'
        '...,toxic,thằng,toxic,B-toxic,0,0,5,3f2a…',
    ),
    'csv_spans': ExportFormat(
        stream_csv_spans, 'text/csv; charset=utf-8', '.csv',
        _('CSV — chỉ các cụm đã gán nhãn'), GROUP_SHEET, True,
        _('Gọn nhất để rà soát thủ công: chỉ liệt kê những cụm từ thực sự mang '
          'nhãn, bỏ qua toàn bộ token trung tính.'),
        'comment_id,comment_text,comment_label,span_text,span_label,start,end\n'
        '...,thằng ngu này,toxic,thằng ngu,toxic,0,9',
    ),
    'csv_annotations': ExportFormat(
        stream_csv_annotations, 'text/csv; charset=utf-8', '.csv',
        _('CSV — từng annotation (để tính lại IAA)'), GROUP_SHEET, True,
        _('Mỗi dòng là quyết định của MỘT người trên MỘT bình luận. Đây là dữ liệu '
          'thô cần thiết để tính lại độ đồng thuận (Cohen κ, Krippendorff α) một '
          'cách độc lập.'),
        'comment_id,text,annotator,source,label,review_status,created_at\n'
        '...,thằng ngu này,alice,manual,toxic,conflict,2026-09-03T…\n'
        '...,thằng ngu này,bob,manual,clean,conflict,2026-09-03T…',
    ),
    'xlsx': ExportFormat(
        build_xlsx,
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        '.xlsx', _('Excel — nhiều sheet, cho người rà soát'), GROUP_SHEET, False,
        _('Sổ Excel gồm 3 sheet: Câu, Cụm đã gán nhãn, Thông tin. Dành cho chuyên '
          'gia lĩnh vực không dùng công cụ kỹ thuật. Giới hạn 100.000 dòng — dữ '
          'liệu lớn hơn nên dùng CSV.'),
        _('Sheet "Câu"            : mỗi dòng một bình luận\n'
          'Sheet "Cụm đã gán nhãn": mỗi dòng một cụm\n'
          'Sheet "Thông tin"      : tên dự án, thời điểm xuất, số lượng'),
    ),

    # --- Huấn luyện mô hình ---
    'json_llm': ExportFormat(
        stream_json_llm, 'application/jsonl', '.jsonl',
        _('JSONL hội thoại — fine-tune LLM'), GROUP_TRAINING, True,
        _('Dạng messages (system/user/assistant) để fine-tune mô hình ngôn ngữ. '
          'NÊN dùng cùng phạm vi "chỉ nhãn đã chốt" — xuất cả bình luận chưa gán '
          'nhãn sẽ dạy mô hình rằng "chưa gán nhãn" nghĩa là "O".'),
        '{"messages": [\n'
        '  {"role": "system", "content": "Bạn là hệ thống gán nhãn…"},\n'
        '  {"role": "user", "content": "thằng ngu này"},\n'
        '  {"role": "assistant", "content":\n'
        '    "{\\"label\\": \\"toxic\\", \\"spans\\": [...]}"}\n'
        '], "metadata": {"review_status": "agreed"}}',
    ),
}

# Tên cũ vẫn dùng được để không phá liên kết/kịch bản đã có.
LEGACY_FORMAT_ALIASES = {
    'xml_conll': 'xml',
}

# Giữ tên biến cũ cho mã nguồn và test đang tham chiếu.
EXPORTERS = EXPORT_FORMATS


def format_choices() -> list[dict]:
    """Danh sách định dạng để hiển thị trên giao diện, đã nhóm sẵn."""
    return [{
        'key': key,
        'label': meta.label,
        'group': meta.group,
        'extension': meta.extension,
        'streaming': meta.streaming,
        'description': meta.description,
        'sample': meta.sample,
    } for key, meta in EXPORT_FORMATS.items()]


def format_groups() -> dict:
    """Định dạng đã gom theo nhóm, giữ đúng thứ tự khai báo."""
    grouped: dict = {}
    for choice in format_choices():
        grouped.setdefault(str(choice['group']), []).append(choice)
    return grouped


def resolve_format(export_format: str) -> str:
    return LEGACY_FORMAT_ALIASES.get(export_format, export_format)


# ---------------------------------------------------------------------------
# API công khai
# ---------------------------------------------------------------------------
def generate_export(project, youtube_link, export_format, filter_label='all',
                    requested_by=None, review_filter='all'):
    """Trả về response chứa dữ liệu đã xuất."""
    export_format = resolve_format(export_format)
    if export_format not in EXPORT_FORMATS:
        return HttpResponse('Invalid export format', status=400)

    meta = EXPORT_FORMATS[export_format]

    queryset = _comments_queryset(
        project, youtube_link, filter_label, review_filter=review_filter
    )

    filename = (
        f'{safe_filename(project.name)}_{export_format}_'
        f'{timezone.now():%Y%m%d_%H%M%S}{meta.extension}'
    )

    ExportRecord.objects.create(
        project=project,
        youtube_link=youtube_link,
        export_format=export_format[:30],
        filter_toxicity=(filter_label or 'all')[:20],
        comment_count=0,
        file_size='streamed' if meta.streaming else 'buffered',
    )

    if not meta.streaming:
        try:
            payload = meta.builder(queryset, project=project)
        except RuntimeError as exc:
            return HttpResponse(str(exc), status=503)
        response = HttpResponse(payload, content_type=meta.content_type)
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    response = StreamingHttpResponse(
        meta.builder(queryset, project=project), content_type=meta.content_type
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response['X-Accel-Buffering'] = 'no'
    return response


# Việc xuất chạy hai lượt trên dữ liệu: lượt ghi file và lượt đếm lại. Chia
# thanh tiến độ theo đúng tỉ lệ đó để con số không nhảy giật.
WRITE_SHARE = 0.7


def export_to_file(project, youtube_link, export_format, filter_label, target_path,
                   review_filter='all', progress=None):
    """
    Ghi dữ liệu xuất ra file (dùng cho ExportRecord và DatasetVersion).

    `progress(percent, step)`: hàm gọi lại để báo tiến độ; bỏ trống thì chạy
    im lặng như cũ.
    """
    export_format = resolve_format(export_format)
    if export_format not in EXPORT_FORMATS:
        raise ValueError(f'Định dạng không hợp lệ: {export_format}')

    meta = EXPORT_FORMATS[export_format]
    queryset = _comments_queryset(
        project, youtube_link, filter_label, review_filter=review_filter
    )

    path = Path(f'{target_path}{meta.extension}')
    path.parent.mkdir(parents=True, exist_ok=True)

    def report(percent, step):
        if progress is not None:
            progress(max(0, min(100, int(percent))), step)

    total = queryset.count() or 1
    report(0, _('Đang chuẩn bị dữ liệu'))

    writing = _ProgressQuerySet(queryset, lambda done: report(
        done / total * 100 * WRITE_SHARE,
        _('Đang ghi %(done)s/%(total)s bình luận') % {'done': done, 'total': total},
    ))

    token_count = 0
    comment_count = 0

    if meta.streaming:
        with open(path, 'w', encoding='utf-8') as handle:
            for chunk in meta.builder(writing, project=project):
                handle.write(chunk)
    else:
        with open(path, 'wb') as handle:
            handle.write(meta.builder(writing, project=project))

    # Đếm trên dữ liệu thực sự được xuất, không dùng Count('tokens') trên bảng
    # Token: token chỉ tồn tại khi đã gán nhãn, nên con số đó nhỏ hơn thực tế
    # rất nhiều (25 token cho 10.376 bình luận).
    for comment in queryset.iterator(chunk_size=CHUNK_SIZE):
        comment_count += 1
        token_count += len(comment.display_tokens)
        if comment_count % 100 == 0:
            report(
                100 * WRITE_SHARE + comment_count / total * 100 * (1 - WRITE_SHARE),
                _('Đang kiểm đếm %(done)s/%(total)s bình luận') % {
                    'done': comment_count, 'total': total,
                },
            )

    report(100, _('Hoàn tất'))
    return {
        'path': str(path),
        'comment_count': comment_count,
        'token_count': token_count,
    }
