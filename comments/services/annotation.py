"""
Logic nghiệp vụ gán nhãn đa người (multi-annotator).

Mọi thay đổi nhãn phải đi qua module này để đảm bảo:
1. Annotation của từng người được lưu riêng (không ghi đè lẫn nhau).
2. Nhãn vàng (gold_label) được tính lại theo quy tắc đồng thuận.
3. Mọi thay đổi được ghi vào nhật ký kiểm toán (AnnotationEvent).
4. Cột manual_label/ai_label (bản sao tính sẵn) luôn đồng bộ với annotation.
"""
from __future__ import annotations

import logging
import uuid
from collections import Counter

from django.db import transaction
from django.utils import timezone

from ..models import (
    AnnotationAssignment,
    AnnotationEvent,
    Comment,
    CommentAnnotation,
    ProjectLabel,
    Token,
    TokenAnnotation,
)

logger = logging.getLogger(__name__)

NO_LABEL = 'O'

# Nhãn hiển thị khi annotator đã xét và kết luận "không có nhãn". Khác hẳn với
# "chưa gán": đây là một quyết định có chủ đích nên phải thấy được trên màn hình.
NO_LABEL_DATA = {'id': None, 'name': NO_LABEL, 'color': '#9ca3af'}


def _label_key(project_label: ProjectLabel | None) -> str:
    """Khoá so sánh nhãn; None (không nhãn) coi như 'O'."""
    return str(project_label.id) if project_label else NO_LABEL


def _log_event(*, project, comment, actor, action, old_value='', new_value='',
               token_position=None, detail=None):
    AnnotationEvent.objects.create(
        project=project,
        comment=comment,
        token_position=token_position,
        actor=actor,
        action=action,
        old_value=(old_value or '')[:200],
        new_value=(new_value or '')[:200],
        detail=detail,
    )


# Công khai cho service khác dùng chung (ai_review): cùng một nhật ký kiểm
# toán và cùng một quy ước khoá nhãn.
log_event = _log_event
label_key = _label_key


# ---------------------------------------------------------------------------
# Góc nhìn của một annotator
#
# comment.manual_label / token.manual_label là bản sao tính sẵn dùng chung cho
# cả dự án: nó giữ annotation thủ công gần nhất, của bất kỳ ai. Đem nó ra màn
# hình gán nhãn thì người này thấy nhãn của người kia. Các hàm dưới đây trả về
# đúng phần việc của một người, đọc thẳng từ CommentAnnotation/TokenAnnotation.
# ---------------------------------------------------------------------------
def label_payload(project_label: ProjectLabel | None) -> dict | None:
    if project_label is None:
        return None
    return {
        'id': str(project_label.id),
        'name': project_label.display_name,
        'color': project_label.display_color,
    }


def my_comment_label(comment: Comment, user) -> dict | None:
    """Nhãn cấp câu do chính `user` gán, hoặc None nếu người này chưa gán."""
    annotation = comment.annotations.filter(
        annotator=user, source='manual'
    ).select_related('project_label__label').first()
    if annotation is None:
        return None
    return label_payload(annotation.project_label) or NO_LABEL_DATA


def my_comment_label_map(comment_ids, user) -> dict:
    """{comment_id: nhãn của user} cho nhiều comment trong một truy vấn."""
    if not comment_ids or user is None or not getattr(user, 'is_authenticated', False):
        return {}
    return {
        annotation.comment_id: label_payload(annotation.project_label) or NO_LABEL_DATA
        for annotation in CommentAnnotation.objects.filter(
            comment_id__in=comment_ids, annotator=user, source='manual'
        ).select_related('project_label__label')
    }


def my_token_label_map(comment_ids, user) -> dict:
    """{comment_id: {vị trí token: nhãn của user}} trong một truy vấn."""
    if not comment_ids or user is None or not getattr(user, 'is_authenticated', False):
        return {}
    result: dict = {}
    for annotation in TokenAnnotation.objects.filter(
        token__comment_id__in=comment_ids, annotator=user, source='manual'
    ).select_related('token', 'project_label__label'):
        result.setdefault(annotation.token.comment_id, {})[annotation.token.position] = (
            label_payload(annotation.project_label)
        )
    return result


