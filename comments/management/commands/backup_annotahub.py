import json
from pathlib import Path

from django.core.management.base import BaseCommand

from comments.models import Comment, ExportRecord, Project, TaskProgress, Token, YouTubeLink


def _dt(value):
    return value.isoformat() if value else None
class Command(BaseCommand):
    help = 'Backup AnnotaHub data to a JSON file.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--output',
            default='backups/annotahub_backup.json',
            help='Path to the backup JSON file.',
        )

    def handle(self, *args, **options):
        output_path = Path(options['output'])
        output_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            'projects': [],
            'youtube_links': [],
            'comments': [],
            'tokens': [],
            'task_progress': [],
            'export_records': [],
        }

        for project in Project.objects.all():
            data['projects'].append({
                'id': str(project.id),
                'name': project.name,
                'description': project.description,
                'created_at': _dt(project.created_at),
                'updated_at': _dt(project.updated_at),
            })

        for link in YouTubeLink.objects.all():
            data['youtube_links'].append({
                'id': str(link.id),
                'project_id': str(link.project_id),
                'video_id': link.video_id,
                'url': link.url,
                'title': link.title,
                'channel': link.channel,
                'thumbnail': link.thumbnail,
                'status': link.status,
                'comment_count': link.comment_count,
                'added_at': _dt(link.added_at),
                'updated_at': _dt(link.updated_at),
                'view_count': link.view_count,
                'like_count': link.like_count,
            })

        for comment in Comment.objects.all():
            data['comments'].append({
                'id': str(comment.id),
                'youtube_link_id': str(comment.youtube_link_id),
                'youtube_comment_id': comment.youtube_comment_id,
                'author': comment.author,
                'author_channel_url': comment.author_channel_url,
                'avatar_url': comment.avatar_url,
                'text': comment.text,
                'original_text': comment.original_text,
                'is_meaningful': comment.is_meaningful,
                'like_count': comment.like_count,
                'published_at': _dt(comment.published_at),
                'updated_at_source': _dt(comment.updated_at_source),
                'is_public': comment.is_public,
                'toxicity_label': comment.toxicity_label,
                'toxicity_confidence': comment.toxicity_confidence,
                'annotation_source': comment.annotation_source,
                'model_response': comment.model_response,
                'fetched_at': _dt(comment.fetched_at),
                'annotated_at': _dt(comment.annotated_at),
                'updated_at': _dt(comment.updated_at),
            })

        for token in Token.objects.all():
            data['tokens'].append({
                'id': str(token.id),
                'comment_id': str(token.comment_id),
                'text': token.text,
                'position': token.position,
                'start_offset': token.start_offset,
                'end_offset': token.end_offset,
                'is_toxic': token.is_toxic,
                'toxicity_score': token.toxicity_score,
                'annotated_at': _dt(token.annotated_at),
                'annotation_source': token.annotation_source,
            })

        for task in TaskProgress.objects.all():
            data['task_progress'].append({
                'id': str(task.id),
                'youtube_link_id': str(task.youtube_link_id),
                'task_type': task.task_type,
                'task_id': task.task_id,
                'status': task.status,
                'progress_percent': task.progress_percent,
                'current_step': task.current_step,
                'total_items': task.total_items,
                'processed_items': task.processed_items,
                'error_message': task.error_message,
                'started_at': _dt(task.started_at),
                'completed_at': _dt(task.completed_at),
                'created_at': _dt(task.created_at),
            })

        for export_record in ExportRecord.objects.all():
            data['export_records'].append({
                'id': str(export_record.id),
                'project_id': str(export_record.project_id),
                'youtube_link_id': str(export_record.youtube_link_id) if export_record.youtube_link_id else None,
                'export_format': export_record.export_format,
                'filter_toxicity': export_record.filter_toxicity,
                'comment_count': export_record.comment_count,
                'token_count': export_record.token_count,
                'file_size': export_record.file_size,
                'generated_at': _dt(export_record.generated_at),
            })

        output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(f'Backup written to {output_path}'))
