"""
Data migration: dựng span_group cho token đã gán nhãn từ trước.

Trường `span_group` mới cho phép xuất BIO chính xác. Với dữ liệu cũ không còn
thông tin ranh giới cụm, ta dùng cách diễn giải hợp lý nhất còn lại: các token
LIỀN KỀ trong cùng một bình luận, mang CÙNG một nhãn, được coi là MỘT cụm.

Đây đúng là cách annotator thao tác trong giao diện (kéo chọn cả cụm rồi gán
một nhãn), nên suy luận này khớp với ý định gốc trong đại đa số trường hợp.
Token gán nhãn từ sau migration này sẽ có span_group chính xác, không phải suy đoán.
"""
import uuid

from django.db import migrations

BATCH = 1000


def forwards(apps, schema_editor):
    Token = apps.get_model('comments', 'Token')

    labelled = (
        Token.objects.filter(span_group__isnull=True)
        .exclude(manual_label__isnull=True, ai_label__isnull=True, gold_label__isnull=True)
        .order_by('comment_id', 'position')
        .values_list('id', 'comment_id', 'position',
                     'gold_label_id', 'manual_label_id', 'ai_label_id')
    )

    updates = []
    previous_comment = None
    previous_position = None
    previous_label = None
    current_group = None

    for token_id, comment_id, position, gold_id, manual_id, ai_id in labelled.iterator(
        chunk_size=BATCH
    ):
        label_id = gold_id or manual_id or ai_id
        contiguous = (
            comment_id == previous_comment
            and previous_position is not None
            and position == previous_position + 1
            and label_id == previous_label
        )
        if not contiguous:
            current_group = uuid.uuid4()

        updates.append((token_id, current_group))
        previous_comment = comment_id
        previous_position = position
        previous_label = label_id

        if len(updates) >= BATCH:
            _flush(Token, updates)
            updates = []

    if updates:
        _flush(Token, updates)


def _flush(Token, updates):
    """Ghi theo nhóm cùng span_group để giảm số câu lệnh UPDATE."""
    by_group = {}
    for token_id, group in updates:
        by_group.setdefault(group, []).append(token_id)
    for group, token_ids in by_group.items():
        Token.objects.filter(id__in=token_ids).update(span_group=group)


def backwards(apps, schema_editor):
    apps.get_model('comments', 'Token').objects.update(span_group=None)


class Migration(migrations.Migration):

    dependencies = [
        ('comments', '0016_token_span_group'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
