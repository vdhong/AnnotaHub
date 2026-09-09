"""
Chốt và phục hồi phiên bản dataset.

Một `DatasetVersion` gồm HAI file:

- `file_path`: bản xuất cho người dùng cuối, định dạng do người chốt chọn
                    (CoNLL, JSONL, CSV…). Dùng để tải về, công bố, huấn luyện.
- `snapshot_path`: ảnh chụp nhãn ở định dạng chuẩn nội bộ, luôn giống nhau bất
                    kể định dạng xuất. Đây mới là thứ dùng để phục hồi dự án.

Tách đôi như vậy vì phần lớn định dạng xuất (CoNLL, CSV token…) không mang đủ
thông tin để dựng lại trạng thái: thiếu id bình luận, thiếu review_status,
thiếu phân biệt nhãn thủ công với nhãn đã chốt.

Ảnh chụp là JSON Lines nén gzip: dòng đầu là phần đầu (header), mỗi dòng sau là
một bình luận. Nhờ vậy cả khi ghi lẫn khi đọc đều chạy theo luồng, không nạp
toàn bộ dataset vào RAM.
"""
from __future__ import annotations

import gzip
import json
import logging
import uuid
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from ..models import Comment, ProjectLabel, Token

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1
SNAPSHOT_SUFFIX = '.snapshot.jsonl.gz'

# Số bình luận xử lý mỗi lượt khi phục hồi. Đủ lớn để ít truy vấn, đủ nhỏ để
# không giữ hàng chục nghìn đối tượng trong RAM.
BATCH_SIZE = 500

# Các trường của Comment mà ảnh chụp chịu trách nhiệm phục hồi.
COMMENT_FIELDS = ('gold_label', 'manual_label', 'review_status', 'is_meaningful')


# ---------------------------------------------------------------------------
# Ghi ảnh chụp
# ---------------------------------------------------------------------------
def snapshot_path_for(target_path) -> Path:
    """Đường dẫn ảnh chụp đi kèm một file xuất."""
    return Path(f'{target_path}{SNAPSHOT_SUFFIX}')


def _label_name(project_label) -> str | None:
    return project_label.display_name if project_label else None