def tokens_for_user(comment: Comment, token_labels: dict | None = None) -> list[dict]:
    """
    Danh sách token để hiển thị, trong đó `manual_label` là nhãn của chính
    người đang xem.

    `ai_label` giữ nguyên: gợi ý của máy không phải việc của người khác, và
    annotator cần thấy nó để đối chiếu.
    """
    token_labels = token_labels or {}
    rows = []
    for token in comment.display_tokens:
        mine = token_labels.get(token['position'])
        rows.append({
            **token,
            'manual_label': mine,
            'my_label': mine,
            # Nhãn hiệu lực theo góc nhìn của tôi: nhãn tôi gán, chưa gán thì
            # rơi về gợi ý của AI. Không bao giờ là nhãn của annotator khác.
            'effective_label': mine or token['ai_label'],
            # Đang hiển thị nhãn của AI vì tôi chưa gán -> giao diện phải vẽ
            # khác đi, không được để người dùng tưởng đó là nhãn mình đã chọn.
            'is_ai_suggestion': mine is None and token['ai_label'] is not None,
        })
    return rows


def attach_my_annotations(comments, user) -> list:
    """
    Gắn `my_label` và `my_tokens` vào từng comment để template dùng trực tiếp.

    Django template không gọi được method có tham số, nên góc nhìn cá nhân phải
    được tính sẵn ở view rồi đính vào đối tượng.
    """
    comments = list(comments)
    comment_ids = [comment.id for comment in comments]
    label_map = my_comment_label_map(comment_ids, user)
    token_map = my_token_label_map(comment_ids, user)

    for comment in comments:
        comment.my_label = label_map.get(comment.id)
        comment.my_tokens = tokens_for_user(comment, token_map.get(comment.id))
        # Số token AI đã gán mà người này chưa đụng tới: con số hiện cạnh nút
        # "Đồng ý với AI" để biết bấm vào là nhận thêm bao nhiêu nhãn token.
        comment.ai_token_count = sum(
            1 for token in comment.my_tokens if token['is_ai_suggestion']
        )
    return comments


