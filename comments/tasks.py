"""
Celery Tasks for YouTube Comment Collection and Toxicity Annotation
"""
import logging
import uuid
from celery import shared_task
from django.utils import timezone
from django.db import transaction

from .models import YouTubeLink, Comment, Token, TaskProgress, Project, Label, ProjectLabel, UserSettings
from .services.youtube_service import extract_video_id, get_video_info, fetch_comments
from .services.ollama_service import (
    annotate_comment,
    process_comment,
    create_token_annotations,
    get_comment_label_name,
)


def _get_owner_settings(project: Project):
    """
    Get the UserSettings for the project owner.
    Returns a UserSettings instance, or None if the owner has no custom settings.
    """
    try:
        return UserSettings.objects.get(user=project.owner)
    except UserSettings.DoesNotExist:
        return None


def _get_owner_youtube_api_key(project: Project):
    """Get YouTube API key from project owner's settings, or None to use global defaults."""
    owner_settings = _get_owner_settings(project)
    if owner_settings and owner_settings.has_youtube_api_key:
        return owner_settings.youtube_api_key
    return None


def _get_owner_ollama_config(project: Project):
    """
    Get Ollama config from project owner's settings.
    Returns (base_url, api_key, model) tuple, or (None, None, None) to use global defaults.
    """
    owner_settings = _get_owner_settings(project)
    if owner_settings and owner_settings.has_ollama_config:
        return (
            owner_settings.ollama_base_url,
            owner_settings.ollama_api_key,
            owner_settings.ollama_model,
        )
    return (None, None, None)


def _gather_labels_info(youtube_link: YouTubeLink):
    """
    Build labels_info list from project's ProjectLabel entries.
    Returns list of dicts with 'name', 'description', 'color' - or empty list.
    """
    project_labels = ProjectLabel.objects.filter(
        project=youtube_link.project
    ).select_related('label')
    if not project_labels.exists():
        return None  # Use legacy mode

    result = []
    for pl in project_labels:
        result.append({
            'name': pl.display_name,
            'description': pl.display_description or '',
            'color': pl.display_color,
        })
    return result


def _find_project_label(project, label_name):
    """
    Find a ProjectLabel by label name (case-insensitive).
    Returns the ProjectLabel instance or None.
    """
    if not label_name:
        return None
    for pl in ProjectLabel.objects.filter(project=project).select_related('label'):
        if pl.label.name.lower() == label_name.lower():
            return pl
    return None


def _apply_labels_to_comment(comment, annotation, youtube_link, labels_info):
    """
    Apply comment-level AI label from annotation result to the comment via ProjectLabel.
    Also handles token-level AI labels.
    Each token/comment gets exactly ONE ai_label (from AI) and optionally ONE manual_label (from user).
    """
    project = youtube_link.project

    # --- Comment-level AI label ---
    comment_label_name = get_comment_label_name(annotation)
    comment.ai_label = _find_project_label(project, comment_label_name)

    # --- Token-level AI labels ---
    vietnamese_text = annotation.get('vietnamese_text', comment.text)
    token_annotations = create_token_annotations(
        vietnamese_text, annotation, labels_info=labels_info
    )

    # Delete old tokens and create new ones
    Token.objects.filter(comment=comment).delete()

    for token_data in token_annotations:
        assigned_label_name = token_data.get('assigned_label')
        # Find matching ProjectLabel for AI label
        token_ai_label = _find_project_label(project, assigned_label_name)

        token = Token.objects.create(
            comment=comment,
            text=token_data['text'],
            position=token_data['position'],
            start_offset=token_data['start_offset'],
            end_offset=token_data['end_offset'],
            ai_label=token_ai_label,
            toxicity_score=token_data.get('toxicity_score'),
            annotated_at=timezone.now(),
            annotation_source='auto'
        )

logger = logging.getLogger(__name__)
TERMINAL_TASK_STATUSES = ('completed', 'failed', 'cancelled')


def _trigger_annotation(youtube_link_id: str):
    """Helper to trigger the annotation task for a given link."""
    try:
        youtube_link = YouTubeLink.objects.get(id=youtube_link_id)
    except YouTubeLink.DoesNotExist:
        logger.error(f"YouTubeLink {youtube_link_id} not found for annotation trigger")
        return

    youtube_link.status = 'annotating'
    youtube_link.save(update_fields=['status', 'updated_at'])
    enqueue_annotation_task(youtube_link, 'Starting annotation')