def write_snapshot(project, target_path) -> dict:
    """
    Ghi ảnh chụp toàn bộ nhãn hiện tại của dự án ra `target_path`.

    Chụp tất cả bình luận, kể cả bình luận chưa có nhãn, vì phục hồi phải xoá
    được cả những nhãn được gán SAU thời điểm chốt.
    """
    path = Path(target_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    queryset = (
        Comment.objects
        .filter(youtube_link__project=project)
        .select_related(
            'youtube_link',
            'gold_label__label',
            'manual_label__label',
        )
        .prefetch_related('tokens__gold_label__label', 'tokens__manual_label__label')
        .order_by('youtube_link_id', 'youtube_comment_id')
    )

    count = 0
    with gzip.open(path, 'wt', encoding='utf-8') as handle:
        handle.write(json.dumps({
            'snapshot_version': SNAPSHOT_VERSION,
            'project_id': str(project.id),
            'project_name': project.name,
            'created_at': timezone.now().isoformat(),
        }, ensure_ascii=False) + '\n')

        for comment in queryset.iterator(chunk_size=BATCH_SIZE):
            tokens = [
                [
                    token.position,
                    _label_name(token.gold_label),
                    _label_name(token.manual_label),
                    str(token.span_group) if token.span_group else None,
                ]
                for token in comment.tokens.all()
                if token.gold_label_id or token.manual_label_id or token.span_group
            ]
            handle.write(json.dumps({
                'link': comment.youtube_link.video_id,
                'cid': comment.youtube_comment_id,
                'gold': _label_name(comment.gold_label),
                'manual': _label_name(comment.manual_label),
                'review_status': comment.review_status,
                'is_meaningful': comment.is_meaningful,
                'tokens': tokens,
            }, ensure_ascii=False) + '\n')
            count += 1

    logger.info('Đã ghi ảnh chụp %s bình luận cho dự án %s', count, project.name)
    return {'path': str(path), 'comment_count': count}


# ---------------------------------------------------------------------------
# Đọc & phục hồi
# ---------------------------------------------------------------------------
def read_snapshot_header(path) -> dict:
    """Đọc riêng dòng header: dùng để kiểm tra tương thích trước khi phục hồi."""
    with gzip.open(path, 'rt', encoding='utf-8') as handle:
        return json.loads(handle.readline())


def _iter_batches(path, size=BATCH_SIZE):
    """Đọc ảnh chụp theo lô, bỏ qua dòng header."""
    with gzip.open(path, 'rt', encoding='utf-8') as handle:
        handle.readline()  # header
        batch = []
        for line in handle:
            line = line.strip()
            if not line:
                continue
            batch.append(json.loads(line))
            if len(batch) >= size:
                yield batch
                batch = []
        if batch:
            yield batch


class SnapshotIncompatible(Exception):
    """Ảnh chụp thuộc dự án khác hoặc do phiên bản định dạng mới hơn ghi ra."""


def restore_snapshot(project, path) -> dict:
    """
    Đưa nhãn của dự án về đúng trạng thái trong ảnh chụp.

    Phục hồi:  nhãn đã chốt (gold), nhãn thủ công, trạng thái phân xử, cờ
               "bình luận có nghĩa", nhãn token và nhóm span.
    Không đụng tới: bình luận (không xoá, không thêm), và lịch sử
               `CommentAnnotation` / `TokenAnnotation`: đó là dấu vết ai đã gán
               gì, viết lại sẽ làm sai lệch dữ liệu kiểm toán và chỉ số IAA.

    `skipped` đếm dòng trong ảnh chụp không còn bình luận tương ứng (đã bị xoá
    sau khi chốt). `untouched` đếm chiều ngược lại: bình luận hiện có nhưng
    không nằm trong ảnh chụp: thêm vào sau khi chốt, nên được giữ nguyên.
    """
    header = read_snapshot_header(path)
    if header.get('snapshot_version', 0) > SNAPSHOT_VERSION:
        raise SnapshotIncompatible(
            'Ảnh chụp được ghi bởi phiên bản phần mềm mới hơn.'
        )
    if header.get('project_id') and header['project_id'] != str(project.id):
        raise SnapshotIncompatible('Ảnh chụp thuộc về một dự án khác.')

    labels = {
        pl.display_name: pl
        for pl in ProjectLabel.objects.filter(project=project).select_related('label')
    }
    links = {
        video_id: link_id
        for link_id, video_id in project.youtubelinks.values_list('id', 'video_id')
    }

    stats = {
        'comments': 0, 'tokens': 0, 'skipped': 0,
        'missing_labels': set(), 'total': 0,
    }

    def resolve(name):
        """Tên nhãn -> ProjectLabel. Nhãn đã bị gỡ khỏi dự án thì bỏ qua."""
        if name is None:
            return None
        pl = labels.get(name)
        if pl is None:
            stats['missing_labels'].add(name)
        return pl

    with transaction.atomic():
        for batch in _iter_batches(path):
            stats['total'] += len(batch)
            by_key = {}
            for row in batch:
                link_id = links.get(row['link'])
                if link_id is None:
                    # Nguồn dữ liệu đã bị xoá khỏi dự án sau khi chốt phiên bản.
                    stats['skipped'] += 1
                    continue
                by_key[(link_id, row['cid'])] = row

            comments = list(
                Comment.objects
                .filter(
                    youtube_link_id__in={k[0] for k in by_key if k[0]},
                    youtube_comment_id__in={k[1] for k in by_key},
                )
                .only(*COMMENT_FIELDS, 'youtube_link_id', 'youtube_comment_id')
            )

            to_update = []
            token_rows = {}
            for comment in comments:
                row = by_key.pop(
                    (comment.youtube_link_id, comment.youtube_comment_id), None
                )
                if row is None:
                    continue
                comment.gold_label = resolve(row['gold'])
                comment.manual_label = resolve(row['manual'])
                comment.review_status = row['review_status']
                comment.is_meaningful = row['is_meaningful']
                to_update.append(comment)
                if row['tokens']:
                    token_rows[comment.id] = {t[0]: t for t in row['tokens']}

            if to_update:
                Comment.objects.bulk_update(to_update, COMMENT_FIELDS)
                stats['comments'] += len(to_update)

            stats['skipped'] += len(by_key)

            # Token: xoá sạch nhãn của mọi bình luận trong lô rồi đặt lại theo
            # ảnh chụp: nếu chỉ ghi đè, nhãn gán sau khi chốt sẽ còn sót lại.
            comment_ids = [c.id for c in to_update]
            if comment_ids:
                stats['tokens'] += _restore_tokens(comment_ids, token_rows, resolve)

    stats['missing_labels'] = sorted(stats['missing_labels'])
    stats['untouched'] = max(
        0,
        Comment.objects.filter(youtube_link__project=project).count()
        - stats['comments'],
    )
    logger.info(
        'Phục hồi dự án %s: %s bình luận, %s token, bỏ qua %s',
        project.name, stats['comments'], stats['tokens'], stats['skipped'],
    )
    return stats


def _restore_tokens(comment_ids, token_rows, resolve) -> int:
    """Đặt lại nhãn token cho một lô bình luận. Trả về số token có nhãn."""
    tokens = list(
        Token.objects.filter(comment_id__in=comment_ids)
        .only('comment_id', 'position', 'gold_label', 'manual_label', 'span_group')
    )
    changed = 0
    for token in tokens:
        row = token_rows.get(token.comment_id, {}).get(token.position)
        if row is None:
            token.gold_label = None
            token.manual_label = None
            token.span_group = None
            continue
        _, gold, manual, span = row
        token.gold_label = resolve(gold)
        token.manual_label = resolve(manual)
        token.span_group = uuid.UUID(span) if span else None
        changed += 1

    if tokens:
        Token.objects.bulk_update(
            tokens, ['gold_label', 'manual_label', 'span_group']
        )
    return changed