# ---------------------------------------------------------------------------
# Tính nhãn vàng từ các annotation
# ---------------------------------------------------------------------------
def recompute_comment_gold(comment: Comment, *, actor=None) -> dict:
    """
    Tính lại gold_label + review_status của một comment từ toàn bộ annotation.

    Quy tắc:
    - Có annotation 'adjudicated' -> dùng luôn, trạng thái 'adjudicated'.
    - Số annotation 'manual' < annotators_per_comment -> 'pending', chưa chốt.
    - Đủ số lượng và tất cả trùng nhau -> 'agreed', chốt nhãn đó.
    - Đủ số lượng nhưng khác nhau  -> 'conflict', chờ chủ dự án phân xử.
      (Nếu project.auto_adjudicate và có đa số tuyệt đối thì chốt theo đa số.)
    """
    project = comment.youtube_link.project
    required = max(1, project.annotators_per_comment)

    annotations = list(
        comment.annotations.select_related('project_label__label').all()
    )
    adjudicated = next((a for a in annotations if a.source == 'adjudicated'), None)
    manual = [a for a in annotations if a.source == 'manual']
    ai = next((a for a in annotations if a.source == 'ai'), None)

    old_gold = _label_key(comment.gold_label)
    new_gold_label = None
    status = 'pending'

    if adjudicated is not None:
        new_gold_label = adjudicated.project_label
        status = 'adjudicated'
    elif len(manual) >= required and manual:
        keys = [_label_key(a.project_label) for a in manual]
        counts = Counter(keys)
        top_key, top_n = counts.most_common(1)[0]
        if len(counts) == 1:
            status = 'agreed'
            new_gold_label = manual[0].project_label
        elif project.auto_adjudicate and top_n > len(manual) / 2:
            # Đa số tuyệt đối -> chốt tự động nhưng vẫn đánh dấu là conflict
            # để chủ dự án biết đã từng có bất đồng.
            status = 'conflict'
            new_gold_label = next(
                (a.project_label for a in manual if _label_key(a.project_label) == top_key),
                None,
            )
        else:
            status = 'conflict'
            new_gold_label = None
    elif manual:
        status = 'pending'

    # manual_label giữ vai trò bản sao tương thích ngược: ưu tiên gold, nếu
    # chưa chốt thì lấy annotation thủ công gần nhất để UI vẫn hiển thị được.
    latest_manual = max(manual, key=lambda a: a.updated_at) if manual else None
    manual_label = new_gold_label or (latest_manual.project_label if latest_manual else None)

    comment.gold_label = new_gold_label
    comment.manual_label = manual_label
    comment.review_status = status
    comment.manual_annotation_count = len(manual)
    if ai is not None:
        comment.ai_label = ai.project_label

    comment.save(update_fields=[
        'gold_label', 'manual_label', 'review_status',
        'manual_annotation_count', 'ai_label', 'updated_at',
    ])

    new_gold = _label_key(comment.gold_label)
    if old_gold != new_gold and actor is not None:
        _log_event(
            project=project, comment=comment, actor=actor, action='adjudicate',
            old_value=old_gold, new_value=new_gold,
            detail={'review_status': status, 'annotator_count': len(manual)},
        )

    return {
        'review_status': status,
        'gold_label': comment.gold_label,
        'manual_count': len(manual),
        'required': required,
    }


def recompute_token_gold(token: Token) -> None:
    """Tính lại gold_label của một token theo cùng quy tắc đồng thuận."""
    project = token.comment.youtube_link.project
    required = max(1, project.annotators_per_comment)

    annotations = list(token.annotations.select_related('project_label__label').all())
    adjudicated = next((a for a in annotations if a.source == 'adjudicated'), None)
    manual = [a for a in annotations if a.source == 'manual']
    ai = next((a for a in annotations if a.source == 'ai'), None)

    gold = None
    if adjudicated is not None:
        gold = adjudicated.project_label
    elif len(manual) >= required and manual:
        counts = Counter(_label_key(a.project_label) for a in manual)
        top_key, top_n = counts.most_common(1)[0]
        if len(counts) == 1 or top_n > len(manual) / 2:
            gold = next(
                (a.project_label for a in manual if _label_key(a.project_label) == top_key),
                None,
            )

    latest_manual = max(manual, key=lambda a: a.updated_at) if manual else None
    token.gold_label = gold
    token.manual_label = gold or (latest_manual.project_label if latest_manual else None)
    if ai is not None:
        token.ai_label = ai.project_label
    token.save(update_fields=['gold_label', 'manual_label', 'ai_label'])


# ---------------------------------------------------------------------------
# API nghiệp vụ: gán nhãn
# ---------------------------------------------------------------------------
@transaction.atomic
def set_comment_label(comment: Comment, user, project_label: ProjectLabel | None, *,
                      source='manual', note='', time_spent_ms=0) -> dict:
    """
    Ghi nhãn cấp câu của một annotator.

    Không ghi đè annotation của người khác: mỗi (comment, annotator, source)
    là một bản ghi riêng.
    """
    comment = Comment.objects.select_for_update().select_related(
        'youtube_link__project'
    ).get(pk=comment.pk)
    project = comment.youtube_link.project

    existing = comment.annotations.filter(
        annotator=user if source != 'ai' else None, source=source
    ).select_related('project_label__label').first()
    old_value = _label_key(existing.project_label) if existing else NO_LABEL

    annotation, _created = CommentAnnotation.objects.update_or_create(
        comment=comment,
        annotator=user if source != 'ai' else None,
        source=source,
        defaults={
            'project_label': project_label,
            'is_meaningful': True,
            'note': note,
            'time_spent_ms': time_spent_ms or 0,
        },
    )

    comment.is_meaningful = True
    comment.annotated_at = timezone.now()
    comment.annotation_source = _derive_annotation_source(comment)
    comment.save(update_fields=['is_meaningful', 'annotated_at', 'annotation_source'])

    _log_event(
        project=project, comment=comment, actor=user,
        action='comment_label',
        old_value=old_value, new_value=_label_key(project_label),
        detail={'source': source},
    )

    _mark_assignment_done(comment, user)
    result = recompute_comment_gold(comment, actor=user)
    result['annotation_id'] = str(annotation.id)
    return result