def _bootstrap_task_progress(youtube_link: YouTubeLink, task_type: str, task_id: str, current_step: str):
    """Create or refresh the task progress row that a worker will own."""
    return TaskProgress.objects.update_or_create(
        task_id=task_id,
        task_type=task_type,
        defaults={
            'youtube_link': youtube_link,
            'status': 'running',
            'progress_percent': 0,
            'current_step': current_step,
            'total_items': 0,
            'processed_items': 0,
            'error_message': '',
            'started_at': timezone.now(),
            'completed_at': None,
        },
    )[0]


def get_effective_task_progress(youtube_link_id: str, task_type: str):
    """
    Return the active task progress for a link.

    Prefer a running task if it exists, otherwise return the latest task
    snapshot of the requested type.
    """
    running_task = TaskProgress.objects.filter(
        youtube_link_id=youtube_link_id,
        task_type=task_type,
        status='running',
    ).order_by('-created_at').first()
    if running_task:
        return running_task
    return TaskProgress.objects.filter(
        youtube_link_id=youtube_link_id,
        task_type=task_type,
    ).order_by('-created_at').first()


def enqueue_fetch_comments_task(youtube_link: YouTubeLink, current_step: str = 'Queued for comment fetching'):
    """Create a progress row and enqueue the fetch task with a stable task id."""
    task_id = str(uuid.uuid4())
    _bootstrap_task_progress(youtube_link, 'fetching', task_id, current_step)
    fetch_comments_task.apply_async((str(youtube_link.id),), task_id=task_id)
    return task_id


def enqueue_annotation_task(youtube_link: YouTubeLink, current_step: str = 'Queued for annotation'):
    """Create a progress row and enqueue the annotation task with a stable task id."""
    task_id = str(uuid.uuid4())
    _bootstrap_task_progress(youtube_link, 'annotating', task_id, current_step)
    annotate_comments_task.apply_async((str(youtube_link.id),), task_id=task_id)
    return task_id


def _update_progress(task_progress, progress_percent, current_step, total, processed):
    """Update task progress in database."""
    try:
        task_progress.progress_percent = progress_percent
        task_progress.current_step = current_step
        task_progress.total_items = total
        task_progress.processed_items = processed
        task_progress.save(update_fields=[
            'progress_percent', 'current_step', 'total_items', 'processed_items'
        ])
    except Exception as e:
        logger.error(f"Error updating progress: {e}")


def _derive_link_status(youtube_link: YouTubeLink) -> str:
    """Compute a stable link status from the data currently stored."""
    if not youtube_link.comments.exists():
        return 'pending'
    if youtube_link.comments.filter(ai_label__isnull=True).exclude(is_meaningful=False).exists():
        return 'completed'
    return 'annotated'


def cancel_tasks_for_link_now(youtube_link_id: str):
    """Synchronously revoke running tasks for a link."""
    from annotahub.celery import app

    running_tasks = list(TaskProgress.objects.filter(
        youtube_link_id=youtube_link_id,
        status__in=['pending', 'running']
    ))

    cancelled_count = 0
    for task in running_tasks:
        if task.task_id:
            app.control.revoke(task.task_id, terminate=True)
        task.status = 'cancelled'
        task.current_step = task.current_step or 'Task cancelled by user'
        task.completed_at = timezone.now()
        task.save(update_fields=['status', 'current_step', 'completed_at'])
        cancelled_count += 1

    try:
        youtube_link = YouTubeLink.objects.get(id=youtube_link_id)
        youtube_link.status = _derive_link_status(youtube_link)
        youtube_link.save(update_fields=['status', 'updated_at'])
        link_status = youtube_link.status
    except YouTubeLink.DoesNotExist:
        link_status = None

    logger.info(f"Cancelled {cancelled_count} tasks for link {youtube_link_id}")
    return {'cancelled': cancelled_count, 'status': link_status}


