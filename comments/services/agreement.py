"""
Đo độ đồng thuận giữa những người gán nhãn (Inter-Annotator Agreement).

Đây là chỉ số bắt buộc để một bộ dữ liệu gán nhãn có giá trị khoa học: nó trả
lời câu hỏi "hai người độc lập cùng đọc một comment có gán cùng một nhãn không?".

Cung cấp:
- percent_agreement : tỉ lệ đồng thuận thô (dễ hiểu nhưng lạc quan quá mức).
- cohen_kappa       : cho đúng 2 annotator, đã loại trừ đồng thuận ngẫu nhiên.
- krippendorff_alpha: cho số lượng bất kỳ annotator, chịu được dữ liệu khuyết.
"""
from __future__ import annotations

import itertools
import logging
from collections import defaultdict

from ..models import Comment, CommentAnnotation

logger = logging.getLogger(__name__)

NO_LABEL = 'O'


def _matrix_for_project(project, *, link=None) -> dict[str, dict[str, str]]:
    """
    Dựng ma trận {comment_id: {annotator_id: label_key}} từ annotation thủ công.

    Chỉ lấy annotation của người thật (source='manual'); nhãn AI không tham gia
    tính đồng thuận giữa người với người.
    """
    queryset = CommentAnnotation.objects.filter(
        comment__youtube_link__project=project,
        source='manual',
        annotator__isnull=False,
    )
    if link is not None:
        queryset = queryset.filter(comment__youtube_link=link)

    rows = queryset.values_list('comment_id', 'annotator_id', 'project_label_id')

    matrix: dict[str, dict[str, str]] = defaultdict(dict)
    for comment_id, annotator_id, label_id in rows:
        matrix[str(comment_id)][str(annotator_id)] = (
            str(label_id) if label_id else NO_LABEL
        )
    return matrix


def percent_agreement(matrix) -> tuple[float | None, int]:
    """
    Tỉ lệ cặp annotator đồng ý, trên các comment có >= 2 người gán nhãn.
    Trả về (tỉ lệ, số comment được tính).
    """
    agree = 0
    total = 0
    counted = 0
    for labels in matrix.values():
        if len(labels) < 2:
            continue
        counted += 1
        for a, b in itertools.combinations(labels.values(), 2):
            total += 1
            if a == b:
                agree += 1
    if total == 0:
        return None, 0
    return agree / total, counted


def cohen_kappa(matrix, annotator_a: str, annotator_b: str) -> tuple[float | None, int]:
    """
    Cohen's kappa cho một cặp annotator, tính trên các comment cả hai cùng gán.

    kappa = (Po - Pe) / (1 - Pe)
      Po: tỉ lệ đồng thuận quan sát được
      Pe: tỉ lệ đồng thuận kỳ vọng nếu cả hai gán nhãn ngẫu nhiên độc lập
    """
    pairs = [
        (labels[annotator_a], labels[annotator_b])
        for labels in matrix.values()
        if annotator_a in labels and annotator_b in labels
    ]
    n = len(pairs)
    if n == 0:
        return None, 0

    observed = sum(1 for a, b in pairs if a == b) / n

    labels_a = defaultdict(int)
    labels_b = defaultdict(int)
    for a, b in pairs:
        labels_a[a] += 1
        labels_b[b] += 1

    expected = sum(
        (labels_a[label] / n) * (labels_b[label] / n)
        for label in set(labels_a) | set(labels_b)
    )
    if expected >= 1.0:
        # Cả hai luôn gán đúng một nhãn duy nhất -> kappa không xác định.
        return (1.0 if observed == 1.0 else 0.0), n
    return (observed - expected) / (1 - expected), n


