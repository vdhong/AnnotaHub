"""
RESTful API views for AnnotaHub.
Provides JSON endpoints for all operations.
"""
import json
import logging
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.views import View
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.utils import timezone
from .models import Project, YouTubeLink, Comment, Token, TaskProgress, Label, ProjectLabel
from .services.youtube_service import extract_video_id, get_video_info
from .services.ollama_service import annotate_comment, create_token_annotations
from .tasks import (
    fetch_comments_task,
    annotate_comments_task,
    cancel_tasks_for_link_now,
    enqueue_fetch_comments_task,
    enqueue_annotation_task,
    get_effective_task_progress,
    clear_link_data_for_refetch,
)
from .export_service import generate_export

logger = logging.getLogger(__name__)


@method_decorator([require_http_methods(["GET"])], name='dispatch')
class ProjectListView(View):
    """List all projects (GET) or create a new one (POST)."""
    def get(self, request):
        projects = Project.objects.all().annotate(
            link_count=Count('youtubelinks', distinct=True),
            comment_count=Count('youtubelinks__comments', distinct=True),
        )
        data = [{
            'id': str(p.id),
            'name': p.name,
            'description': p.description,
            'link_count': p.link_count,
            'comment_count': p.comment_count,
            'created_at': p.created_at.isoformat(),
        } for p in projects]
        return JsonResponse({'projects': data})


@method_decorator([csrf_exempt, require_http_methods(["POST"])], name='dispatch')
class ProjectCreateView(View):
    """Create a new project."""
    def post(self, request):
        try:
            data = json.loads(request.body)
            name = data.get('name', '').strip()
            description = data.get('description', '').strip()

            if not name:
                return JsonResponse({'error': 'Project name is required'}, status=400)

            project = Project.objects.create(name=name, description=description)
            return JsonResponse({
                'id': str(project.id),
                'name': project.name,
                'description': project.description,
            }, status=201)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)


@method_decorator([csrf_exempt, require_http_methods(["GET", "PUT", "DELETE"])], name='dispatch')
class ProjectDetailView(View):
    """Get, update, or delete a project."""
    def get(self, request, project_id):
        project = get_object_or_404(Project, id=project_id)
        links = YouTubeLink.objects.filter(project=project)
        data = {
            'id': str(project.id),
            'name': project.name,
            'description': project.description,
            'links': [{
                'id': str(l.id),
                'video_id': l.video_id,
                'url': l.url,
                'title': l.title,
                'channel': l.channel,
                'thumbnail': l.thumbnail,
                'status': l.status,
                'comment_count': l.comment_count,
            } for l in links],
        }
        return JsonResponse(data)

    def put(self, request, project_id):
        project = get_object_or_404(Project, id=project_id)
        try:
            data = json.loads(request.body)
            if 'name' in data:
                project.name = data['name']
            if 'description' in data:
                project.description = data['description']
            project.save()
            return JsonResponse({'id': str(project.id), 'name': project.name})
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

    def delete(self, request, project_id):
        project = get_object_or_404(Project, id=project_id)
        for link in project.youtubelinks.all():
            cancel_tasks_for_link_now(str(link.id))
        project.delete()
        return JsonResponse({'message': 'Project deleted'}, status=200)


@method_decorator([csrf_exempt, require_http_methods(["POST", "GET"])], name='dispatch')
class LinkManageView(View):
    """Add a YouTube link to a project or list links."""
    def get(self, request, project_id):
        project = get_object_or_404(Project, id=project_id)
        links = YouTubeLink.objects.filter(project=project)
        data = [{
            'id': str(l.id),
            'video_id': l.video_id,
            'url': l.url,
            'title': l.title,
            'status': l.status,
            'comment_count': l.comment_count,
        } for l in links]
        return JsonResponse({'links': data})

    def post(self, request, project_id):
        project = get_object_or_404(Project, id=project_id)
        try:
            data = json.loads(request.body)
            url = data.get('url', '').strip()

            if not url:
                return JsonResponse({'error': 'URL is required'}, status=400)

            video_id = extract_video_id(url)
            if not video_id:
                return JsonResponse({'error': 'Invalid YouTube URL'}, status=400)

            if YouTubeLink.objects.filter(project=project, video_id=video_id).exists():
                return JsonResponse({'error': 'This video already exists in the project'}, status=400)

            video_info = get_video_info(video_id) or {}
            link = YouTubeLink.objects.create(
                project=project,
                video_id=video_id,
                url=url,
                title=video_info.get('title', ''),
                channel=video_info.get('channel', ''),
                thumbnail=video_info.get('thumbnail', ''),
                comment_count = video_info.get('comment_count', 0),
                view_count = video_info.get('view_count', 0),
                like_count = video_info.get('like_count', 0),
            )

            enqueue_fetch_comments_task(link, 'Starting comment fetch')
            return JsonResponse({
                'id': str(link.id),
                'message': 'Comment fetching started',
                'status': 'fetching',
            }, status=201)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)


