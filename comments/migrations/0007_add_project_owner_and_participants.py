# Migration: Add owner and participants fields to Project model
# - owner: ForeignKey to User (the project owner)
# - participants: ManyToMany to User (users with label-only access)

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('comments', '0006_dual_label_system'),
    ]

    operations = [
        # Add owner field as nullable first
        migrations.AddField(
            model_name='project',
            name='owner',
            field=models.ForeignKey(
                null=True,
                blank=True,
                help_text='The user who owns this project',
                on_delete=django.db.models.deletion.CASCADE,
                related_name='owned_projects',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        # Add participants ManyToMany field
        migrations.AddField(
            model_name='project',
            name='participants',
            field=models.ManyToManyField(
                blank=True,
                help_text='Users who can participate in this project (label-only access)',
                related_name='participated_projects',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]