@transaction.atomic
def set_token_label(comment: Comment, user, position: int,
                    project_label: ProjectLabel | None, *, source='manual',
                    span_group=None) -> Token | None:
    """
    Ghi nhãn cấp token của một annotator tại vị trí `position`.

    `span_group`: định danh cụm. Khi gán một token đơn lẻ, hệ thống sinh giá trị
    mới -> token đó là một cụm độc lập. Khi gán cả dải (kéo chọn), mọi token
    dùng CHUNG một giá trị -> xuất ra BIO chính xác (B- cho token đầu, I- cho
    các token sau) thay vì phải đoán từ việc nhãn có giống nhau hay không.
    """
    token = comment.get_or_create_token_for_position(position)
    if token is None:
        return None

    project = comment.youtube_link.project
    existing = token.annotations.filter(
        annotator=user if source != 'ai' else None, source=source
    ).select_related('project_label__label').first()
    old_value = _label_key(existing.project_label) if existing else NO_LABEL

    TokenAnnotation.objects.update_or_create(
        token=token,
        annotator=user if source != 'ai' else None,
        source=source,
        defaults={'project_label': project_label},
    )
    token.annotation_source = 'manual' if source != 'ai' else 'auto'
    token.annotated_at = timezone.now()
    token.span_group = (
        span_group if project_label is not None
        else None  # bỏ nhãn thì cũng bỏ luôn liên kết cụm
    ) or (uuid.uuid4() if project_label is not None else None)
    token.save(update_fields=['annotation_source', 'annotated_at', 'span_group'])

    recompute_token_gold(token)

    _log_event(
        project=project, comment=comment, actor=user, action='token_label',
        token_position=position,
        old_value=old_value, new_value=_label_key(project_label),
        detail={'token_text': token.text},
    )
    return token


@transaction.atomic
def set_token_span_label(comment: Comment, user, start_position: int, end_position: int,
                         project_label: ProjectLabel | None) -> list[Token]:
    """
    Gán cùng một nhãn cho một dải token liên tiếp [start, end].

    Cho phép annotator kéo chọn cả cụm từ thay vì click từng token:
    đây là thao tác chiếm phần lớn thời gian gán nhãn cấp token.
    """
    if end_position < start_position:
        start_position, end_position = end_position, start_position

    comment.ensure_token_inventory()
    # Một định danh cụm cho cả dải: đây chính là thông tin cho phép xuất BIO
    # chính xác thay vì suy đoán từ các nhãn giống nhau liền kề.
    group = uuid.uuid4() if project_label is not None else None

    tokens = []
    for position in range(start_position, end_position + 1):
        token = set_token_label(comment, user, position, project_label,
                                span_group=group)
        if token is not None:
            tokens.append(token)
    return tokens