@method_decorator([require_http_methods(["GET"])], name='dispatch')
class LinkStatusView(View):
    """Get status and progress for a YouTube link."""
    def get(self, request, link_id):
        link = get_object_or_404(YouTubeLink, id=link_id)
        total_comments = link.comments.count()
        annotated_comments = link.comments.filter(toxicity_label__isnull=False).count()
        toxic_comments = link.comments.filter(toxicity_label='toxic').count()
        unannotated_count = link.comments.filter(toxicity_label__isnull=True).exclude(is_meaningful=False).count()
        skipped_count = link.comments.filter(is_meaningful=False).count()

        tasks_data = []
        for task in (
            get_effective_task_progress(str(link.id), 'fetching'),
            get_effective_task_progress(str(link.id), 'annotating'),
        ):
            if not task:
                continue
            tasks_data.append({
                'type': task.task_type,
                'status': task.status,
                'status_display': task.get_status_display(),
                'progress': task.progress_percent,
                'step': task.current_step,
                'total': task.total_items,
                'processed': task.processed_items,
            })

        return JsonResponse({
            'id': str(link.id),
            'video_id': link.video_id,
            'title': link.title,
            'status': link.status,
            'comment_count': total_comments,
            'stats': {
                'total_comments': total_comments,
                'annotated_comments': annotated_comments,
                'toxic_comments': toxic_comments,
                'unannotated_count': unannotated_count,
                'skipped_count': skipped_count,
            },
            'tasks': tasks_data,
        })


@method_decorator([csrf_exempt, require_http_methods(["GET", "DELETE"])], name='dispatch')
class LinkCommentsView(View):
    """List comments for a YouTube link or delete the link."""
    def get(self, request, link_id):
        link = get_object_or_404(YouTubeLink, id=link_id)
        filter_status = request.GET.get('filter', 'all')
        page = int(request.GET.get('page', 1))
        per_page = int(request.GET.get('per_page', 50))

        comments_query = link.comments.prefetch_related(
            'tokens', 'tokens__ai_label', 'tokens__ai_label__label',
            'tokens__manual_label', 'tokens__manual_label__label',
            'ai_label', 'ai_label__label', 'manual_label', 'manual_label__label'
        )
        if filter_status == 'annotated':
            comments_query = comments_query.filter(toxicity_label__isnull=False)
        elif filter_status == 'unannotated':
            comments_query = comments_query.filter(toxicity_label__isnull=True).exclude(is_meaningful=False)
        elif filter_status == 'toxic':
            comments_query = comments_query.filter(toxicity_label='toxic')

        total = comments_query.count()
        start = (page - 1) * per_page
        comments = comments_query[start:start + per_page]

        data = [{
            'id': str(c.id),
            'text': c.text,
            'author': c.author,
            'label': c.toxicity_label,
            'is_meaningful': c.is_meaningful,
            'confidence': c.toxicity_confidence,
            'tokens': c.display_tokens,
        } for c in comments]

        return JsonResponse({
            'comments': data,
            'total': total,
            'page': page,
            'per_page': per_page,
        })


