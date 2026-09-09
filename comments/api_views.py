"""
REST API cho AnnotaHub: xây trên Django REST Framework.

Về bảo mật: mặc định của DRF trong settings là IsAuthenticated. Mọi view ở đây
đều đi qua helper trong comments.permissions để kiểm tra quyền trên dự án
tương ứng. Không view nào được truy vấn trực tiếp bằng ID mà bỏ qua bước này.
"""
from __future__ import annotations

import logging

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, Exists, Min, OuterRef, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import task_messages
from .export_service import generate_export
from .models import (
    AnnotationAssignment,
    Comment,
    CommentAnnotation,
    DatasetVersion,
    ExportRecord,
    Label,
    Project,
    ProjectLabel,
    Token,
    YouTubeLink,
)
from .permissions import (
    get_comment_or_404,
    get_link_or_404,
    get_project_or_404,
    is_owner_level,
    role_for,
    visible_projects,
)
from .services import agreement as agreement_service
from .services import ai_review as ai_review_service
from .services import annotation as annotation_service
from .services.stats import label_stats_for_link, link_counters
from .services.youtube_service import extract_video_id, get_video_info
from .tasks import (
    cancel_tasks_for_link_now,
    clear_link_data_for_refetch,
    enqueue_annotation_task,
    enqueue_fetch_comments_task,
    get_effective_task_progress,
)

logger = logging.getLogger(__name__)


class BaseAPIView(APIView):
    """View cơ sở: bắt buộc đăng nhập cho mọi endpoint."""

    permission_classes = [IsAuthenticated]


def _resolve_project_label(project, label_id):
    """Lấy ProjectLabel thuộc đúng dự án; None nếu label_id rỗng."""
    if not label_id:
        return None
    return ProjectLabel.objects.filter(id=label_id, project=project).first()


def _task_payload(task):
    return {
        'type': task.task_type,
        'status': task.status,
        'status_display': task.get_status_display(),
        'progress': task.progress_percent,
        'step': task_messages.render(
            task.current_step, task.step_params,
            processed=task.processed_items, total=task.total_items,
        ),
        'total': task.total_items,
        'processed': task.processed_items,
        'error_message': task_messages.render(task.error_message),
    }


# ---------------------------------------------------------------------------
# Dự án
# ---------------------------------------------------------------------------
class ProjectListView(BaseAPIView):
    """Danh sách dự án mà người dùng hiện tại có quyền xem."""

    def get(self, request):
        projects = visible_projects(request.user).select_related('owner').annotate(
            link_count=Count('youtubelinks', distinct=True),
            comment_count=Count('youtubelinks__comments', distinct=True),
        )
        data = [{
            'id': str(p.id),
            'name': p.name,
            'description': p.description,
            'link_count': p.link_count,
            'comment_count': p.comment_count,
            'is_locked': p.is_locked,
            # role phân biệt 'owner' (chủ thật) với 'admin' (superuser trên dự
            # án của người khác). is_owner chỉ đúng khi thực sự sở hữu.
            'role': role_for(p, request.user),
            'is_owner': p.owner_id == request.user.id,
            'owner': p.owner.username,
            'created_at': p.created_at.isoformat(),
        } for p in projects]
        return Response({'projects': data})


class ProjectCreateView(BaseAPIView):
    def post(self, request):
        name = (request.data.get('name') or '').strip()
        description = (request.data.get('description') or '').strip()

        if not name:
            return Response({'error': _('Tên dự án là bắt buộc.')},
                            status=status.HTTP_400_BAD_REQUEST)
        if Project.objects.filter(name=name).exists():
            return Response({'error': _('Tên dự án đã tồn tại.')},
                            status=status.HTTP_400_BAD_REQUEST)

        # owner là NOT NULL; thiếu nó thì create() ném IntegrityError thành 500.
        project = Project.objects.create(
            name=name, description=description, owner=request.user
        )
        return Response({
            'id': str(project.id),
            'name': project.name,
            'description': project.description,
        }, status=status.HTTP_201_CREATED)


class ProjectDetailView(BaseAPIView):
    def get(self, request, project_id):
        project = get_project_or_404(request.user, project_id)
        links = YouTubeLink.objects.filter(project=project)
        return Response({
            'id': str(project.id),
            'name': project.name,
            'description': project.description,
            'is_locked': project.is_locked,
            'guideline': project.guideline,
            'annotators_per_comment': project.annotators_per_comment,
            'role': role_for(project, request.user),
            'is_owner': project.owner_id == request.user.id,
            'owner': project.owner.username,
            'links': [{
                'id': str(link.id),
                'kind': link.kind,
                'video_id': link.video_id,
                'url': link.url,
                'title': link.title,
                'channel': link.channel,
                'thumbnail': link.thumbnail,
                'status': link.status,
                'comment_count': link.comment_count,
            } for link in links],
        })

    def put(self, request, project_id):
        project = get_project_or_404(request.user, project_id, require_owner=True,
                                     require_unlocked=True)
        if 'name' in request.data:
            name = (request.data.get('name') or '').strip()
            if not name:
                return Response({'error': _('Tên dự án là bắt buộc.')},
                                status=status.HTTP_400_BAD_REQUEST)
            if Project.objects.filter(name=name).exclude(pk=project.pk).exists():
                return Response({'error': _('Tên dự án đã tồn tại.')},
                                status=status.HTTP_400_BAD_REQUEST)
            project.name = name
        if 'description' in request.data:
            project.description = request.data['description']
        if 'guideline' in request.data:
            project.guideline = request.data['guideline']
        if 'annotators_per_comment' in request.data:
            try:
                project.annotators_per_comment = max(
                    1, min(10, int(request.data['annotators_per_comment']))
                )
            except (TypeError, ValueError):
                return Response({'error': _('annotators_per_comment không hợp lệ.')},
                                status=status.HTTP_400_BAD_REQUEST)
        project.save()
        return Response({'id': str(project.id), 'name': project.name})

    def delete(self, request, project_id):
        project = get_project_or_404(request.user, project_id, require_owner=True)
        for link in project.youtubelinks.all():
            cancel_tasks_for_link_now(str(link.id))
        project.delete()
        return Response({'message': _('Đã xoá dự án.')})


