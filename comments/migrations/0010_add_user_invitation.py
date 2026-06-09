# Generated migration for UserInvitation model

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('comments', '0009_add_label_owner'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='UserInvitation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('email', models.EmailField(db_index=True, max_length=254)),
                ('token', models.CharField(db_index=True, max_length=64, unique=True)),
                ('is_used', models.BooleanField(default=False, help_text='Whether this invitation has been used')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('expires_at', models.DateTimeField(blank=True, help_text='Expiration time for the invitation link', null=True)),
                ('inviter', models.ForeignKey(help_text='User who sent the invitation', on_delete=django.db.models.deletion.CASCADE, related_name='sent_invitations', to=settings.AUTH_USER_MODEL)),
                ('project', models.ForeignKey(help_text='The project that triggered this invitation', on_delete=django.db.models.deletion.CASCADE, related_name='invitations', to='comments.project')),
                ('user', models.ForeignKey(blank=True, help_text='The user account associated with this invitation', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='invitations', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]