@method_decorator([csrf_exempt, require_http_methods(["GET", "POST"])], name='dispatch')
class LinkExportView(View):
    """Export data for a YouTube link."""
    def post(self, request, link_id):
        link = get_object_or_404(YouTubeLink, id=link_id)
        try:
            data = json.loads(request.body)
            export_format = data.get('format', 'json_sentence')
            filter_toxicity = data.get('filter', 'all')
            return generate_export(link.project, link, export_format, filter_toxicity)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)


@method_decorator([require_http_methods(["GET"])], name='dispatch')
class CommentTokensView(View):
    """Get tokens for a comment."""
    def get(self, request, comment_id):
        comment = get_object_or_404(Comment, id=comment_id)
        tokens = comment.display_tokens

        return JsonResponse({
            'comment': {
                'id': str(comment.id),
                'text': comment.text,
                'label': comment.toxicity_label,
                'is_meaningful': comment.is_meaningful,
            },
            'tokens': tokens,
        })


@method_decorator([csrf_exempt, require_http_methods(["POST"])], name='dispatch')
class ToggleTokenView(View):
    """
    Toggle token label between toxic-like and O (neutral).
    Sets manual_label on the token (overrides ai_label for display).
    """
    def post(self, request, comment_id, token_position):
        comment = get_object_or_404(Comment, id=comment_id)
        token = comment.get_or_create_token_for_position(token_position)
        if token is None:
            return JsonResponse({'success': False, 'error': 'Token not found'}, status=404)

        project = comment.youtube_link.project
        currently_toxic = token.is_toxic

        if currently_toxic:
            o_label = None
            for pl in ProjectLabel.objects.filter(project=project).select_related('label'):
                if pl.label.name.upper() == 'O' or pl.label.name.lower() == 'non_toxic':
                    o_label = pl
                    break
            token.manual_label = o_label
        else:
            toxic_label = None
            for pl in ProjectLabel.objects.filter(project=project).select_related('label'):
                if pl.label.name.lower() in ('toxic', 'offensive', 'abusive', 'hate'):
                    toxic_label = pl
                    break
            token.manual_label = toxic_label

        token.annotation_source = 'manual'
        token.save(update_fields=['manual_label', 'annotation_source'])

        comment.update_comment_label()
        comment.is_meaningful = True
        comment.annotated_at = timezone.now()
        if comment.annotation_source == 'auto':
            comment.annotation_source = 'mixed'
        elif comment.annotation_source is None:
            comment.annotation_source = 'manual'
        comment.save(update_fields=['annotation_source', 'is_meaningful', 'annotated_at'])

        return JsonResponse({
            'success': True,
            'token_text': token.text,
            'is_toxic': token.is_toxic,
            'comment_label': comment.toxicity_label,
            'effective_label': token.effective_label_data,
            'ai_label': token.ai_label_data,
            'manual_label': token.manual_label_data,
        })


@method_decorator([csrf_exempt, require_http_methods(["POST"])], name='dispatch')
class ManualLabelView(View):
    """Manually set the toxicity label for a comment via API."""
    def post(self, request, comment_id):
        from django.utils import timezone
        comment = get_object_or_404(Comment, id=comment_id)
        label = request.POST.get('label', '').strip()

        if label in ('toxic', 'non_toxic'):
            comment.toxicity_label = label
            comment.annotation_source = 'manual'
            comment.annotated_at = timezone.now()
            comment.is_meaningful = True
            comment.save(update_fields=['toxicity_label', 'annotation_source', 'annotated_at', 'is_meaningful'])

            return JsonResponse({
                'success': True,
                'label': comment.toxicity_label,
            })

        return JsonResponse({'success': False, 'error': 'Invalid label'}, status=400)


@method_decorator([csrf_exempt, require_http_methods(["POST"])], name='dispatch')
class StopFetchTaskView(View):
    """Stop the fetch comments task (API)."""
    def post(self, request, link_id):
        link = get_object_or_404(YouTubeLink, id=link_id)
        cancel_tasks_for_link_now(str(link.id))
        return JsonResponse({
            'success': True,
            'message': 'Fetch task stop requested.',
        })