def clear_link_data_for_refetch(youtube_link_id: str):
    """
    Remove all stored comments, tokens, and task progress for a link before refetching.

    This is used by the "Clear and Refetch" action to give the user a clean slate.
    """
    try:
        youtube_link = YouTubeLink.objects.get(id=youtube_link_id)
    except YouTubeLink.DoesNotExist:
        logger.error(f"YouTubeLink {youtube_link_id} not found for clear/refetch")
        return {'status': 'error', 'message': 'YouTubeLink not found'}

    cancel_result = cancel_tasks_for_link_now(youtube_link_id)

    deleted_comments_count = youtube_link.comments.count()
    youtube_link.comments.all().delete()
    TaskProgress.objects.filter(youtube_link=youtube_link).delete()

    youtube_link.comment_count = 0
    youtube_link.status = 'pending'
    youtube_link.save(update_fields=['comment_count', 'status', 'updated_at'])

    logger.info(
        "Cleared %s comments and reset link %s for refetch",
        deleted_comments_count,
        youtube_link_id,
    )
    return {
        'status': 'reset',
        'deleted_comments': deleted_comments_count,
        'cancelled_tasks': cancel_result.get('cancelled', 0),
    }


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def fetch_comments_task(self, youtube_link_id: str):
    """
    Task to fetch comments from a YouTube video.

    Args:
        youtube_link_id: UUID of the YouTubeLink instance
    """
    try:
        youtube_link = YouTubeLink.objects.get(id=youtube_link_id)
    except YouTubeLink.DoesNotExist:
        logger.error(f"YouTubeLink {youtube_link_id} not found")
        return {'status': 'error', 'message': 'YouTubeLink not found'}

    logger.info(f"Starting comment fetch for video {youtube_link.video_id}")

    # Create task progress record
    task_progress = _bootstrap_task_progress(
        youtube_link,
        'fetching',
        self.request.id,
        'Fetching comments from YouTube'
    )

    try:
        # Update link status
        youtube_link.status = 'fetching'
        youtube_link.save(update_fields=['status', 'updated_at'])

        # Define progress callback
        def on_progress(progress_percent, current_step, total, processed):
            _update_progress(task_progress, progress_percent, current_step, total, processed)

        # Get owner's YouTube API key (falls back to global settings if not set)
        owner_api_key = _get_owner_youtube_api_key(youtube_link.project)

        # Fetch comments (max_results=None auto-detects total comment count)
        comment_data_list = fetch_comments(
            youtube_link.video_id,
            max_results=youtube_link.comment_count or None,
            on_progress=on_progress,
            api_key=owner_api_key
        )

        # Store comments in database
        created_count = 0
        with transaction.atomic():
            for data in comment_data_list:
                Comment.objects.update_or_create(
                    youtube_link=youtube_link,
                    youtube_comment_id=data['youtube_comment_id'],
                    defaults={
                        'author': data.get('author', ''),
                        'author_channel_url': data.get('author_channel_url', ''),
                        'avatar_url': data.get('avatar_url', ''),
                        'text': data.get('text', ''),
                        'like_count': data.get('like_count', 0),
                        'published_at': data.get('published_at'),
                        'updated_at_source': data.get('updated_at'),
                        'is_public': data.get('is_public', True),
                    }
                )
                created_count += 1

        # Update youtube link
        youtube_link.comment_count = created_count
        youtube_link.status = 'completed'
        youtube_link.save(update_fields=['comment_count', 'status', 'updated_at'])

        # Mark task as completed
        task_progress.status = 'completed'
        task_progress.progress_percent = 100
        task_progress.current_step = f"Fetched {created_count} comments"
        task_progress.total_items = created_count
        task_progress.processed_items = created_count
        task_progress.completed_at = timezone.now()
        task_progress.save(update_fields=[
            'status', 'progress_percent', 'current_step',
            'total_items', 'processed_items', 'completed_at'
        ])

        logger.info(f"Fetched {created_count} comments for video {youtube_link.video_id}")

        # Trigger annotation task automatically after fetch completes
        _trigger_annotation(youtube_link_id)

        return {
            'status': 'success',
            'comments_fetched': created_count,
            'youtube_link_id': youtube_link_id
        }

    except Exception as e:
        logger.error(f"Error fetching comments for {youtube_link.video_id}: {e}")
        youtube_link.status = 'failed'
        youtube_link.save(update_fields=['status', 'updated_at'])

        task_progress.status = 'failed'
        task_progress.error_message = str(e)
        task_progress.completed_at = timezone.now()
        task_progress.save(update_fields=['status', 'error_message', 'completed_at'])

        raise self.retry(exc=e) if self.request.retries < self.max_retries else e


