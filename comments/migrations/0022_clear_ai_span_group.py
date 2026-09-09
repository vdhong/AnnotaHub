"""
Bỏ span_group trên các token do AI gán.

Ranh giới cụm bây giờ chỉ còn là thông tin của người gán nhãn: ai kéo chọn cả
cụm thì cụm đó có ý nghĩa, còn chỗ LLM tự cắt thì không đáng tin bằng. Khi
thiếu span_group, export gộp các token liền kề cùng nhãn, đủ dùng cho nhãn AI.

Token nào người đã chạm vào (annotation_source='manual') thì giữ nguyên.
"""
from django.db import migrations


def clear_ai_span_group(apps, schema_editor):
    Token = apps.get_model('comments', 'Token')
    Token.objects.filter(annotation_source='auto').update(span_group=None)


def noop(apps, schema_editor):
    """Không khôi phục được: giá trị cũ chỉ là UUID sinh ngẫu nhiên lúc chạy."""


class Migration(migrations.Migration):

    dependencies = [
        ('comments', '0021_datasetversion_current_step_and_more'),
    ]

    operations = [
        migrations.RunPython(clear_ai_span_group, noop),
    ]
