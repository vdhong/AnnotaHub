from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('comments', '0002_comment_original_text'),
    ]

    operations = [
        migrations.AddField(
            model_name='comment',
            name='is_meaningful',
            field=models.BooleanField(blank=True, default=None, help_text='Whether the comment contains meaningful content that should be labeled', null=True),
        ),
    ]
