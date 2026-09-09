"""
Data migration: chuyển dữ liệu nhãn cũ sang mô hình đa annotator.

Mô hình cũ chỉ lưu MỘT manual_label + MỘT ai_label thẳng trên Comment/Token,
không biết ai đã gán nhãn. Migration này dựng lại lịch sử đó thành các bản ghi
CommentAnnotation/TokenAnnotation:

1. source_text = text  -> giữ bản gốc bất biến cho mọi comment hiện có.
2. ai_processed = True cho comment AI đã xử lý, để hệ thống KHÔNG gọi lại LLM
   (và không tính tiền lại) trên toàn bộ dữ liệu cũ.
3. Tạo annotation 'ai' từ ai_label, annotation 'manual' từ manual_label — quy
   cho CHỦ DỰ ÁN, vì trong mô hình cũ chỉ chủ dự án mới gán nhãn được.
4. gold_label = manual_label và review_status='agreed': dữ liệu cũ chỉ có một
   người gán nhãn nên không thể có bất đồng.

Đảo ngược được: reverse() xoá sạch annotation do migration này sinh ra.
"""
from django.db import migrations
from django.db.models import F

BATCH = 2000


def forwards(apps, schema_editor):
    Comment = apps.get_model('comments', 'Comment')
    Token = apps.get_model('comments', 'Token')
    CommentAnnotation = apps.get_model('comments', 'CommentAnnotation')
    TokenAnnotation = apps.get_model('comments', 'TokenAnnotation')

    # --- 1. Bản gốc bất biến ---
    Comment.objects.filter(source_text='').update(source_text=F('text'))

    # --- 2. Cờ đã xử lý bởi AI ---
    Comment.objects.filter(ai_label__isnull=False).update(ai_processed=True)
    Comment.objects.filter(annotated_at__isnull=False).update(ai_processed=True)
    Comment.objects.filter(is_meaningful=False).update(ai_processed=True)

    # --- 3a. Annotation cấp câu do AI gán ---
    rows = Comment.objects.filter(ai_label__isnull=False).values_list(
        'id', 'ai_label_id', 'toxicity_confidence', 'is_meaningful'
    )
    _bulk(CommentAnnotation, (
        CommentAnnotation(
            comment_id=comment_id, annotator=None, source='ai',
            project_label_id=label_id, confidence=confidence,
            is_meaningful=is_meaningful,
        )
        for comment_id, label_id, confidence, is_meaningful in rows.iterator(chunk_size=BATCH)
    ))

    # --- 3b. Annotation cấp câu do người gán (quy cho chủ dự án) ---
    rows = Comment.objects.filter(manual_label__isnull=False).values_list(
        'id', 'manual_label_id', 'youtube_link__project__owner_id'
    )
    _bulk(CommentAnnotation, (
        CommentAnnotation(
            comment_id=comment_id, annotator_id=owner_id, source='manual',
            project_label_id=label_id, is_meaningful=True,
        )
        for comment_id, label_id, owner_id in rows.iterator(chunk_size=BATCH)
    ))

    # --- 4. Nhãn vàng + trạng thái review ---
    Comment.objects.filter(manual_label__isnull=False).update(
        gold_label=F('manual_label'),
        review_status='agreed',
        manual_annotation_count=1,
    )

    # --- 5. Annotation cấp token ---
    rows = Token.objects.filter(ai_label__isnull=False).values_list(
        'id', 'ai_label_id', 'toxicity_score'
    )
    _bulk(TokenAnnotation, (
        TokenAnnotation(
            token_id=token_id, annotator=None, source='ai',
            project_label_id=label_id, score=score,
        )
        for token_id, label_id, score in rows.iterator(chunk_size=BATCH)
    ))

    rows = Token.objects.filter(manual_label__isnull=False).values_list(
        'id', 'manual_label_id', 'toxicity_score',
        'comment__youtube_link__project__owner_id',
    )
    _bulk(TokenAnnotation, (
        TokenAnnotation(
            token_id=token_id, annotator_id=owner_id, source='manual',
            project_label_id=label_id, score=score,
        )
        for token_id, label_id, score, owner_id in rows.iterator(chunk_size=BATCH)
    ))

    Token.objects.filter(manual_label__isnull=False).update(gold_label=F('manual_label'))


def _bulk(model, generator):
    """Ghi theo lô để không nạp toàn bộ dữ liệu vào RAM."""
    buffer = []
    for obj in generator:
        buffer.append(obj)
        if len(buffer) >= BATCH:
            model.objects.bulk_create(buffer, ignore_conflicts=True)
            buffer = []
    if buffer:
        model.objects.bulk_create(buffer, ignore_conflicts=True)


def backwards(apps, schema_editor):
    apps.get_model('comments', 'CommentAnnotation').objects.all().delete()
    apps.get_model('comments', 'TokenAnnotation').objects.all().delete()
    apps.get_model('comments', 'Comment').objects.update(
        gold_label=None, review_status='pending', manual_annotation_count=0,
        ai_processed=False, ai_processed_at=None, source_text='',
    )
    apps.get_model('comments', 'Token').objects.update(gold_label=None)


class Migration(migrations.Migration):

    dependencies = [
        ('comments', '0014_annotationassignment_annotationevent_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
