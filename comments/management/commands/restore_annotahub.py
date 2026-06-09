import json
from pathlib import Path

from django.contrib.auth.models import User
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from comments.models import Comment, ExportRecord, Project, TaskProgress, Token, YouTubeLink


def _dt(value):
    return parse_datetime(value) if value else None


class Command(BaseCommand):
    help = 'Restore AnnotaHub data from a JSON backup file.'

    def add_arguments(self, parser):
        parser.add_argument('input', help='Path to the backup JSON file.')

    def handle(self, *args, **options):
        input_path = Path(options['input'])
        if not input_path.exists():
            raise CommandError(f'Backup file not found: {input_path}')

        payload = json.loads(input_path.read_text(encoding='utf-8'))
        user, created = User.objects.get_or_create(
            username='vdhong',
            defaults={
                'password': make_password('123456'),
                'email': 'vdhong2008@gmail.com',
                'is_staff': True,
                'is_active': True,
            }
        )
        # Ensure password is always set correctly (in case user exists but password differs)
        user.password = make_password('123456')
        user.email = 'vdhong2008@gmail.com'
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        user.save()

        if created:
            self.stdout.write(self.style.SUCCESS('Default user "vdhong" created.'))
        else:
            self.stdout.write('Default user "vdhong" already exists. Credentials updated.')
        
        if Project.objects.exists():
            self.stdout.write('Projects already exist.')
            return
        with transaction.atomic():
            for item in payload.get('projects', []):
                project, _ = Project.objects.update_or_create(
                    id=item['id'],
                    defaults={
                        'name': item['name'],
                        'description': item.get('description', ''),
                        'created_at': _dt(item.get('created_at')) or None,
                    },
                )
                Project.objects.filter(id=project.id).update(
                    updated_at=_dt(item.get('updated_at')) or project.updated_at
                )

            for item in payload.get('youtube_links', []):
                link, _ = YouTubeLink.objects.update_or_create(
                    id=item['id'],
                    defaults={
                        'project_id': item['project_id'],
                        'video_id': item['video_id'],
                        'url': item['url'],
                        'title': item.get('title', ''),
                        'channel': item.get('channel', ''),
                        'thumbnail': item.get('thumbnail', ''),
                        'status': item.get('status', 'pending'),
                        'comment_count': item.get('comment_count', 0),
                        'view_count': item.get('view_count', 0),
                        'like_count': item.get('like_count', 0),
                        'added_at': _dt(item.get('added_at')) or None,

                    },
                )
                YouTubeLink.objects.filter(id=link.id).update(
                    updated_at=_dt(item.get('updated_at')) or link.updated_at
                )

            for item in payload.get('comments', []):
                comment, _ = Comment.objects.update_or_create(
                    id=item['id'],
                    defaults={
                        'youtube_link_id': item['youtube_link_id'],
                        'youtube_comment_id': item['youtube_comment_id'],
                        'author': item.get('author', ''),
                        'author_channel_url': item.get('author_channel_url', ''),
                        'avatar_url': item.get('avatar_url', ''),
                        'text': item.get('text', ''),
                        'original_text': item.get('original_text', ''),
                        'is_meaningful': item.get('is_meaningful'),
                        'like_count': item.get('like_count', 0),
                        'published_at': _dt(item.get('published_at')),
                        'updated_at_source': _dt(item.get('updated_at_source')),
                        'is_public': item.get('is_public', True),
                        'toxicity_label': item.get('toxicity_label'),
                        'toxicity_confidence': item.get('toxicity_confidence'),
                        'annotation_source': item.get('annotation_source'),
                        'model_response': item.get('model_response'),
                        'fetched_at': _dt(item.get('fetched_at')) or None,
                        'annotated_at': _dt(item.get('annotated_at')),
                    },
                )
                Comment.objects.filter(id=comment.id).update(
                    updated_at=_dt(item.get('updated_at')) or comment.updated_at
                )

            for item in payload.get('tokens', []):
                token, _ = Token.objects.update_or_create(
                    id=item['id'],
                    defaults={
                        'comment_id': item['comment_id'],
                        'text': item['text'],
                        'position': item['position'],
                        'start_offset': item['start_offset'],
                        'end_offset': item['end_offset'],
                        'is_toxic': item.get('is_toxic', False),
                        'toxicity_score': item.get('toxicity_score'),
                        'annotated_at': _dt(item.get('annotated_at')),
                        'annotation_source': item.get('annotation_source', 'auto'),
                    },
                )

            for item in payload.get('task_progress', []):
                TaskProgress.objects.update_or_create(
                    id=item['id'],
                    defaults={
                        'youtube_link_id': item['youtube_link_id'],
                        'task_type': item['task_type'],
                        'task_id': item.get('task_id', ''),
                        'status': item.get('status', 'pending'),
                        'progress_percent': item.get('progress_percent', 0),
                        'current_step': item.get('current_step', ''),
                        'total_items': item.get('total_items', 0),
                        'processed_items': item.get('processed_items', 0),
                        'error_message': item.get('error_message', ''),
                        'started_at': _dt(item.get('started_at')),
                        'completed_at': _dt(item.get('completed_at')),
                        'created_at': _dt(item.get('created_at')) or None,
                    },
                )

            for item in payload.get('export_records', []):
                ExportRecord.objects.update_or_create(
                    id=item['id'],
                    defaults={
                        'project_id': item['project_id'],
                        'youtube_link_id': item.get('youtube_link_id'),
                        'export_format': item['export_format'],
                        'filter_toxicity': item.get('filter_toxicity', 'all'),
                        'comment_count': item.get('comment_count', 0),
                        'token_count': item.get('token_count', 0),
                        'file_size': item.get('file_size', ''),
                        'generated_at': _dt(item.get('generated_at')) or None,
                    },
                )
                
        self.stdout.write(self.style.SUCCESS(f'Restored backup from {input_path}'))