def krippendorff_alpha(matrix) -> tuple[float | None, int]:
    """
    Krippendorff's alpha cho dữ liệu định danh (nominal), nhiều annotator,
    chấp nhận dữ liệu khuyết (không phải ai cũng gán mọi comment).

    alpha = 1 - Do/De
      Do: bất đồng quan sát được
      De: bất đồng kỳ vọng khi gán ngẫu nhiên
    """
    units = [labels for labels in matrix.values() if len(labels) >= 2]
    if not units:
        return None, 0

    # Tần suất nhãn trên toàn bộ (dùng cho bất đồng kỳ vọng).
    global_counts = defaultdict(int)
    total_values = 0
    for labels in units:
        for value in labels.values():
            global_counts[value] += 1
            total_values += 1

    if total_values < 2:
        return None, 0

    # Bất đồng quan sát được, chuẩn hoá theo số cặp trong từng đơn vị.
    observed_disagreement = 0.0
    for labels in units:
        values = list(labels.values())
        m = len(values)
        pairs = 0
        disagree = 0
        for a, b in itertools.combinations(values, 2):
            pairs += 1
            if a != b:
                disagree += 1
        if pairs:
            observed_disagreement += (disagree / pairs) * (m / (m - 1)) if m > 1 else 0

    n_units = len(units)
    Do = observed_disagreement / n_units

    # Bất đồng kỳ vọng = xác suất hai giá trị rút ngẫu nhiên khác nhau.
    De = 1.0 - sum(
        (count / total_values) ** 2 for count in global_counts.values()
    )
    De = De * total_values / (total_values - 1) if total_values > 1 else De

    if De == 0:
        return (1.0 if Do == 0 else 0.0), n_units
    return 1 - (Do / De), n_units


def interpret(alpha: float | None) -> str:
    """Diễn giải chỉ số theo thang Landis & Koch / Krippendorff."""
    if alpha is None:
        return 'chưa đủ dữ liệu'
    if alpha < 0:
        return 'tệ hơn ngẫu nhiên'
    if alpha < 0.20:
        return 'rất thấp'
    if alpha < 0.40:
        return 'thấp'
    if alpha < 0.60:
        return 'trung bình'
    if alpha < 0.667:
        return 'khá'
    if alpha < 0.80:
        return 'chấp nhận được để công bố'
    return 'tốt'


def project_agreement_report(project, *, link=None) -> dict:
    """Báo cáo đầy đủ về độ đồng thuận của một dự án."""
    from django.contrib.auth.models import User

    matrix = _matrix_for_project(project, link=link)

    annotator_ids = sorted({aid for labels in matrix.values() for aid in labels})
    usernames = {
        str(u.id): (u.get_full_name() or u.username)
        for u in User.objects.filter(id__in=annotator_ids)
    }

    pa, pa_units = percent_agreement(matrix)
    alpha, alpha_units = krippendorff_alpha(matrix)

    pairwise = []
    for a, b in itertools.combinations(annotator_ids, 2):
        kappa, n = cohen_kappa(matrix, a, b)
        if n > 0:
            pairwise.append({
                'annotator_a': usernames.get(a, a),
                'annotator_b': usernames.get(b, b),
                'kappa': round(kappa, 4) if kappa is not None else None,
                'overlap': n,
                'interpretation': interpret(kappa),
            })
    pairwise.sort(key=lambda row: (row['kappa'] is None, row['kappa'] or 0))

    total_comments = Comment.objects.filter(
        youtube_link__project=project
    ).count() if link is None else Comment.objects.filter(youtube_link=link).count()

    return {
        'total_comments': total_comments,
        'multi_annotated_units': alpha_units,
        'annotator_count': len(annotator_ids),
        'percent_agreement': round(pa, 4) if pa is not None else None,
        'percent_agreement_units': pa_units,
        'krippendorff_alpha': round(alpha, 4) if alpha is not None else None,
        'alpha_interpretation': interpret(alpha),
        'pairwise_kappa': pairwise,
    }


def annotator_productivity(project) -> list[dict]:
    """Năng suất từng annotator: số comment đã gán, thời gian trung bình."""
    from django.db.models import Avg, Count

    rows = (
        CommentAnnotation.objects.filter(
            comment__youtube_link__project=project,
            source='manual',
            annotator__isnull=False,
        )
        .values('annotator__id', 'annotator__username', 'annotator__first_name',
                'annotator__last_name')
        .annotate(
            annotated=Count('id'),
            avg_time_ms=Avg('time_spent_ms'),
        )
        .order_by('-annotated')
    )

    result = []
    for row in rows:
        full_name = f"{row['annotator__first_name']} {row['annotator__last_name']}".strip()
        result.append({
            'annotator_id': str(row['annotator__id']),
            'name': full_name or row['annotator__username'],
            'annotated': row['annotated'],
            'avg_seconds': round((row['avg_time_ms'] or 0) / 1000, 1),
        })
    return result