@transaction.atomic
def remove_comment_label(comment: Comment, user) -> dict:
    """
    gỡ bỏ annotation của `user` khỏi comment.

    Khác với set_comment_label(..., project_label=None): hàm đó ghi nhận
    "tôi đã xét và kết luận không có nhãn" (vẫn tính là đã gán nhãn), còn hàm
    này xoá hẳn quyết định của người dùng: câu trở lại trạng thái chưa ai xét.
    """
    project = comment.youtube_link.project
    existing = comment.annotations.filter(
        annotator=user, source='manual'
    ).select_related('project_label__label').first()

    old_value = _label_key(existing.project_label) if existing else NO_LABEL
    if existing is not None:
        existing.delete()

    AnnotationAssignment.objects.filter(
        comment=comment, annotator=user, status='done'
    ).update(status='pending', completed_at=None)

    comment.annotation_source = _derive_annotation_source(comment)
    comment.save(update_fields=['annotation_source'])

    _log_event(
        project=project, comment=comment, actor=user, action='comment_label',
        old_value=old_value, new_value='(đã gỡ bỏ)',
    )
    return recompute_comment_gold(comment, actor=user)


@transaction.atomic
def skip_comment(comment: Comment, user, *, skipped=True) -> None:
    """Đánh dấu comment không có nội dung đáng gán nhãn (hoặc bỏ đánh dấu)."""
    project = comment.youtube_link.project
    old = 'skipped' if comment.is_meaningful is False else 'active'

    comment.is_meaningful = not skipped
    if skipped:
        comment.gold_label = None
        comment.manual_label = None
        comment.review_status = 'agreed'
        comment.annotations.filter(source='manual').delete()
        Token.objects.filter(comment=comment).update(
            manual_label=None, gold_label=None
        )
    comment.annotated_at = timezone.now()
    comment.save(update_fields=[
        'is_meaningful', 'gold_label', 'manual_label', 'review_status', 'annotated_at',
    ])

    _log_event(
        project=project, comment=comment, actor=user, action='skip',
        old_value=old, new_value='skipped' if skipped else 'active',
    )
    _mark_assignment_done(comment, user)


@transaction.atomic
def adjudicate_comment(comment: Comment, owner, project_label: ProjectLabel | None,
                       *, note='') -> dict:
    """Chủ dự án chốt nhãn cuối cùng cho một comment đang bất đồng."""
    return set_comment_label(
        comment, owner, project_label, source='adjudicated', note=note
    )


def _derive_annotation_source(comment: Comment) -> str:
    """Suy ra annotation_source ('auto' / 'manual' / 'mixed') từ các annotation."""
    sources = set(comment.annotations.values_list('source', flat=True))
    has_ai = 'ai' in sources
    has_human = bool(sources & {'manual', 'adjudicated'})
    if has_ai and has_human:
        return 'mixed'
    if has_human:
        return 'manual'
    if has_ai:
        return 'auto'
    return 'manual'


def _mark_assignment_done(comment: Comment, user) -> None:
    """Đánh dấu phân công tương ứng là đã hoàn thành (nếu có)."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return
    AnnotationAssignment.objects.filter(
        comment=comment, annotator=user, status='pending'
    ).update(status='done', completed_at=timezone.now())


# ---------------------------------------------------------------------------
# Ghi nhãn của AI (dùng trong Celery task)
# ---------------------------------------------------------------------------
@transaction.atomic
def store_ai_comment_annotation(comment: Comment, project_label: ProjectLabel | None,
                                *, confidence=None, is_meaningful=True) -> None:
    """Ghi annotation cấp câu do AI sinh ra (annotator=None, source='ai')."""
    CommentAnnotation.objects.update_or_create(
        comment=comment,
        annotator=None,
        source='ai',
        defaults={
            'project_label': project_label,
            'confidence': confidence,
            'is_meaningful': is_meaningful,
        },
    )


def store_ai_token_annotations(tokens_with_labels) -> None:
    """
    Ghi hàng loạt annotation token của AI.

    `tokens_with_labels`: iterable của (Token, ProjectLabel|None, score).
    """
    rows = [
        TokenAnnotation(
            token=token,
            annotator=None,
            source='ai',
            project_label=project_label,
            score=score,
        )
        for token, project_label, score in tokens_with_labels
    ]
    if rows:
        TokenAnnotation.objects.bulk_create(rows, ignore_conflicts=True)