@shared_task(bind=True, max_retries=3, default_retry_delay=120)
def annotate_comments_task(self, youtube_link_id: str):
    """
    Task to annotate comments for toxicity using Ollama.
    Handles language detection, translation to Vietnamese, and token-level annotation.

    Args:
        youtube_link_id: UUID of the YouTubeLink instance
    """
    try:
        youtube_link = YouTubeLink.objects.get(id=youtube_link_id)
    except YouTubeLink.DoesNotExist:
        logger.error(f"YouTubeLink {youtube_link_id} not found")
        return {'status': 'error', 'message': 'YouTubeLink not found'}

    logger.info(f"Starting annotation for video {youtube_link.video_id}")

    # Get unannotated comments
    comments = youtube_link.comments.filter(ai_label__isnull=True).exclude(is_meaningful=False)
    total_comments = comments.count()

    if total_comments == 0:
        logger.info(f"No comments to annotate for video {youtube_link.video_id}")
        TaskProgress.objects.create(
            youtube_link=youtube_link,
            task_type='annotating',
            task_id=self.request.id,
            status='completed',
            progress_percent=100,
            current_step='No comments to annotate',
            total_items=0,
            processed_items=0,
            started_at=timezone.now(),
            completed_at=timezone.now()
        )
        youtube_link.status = 'annotated'
        youtube_link.save(update_fields=['status', 'updated_at'])
        return {'status': 'success', 'annotated': 0}

    task_progress = _bootstrap_task_progress(
        youtube_link,
        'annotating',
        self.request.id,
        'Annotating comments with Ollama'
    )
    task_progress.total_items = total_comments
    task_progress.save(update_fields=['total_items'])

    try:
        # Update link status
        youtube_link.status = 'annotating'
        youtube_link.save(update_fields=['status', 'updated_at'])

        # Gather labels_info from project's ProjectLabel entries
        labels_info = _gather_labels_info(youtube_link)
        if not labels_info:
            task_progress.status = 'failed'
            task_progress.error_message = "Chưa thiết lập nhãn cho dự án hiện tại."
            task_progress.completed_at = timezone.now()
            task_progress.save(update_fields=['status', 'error_message', 'completed_at'])
            youtube_link.status = 'pending'
            youtube_link.save(update_fields=['status', 'updated_at'])
            return {'status': 'success', 'annotated': 0}
        # Get owner's Ollama config (falls back to global settings if not set)
        owner_ollama_base_url, owner_ollama_api_key, owner_ollama_model = _get_owner_ollama_config(youtube_link.project)
        if not owner_ollama_base_url or not owner_ollama_api_key or not owner_ollama_model:
            task_progress.status = 'failed'
            task_progress.error_message = "Chưa thiết lập API KEY, OLLAMA URL và OLLAMA MODEL."
            task_progress.completed_at = timezone.now()
            task_progress.save(update_fields=['status', 'error_message', 'completed_at'])
            youtube_link.status = 'pending'
            youtube_link.save(update_fields=['status', 'updated_at'])
            return {'status': 'success', 'annotated': 0}
        annotated_count = 0
        processed_count = 0

        for comment in comments:
            try:
                # Process comment with one Ollama call (pass labels_info for custom labels)
                result = process_comment(
                    comment.text,
                    labels_info=labels_info,
                    ollama_base_url=owner_ollama_base_url,
                    ollama_api_key=owner_ollama_api_key,
                    ollama_model=owner_ollama_model,
                )

                if result and result.get('annotation'):
                    annotation = result['annotation']

                    # Save annotation metadata.
                    comment.annotation_source = 'auto'
                    comment.model_response = annotation
                    comment.annotated_at = timezone.now()
                    comment.is_meaningful = annotation.get('is_meaningful', True)
                    source_is_vietnamese = annotation.get('source_is_vietnamese', True)

                    # Store the Vietnamese text used for labeling and the original
                    original_source_text = comment.text
                    vietnamese_text = result.get('vietnamese_text', comment.text)
                    original_text = result.get('original_text', '')
                    comment.text = vietnamese_text

                    if source_is_vietnamese is False:
                        comment.original_text = original_text or original_source_text
                    else:
                        comment.original_text = ''

                    if comment.is_meaningful is False:
                        comment.manual_label = None
                        comment.toxicity_confidence = None
                        comment.ai_label = None
                        Token.objects.filter(comment=comment).delete()
                        comment.save(update_fields=[
                            'manual_label', 'toxicity_confidence',
                            'ai_label', 'annotation_source', 'model_response', 'annotated_at',
                            'is_meaningful', 'text', 'original_text'
                        ])
                    else:
                        # Apply labels (comment + token level) using helper
                        # This sets comment.ai_label and creates tokens with ai_label
                        _apply_labels_to_comment(comment, annotation, youtube_link, labels_info)

                        # Also save legacy fields
                        comment.toxicity_confidence = annotation.get('confidence', 0.5)
                        comment.save(update_fields=[
                            'toxicity_confidence',
                            'ai_label', 'annotation_source', 'model_response', 'annotated_at',
                            'is_meaningful', 'text', 'original_text'
                        ])

                        annotated_count += 1

                processed_count += 1

                # Update progress
                progress = int((processed_count / max(total_comments, 1)) * 100)
                _update_progress(
                    task_progress, progress,
                    f"Annotated {processed_count}/{total_comments} comments",
                    total_comments, processed_count
                )

            except Exception as e:
                logger.error(f"Error annotating comment {comment.id}: {e}")
                processed_count += 1
                continue

        # Mark task as completed
        task_progress.status = 'completed'
        task_progress.progress_percent = 100
        task_progress.current_step = f"Annotated {annotated_count}/{total_comments} comments"
        task_progress.processed_items = total_comments
        task_progress.total_items = total_comments
        task_progress.completed_at = timezone.now()
        task_progress.save(update_fields=[
            'status', 'progress_percent', 'current_step',
            'processed_items', 'total_items', 'completed_at'
        ])

        # Update youtube link status
        youtube_link.status = 'annotated'
        youtube_link.save(update_fields=['status', 'updated_at'])

        logger.info(
            f"Annotated {annotated_count}/{total_comments} comments for video {youtube_link.video_id}"
        )

        return {
            'status': 'success',
            'annotated': annotated_count,
            'total': total_comments
        }

    except Exception as e:
        logger.error(f"Error annotating comments for {youtube_link.video_id}: {e}")

        task_progress.status = 'failed'
        task_progress.error_message = str(e)
        task_progress.completed_at = timezone.now()
        task_progress.save(update_fields=['status', 'error_message', 'completed_at'])

        raise self.retry(exc=e) if self.request.retries < self.max_retries else e