# ---------------------------------------------------------------------------
# Link / nguồn dữ liệu
# ---------------------------------------------------------------------------
class LinkManageView(BaseAPIView):
    throttle_scope = 'heavy'

    def get(self, request, project_id):
        project = get_project_or_404(request.user, project_id)
        links = YouTubeLink.objects.filter(project=project)
        return Response({'links': [{
            'id': str(link.id),
            'kind': link.kind,
            'video_id': link.video_id,
            'url': link.url,
            'title': link.title,
            'status': link.status,
            'comment_count': link.comment_count,
        } for link in links]})

    def post(self, request, project_id):
        project = get_project_or_404(request.user, project_id, require_owner=True,
                                     require_unlocked=True)
        url = (request.data.get('url') or '').strip()
        if not url:
            return Response({'error': _('URL là bắt buộc.')},
                            status=status.HTTP_400_BAD_REQUEST)

        video_id = extract_video_id(url)
        if not video_id:
            return Response({'error': _('URL YouTube không hợp lệ.')},
                            status=status.HTTP_400_BAD_REQUEST)
        if YouTubeLink.objects.filter(project=project, video_id=video_id).exists():
            return Response({'error': _('Video này đã có trong dự án.')},
                            status=status.HTTP_400_BAD_REQUEST)

        from .tasks import get_owner_youtube_api_key
        try:
            video_info = get_video_info(
                video_id, api_key=get_owner_youtube_api_key(project)
            ) or {}
        except Exception as exc:
            logger.warning('Không lấy được thông tin video %s: %s', video_id, exc)
            return Response(
                {'error': _('Không lấy được thông tin video. Kiểm tra link hoặc YouTube API key.')},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        link = YouTubeLink.objects.create(
            project=project,
            kind='youtube',
            video_id=video_id,
            url=url,
            title=video_info.get('title', ''),
            channel=video_info.get('channel', ''),
            thumbnail=video_info.get('thumbnail', ''),
            comment_count=video_info.get('comment_count', 0),
            view_count=video_info.get('view_count', 0),
            like_count=video_info.get('like_count', 0),
        )
        enqueue_fetch_comments_task(link, 'Starting comment fetch')
        return Response({
            'id': str(link.id),
            'message': _('Đã bắt đầu tải bình luận.'),
            'status': 'fetching',
        }, status=status.HTTP_201_CREATED)


class LinkStatusView(BaseAPIView):
    def get(self, request, link_id):
        link = get_link_or_404(request.user, link_id)
        project_labels = ProjectLabel.objects.filter(
            project=link.project
        ).select_related('label')

        # Số liệu theo góc nhìn của chính người đang xem: màn hình gán nhãn
        # poll endpoint này, nếu trả số của cả nhóm thì các thẻ thống kê vừa
        # render đúng đã bị ghi đè lại thành thành quả của người khác.
        counters = link_counters(link, user=request.user)
        tasks_data = [
            _task_payload(task)
            for task in (
                get_effective_task_progress(str(link.id), 'fetching'),
                get_effective_task_progress(str(link.id), 'annotating'),
                get_effective_task_progress(str(link.id), 'importing'),
            )
            if task
        ]

        return Response({
            'id': str(link.id),
            'video_id': link.video_id,
            'title': link.title,
            'status': link.status,
            'comment_count': counters['total_comments'],
            'stats': {
                'total_comments': counters['total_comments'],
                'label_stats': label_stats_for_link(project_labels, link,
                                                    user=request.user),
                'unannotated_count': counters['unannotated_count'],
                'manual_pending_count': counters['manual_pending_count'],
                'skipped_count': counters['skipped_count'],
                'annotated_comments': counters['annotated_comments'],
            },
            'tasks': tasks_data,
        })


class LinkCommentsView(BaseAPIView):
    def get(self, request, link_id):
        link = get_link_or_404(request.user, link_id)
        filter_status = request.query_params.get('filter', 'all')

        try:
            page = max(1, int(request.query_params.get('page', 1)))
            per_page = max(1, min(200, int(request.query_params.get('per_page', 50))))
        except (TypeError, ValueError):
            return Response({'error': _('page/per_page phải là số nguyên.')},
                            status=status.HTTP_400_BAD_REQUEST)

        queryset = link.comments.select_related(
            'ai_label__label', 'manual_label__label', 'gold_label__label'
        ).prefetch_related('tokens')

        # "đã/chưa gán" và "theo nhãn" tính theo annotation của chính người gọi.
        mine = Q(annotations__annotator=request.user, annotations__source='manual')

        if filter_status == 'annotated':
            queryset = queryset.filter(mine)
        elif filter_status == 'unannotated':
            queryset = queryset.exclude(mine).exclude(is_meaningful=False)
        elif filter_status == 'conflict':
            queryset = queryset.filter(review_status='conflict')
        elif filter_status == 'skipped':
            queryset = queryset.filter(is_meaningful=False)
        elif filter_status.startswith('label_'):
            label_id = filter_status.split('_', 1)[1]
            queryset = queryset.filter(
                annotations__annotator=request.user,
                annotations__source='manual',
                annotations__project_label_id=label_id,
            )
        queryset = queryset.distinct()

        total = queryset.count()
        start = (page - 1) * per_page
        comments = annotation_service.attach_my_annotations(
            queryset[start:start + per_page], request.user
        )

        return Response({
            'comments': [{
                'id': str(c.id),
                'text': c.text,
                'source_text': c.source_text,
                'author': c.author,
                'label': c.toxicity_label,
                'review_status': c.review_status,
                'manual_annotation_count': c.manual_annotation_count,
                'is_meaningful': c.is_meaningful,
                'confidence': c.toxicity_confidence,
                'my_label': c.my_label,
                'tokens': c.my_tokens,
            } for c in comments],
            'total': total,
            'page': page,
            'per_page': per_page,
        })


class LinkExportView(BaseAPIView):
    throttle_scope = 'export'

    def post(self, request, link_id):
        link = get_link_or_404(request.user, link_id)
        return generate_export(
            link.project, link,
            request.data.get('format', 'json_sentence'),
            request.data.get('filter', 'all'),
            requested_by=request.user,
            review_filter=request.data.get('review', 'all'),
        )


# ---------------------------------------------------------------------------
# Gán nhãn
# ---------------------------------------------------------------------------
class CommentTokensView(BaseAPIView):
    def get(self, request, comment_id):
        comment = get_comment_or_404(request.user, comment_id)
        return Response({
            'comment': {
                'id': str(comment.id),
                'text': comment.text,
                'label': comment.toxicity_label,
                'review_status': comment.review_status,
                'is_meaningful': comment.is_meaningful,
                'my_label': _my_label_data(comment, request.user),
            },
            'tokens': annotation_service.tokens_for_user(
                comment,
                annotation_service.my_token_label_map(
                    [comment.id], request.user
                ).get(comment.id),
            ),
        })


class SetCommentLabelView(BaseAPIView):
    throttle_scope = 'annotation'

    def post(self, request, comment_id):
        comment = get_comment_or_404(request.user, comment_id, require_unlocked=True)
        project = comment.youtube_link.project

        # remove=true gỡ bỏ hẳn quyết định của người dùng (câu trở lại "chưa
        # gán"), khác với label_id=null nghĩa là "đã xét, kết luận không nhãn".
        if str(request.data.get('remove', '')).lower() in ('1', 'true', 'yes'):
            result = annotation_service.remove_comment_label(comment, request.user)
            comment.refresh_from_db()
            return Response({
                'success': True,
                'removed': True,
                'label': comment.toxicity_label,
                'effective_label': comment.effective_label_data,
                'my_label': None,
                'review_status': result['review_status'],
                'manual_count': result['manual_count'],
                'progress': _my_progress(request.user, comment.youtube_link),
            })

        label_id = request.data.get('label_id')
        if not label_id and request.data.get('label_ids'):
            label_id = request.data['label_ids'][0]
        project_label = _resolve_project_label(project, label_id)
        if label_id and project_label is None:
            return Response({'success': False, 'error': _('Nhãn không thuộc dự án này.')},
                            status=status.HTTP_400_BAD_REQUEST)

        result = annotation_service.set_comment_label(
            comment, request.user, project_label,
            note=request.data.get('note', '') or '',
            time_spent_ms=int(request.data.get('time_spent_ms') or 0),
        )
        comment.refresh_from_db()

        return Response({
            'success': True,
            'label': comment.toxicity_label,
            'effective_label': comment.effective_label_data,
            'ai_label': comment.ai_label_data,
            'manual_label': comment.manual_label_data,
            # Nhãn do chính người dùng này gán, để giao diện hiển thị lại đúng
            # khi quay về câu đã làm.
            'my_label': _my_label_data(comment, request.user),
            'review_status': result['review_status'],
            'manual_count': result['manual_count'],
            'required_annotators': result['required'],
            'progress': _my_progress(request.user, comment.youtube_link),
        })


class SetTokenLabelView(BaseAPIView):
    throttle_scope = 'annotation'

    def post(self, request, comment_id, token_position):
        comment = get_comment_or_404(request.user, comment_id, require_unlocked=True)
        project = comment.youtube_link.project

        label_id = request.data.get('label_id')
        if not label_id and request.data.get('label_ids'):
            label_id = request.data['label_ids'][0]
        project_label = _resolve_project_label(project, label_id)
        if label_id and project_label is None:
            return Response({'success': False, 'error': _('Nhãn không thuộc dự án này.')},
                            status=status.HTTP_400_BAD_REQUEST)

        token = annotation_service.set_token_label(
            comment, request.user, token_position, project_label
        )
        if token is None:
            return Response({'success': False, 'error': _('Không tìm thấy token.')},
                            status=status.HTTP_404_NOT_FOUND)

        comment.refresh_from_db()
        return Response({
            'success': True,
            'token_text': token.text,
            'is_toxic': token.is_toxic,
            'comment_label': comment.toxicity_label,
            'effective_label': token.effective_label_data,
            'ai_label': token.ai_label_data,
            'manual_label': token.manual_label_data,
        })


class SetTokenSpanLabelView(BaseAPIView):
    """Gán một nhãn cho cả dải token liên tiếp (kéo chọn cụm từ)."""

    throttle_scope = 'annotation'

    def post(self, request, comment_id):
        comment = get_comment_or_404(request.user, comment_id, require_unlocked=True)
        project = comment.youtube_link.project

        try:
            start_position = int(request.data['start'])
            end_position = int(request.data['end'])
        except (KeyError, TypeError, ValueError):
            return Response({'success': False, 'error': _('Cần có start và end là số nguyên.')},
                            status=status.HTTP_400_BAD_REQUEST)

        project_label = _resolve_project_label(project, request.data.get('label_id'))
        if request.data.get('label_id') and project_label is None:
            return Response({'success': False, 'error': _('Nhãn không thuộc dự án này.')},
                            status=status.HTTP_400_BAD_REQUEST)

        tokens = annotation_service.set_token_span_label(
            comment, request.user, start_position, end_position, project_label
        )
        return Response({
            'success': True,
            'updated': len(tokens),
            'tokens': [{
                'position': t.position,
                'text': t.text,
                'effective_label': t.effective_label_data,
            } for t in tokens],
        })


class AcceptAiSuggestionView(BaseAPIView):
    """
    "Đồng ý với AI": chép đề xuất của AI thành nhãn của chính người dùng.

    Từ lúc này comment coi như do người đó gán: sửa token nào thì chỉ token đó
    đổi, những chỗ không đụng tới là họ đã đồng ý với AI.
    """

    throttle_scope = 'annotation'

    def post(self, request, comment_id):
        comment = get_comment_or_404(request.user, comment_id, require_unlocked=True)

        result = ai_review_service.accept_ai_suggestion(comment, request.user)
        if not result.get('accepted'):
            return Response(
                {'success': False, 'error': _('Bình luận này không có đề xuất của AI.')},
                status=status.HTTP_400_BAD_REQUEST,
            )

        comment.refresh_from_db()
        token_map = annotation_service.my_token_label_map([comment.id], request.user)
        return Response({
            'success': True,
            'my_label': result['my_label'],
            'tokens_accepted': result['tokens_accepted'],
            'tokens': annotation_service.tokens_for_user(comment, token_map.get(comment.id)),
            'review_status': result['review_status'],
            'manual_count': result['manual_count'],
            'is_meaningful': comment.is_meaningful,
            'progress': _my_progress(request.user, comment.youtube_link),
        })


class SkipCommentView(BaseAPIView):
    throttle_scope = 'annotation'

    def post(self, request, comment_id):
        comment = get_comment_or_404(request.user, comment_id, require_unlocked=True)
        skipped = request.data.get('skipped', True)
        annotation_service.skip_comment(comment, request.user, skipped=bool(skipped))
        comment.refresh_from_db()
        return Response({
            'success': True,
            'is_meaningful': comment.is_meaningful,
            'review_status': comment.review_status,
            'my_label': _my_label_data(comment, request.user),
            'progress': _my_progress(request.user, comment.youtube_link),
        })


class CommentAnnotationsView(BaseAPIView):
    """Xem toàn bộ annotation của một comment: ai đã gán nhãn gì."""

    def get(self, request, comment_id):
        comment = get_comment_or_404(request.user, comment_id)
        annotations = comment.annotations.select_related(
            'annotator', 'project_label__label'
        ).all()
        return Response({
            'comment_id': str(comment.id),
            'review_status': comment.review_status,
            'gold_label': (
                {'id': str(comment.gold_label.id), 'name': comment.gold_label.display_name}
                if comment.gold_label else None
            ),
            'annotations': [{
                'id': str(a.id),
                'annotator': (
                    a.annotator.get_full_name() or a.annotator.username
                ) if a.annotator else 'AI',
                'annotator_id': str(a.annotator_id) if a.annotator_id else None,
                'source': a.source,
                'label': a.label_name,
                'label_id': str(a.project_label_id) if a.project_label_id else None,
                'confidence': a.confidence,
                'note': a.note,
                'created_at': a.created_at.isoformat(),
            } for a in annotations],
        })


# ---------------------------------------------------------------------------
# Hàng đợi gán nhãn & phân công
# ---------------------------------------------------------------------------
class AnnotationQueueView(BaseAPIView):
    """
    Danh sách comment cho màn hình gán nhãn nhanh, theo ba chế độ.

    - `todo` (mặc định): việc còn lại của người này. Thứ tự ưu tiên
      (active learning §26): comment được phân công riêng > comment AI ít chắc
      chắn nhất > comment chưa đủ số annotator.
    - `done`: comment chính người này đã gán, xếp theo thời điểm gán tăng dần
      nên phần tử cuối là câu vừa làm xong.
    - `all`: toàn bộ comment của nguồn dữ liệu, kể cả câu đã đủ annotator hoặc
      đã bị đánh dấu bỏ qua.

    Ba chế độ ghép lại thành một dòng thời gian liền mạch
    [done cũ → done mới] [todo], nên bấm "Trước" ở đầu hàng đợi là quay lại
    đúng câu vừa gán, không phải mở màn hình danh sách để sửa.
    """

    MODES = ('todo', 'done', 'all')

    def _todo_queryset(self, link, project, user):
        """Việc còn lại của `user`, kèm cờ cho biết có đang chạy theo phân công."""
        already_done = Comment.objects.filter(
            youtube_link=link,
            annotations__annotator=user,
            annotations__source='manual',
        ).values('id')

        base = link.comments.exclude(is_meaningful=False).exclude(id__in=already_done)

        assigned_ids = set(
            AnnotationAssignment.objects.filter(
                annotator=user, project=project, status='pending',
                comment__youtube_link=link,
            ).values_list('comment_id', flat=True)
        )
        if assigned_ids:
            queryset = base.filter(id__in=assigned_ids)
        else:
            queryset = base.filter(
                manual_annotation_count__lt=max(1, project.annotators_per_comment)
            )

        # Câu đã có đề xuất của AI xếp trước.
        #
        # Không xếp thuần theo `toxicity_confidence` tăng dần: câu mà AI không
        # đưa ra nhãn nào cũng mang confidence mặc định 0.0, nên chúng chiếm
        # hết đầu hàng đợi và người dùng lướt mãi không gặp đề xuất nào để
        # soát. "Không có dự đoán" khác hẳn "dự đoán mà không chắc".
        has_ai = Exists(
            CommentAnnotation.objects.filter(comment=OuterRef('pk'), source='ai')
        )
        return (
            queryset.annotate(has_ai_suggestion=has_ai).order_by(
                'manual_annotation_count',
                '-has_ai_suggestion',
                'toxicity_confidence',
                'fetched_at',
            ),
            bool(assigned_ids),
        )

    def _done_queryset(self, link, user):
        """
        Comment người này đã gán, xếp theo thời điểm gán lần đầu.

        Dùng `created_at` chứ không phải `updated_at`: sửa lại nhãn một câu cũ
        không được đẩy nó xuống cuối danh sách, nếu không mỗi lần sửa xong thì
        thứ tự lại đảo và người dùng mất dấu chỗ đang đứng.
        """
        mine = Q(annotations__annotator=user, annotations__source='manual')
        return (
            link.comments
            .annotate(my_annotated_at=Min('annotations__created_at', filter=mine))
            .filter(my_annotated_at__isnull=False)
            .order_by('my_annotated_at', 'fetched_at')
        )

    def get(self, request, link_id):
        link = get_link_or_404(request.user, link_id)
        project = link.project

        mode = request.query_params.get('mode', 'todo')
        if mode not in self.MODES:
            mode = 'todo'
        try:
            limit = max(1, min(50, int(request.query_params.get('limit', 10))))
        except (TypeError, ValueError):
            limit = 10
        try:
            offset = max(0, int(request.query_params.get('offset', 0)))
        except (TypeError, ValueError):
            offset = 0

        todo, assigned_mode = self._todo_queryset(link, project, request.user)
        done = self._done_queryset(link, request.user)
        counts = {
            'todo': todo.count(),
            'done': done.count(),
            'all': link.comments.count(),
        }

        if mode == 'done':
            queryset = done
        elif mode == 'all':
            queryset = link.comments.order_by('fetched_at')
        else:
            queryset = todo

        # offset vượt quá cuối danh sách (ví dụ vừa gán xong câu cuối rồi tải
        # lại trang) -> lùi về trang cuối còn dữ liệu thay vì trả rỗng.
        total = counts[mode]
        if offset and offset >= total:
            offset = max(0, ((total - 1) // limit) * limit) if total else 0

        comments = list(
            queryset.select_related('ai_label__label').prefetch_related('tokens')
            [offset:offset + limit]
        )
        comment_ids = [c.id for c in comments]
        label_map = annotation_service.my_comment_label_map(comment_ids, request.user)
        token_map = annotation_service.my_token_label_map(comment_ids, request.user)

        return Response({
            'link_id': str(link.id),
            'mode': mode,
            'limit': limit,
            'offset': offset,
            'total': total,
            # Luôn là số việc còn lại của chính người này, bất kể đang xem chế
            # độ nào: đây là con số hiển thị trên thanh tiến độ.
            'remaining': counts['todo'],
            'counts': counts,
            'assigned_mode': assigned_mode,
            'progress': _my_progress(request.user, link),
            'comments': [
                _queue_row(c, my_label=label_map.get(c.id),
                           token_labels=token_map.get(c.id))
                for c in comments
            ],
        })


def _my_progress(user, link) -> dict:
    """Số liệu tiến độ của chính người dùng hiện tại."""
    project = link.project
    annotated_here = CommentAnnotation.objects.filter(
        comment__youtube_link=link, annotator=user, source='manual'
    ).count()
    annotated_project = CommentAnnotation.objects.filter(
        comment__youtube_link__project=project, annotator=user, source='manual'
    ).count()
    skipped_here = Comment.objects.filter(
        youtube_link=link, is_meaningful=False
    ).count()
    total_here = Comment.objects.filter(youtube_link=link).exclude(
        is_meaningful=False
    ).count()

    return {
        'my_annotated_link': annotated_here,
        'my_annotated_project': annotated_project,
        'link_total': total_here,
        'link_skipped': skipped_here,
        'percent': round(100 * annotated_here / total_here, 1) if total_here else 0.0,
    }


def _queue_row(comment, my_label=None, token_labels=None) -> dict:
    """Một bình luận trong hàng đợi, kèm nhãn mà chính người dùng đã gán."""
    return {
        'id': str(comment.id),
        'text': comment.text,
        'source_text': comment.source_text,
        'author': comment.author,
        'ai_label': comment.ai_label_data,
        'ai_confidence': comment.toxicity_confidence,
        'review_status': comment.review_status,
        'manual_annotation_count': comment.manual_annotation_count,
        'is_meaningful': comment.is_meaningful,
        # Nhãn do chính người dùng hiện tại gán: dùng để hiển thị khi quay lại
        # câu đã làm. Khác với review_status (trạng thái đồng thuận của cả nhóm).
        'my_label': my_label,
        # Token cũng theo góc nhìn cá nhân: dùng display_tokens thì annotator
        # thấy sẵn nhãn token của người khác và bị dẫn dắt theo.
        'tokens': annotation_service.tokens_for_user(comment, token_labels),
    }


def _my_label_data(comment, user):
    """Nhãn của chính `user` trên `comment`, hoặc None nếu chưa gán."""
    return annotation_service.my_comment_label(comment, user)


class AssignWorkView(BaseAPIView):
    """Chủ dự án phân công comment cho các annotator."""

    throttle_scope = 'heavy'

    def get(self, request, project_id):
        project = get_project_or_404(request.user, project_id, require_owner=True)
        rows = (
            AnnotationAssignment.objects.filter(project=project)
            .values('annotator__username')
            .annotate(
                total=Count('id'),
                done=Count('id', filter=Q(status='done')),
            )
            .order_by('annotator__username')
        )
        return Response({'assignments': list(rows)})

    @transaction.atomic
    def post(self, request, project_id):
        project = get_project_or_404(request.user, project_id, require_owner=True,
                                     require_unlocked=True)

        annotator_ids = request.data.get('annotator_ids') or []
        try:
            per_annotator = max(1, min(5000, int(request.data.get('per_annotator', 100))))
        except (TypeError, ValueError):
            return Response({'error': _('per_annotator không hợp lệ.')},
                            status=status.HTTP_400_BAD_REQUEST)

        allowed_ids = {
            str(uid) for uid in project.participants.values_list('id', flat=True)
        } | {str(project.owner_id)}
        annotators = list(
            User.objects.filter(id__in=[
                aid for aid in annotator_ids if str(aid) in allowed_ids
            ])
        )
        if not annotators:
            return Response({'error': _('Không có annotator hợp lệ nào được chọn.')},
                            status=status.HTTP_400_BAD_REQUEST)

        link_id = request.data.get('link_id')
        pool = Comment.objects.filter(youtube_link__project=project).exclude(
            is_meaningful=False
        )
        if link_id:
            pool = pool.filter(youtube_link_id=link_id)
        pool = pool.filter(
            manual_annotation_count__lt=max(1, project.annotators_per_comment)
        ).order_by('toxicity_confidence', 'fetched_at')

        created = 0
        candidates = list(pool.values_list('id', flat=True)[:per_annotator * len(annotators)])
        for index, comment_id in enumerate(candidates):
            annotator = annotators[index % len(annotators)]
            _obj, was_created = AnnotationAssignment.objects.get_or_create(
                project=project, comment_id=comment_id, annotator=annotator,
                defaults={'status': 'pending'},
            )
            created += int(was_created)

        return Response({
            'success': True,
            'assigned': created,
            'annotators': [a.username for a in annotators],
        })


# ---------------------------------------------------------------------------
# Chất lượng & phân xử
# ---------------------------------------------------------------------------
class AgreementView(BaseAPIView):
    """Báo cáo độ đồng thuận giữa các annotator (IAA)."""

    throttle_scope = 'heavy'

    def get(self, request, project_id):
        project = get_project_or_404(request.user, project_id, require_owner=True)
        link = None
        link_id = request.query_params.get('link_id')
        if link_id:
            link = YouTubeLink.objects.filter(id=link_id, project=project).first()
        report = agreement_service.project_agreement_report(project, link=link)
        report['productivity'] = agreement_service.annotator_productivity(project)
        return Response(report)


class ConflictListView(BaseAPIView):
    """Danh sách comment đang bất đồng, chờ chủ dự án phân xử."""

    def get(self, request, project_id):
        project = get_project_or_404(request.user, project_id, require_owner=True)
        try:
            limit = max(1, min(200, int(request.query_params.get('limit', 50))))
        except (TypeError, ValueError):
            limit = 50

        conflicts = (
            Comment.objects.filter(
                youtube_link__project=project, review_status='conflict'
            )
            .select_related('youtube_link')
            .prefetch_related('annotations__annotator', 'annotations__project_label__label')
            .order_by('-updated_at')[:limit]
        )

        return Response({'conflicts': [{
            'id': str(c.id),
            'text': c.text,
            'link_id': str(c.youtube_link_id),
            'link_title': c.youtube_link.title,
            'annotations': [{
                'annotator': (
                    a.annotator.get_full_name() or a.annotator.username
                ) if a.annotator else 'AI',
                'source': a.source,
                'label': a.label_name,
                'label_id': str(a.project_label_id) if a.project_label_id else None,
            } for a in c.annotations.all()],
        } for c in conflicts]})


class AdjudicateView(BaseAPIView):
    """Chủ dự án chốt nhãn cuối cùng cho một comment bất đồng."""

    def post(self, request, comment_id):
        comment = get_comment_or_404(request.user, comment_id, require_owner=True,
                                     require_unlocked=True)
        project = comment.youtube_link.project
        project_label = _resolve_project_label(project, request.data.get('label_id'))
        if request.data.get('label_id') and project_label is None:
            return Response({'success': False, 'error': _('Nhãn không thuộc dự án này.')},
                            status=status.HTTP_400_BAD_REQUEST)

        result = annotation_service.adjudicate_comment(
            comment, request.user, project_label,
            note=request.data.get('note', '') or '',
        )
        comment.refresh_from_db()
        return Response({
            'success': True,
            'review_status': result['review_status'],
            'gold_label': comment.effective_label_data,
        })


class AnnotatorProgressView(BaseAPIView):
    """Tiến độ và năng suất từng annotator trong dự án."""

    def get(self, request, project_id):
        project = get_project_or_404(request.user, project_id)
        if not is_owner_level(project, request.user):
            # Annotator chỉ xem được tiến độ của chính mình.
            rows = [
                row for row in agreement_service.annotator_productivity(project)
                if row['annotator_id'] == str(request.user.id)
            ]
            return Response({'productivity': rows, 'scope': 'self'})
        return Response({
            'productivity': agreement_service.annotator_productivity(project),
            'scope': 'project',
        })


# ---------------------------------------------------------------------------
# Điều khiển task
# ---------------------------------------------------------------------------
class StopFetchTaskView(BaseAPIView):
    def post(self, request, link_id):
        link = get_link_or_404(request.user, link_id, require_owner=True)
        cancel_tasks_for_link_now(str(link.id))
        return Response({'success': True, 'message': _('Đã yêu cầu dừng tải bình luận.')})


class StopAnnotationTaskView(BaseAPIView):
    def post(self, request, link_id):
        link = get_link_or_404(request.user, link_id, require_owner=True)
        cancel_tasks_for_link_now(str(link.id))
        return Response({'success': True, 'message': _('Đã yêu cầu dừng gán nhãn AI.')})


class RetryFetchView(BaseAPIView):
    throttle_scope = 'heavy'

    def post(self, request, link_id):
        link = get_link_or_404(request.user, link_id, require_owner=True,
                               require_unlocked=True)
        cancel_tasks_for_link_now(str(link.id))
        link.status = 'pending'
        link.save(update_fields=['status', 'updated_at'])
        enqueue_fetch_comments_task(link, 'Refetching comments without clearing existing data')
        return Response({
            'success': True,
            'message': _('Đang tải lại bình luận. Dữ liệu hiện có được giữ nguyên.'),
        })


class ClearAndRefetchView(BaseAPIView):
    throttle_scope = 'heavy'

    def post(self, request, link_id):
        link = get_link_or_404(request.user, link_id, require_owner=True,
                               require_unlocked=True)
        result = clear_link_data_for_refetch(str(link.id))
        if result.get('status') == 'error':
            return Response({'success': False, 'message': result.get('message', '')},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        enqueue_fetch_comments_task(link, 'Clearing old comments and refetching')
        return Response({
            'success': True,
            'message': _('Đã xoá %(n)d bình luận và bắt đầu tải lại.') % {
                'n': result.get('deleted_comments', 0)
            },
            'cleared_comments': result.get('deleted_comments', 0),
        })


class ContinueAnnotationView(BaseAPIView):
    throttle_scope = 'heavy'

    def post(self, request, link_id):
        link = get_link_or_404(request.user, link_id, require_owner=True,
                               require_unlocked=True)
        pending = link.comments.filter(ai_processed=False).exclude(
            is_meaningful=False
        ).count()
        if pending == 0:
            return Response({'success': False, 'message': _('Không còn bình luận nào chưa gán nhãn.')},
                            status=status.HTTP_400_BAD_REQUEST)

        running = get_effective_task_progress(str(link.id), 'annotating')
        if running and running.status == 'running':
            return Response({
                'success': True,
                'already_running': True,
                'message': _('Tác vụ gán nhãn đang chạy.'),
                'task': _task_payload(running),
            })

        enqueue_annotation_task(link, 'Continuing annotation')
        return Response({
            'success': True,
            'message': _('Đang gán nhãn tiếp %(n)d bình luận.') % {'n': pending},
        })


class ReannotateLinkView(BaseAPIView):
    """
    Chạy lại gán nhãn AI cho toàn bộ link.

    Mặc định chỉ đụng tới nhãn AI. Nhãn người gán, nhãn đã chốt và cụm token do
    người kéo chọn được giữ nguyên; muốn xoá chúng phải gửi tường minh
    reset_manual=true.

    Nhãn AI cũ không bị xoá ngay từ đầu mà được ghi đè dần khi task chạy tới
    từng bình luận. Bấm Stop giữa chừng vì thế chỉ dừng lại, chứ không để lại
    một đống bình luận trắng nhãn ở phần chưa chạy tới.
    """

    throttle_scope = 'heavy'

    @transaction.atomic
    def post(self, request, link_id):
        link = get_link_or_404(request.user, link_id, require_owner=True,
                               require_unlocked=True)
        reset_manual = str(request.data.get('reset_manual', '')).lower() in ('1', 'true', 'yes')

        cancel_tasks_for_link_now(str(link.id))

        from .models import CommentAnnotation

        # Cờ này quyết định bình luận nào được đưa vào batch, nên bắt buộc phải
        # đặt lại. Các cột nhãn thì không: chúng được ghi đè khi task chạy tới.
        update_fields = {
            'ai_processed': False,
            'ai_processed_at': None,
        }

        if reset_manual:
            CommentAnnotation.objects.filter(comment__youtube_link=link).delete()
            update_fields.update({
                'ai_label': None,
                'toxicity_confidence': None,
                'model_response': None,
                'manual_label': None,
                'gold_label': None,
                'review_status': 'pending',
                'manual_annotation_count': 0,
                'annotated_at': None,
                'is_meaningful': None,
            })
            Token.objects.filter(comment__youtube_link=link).delete()

        link.comments.update(**update_fields)

        from .services.annotation import _log_event
        _log_event(
            project=link.project, comment=None, actor=request.user, action='reset',
            old_value='ai+manual' if reset_manual else 'ai',
            new_value='cleared',
            detail={'link_id': str(link.id), 'reset_manual': reset_manual},
        )

        enqueue_annotation_task(link, 'Re-annotating all comments')
        return Response({
            'success': True,
            'message': (
                _('Đã đặt lại toàn bộ nhãn (kể cả nhãn thủ công) và bắt đầu gán nhãn lại.')
                if reset_manual else
                _('Đã bắt đầu gán nhãn lại. Nhãn thủ công được giữ nguyên.')
            ),
        })


# ---------------------------------------------------------------------------
# Nhãn
# ---------------------------------------------------------------------------
class LabelListView(BaseAPIView):
    """Danh sách nhãn của chính người dùng hiện tại."""

    def get(self, request):
        labels = Label.objects.filter(owner=request.user).annotate(
            assignment_count=Count('projectlabels', distinct=True),
        ).order_by('name')
        return Response({'labels': [{
            'id': str(label.id),
            'name': label.name,
            'description': label.description,
            'color': label.color,
            'is_active': label.is_active,
            'assignment_count': label.assignment_count,
        } for label in labels]})


class LabelCreateView(BaseAPIView):
    def post(self, request):
        name = (request.data.get('name') or '').strip()
        description = (request.data.get('description') or '').strip()
        color = (request.data.get('color') or '#FF0000').strip()

        if not name:
            return Response({'error': _('Tên nhãn là bắt buộc.')},
                            status=status.HTTP_400_BAD_REQUEST)
        # Ràng buộc thật là unique_together(owner, name), không phải name toàn cục.
        if Label.objects.filter(owner=request.user, name=name).exists():
            return Response({'error': _('Bạn đã có nhãn trùng tên.')},
                            status=status.HTTP_400_BAD_REQUEST)

        label = Label.objects.create(
            owner=request.user, name=name, description=description, color=color
        )
        return Response({
            'id': str(label.id),
            'name': label.name,
            'description': label.description,
            'color': label.color,
        }, status=status.HTTP_201_CREATED)


class ProjectLabelsView(BaseAPIView):
    def get(self, request, project_id):
        project = get_project_or_404(request.user, project_id)
        project_labels = ProjectLabel.objects.filter(
            project=project
        ).select_related('label')
        return Response({'project_labels': [{
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
        } for pl in project_labels]})

    def post(self, request, project_id):
        project = get_project_or_404(request.user, project_id, require_owner=True,
                                     require_unlocked=True)
        action = request.data.get('action', '')

        if action == 'add_label':
            label_id = request.data.get('label_id')
            if not label_id:
                return Response({'error': 'label_id là bắt buộc'},
                                status=status.HTTP_400_BAD_REQUEST)
            # Chỉ cho phép gắn nhãn mà chủ dự án sở hữu.
            label = Label.objects.filter(id=label_id, owner=project.owner).first()
            if label is None:
                return Response({'error': _('Nhãn không tồn tại hoặc không thuộc chủ dự án.')},
                                status=status.HTTP_404_NOT_FOUND)
            _pl, created = ProjectLabel.objects.get_or_create(project=project, label=label)
            return Response({'success': True, 'created': created})

        if action == 'remove_label':
            project_label_id = request.data.get('project_label_id')
            pl = ProjectLabel.objects.filter(id=project_label_id, project=project).first()
            if pl is None:
                return Response({'error': _('Không tìm thấy nhãn trong dự án.')},
                                status=status.HTTP_404_NOT_FOUND)
            pl.delete()
            return Response({'success': True})

        if action == 'update_override':
            project_label_id = request.data.get('project_label_id')
            pl = ProjectLabel.objects.filter(id=project_label_id, project=project).first()
            if pl is None:
                return Response({'error': _('Không tìm thấy nhãn trong dự án.')},
                                status=status.HTTP_404_NOT_FOUND)
            for field in ('override_name', 'override_description', 'override_color'):
                if field in request.data:
                    setattr(pl, field, request.data[field] or None)
            pl.save()
            return Response({'success': True})

        if action == 'add_custom_label':
            name = (request.data.get('custom_name') or '').strip()
            if not name:
                return Response({'error': 'custom_name là bắt buộc'},
                                status=status.HTTP_400_BAD_REQUEST)
            label, created = Label.objects.get_or_create(
                owner=project.owner,
                name=name,
                defaults={
                    'description': (request.data.get('custom_description') or '').strip(),
                    'color': (request.data.get('custom_color') or '#FF0000').strip(),
                },
            )
            ProjectLabel.objects.get_or_create(project=project, label=label)
            return Response({'success': True, 'created': created})

        return Response({'error': _('Hành động không hợp lệ.')},
                        status=status.HTTP_400_BAD_REQUEST)


class ExportFormatsView(BaseAPIView):
    """Danh sách định dạng xuất khả dụng, đã nhóm sẵn cho giao diện."""

    def get(self, request):
        from .export_service import format_choices

        # Chuỗi gettext_lazy phải ép sang str trước khi JSON hoá.
        formats = [{
            key: (str(value) if key in ('label', 'group', 'description', 'sample')
                  else value)
            for key, value in choice.items()
        } for choice in format_choices()]

        return Response({
            'formats': formats,
            'review_filters': [
                {'key': 'all', 'label': str(_('Tất cả bình luận (kể cả chưa gán nhãn)'))},
                {'key': 'labelled', 'label': str(_('Đã có nhãn (AI hoặc người)'))},
                {'key': 'human', 'label': str(_('Có nhãn do người gán'))},
                {'key': 'gold', 'label': str(_('Chỉ nhãn đã chốt — dùng để công bố'))},
            ],
        })


class ExportJobsView(BaseAPIView):
    """
    Trạng thái các lần xuất gần đây: nguồn dữ liệu cho thanh tiến độ và nút
    tải trên trang Xuất dữ liệu.
    """

    def get(self, request, project_id):
        project = get_project_or_404(request.user, project_id)
        records = (
            ExportRecord.objects.filter(project=project)
            .select_related('requested_by', 'youtube_link')[:50]
        )
        return Response({'exports': [{
            'id': str(r.id),
            'format': r.export_format,
            'status': r.status,
            'progress_percent': r.progress_percent,
            'current_step': r.current_step,
            'comment_count': r.comment_count,
            'token_count': r.token_count,
            'file_size': r.file_size,
            'can_download': r.can_download,
            'download_url': (
                reverse('comments:export_download', args=[project.id, r.id])
                if r.can_download else None
            ),
            'error': r.error_message,
            'scope': r.youtube_link.title if r.youtube_link else None,
            'requested_by': (
                r.requested_by.get_full_name() or r.requested_by.username
            ) if r.requested_by else None,
            'generated_at': r.generated_at.isoformat(),
        } for r in records]})


# ---------------------------------------------------------------------------
# Phiên bản dataset
# ---------------------------------------------------------------------------
class DatasetVersionView(BaseAPIView):
    """Liệt kê và tạo phiên bản dataset (snapshot có thể tái lập)."""

    throttle_scope = 'heavy'

    def get(self, request, project_id):
        project = get_project_or_404(request.user, project_id)
        versions = DatasetVersion.objects.filter(project=project).select_related('created_by')
        return Response({'versions': [{
            'id': str(v.id),
            'version': v.version,
            'status': v.status,
            'progress_percent': v.progress_percent,
            'current_step': v.current_step,
            'export_format': v.export_format,
            'comment_count': v.comment_count,
            'token_count': v.token_count,
            'annotator_count': v.annotator_count,
            'agreement_score': v.agreement_score,
            'checksum_sha256': v.checksum_sha256,
            'notes': v.notes,
            'created_by': (
                v.created_by.get_full_name() or v.created_by.username
            ) if v.created_by else None,
            'created_at': v.created_at.isoformat(),
        } for v in versions]})

    def post(self, request, project_id):
        project = get_project_or_404(request.user, project_id, require_owner=True)
        version = (request.data.get('version') or '').strip()
        if not version:
            version = timezone.now().strftime('v%Y%m%d-%H%M')
        if DatasetVersion.objects.filter(project=project, version=version).exists():
            return Response({'error': _('Phiên bản này đã tồn tại.')},
                            status=status.HTTP_400_BAD_REQUEST)

        record = DatasetVersion.objects.create(
            project=project,
            version=version,
            notes=(request.data.get('notes') or '').strip(),
            export_format=request.data.get('format', 'json_token'),
            created_by=request.user,
            status='building',
        )

        from .tasks import build_dataset_version
        build_dataset_version.delay(
            str(record.id), review_filter=request.data.get('review', 'gold')
        )

        return Response({
            'success': True,
            'id': str(record.id),
            'version': record.version,
            'status': record.status,
            'message': _('Đang tạo phiên bản dataset ở chế độ nền.'),
        }, status=status.HTTP_202_ACCEPTED)
