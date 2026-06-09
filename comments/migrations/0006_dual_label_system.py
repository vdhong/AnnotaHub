# Migration: Dual-label system (AI label + Manual label) for Token and Comment
# Replaces ManyToMany label relationships with single ForeignKey labels.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('comments', '0005_add_multi_label_system'),
    ]

    operations = [
        # --- Remove old ManyToMany relationships ---
        migrations.RemoveField(
            model_name='token',
            name='project_token_labels',
        ),
        migrations.RemoveField(
            model_name='comment',
            name='project_comment_labels',
        ),

        # --- Add ai_label and manual_label to Token ---
        migrations.AddField(
            model_name='token',
            name='ai_label',
            field=models.ForeignKey(
                blank=True,
                help_text='Label assigned by AI',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='tokens_ai_labeled',
                to='comments.projectlabel',
            ),
        ),
        migrations.AddField(
            model_name='token',
            name='manual_label',
            field=models.ForeignKey(
                blank=True,
                help_text='Label assigned by user (overrides AI label for display)',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='tokens_manual_labeled',
                to='comments.projectlabel',
            ),
        ),

        # --- Add ai_label and manual_label to Comment ---
        migrations.AddField(
            model_name='comment',
            name='ai_label',
            field=models.ForeignKey(
                blank=True,
                help_text='Label assigned by AI',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='comments_ai_labeled',
                to='comments.projectlabel',
            ),
        ),
        migrations.AddField(
            model_name='comment',
            name='manual_label',
            field=models.ForeignKey(
                blank=True,
                help_text='Label assigned by user (overrides AI label for display)',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='comments_manual_labeled',
                to='comments.projectlabel',
            ),
        ),
    ]