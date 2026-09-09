"""
Thống kê nhãn: tính bằng SQL aggregation thay vì gộp trong Python.

Thay thế hàm count_label() cũ vốn kéo toàn bộ cặp (comment_id, label_id) về
tầng ứng dụng rồi đếm bằng collections.Counter.
"""
from __future__ import annotations

from django.db.models import Count, Q

from ..models import Comment, CommentAnnotation, ProjectLabel


def label_stats_for_link(project_labels, link, *, user=None) -> list[dict]:
    """
    Đếm số comment mang mỗi nhãn.

    `user` có giá trị -> đếm nhãn do chính người đó gán. Đây là con số dùng cho
    màn hình gán nhãn: nếu đếm theo manual_label (bản sao dùng chung) thì bảng
    thống kê của annotator lại là thành quả của cả nhóm.
    `user` là None -> nhãn hiệu lực của cả dự án (manual_label, không có thì ai_label).

    Một comment chỉ được tính cho đúng một nhãn nên tổng các count không vượt
    quá tổng số comment.
    """
    project_labels = list(project_labels)
    if not project_labels:
        return []

    counts: dict = {}
    if user is not None and getattr(user, 'is_authenticated', False):
        rows = (
            CommentAnnotation.objects
            .filter(comment__youtube_link=link, annotator=user, source='manual',
                    project_label__isnull=False)
            .values('project_label_id')
            .annotate(n=Count('id'))
        )
        counts = {row['project_label_id']: row['n'] for row in rows}
    else:
        rows = (
            Comment.objects.filter(youtube_link=link)
            .exclude(manual_label__isnull=True, ai_label__isnull=True)
            .values('manual_label_id', 'ai_label_id')
            .annotate(n=Count('id'))
        )
        for row in rows:
            effective = row['manual_label_id'] or row['ai_label_id']
            if effective is not None:
                counts[effective] = counts.get(effective, 0) + row['n']

    return [
        {
            'id': str(pl.id),
            'name': pl.display_name,
            'color': pl.display_color,
            'count': counts.get(pl.id, 0),
        }
        for pl in project_labels
    ]


def link_counters(link, *, user=None) -> dict:
    """
    Các con số tổng hợp của một link, gộp trong một truy vấn.

    `user` có giá trị -> "đã gán / còn lại" đếm theo phần việc của chính người
    đó. Nếu đếm theo manual_label thì màn hình báo "đã gán 500" trong khi người
    đang xem chưa gán câu nào, vì đó là việc của người khác.
    """
    mine = Q(annotations__annotator=user, annotations__source='manual')
    per_user = user is not None and getattr(user, 'is_authenticated', False)
    comments = Comment.objects.filter(youtube_link=link)

    agg = comments.aggregate(
        total=Count('id', distinct=True),
        annotated=Count(
            'id',
            filter=mine if per_user
            else Q(manual_label__isnull=False) | Q(ai_label__isnull=False),
            distinct=True,
        ),
        skipped=Count('id', filter=Q(is_meaningful=False), distinct=True),
        # "AI chưa xử lý" phải hiểu đúng như lúc chia batch: chưa chạy qua và
        # không bị người đánh dấu bỏ qua. Ràng buộc is_meaningful=None sẽ hụt,
        # vì AI chạy xong là cột này thành True/False, nên sau khi bấm chạy lại
        # thì không câu nào lọt vào và nút "gán nhãn tiếp" biến mất.
        ai_pending=Count(
            'id',
            filter=Q(ai_processed=False) & (
                Q(is_meaningful=True) | Q(is_meaningful__isnull=True)
            ),
            distinct=True,
        ),
        shared_pending=Count(
            'id',
            filter=Q(manual_label__isnull=True) & ~Q(is_meaningful=False),
            distinct=True,
        ),
    )

    if per_user:
        # Phủ định phải dùng exclude() chứ không phải Count(filter=~Q(...)):
        # với quan hệ một-nhiều, `~Q` chỉ phủ định từng dòng sau khi JOIN, nên
        # một comment do người khác gán vẫn lọt vào "chưa gán" của tôi.
        manual_pending = (
            comments.exclude(is_meaningful=False).exclude(mine).count()
        )
    else:
        manual_pending = agg['shared_pending'] or 0

    return {
        'total_comments': agg['total'] or 0,
        'annotated_comments': agg['annotated'] or 0,
        'skipped_count': agg['skipped'] or 0,
        'unannotated_count': agg['ai_pending'] or 0,
        'manual_pending_count': manual_pending,
    }


def project_label_map(project) -> dict:
    """
    Bảng tra {tên nhãn (lowercase): ProjectLabel}, dựng một lần rồi truyền đi.

    Thay cho _find_project_label() cũ vốn truy vấn DB cho mỗi token (N+1).
    """
    return {
        pl.label.name.lower(): pl
        for pl in ProjectLabel.objects.filter(project=project).select_related('label')
    }
