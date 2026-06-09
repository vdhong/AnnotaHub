# Generated migration for multi-label system

from django.db import migrations, models
import django.db.models.deletion
import uuid
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('comments', '0004_youtubelink_like_count_youtubelink_view_count'),
    ]

    operations = [
        # Create Label model (public labels)
        migrations.CreateModel(
            name='Label',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=100, unique=True)),
                ('description', models.TextField(blank=True, default='', help_text='Description of when to use this label')),
                ('color', models.CharField(default='#FF0000', help_text='Hex color code for display (e.g., #FF0000)', max_length=7)),
                ('is_active', models.BooleanField(default=True, help_text='Whether this label can be assigned to new projects')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['name'],
            },
        ),

        # Create ProjectLabel model (links Label to Project with optional overrides)
        migrations.CreateModel(
            name='ProjectLabel',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='project_labels', to='comments.project')),
                ('label', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='project_labels', to='comments.label')),
                ('override_name', models.CharField(blank=True, max_length=100, null=True)),
                ('override_description', models.TextField(blank=True, null=True)),
                ('override_color', models.CharField(blank=True, max_length=7, null=True)),
            ],
            options={
                'ordering': ['id'],
                'unique_together': {('project', 'label')},
            },
        ),

        # Add ManyToMany from Token to ProjectLabel
        migrations.AddField(
            model_name='token',
            name='project_token_labels',
            field=models.ManyToManyField(
                blank=True,
                help_text='Labels assigned to this token',
                related_name='token_labels',
                to='comments.projectlabel',
            ),
        ),

        # Add ManyToMany from Comment to ProjectLabel
        migrations.AddField(
            model_name='comment',
            name='project_comment_labels',
            field=models.ManyToManyField(
                blank=True,
                help_text='Labels assigned to this comment',
                related_name='comment_labels',
                to='comments.projectlabel',
            ),
        ),
    ]