@shared_task
def cleanup_old_results(days: int = 30):
    """Clean up old task results and progress records."""
    from django.utils import timezone
    from datetime import timedelta

    cutoff_date = timezone.now() - timedelta(days=days)

    # Clean up old completed task progress records
    old_progress = TaskProgress.objects.filter(
        status__in=['completed', 'failed'],
        completed_at__lt=cutoff_date
    )
    count = old_progress.count()
    old_progress.delete()

    logger.info(f"Cleaned up {count} old task progress records")
    return {'cleaned': count}


@shared_task
def cancel_tasks_for_link(youtube_link_id: str):
    """Cancel all running tasks for a YouTube link."""
    return cancel_tasks_for_link_now(youtube_link_id)


@shared_task
def reannotate_all_comments(youtube_link_id: str):
    """
    Reset all annotations and re-run AI annotation from scratch.
    Clears all labels and tokens, then starts fresh annotation.
    """
    try:
        youtube_link = YouTubeLink.objects.get(id=youtube_link_id)
    except YouTubeLink.DoesNotExist:
        logger.error(f"YouTubeLink {youtube_link_id} not found")
        return {'status': 'error', 'message': 'YouTubeLink not found'}

    # Reset all annotations
    youtube_link.comments.update(
        manual_label=None,
        ai_label=None,
        toxicity_confidence=None,
        annotation_source=None,
        model_response=None,
        annotated_at=None,
        original_text='',
        is_meaningful=None,
    )
    Token.objects.filter(comment__youtube_link=youtube_link).delete()

    # Start annotation task
    enqueue_annotation_task(youtube_link, 'Re-annotating all comments')
    logger.info(f"Re-annotation started for link {youtube_link_id}")
    return {'status': 'started', 'youtube_link_id': youtube_link_id}
