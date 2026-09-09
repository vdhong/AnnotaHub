"""
Quy trình "AI gán trước: người soát lại" (pre-annotation review).

AI chạy xong để lại `CommentAnnotation`/`TokenAnnotation` với annotator=None,
source='ai'. Đó là đề xuất, không phải nhãn của ai cả: nó không được tính vào
độ đồng thuận và không thay người dùng quyết định.

Hai việc module này lo:

1. `accept_ai_suggestion()`: người dùng bấm "Đồng ý": chép đề xuất của AI
   thành annotation thủ công của chính họ (cả nhãn câu lẫn nhãn token). Sau đó
   họ sửa chỗ nào thì chỉ chỗ đó thay đổi; chỗ không đụng tới coi như đã đồng ý
, vì nó đã thành nhãn của họ rồi.

2. `ai_review_stats()`: đối chiếu nhãn người với đề xuất của AI để biết AI
   đúng bao nhiêu, bị sửa bao nhiêu. Số liệu được tính lại từ dữ liệu chứ không
   phải đếm lúc bấm nút, nên đúng cả với phần đã gán từ trước và không sai lệch
   khi người dùng sửa đi sửa lại.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q, Subquery

from ..models import Comment, CommentAnnotation, TokenAnnotation
from . import annotation as annotation_service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chấp nhận đề xuất của AI
# ---------------------------------------------------------------------------
def ai_comment_annotation(comment):
    """Đề xuất cấp câu của AI cho comment này, hoặc None."""
    return (
        comment.annotations.filter(annotator__isnull=True, source='ai')
        .select_related('project_label__label')
        .first()
    )


def ai_token_annotations(comment):
    """Đề xuất cấp token của AI, chỉ lấy token thực sự có nhãn."""
    return list(
        TokenAnnotation.objects
        .filter(token__comment=comment, annotator__isnull=True, source='ai',
                project_label__isnull=False)
        .select_related('token', 'project_label__label')
        .order_by('token__position')
    )


def has_ai_suggestion(comment) -> bool:
    return ai_comment_annotation(comment) is not None


@transaction.atomic
def accept_ai_suggestion(comment, user) -> dict:
    """
    Biến đề xuất của AI thành nhãn của `user`.

    Đi qua annotation_service chứ không ghi thẳng vào DB, để nhãn vàng được
    tính lại, phân công được đánh dấu hoàn thành và nhật ký kiểm toán vẫn đầy
    đủ: y như khi người dùng tự bấm chọn từng nhãn.
    """
    suggestion = ai_comment_annotation(comment)
    if suggestion is None:
        return {'accepted': False, 'reason': 'no_suggestion'}

    result = annotation_service.set_comment_label(
        comment, user, suggestion.project_label,
    )

    # Giữ nguyên cụm span của AI: một cụm nhiều token phải ở lại thành một cụm,
    # nếu không lúc xuất BIO sẽ thành nhiều thực thể rời rạc.
    token_count = 0
    for row in ai_token_annotations(comment):
        annotation_service.set_token_label(
            comment, user, row.token.position, row.project_label,
            span_group=row.token.span_group,
        )
        token_count += 1

    annotation_service.log_event(
        project=comment.youtube_link.project, comment=comment, actor=user,
        action='accept_ai',
        old_value='', new_value=annotation_service.label_key(suggestion.project_label),
        detail={'tokens': token_count},
    )

    result.update({
        'accepted': True,
        'tokens_accepted': token_count,
        'my_label': annotation_service.my_comment_label(comment, user),
    })
    return result


# ---------------------------------------------------------------------------
# Thống kê: AI đúng bao nhiêu, bị sửa bao nhiêu
# ---------------------------------------------------------------------------
def _scope(queryset, project=None, link=None, prefix=''):
    if link is not None:
        return queryset.filter(**{f'{prefix}youtube_link': link})
    return queryset.filter(**{f'{prefix}youtube_link__project': project})


def _compare(human_queryset, ai_queryset, join_field: str) -> dict:
    """
    So từng nhãn người gán với đề xuất của AI trên cùng một đối tượng.

    Trả về: đã soát / AI đúng / bị sửa / AI bỏ sót.

    So sánh phải an toàn với null: "AI kết luận không nhãn" và "người kết luận
    không nhãn" là trùng khớp, nhưng trong SQL thì NULL = NULL không bao giờ
    đúng. Vì vậy phải tách riêng nhánh cả hai cùng NULL.
    """
    ai_label = ai_queryset.filter(
        **{join_field: OuterRef(join_field)}
    ).values('project_label_id')[:1]
    ai_exists = ai_queryset.filter(**{join_field: OuterRef(join_field)})

    rows = human_queryset.annotate(
        ai_has=Exists(ai_exists),
        ai_label_id=Subquery(ai_label),
    )
    same = (
        Q(ai_label_id=F('project_label_id'))
        | (Q(ai_label_id__isnull=True) & Q(project_label_id__isnull=True))
    )

    agg = rows.aggregate(
        human_total=Count('id'),
        reviewed=Count('id', filter=Q(ai_has=True)),
        matched=Count('id', filter=Q(ai_has=True) & same),
    )
    reviewed = agg['reviewed'] or 0
    matched = agg['matched'] or 0
    human_total = agg['human_total'] or 0
    return {
        'human_total': human_total,
        'reviewed': reviewed,
        'matched': matched,
        'corrected': reviewed - matched,
        # Người gán nhãn ở chỗ AI không hề có ý kiến: AI bỏ sót.
        'ai_missed': human_total - reviewed,
        'accuracy': round(100 * matched / reviewed, 1) if reviewed else None,
    }


def ai_review_stats(project=None, link=None) -> dict:
    """
    Đối chiếu toàn bộ nhãn người với đề xuất AI, ở cả cấp câu và cấp token.

    Truyền `link` để xem riêng một nguồn dữ liệu, `project` để xem cả dự án.
    """
    comment_stats = _compare(
        _scope(CommentAnnotation.objects.filter(source='manual'),
               project, link, prefix='comment__'),
        _scope(CommentAnnotation.objects.filter(source='ai'),
               project, link, prefix='comment__'),
        'comment_id',
    )
    token_stats = _compare(
        _scope(TokenAnnotation.objects.filter(source='manual'),
               project, link, prefix='token__comment__'),
        _scope(TokenAnnotation.objects.filter(source='ai'),
               project, link, prefix='token__comment__'),
        'token_id',
    )

    # Đề xuất của AI chưa ai đụng tới: phần việc còn có thể tận dụng.
    pending_comments = _scope(
        CommentAnnotation.objects.filter(source='ai'), project, link,
        prefix='comment__',
    ).exclude(
        comment__annotations__source='manual'
    ).count()

    # Câu AI chạy qua nhưng không đưa ra đề xuất nào. Người dùng dễ tưởng
    # "link đã gán nhãn xong" là mọi câu đều có nhãn AI, rồi thấy câu trắng
    # trơn thì nghĩ hệ thống làm mất nhãn. Đưa con số này ra màn hình.
    scope = (
        Comment.objects.filter(youtube_link=link) if link is not None
        else Comment.objects.filter(youtube_link__project=project)
    )
    no_ai = scope.exclude(annotations__source='ai').count()

    return {
        'comments': comment_stats,
        'tokens': token_stats,
        'pending_ai_comments': pending_comments,
        'no_ai_comments': no_ai,
        'total_comments': scope.count(),
    }