@method_decorator([csrf_exempt, require_http_methods(["POST"])], name='dispatch')
class StopAnnotationTaskView(View):
    """Stop the AI annotation task (API)."""
    def post(self, request, link_id):
        link = get_object_or_404(YouTubeLink, id=link_id)
        cancel_tasks_for_link_now(str(link.id))
        return JsonResponse({
            'success': True,
            'message': 'Annotation task stop requested.',
        })


@method_decorator([csrf_exempt, require_http_methods(["POST"])], name='dispatch')
class RetryFetchView(View):
    """Refetch comments without deleting existing stored comments (API)."""
    def post(self, request, link_id):
        link = get_object_or_404(YouTubeLink, id=link_id)
        cancel_tasks_for_link_now(str(link.id))
        link.status = 'pending'
        link.save(update_fields=['status', 'updated_at'])
        enqueue_fetch_comments_task(link, 'Refetching comments without clearing existing data')
        return JsonResponse({
            'success': True,
            'message': f'Refetching comments for "{link.title or link.video_id}". Existing comments will be kept.',
        })


@method_decorator([csrf_exempt, require_http_methods(["POST"])], name='dispatch')
class ClearAndRefetchView(View):
    """Clear existing comments and refetch comments from YouTube (API)."""
    def post(self, request, link_id):
        link = get_object_or_404(YouTubeLink, id=link_id)
        clear_result = clear_link_data_for_refetch(str(link.id))
        if clear_result.get('status') == 'error':
            return JsonResponse({
                'success': False,
                'message': clear_result.get('message', 'Failed to clear link data.'),
            }, status=500)

        enqueue_fetch_comments_task(link, 'Clearing old comments and refetching')
        return JsonResponse({
            'success': True,
            'message': (
                f'Cleared {clear_result.get("deleted_comments", 0)} comments and started refetching '
                f'for "{link.title or link.video_id}".'
            ),
            'cleared_comments': clear_result.get('deleted_comments', 0),
        })


@method_decorator([csrf_exempt, require_http_methods(["POST"])], name='dispatch')
class ContinueAnnotationView(View):
    """Continue annotation for unannotated comments (API)."""
    def post(self, request, link_id):
        link = get_object_or_404(YouTubeLink, id=link_id)
        unannotated = link.comments.filter(toxicity_label__isnull=True).exclude(is_meaningful=False).count()
        if unannotated == 0:
            return JsonResponse({
                'success': False,
                'message': 'No unannotated comments found.',
            }, status=400)

        running_task = get_effective_task_progress(str(link.id), 'annotating')
        if running_task and running_task.status == 'running':
            return JsonResponse({
                'success': True,
                'already_running': True,
                'message': 'Annotation task is already running. Progress updated.',
                'task': {
                    'type': running_task.task_type,
                    'status': running_task.status,
                    'status_display': running_task.get_status_display(),
                    'progress': running_task.progress_percent,
                    'step': running_task.current_step,
                    'total': running_task.total_items,
                    'processed': running_task.processed_items,
                }
            })

        enqueue_annotation_task(link, 'Continuing annotation')
        return JsonResponse({
            'success': True,
            'message': f'Continuing annotation for {unannotated} unannotated comments.',
        })


@method_decorator([csrf_exempt, require_http_methods(["POST"])], name='dispatch')
class ReannotateLinkView(View):
    """Re-run annotation for all comments in a link (API)."""
    def post(self, request, link_id):
        link = get_object_or_404(YouTubeLink, id=link_id)
        cancel_tasks_for_link_now(str(link.id))
        link.comments.update(
            toxicity_label=None,
            toxicity_confidence=None,
            annotation_source=None,
            model_response=None,
            annotated_at=None,
            original_text='',
            is_meaningful=None,
        )
        Token.objects.filter(comment__youtube_link=link).delete()
        enqueue_annotation_task(link, 'Re-annotating all comments')
        return JsonResponse({
            'success': True,
            'message': 'Re-annotation started. All labels have been reset.',
        })


# ==============================
# Label Management API Views
# ==============================

