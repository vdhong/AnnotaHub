# Migration: Add owner field to Label model
# - owner: ForeignKey to User (the label creator/owner)
# - Change unique constraint on name to unique_together (owner, name)
# - Assign existing labels to first superuser or first user

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def assign_owners_to_labels(apps, schema_editor):
    """Assign existing labels without owner to the first user (usually admin)."""
    Label = apps.get_model('comments', 'Label')
    User = apps.get_model(settings.AUTH_USER_MODEL)
    
    labels_without_owner = Label.objects.filter(owner__isnull=True)
    if labels_without_owner.exists():
        # Assign to first user (usually the admin/superuser)
        first_user = User.objects.first()
        if first_user:
            labels_without_owner.update(owner=first_user)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('comments', '0008_add_user_settings'),
    ]

    operations = [
        # Step 1: Remove the unique constraint on name
        migrations.AlterField(
            model_name='label',
            name='name',
            field=models.CharField(max_length=100),
        ),

        # Step 2: Add owner field (nullable first for existing labels)
        migrations.AddField(
            model_name='label',
            name='owner',
            field=models.ForeignKey(
                null=True,
                blank=True,
                help_text='The user who created and owns this label',
                on_delete=django.db.models.deletion.CASCADE,
                related_name='labels',
                to=settings.AUTH_USER_MODEL,
            ),
        ),

        # Step 3: Assign existing labels to first user
        migrations.RunPython(assign_owners_to_labels, migrations.RunPython.noop),

        # Step 4: Add unique_together constraint on (owner, name)
        migrations.AlterUniqueTogether(
            name='label',
            unique_together={('owner', 'name')},
        ),
    ]