@method_decorator([require_http_methods(["GET"])], name='dispatch')
class LabelListView(View):
    """List all public labels."""
    def get(self, request):
        labels = Label.objects.all().order_by('name')
        data = [{
            'id': str(l.id),
            'name': l.name,
            'description': l.description,
            'color': l.color,
            'is_active': l.is_active,
            'assignment_count': l.assignment_count,
        } for l in labels]
        return JsonResponse({'labels': data})


@method_decorator([csrf_exempt, require_http_methods(["POST"])], name='dispatch')
class LabelCreateView(View):
    """Create a new public label."""
    def post(self, request):
        try:
            data = json.loads(request.body)
            name = data.get('name', '').strip()
            description = data.get('description', '').strip()
            color = data.get('color', '#FF0000').strip()

            if not name:
                return JsonResponse({'error': 'Label name is required'}, status=400)

            if Label.objects.filter(name=name).exists():
                return JsonResponse({'error': 'A label with this name already exists'}, status=400)

            label = Label.objects.create(name=name, description=description, color=color)
            return JsonResponse({
                'id': str(label.id),
                'name': label.name,
                'description': label.description,
                'color': label.color,
            }, status=201)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)


@method_decorator([csrf_exempt, require_http_methods(["GET", "POST"])], name='dispatch')
class ProjectLabelsView(View):
    """
    List labels for a project (GET) or manage project labels (POST).
    POST actions: add_label, remove_label, update_override, add_custom_label
    """
    def get(self, request, project_id):
        project = get_object_or_404(Project, id=project_id)
        project_labels = ProjectLabel.objects.filter(project=project).select_related('label')
        data = [{
            'id': str(pl.id),
            'label_id': str(pl.label.id),
            'label_name': pl.label.name,
            'label_color': pl.label.color,
            'display_name': pl.display_name,
            'display_description': pl.display_description,
            'display_color': pl.display_color,
            'override_name': pl.override_name,
            'override_description': pl.override_description,
            'override_color': pl.override_color,
        } for pl in project_labels]
        return JsonResponse({'project_labels': data})

    def post(self, request, project_id):
        project = get_object_or_404(Project, id=project_id)
        try:
            data = json.loads(request.body)
            action = data.get('action', '')

            if action == 'add_label':
                label_id = data.get('label_id')
                if not label_id:
                    return JsonResponse({'error': 'label_id is required'}, status=400)
                label = get_object_or_404(Label, id=label_id)
                pl, created = ProjectLabel.objects.get_or_create(project=project, label=label)
                return JsonResponse({
                    'success': True,
                    'created': created,
                    'message': f'Label "{label.name}" {"added" if created else "already in"} project.',
                })

            elif action == 'remove_label':
                project_label_id = data.get('project_label_id')
                if not project_label_id:
                    return JsonResponse({'error': 'project_label_id is required'}, status=400)
                pl = get_object_or_404(ProjectLabel, id=project_label_id, project=project)
                pl.delete()
                return JsonResponse({'success': True, 'message': 'Label removed from project.'})

            elif action == 'update_override':
                project_label_id = data.get('project_label_id')
                if not project_label_id:
                    return JsonResponse({'error': 'project_label_id is required'}, status=400)
                pl = get_object_or_404(ProjectLabel, id=project_label_id, project=project)
                if 'override_name' in data:
                    pl.override_name = data['override_name'] or None
                if 'override_description' in data:
                    pl.override_description = data['override_description'] or None
                if 'override_color' in data:
                    pl.override_color = data['override_color'] or None
                pl.save()
                return JsonResponse({'success': True, 'message': 'Overrides updated.'})

            elif action == 'add_custom_label':
                name = data.get('custom_name', '').strip()
                description = data.get('custom_description', '').strip()
                color = data.get('custom_color', '#FF0000').strip()
                if not name:
                    return JsonResponse({'error': 'custom_name is required'}, status=400)
                label, created = Label.objects.get_or_create(
                    name=name,
                    defaults={'description': description, 'color': color}
                )
                if created:
                    label.description = description
                    label.color = color
                    label.save()
                ProjectLabel.objects.get_or_create(project=project, label=label)
                return JsonResponse({'success': True, 'message': f'Custom label "{name}" added.'})

            else:
                return JsonResponse({'error': 'Invalid action'}, status=400)

        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
