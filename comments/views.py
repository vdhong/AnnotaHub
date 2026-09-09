"""
Web view cho AnnotaHub: quản lý dự án, xem bình luận, gán nhãn.

Hai quy ước bắt buộc trong file này:
- Không dùng tên biến `_` cho giá trị bỏ đi. `_` là alias của gettext; gán đè
  lên nó biến toàn bộ hàm thành lỗi TypeError khi dịch chuỗi.
- Mọi truy cập vào Project/YouTubeLink/Comment phải đi qua comments.permissions.
"""
import csv
import io
import json
import logging
import time
from pathlib import Path

import redis
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import close_old_connections, connection, transaction
from django.db.models import Count, Q
from django.http import (
    FileResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from . import task_messages
from .models import (
    Comment,
    DatasetVersion,
    EmailVerification,
    ExportRecord,
    Label,
    Project,
    ProjectLabel,
    TaskProgress,
    UserInvitation,
    UserSettings,
    YouTubeLink,
)
from .permissions import (
    ROLE_ADMIN,
    ROLE_OWNER,
    admin_projects,
    get_link_or_404,
    get_project_or_404,
    is_owner_level,
    owned_projects,
    role_for,
    visible_projects,
)
from .services import agreement as agreement_service
from .services import ai_review as ai_review_service
from .services import annotation as annotation_service
from .services import versioning as versioning_service
from .services.email_verification_service import send_verification_email
from .services.invitation_service import send_invitation_email
from .services.stats import label_stats_for_link, link_counters
from .services.youtube_service import extract_video_id, get_video_info
from .tasks import (
    build_version,
    cancel_tasks_for_link_now,
    enqueue_fetch_comments_task,
    get_effective_task_progress,
    get_owner_youtube_api_key,
    run_csv_import,
    run_export,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trang lỗi
# ---------------------------------------------------------------------------
def handler403(request, exception=None):
    return render(request, 'comments/error.html', {
        'code': 403,
        'title': _('Không có quyền truy cập'),
        'detail': str(exception) if exception else _('Bạn không có quyền thực hiện hành động này.'),
    }, status=403)


def handler404(request, exception=None):
    return render(request, 'comments/error.html', {
        'code': 404,
        'title': _('Không tìm thấy'),
        'detail': _('Trang hoặc tài nguyên bạn yêu cầu không tồn tại.'),
    }, status=404)


def handler500(request):
    return render(request, 'comments/error.html', {
        'code': 500,
        'title': _('Lỗi hệ thống'),
        'detail': _('Đã có lỗi xảy ra. Quản trị viên đã được thông báo.'),
    }, status=500)


# ---------------------------------------------------------------------------
# Rate limiting đơn giản (dựa trên cache Redis)
# ---------------------------------------------------------------------------
def _rate_limit_key(request, scope):
    ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
    ip = ip or request.META.get('REMOTE_ADDR', 'unknown')
    return f'ratelimit:{scope}:{ip}'


def _is_rate_limited(request, scope, limit=None, window=None):
    """Trả True nếu vượt ngưỡng. Bảo vệ đăng nhập/đăng ký khỏi brute-force."""
    limit = limit or settings.LOGIN_RATELIMIT_ATTEMPTS
    window = window or settings.LOGIN_RATELIMIT_WINDOW
    key = _rate_limit_key(request, scope)
    try:
        count = cache.get_or_set(key, 0, window)
        count = cache.incr(key)
    except Exception:
        # Cache hỏng thì không chặn người dùng hợp lệ.
        return False
    return count > limit


def _reset_rate_limit(request, scope):
    try:
        cache.delete(_rate_limit_key(request, scope))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Xác thực
# ---------------------------------------------------------------------------
def custom_login(request):
    if request.user.is_authenticated:
        return redirect('comments:project_list')

    next_url = request.GET.get('next', '')

    if request.method == 'POST':
        if _is_rate_limited(request, 'login'):
            messages.error(request, _(
                'Bạn đã thử đăng nhập quá nhiều lần. Vui lòng đợi vài phút rồi thử lại.'
            ))
            return render(request, 'comments/login.html',
                          {'form': AuthenticationForm(), 'next': next_url}, status=429)

        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            auth_login(request, form.get_user())
            _reset_rate_limit(request, 'login')
            next_url = request.POST.get('next', request.GET.get('next', ''))
            # Chỉ chuyển hướng tới URL nội bộ: chặn open redirect.
            if next_url and next_url.startswith('/') and not next_url.startswith('//'):
                return redirect(next_url)
            return redirect('comments:project_list')
        messages.error(request, _('Tên đăng nhập hoặc mật khẩu không đúng.'))
    else:
        form = AuthenticationForm()

    return render(request, 'comments/login.html', {'form': form, 'next': next_url})


def custom_logout(request):
    auth_logout(request)
    messages.info(request, _('Bạn đã đăng xuất thành công.'))
    return redirect('comments:login')


def register(request):
    if request.user.is_authenticated:
        return redirect('comments:project_list')

    if request.method == 'POST':
        if _is_rate_limited(request, 'register', limit=5, window=600):
            messages.error(request, _('Bạn đã đăng ký quá nhiều lần. Vui lòng thử lại sau.'))
            return redirect('comments:register')

        username = request.POST.get('username', '').strip()
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        email = request.POST.get('email', '').strip().lower()
        password = request.POST.get('password', '')
        password_confirm = request.POST.get('password_confirm', '')

        errors = []
        if not username:
            errors.append(_('Tên đăng nhập là bắt buộc.'))
        elif len(username) < 3:
            errors.append(_('Tên đăng nhập phải có ít nhất 3 ký tự.'))
        elif len(username) > 150:
            errors.append(_('Tên đăng nhập phải có tối đa 150 ký tự.'))
        elif User.objects.filter(username=username).exists():
            errors.append(_('Tên đăng nhập "%(username)s" đã được sử dụng.') % {'username': username})

        if not f'{first_name} {last_name}'.strip():
            errors.append(_('Họ và tên là bắt buộc.'))

        if not email:
            errors.append(_('Địa chỉ email là bắt buộc.'))
        elif User.objects.filter(email=email).exists():
            errors.append(_('Địa chỉ email "%(email)s" đã được đăng ký.') % {'email': email})

        if not password:
            errors.append(_('Mật khẩu là bắt buộc.'))
        elif len(password) < 8:
            errors.append(_('Mật khẩu phải có ít nhất 8 ký tự.'))
        elif password != password_confirm:
            errors.append(_('Hai mật khẩu nhập lại không khớp.'))

        if errors:
            for error in errors:
                messages.error(request, error)
            return render(request, 'comments/register.html', {
                'username': username, 'first_name': first_name,
                'last_name': last_name, 'email': email,
            })

        user = User.objects.create_user(username=username, email=email, password=password)
        user.first_name = first_name
        user.last_name = last_name
        user.is_active = False
        user.save()

        verification = EmailVerification.objects.create(user=user)
        if send_verification_email(email, verification.token):
            messages.success(request, _(
                'Đăng ký tài khoản thành công! Vui lòng kiểm tra hộp thư %(email)s '
                'để xác thực địa chỉ email. Liên kết xác thực sẽ hết hạn sau 7 ngày.'
            ) % {'email': email})
        else:
            messages.warning(request, _(
                'Đăng ký tài khoản thành công nhưng không thể gửi email xác thực. '
                'Vui lòng nhấn nút "Gửi lại email" bên dưới hoặc liên hệ quản trị viên.'
            ))

        return render(request, 'comments/verification_sent.html',
                      {'email': email, 'user': user})

    return render(request, 'comments/register.html', {
        'username': '', 'first_name': '', 'last_name': '', 'email': '',
    })


def verify_email(request, token):
    verification = EmailVerification.objects.filter(token=token).select_related('user').first()
    if verification is None:
        messages.error(request, _('Liên kết xác thực không hợp lệ.'))
        return redirect('comments:login')

    if verification.is_verified:
        messages.info(request, _('Email đã được xác thực. Vui lòng đăng nhập.'))
        return redirect('comments:login')

    if verification.is_expired():
        messages.error(request, _(
            'Liên kết xác thực đã hết hạn. Vui lòng đăng ký lại hoặc liên hệ quản trị viên.'
        ))
        return redirect('comments:register')

    user = verification.user
    user.is_active = True
    user.save(update_fields=['is_active'])
    verification.is_verified = True
    verification.save(update_fields=['is_verified'])

    auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    messages.success(request, _('Xác thực email thành công! Chào mừng bạn đến với AnnotaHub.'))
    return redirect('comments:project_list')


def resend_verification(request):
    if request.method != 'POST':
        return redirect('comments:register')

    if _is_rate_limited(request, 'resend', limit=5, window=600):
        messages.error(request, _('Bạn đã yêu cầu gửi lại quá nhiều lần. Vui lòng thử lại sau.'))
        return redirect('comments:register')

    email = request.POST.get('email', '').strip().lower()
    if not email:
        messages.error(request, _('Vui lòng nhập địa chỉ email.'))
        return redirect('comments:register')

    user = User.objects.filter(email=email).first()
    # Không tiết lộ email nào đã tồn tại: trả cùng một thông báo trong mọi trường hợp.
    generic = _('Nếu địa chỉ %(email)s tồn tại và chưa xác thực, email xác thực đã được gửi lại.') % {'email': email}

    if user and not user.is_active:
        verification = getattr(user, 'email_verification', None)
        if verification is None:
            verification = EmailVerification.objects.create(user=user)
        verification.expires_at = timezone.now() + timezone.timedelta(days=7)
        verification.save(update_fields=['expires_at'])
        send_verification_email(email, verification.token)

    messages.info(request, generic)
    return redirect('comments:register')


def accept_invitation(request, token):
    invitation = UserInvitation.objects.filter(token=token).select_related(
        'user', 'project', 'inviter'
    ).first()
    if invitation is None:
        messages.error(request, _('Liên kết mời không hợp lệ hoặc đã hết hạn.'))
        return redirect('comments:login')
    if invitation.is_used:
        messages.error(request, _('Liên kết mời này đã được sử dụng.'))
        return redirect('comments:login')
    if invitation.is_expired():
        messages.error(request, _(
            'Liên kết mời đã hết hạn. Vui lòng liên hệ chủ dự án để gửi lại lời mời.'
        ))
        return redirect('comments:login')
    if not invitation.user:
        messages.error(request, _('Không tìm thấy tài khoản liên kết với lời mời này.'))
        return redirect('comments:login')

    user = invitation.user

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        password = request.POST.get('password', '')
        password_confirm = request.POST.get('password_confirm', '')

        if not username:
            messages.error(request, _('Tên đăng nhập là bắt buộc.'))
        elif User.objects.filter(username=username).exclude(pk=user.pk).exists():
            messages.error(request, _('Tên đăng nhập "%(username)s" đã được sử dụng.') % {'username': username})
        elif not password:
            messages.error(request, _('Mật khẩu là bắt buộc.'))
        elif password != password_confirm:
            messages.error(request, _('Hai mật khẩu không khớp.'))
        elif len(password) < 8:
            messages.error(request, _('Mật khẩu phải có ít nhất 8 ký tự.'))
        else:
            with transaction.atomic():
                user.username = username
                user.first_name = first_name
                user.last_name = last_name
                user.set_password(password)
                user.is_active = True
                user.save()

                invitation.is_used = True
                invitation.save(update_fields=['is_used'])

                project = invitation.project
                if not project.participants.filter(pk=user.pk).exists():
                    project.participants.add(user)

            auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            messages.success(request, _(
                'Tài khoản đã được kích hoạt thành công! Bạn đã được thêm vào dự án.'
            ))
            return redirect('comments:project_list')

    return render(request, 'comments/accept_invitation.html', {
        'invitation': invitation,
        'invited_user': user,
        'project_name': invitation.project.name,
        'inviter_name': invitation.inviter.get_full_name() or invitation.inviter.username,
    })


# ---------------------------------------------------------------------------
# Cài đặt người dùng
# ---------------------------------------------------------------------------
@login_required
def user_settings(request):
    settings_obj, _created = UserSettings.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        # Ô để trống nghĩa là giữ nguyên giá trị cũ (vì form chỉ hiển thị giá trị
        # đã che). Muốn xoá thì tick ô clear_* tương ứng.
        for field in ('youtube_api_key', 'ollama_api_key'):
            value = request.POST.get(field, '').strip()
            if request.POST.get(f'clear_{field}') == 'on':
                setattr(settings_obj, field, '')
            elif value:
                setattr(settings_obj, field, value)

        settings_obj.ollama_base_url = request.POST.get('ollama_base_url', '').strip()
        settings_obj.ollama_model = request.POST.get('ollama_model', '').strip()
        settings_obj.save()

        messages.success(request, _('Cài đặt đã được lưu thành công.'))
        return redirect('comments:user_settings')

    return render(request, 'comments/user_settings.html', {
        'user_settings': settings_obj,
        'global_youtube_api_key': bool(settings.YOUTUBE_API_KEY),
        'global_ollama_base_url': settings.OLLAMA_BASE_URL,
        'global_ollama_model': settings.OLLAMA_MODEL,
        'global_has_ollama_api_key': bool(settings.OLLAMA_API_KEY),
    })


# ---------------------------------------------------------------------------
# Quản lý nhãn
# ---------------------------------------------------------------------------
@login_required
def label_list(request):
    # Gộp usage_count/is_in_use vào một truy vấn, thay vì COUNT cho từng nhãn.
    labels = list(
        Label.objects.filter(owner=request.user)
        .annotate(project_count=Count('projectlabels', distinct=True))
        .order_by('name')
    )
    usage = Label.usage_counts_for(labels)
    for label in labels:
        label.usage_count_val = usage.get(label.id, 0)
        label.is_used = usage.get(label.id, 0) > 0
    return render(request, 'comments/label_list.html', {'labels': labels})


@login_required
def label_create(request):
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        color = request.POST.get('color', '#FF0000').strip()

        if not name:
            messages.error(request, _('Tên nhãn là bắt buộc.'))
        elif Label.objects.filter(owner=request.user, name=name).exists():
            messages.error(request, _('Bạn đã có nhãn "%(name)s".') % {'name': name})
        else:
            Label.objects.create(
                owner=request.user, name=name, description=description, color=color
            )
            messages.success(request, _('Nhãn "%(name)s" đã được tạo.') % {'name': name})
        return redirect('comments:label_list')

    return render(request, 'comments/label_form.html', {'action': 'Create'})


@login_required
def label_edit(request, label_id):
    # Lọc theo owner ngay trong truy vấn: người khác nhận 404, không phải 403,
    # nên không suy ra được nhãn đó có tồn tại hay không.
    label = get_object_or_404(Label, id=label_id, owner=request.user)

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        color = request.POST.get('color', '#FF0000').strip()
        is_active = request.POST.get('is_active') == 'on'

        if not name:
            messages.error(request, _('Tên nhãn là bắt buộc.'))
        elif Label.objects.filter(owner=request.user, name=name).exclude(pk=label.pk).exists():
            messages.error(request, _('Bạn đã có nhãn "%(name)s".') % {'name': name})
        else:
            label.name = name
            label.description = description
            label.color = color
            label.is_active = is_active
            label.save()
            messages.success(request, _('Nhãn "%(name)s" đã được cập nhật.') % {'name': name})
            return redirect('comments:label_list')

    return render(request, 'comments/label_form.html', {'label': label, 'action': 'Edit'})


@login_required
@require_POST
def label_delete(request, label_id):
    label = get_object_or_404(Label, id=label_id, owner=request.user)
    if label.is_in_use():
        messages.error(request, _(
            'Không thể xoá nhãn "%(name)s" vì đang được sử dụng trong các bình luận hoặc token.'
        ) % {'name': label.name})
        return redirect('comments:label_list')

    label_name = label.name
    label.delete()
    messages.success(request, _('Nhãn "%(name)s" đã được xoá.') % {'name': label_name})
    return redirect('comments:label_list')


@login_required
def project_labels_settings(request, project_id):
    project = get_project_or_404(request.user, project_id, require_owner=True)

    if request.method == 'POST':
        if project.is_locked:
            messages.error(request, _('Dự án đã bị khoá.'))
            return redirect('comments:project_labels_settings', project_id=project.id)

        action = request.POST.get('action', '')

        if action == 'add_label':
            label_id = request.POST.get('label_id')
            if label_id:
                label = get_object_or_404(Label, id=label_id, owner=project.owner)
                ProjectLabel.objects.get_or_create(project=project, label=label)
                messages.success(request, _('Đã thêm nhãn "%(name)s" vào dự án.') % {'name': label.name})

        elif action == 'remove_label':
            project_label_id = request.POST.get('project_label_id')
            if project_label_id:
                project_label = get_object_or_404(
                    ProjectLabel, id=project_label_id, project=project
                )
                project_label.delete()
                messages.success(request, _('Đã bỏ nhãn khỏi dự án.'))

        elif action == 'update_override':
            project_label_id = request.POST.get('project_label_id')
            if project_label_id:
                project_label = get_object_or_404(
                    ProjectLabel, id=project_label_id, project=project
                )
                project_label.override_name = request.POST.get('override_name') or None
                project_label.override_description = request.POST.get('override_description') or None
                project_label.override_color = request.POST.get('override_color') or None
                project_label.save()
                messages.success(request, _('Đã cập nhật nhãn.'))

        elif action == 'add_custom_label':
            name = request.POST.get('custom_name', '').strip()
            if name:
                label, created = Label.objects.get_or_create(
                    owner=project.owner,
                    name=name,
                    defaults={
                        'description': request.POST.get('custom_description', '').strip(),
                        'color': request.POST.get('custom_color', '#FF0000').strip(),
                    },
                )
                ProjectLabel.objects.get_or_create(project=project, label=label)
                messages.success(request, _('Đã thêm nhãn "%(name)s" vào dự án.') % {'name': name})
            else:
                messages.error(request, _('Tên nhãn là bắt buộc.'))

        elif action == 'update_workflow':
            try:
                project.annotators_per_comment = max(
                    1, min(10, int(request.POST.get('annotators_per_comment', 1)))
                )
            except (TypeError, ValueError):
                messages.error(request, _('Số annotator phải là số nguyên từ 1 đến 10.'))
            else:
                project.auto_adjudicate = request.POST.get('auto_adjudicate') == 'on'
                project.guideline = request.POST.get('guideline', '').strip()
                project.save(update_fields=[
                    'annotators_per_comment', 'auto_adjudicate', 'guideline', 'updated_at'
                ])
                messages.success(request, _('Đã cập nhật cấu hình quy trình gán nhãn.'))

        return redirect('comments:project_labels_settings', project_id=project.id)

    project_labels = list(
        ProjectLabel.objects.filter(project=project).select_related('label')
    )
    assigned_label_ids = [pl.label_id for pl in project_labels]
    available_labels = Label.objects.filter(
        owner=project.owner, is_active=True
    ).exclude(id__in=assigned_label_ids).order_by('name')

    usage = ProjectLabel.usage_counts_for(project_labels)
    for project_label in project_labels:
        stats = usage.get(project_label.id, {})
        project_label.token_usage = stats.get('tokens', 0)
        project_label.comment_usage = stats.get('comments', 0)

    return render(request, 'comments/project_labels_settings.html', {
        'project': project,
        'project_labels': project_labels,
        'available_labels': available_labels,
        'is_owner': True,
    })


# ---------------------------------------------------------------------------
# Dashboard & dự án
# ---------------------------------------------------------------------------
TASK_LABELS = {
    'pending': 'Pending',
    'running': 'Running',
    'completed': 'Completed',
    'failed': 'Failed',
    'cancelled': 'Cancelled',
}


def _serialize_task_progress(progress_record, task_type):
    """Ảnh chụp trạng thái task ở dạng JSON-serializable."""
    if not progress_record:
        return {
            'type': task_type,
            'status': 'pending',
            'status_display': TASK_LABELS['pending'],
            'progress': 0,
            'step': '',
            'total': 0,
            'processed': 0,
        }
    return {
        'type': task_type,
        'status': progress_record.status,
        'status_display': progress_record.get_status_display(),
        'progress': progress_record.progress_percent,
        'step': task_messages.render(
            progress_record.current_step,
            progress_record.step_params,
            processed=progress_record.processed_items,
            total=progress_record.total_items,
        ),
        'total': progress_record.total_items,
        'processed': progress_record.processed_items,
    }


@login_required
def dashboard(request):
    projects = visible_projects(request.user).annotate(
        link_count=Count('youtubelinks', distinct=True),
        comment_count=Count('youtubelinks__comments', distinct=True),
        annotated_count=Count(
            'youtubelinks__comments',
            filter=Q(youtubelinks__comments__manual_label__isnull=False),
            distinct=True,
        ),
    )
    return render(request, 'comments/dashboard.html', {'projects': projects})


def health_check(request):
    """Endpoint kiểm tra sức khoẻ: máy gọi, không có tiền tố ngôn ngữ."""
    components = {'database': {'status': 'unknown'}, 'redis': {'status': 'unknown'}}

    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        components['database'] = {'status': 'ok'}
    except Exception as exc:
        logger.error('Health check: database lỗi: %s', exc)
        components['database'] = {'status': 'error'}

    try:
        redis.Redis.from_url(settings.CELERY_BROKER_URL).ping()
        components['redis'] = {'status': 'ok'}
    except Exception as exc:
        logger.error('Health check: redis lỗi: %s', exc)
        components['redis'] = {'status': 'error'}

    overall_ok = all(component['status'] == 'ok' for component in components.values())
    return JsonResponse({
        'status': 'ok' if overall_ok else 'degraded',
        'components': components,
        'timestamp': timezone.now().isoformat(),
    }, status=200 if overall_ok else 503)


@login_required
def project_list(request):
    counts = {
        'link_count': Count('youtubelinks', distinct=True),
        'comment_count': Count('youtubelinks__comments', distinct=True),
    }

    # "Sở hữu" nghĩa là Project.owner == user. Không gộp superuser vào đây, kẻo
    # mọi dự án của người khác cũng hiện trong mục này và dễ bị xoá nhầm.
    owned = owned_projects(request.user).annotate(**counts)

    participated = Project.objects.filter(participants=request.user).exclude(
        owner=request.user
    ).annotate(**counts)

    # Dự án superuser có quyền quản trị nhưng không sở hữu: hiển thị riêng,
    # ghi rõ ai là chủ thật.
    administered = admin_projects(request.user).exclude(
        participants=request.user
    ).select_related('owner').annotate(**counts)

    return render(request, 'comments/project_list.html', {
        'owned_projects': owned,
        'participated_projects': participated,
        'administered_projects': administered,
        'is_superuser': request.user.is_superuser,
    })


@login_required
def project_create(request):
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()

        if not name:
            messages.error(request, _('Tên dự án là bắt buộc.'))
            return redirect('comments:project_create')
        if Project.objects.filter(name=name).exists():
            messages.error(request, _(
                'Dự án tên "%(name)s" đã tồn tại. Vui lòng chọn tên khác.'
            ) % {'name': name})
            return redirect('comments:project_create')

        project = Project.objects.create(
            name=name, description=description, owner=request.user
        )
        messages.success(request, _('Đã tạo dự án "%(name)s".') % {'name': name})
        return redirect('comments:project_detail', project_id=project.id)

    return render(request, 'comments/project_form.html', {'action': 'Create'})


@login_required
def project_detail(request, project_id):
    project = get_project_or_404(request.user, project_id)
    role = role_for(project, request.user)
    # is_owner = có quyền cấp chủ sở hữu (chủ thật hoặc superuser).
    # is_real_owner phân biệt chủ thật, để giao diện nói đúng sự thật.
    is_owner = role in (ROLE_OWNER, ROLE_ADMIN)
    is_real_owner = role == ROLE_OWNER

    links = list(YouTubeLink.objects.filter(project=project))

    # Lấy tiến độ mới nhất của tất cả link trong 1 truy vấn thay vì N+1.
    progress_map = {}
    for record in TaskProgress.objects.filter(
        youtube_link__in=links
    ).order_by('youtube_link_id', '-created_at'):
        progress_map.setdefault(record.youtube_link_id, record)
    for link in links:
        link.latest_progress = progress_map.get(link.id)

    return render(request, 'comments/project_detail.html', {
        'project': project,
        'links': links,
        'is_owner': is_owner,
        'is_real_owner': is_real_owner,
        'viewing_as_admin': role == ROLE_ADMIN,
        'role': role,
        'is_participant': role == 'annotator',
        'participants': project.participants.all() if is_owner else [],
    })


@login_required
def project_edit(request, project_id):
    project = get_project_or_404(request.user, project_id, require_owner=True)

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()

        if not name:
            messages.error(request, _('Tên dự án là bắt buộc.'))
        elif Project.objects.filter(name=name).exclude(pk=project.pk).exists():
            messages.error(request, _(
                'Dự án tên "%(name)s" đã tồn tại. Vui lòng chọn tên khác.'
            ) % {'name': name})
        else:
            project.name = name
            project.description = description
            project.save()
            messages.success(request, _('Đã cập nhật dự án "%(name)s".') % {'name': name})
            return redirect('comments:project_detail', project_id=project.id)

    return render(request, 'comments/project_form.html', {'project': project, 'action': 'Edit'})


@login_required
@require_POST
def project_delete(request, project_id):
    # lưu Ý: không dùng `_` làm biến bỏ đi ở đây, `_` là gettext.
    project = get_project_or_404(request.user, project_id, require_owner=True)

    for link in project.youtubelinks.all():
        cancel_tasks_for_link_now(str(link.id))

    project_name = project.name
    project.delete()
    messages.success(request, _('Đã xoá dự án "%(name)s".') % {'name': project_name})
    return redirect('comments:project_list')


@login_required
@require_POST
def project_lock(request, project_id):
    project = get_project_or_404(request.user, project_id, require_owner=True)
    project.is_locked = not project.is_locked
    project.save(update_fields=['is_locked', 'updated_at'])
    messages.info(request, _('Đã khoá dự án.') if project.is_locked
                  else _('Đã mở khoá dự án.'))
    return redirect('comments:project_detail', project_id=project.id)


@login_required
def project_manage_participants(request, project_id):
    project = get_project_or_404(request.user, project_id, require_owner=True)

    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'add_participant':
            email = request.POST.get('email', '').strip().lower()
            if not email:
                messages.error(request, _('Vui lòng nhập email.'))
            else:
                user = User.objects.filter(email=email).first()
                if user:
                    if user == request.user:
                        messages.error(request, _('Bạn không thể thêm chính mình vào danh sách tham gia.'))
                    elif project.participants.filter(pk=user.pk).exists():
                        messages.warning(request, _(
                            'User "%(username)s" đã là thành viên tham gia.'
                        ) % {'username': user.username})
                    elif project.owner_id == user.id:
                        messages.warning(request, _(
                            'User "%(username)s" là chủ sở hữu dự án, không cần thêm.'
                        ) % {'username': user.username})
                    else:
                        project.participants.add(user)
                        messages.success(request, _(
                            'Đã thêm "%(username)s" (%(email)s) vào danh sách tham gia.'
                        ) % {'username': user.username, 'email': email})
                else:
                    invitation = _create_invitation(project, request.user, email)
                    if send_invitation_email(email, invitation.token):
                        messages.success(request, _(
                            'Email mời đã được gửi đến %(email)s.'
                        ) % {'email': email})
                    else:
                        link = f'{settings.SITE_URL}/invite/{invitation.token}/'
                        messages.warning(request, _(
                            'Không gửi được email mời. Vui lòng gửi liên kết này thủ công: %(link)s'
                        ) % {'link': link})

        elif action == 'remove_participant':
            user_id = request.POST.get('user_id')
            user = User.objects.filter(pk=user_id).first() if user_id else None
            if user and project.participants.filter(pk=user.pk).exists():
                project.participants.remove(user)
                messages.success(request, _(
                    'Đã xoá "%(username)s" khỏi danh sách tham gia.'
                ) % {'username': user.username})
            else:
                messages.error(request, _('User không tồn tại trong danh sách tham gia.'))

        elif action in ('delete_invitation', 'resend_invitation'):
            invitation_id = request.POST.get('invitation_id')
            invitation = UserInvitation.objects.filter(
                id=invitation_id, project=project, is_used=False
            ).first() if invitation_id else None

            if invitation is None:
                messages.error(request, _('Không tìm thấy lời mời hoặc đã được sử dụng.'))
            elif action == 'delete_invitation':
                email = invitation.email
                invitation.delete()
                messages.success(request, _('Đã xoá lời mời cho %(email)s.') % {'email': email})
            else:
                if invitation.is_expired():
                    invitation.expires_at = timezone.now() + timezone.timedelta(days=7)
                    invitation.save(update_fields=['expires_at'])
                if send_invitation_email(invitation.email, invitation.token):
                    messages.success(request, _(
                        'Email mời đã được gửi lại đến %(email)s.'
                    ) % {'email': invitation.email})
                else:
                    messages.error(request, _(
                        'Không gửi lại được email mời đến %(email)s.'
                    ) % {'email': invitation.email})

        return redirect('comments:project_manage_participants', project_id=project.id)

    return render(request, 'comments/project_participants.html', {
        'project': project,
        'is_owner': True,
        'participants': project.participants.all(),
        'pending_invitations': UserInvitation.objects.filter(
            project=project, is_used=False
        ).select_related('user', 'inviter').order_by('-created_at'),
    })


def _create_invitation(project, inviter, email):
    """Tạo tài khoản chờ kích hoạt + lời mời cho một email chưa có tài khoản."""
    base_username = email.split('@')[0][:140] or 'user'
    username = base_username
    counter = 1
    while User.objects.filter(username=username).exists():
        username = f'{base_username}{counter}'
        counter += 1

    new_user = User.objects.create_user(username=username, email=email)
    # Không đặt mật khẩu ngẫu nhiên: tài khoản chưa dùng được cho tới khi
    # người được mời tự đặt mật khẩu qua liên kết.
    new_user.set_unusable_password()
    new_user.is_active = False
    new_user.save()

    return UserInvitation.objects.create(
        email=email, project=project, inviter=inviter, user=new_user
    )


# ---------------------------------------------------------------------------
# Chất lượng gán nhãn (Giai đoạn 3)
# ---------------------------------------------------------------------------
@login_required
def project_quality(request, project_id):
    """Bảng điều khiển chất lượng: độ đồng thuận + năng suất annotator."""
    project = get_project_or_404(request.user, project_id, require_owner=True)
    report = agreement_service.project_agreement_report(project)
    productivity = agreement_service.annotator_productivity(project)

    status_counts = dict(
        Comment.objects.filter(youtube_link__project=project)
        .values_list('review_status')
        .annotate(n=Count('id'))
    )

    return render(request, 'comments/project_quality.html', {
        'project': project,
        'is_owner': True,
        'report': report,
        'productivity': productivity,
        'status_counts': status_counts,
        'ai_review': ai_review_service.ai_review_stats(project=project),
        'report_json': json.dumps(report, ensure_ascii=False),
    })


@login_required
def project_adjudicate(request, project_id):
    """Màn hình phân xử các comment đang bất đồng."""
    project = get_project_or_404(request.user, project_id, require_owner=True)

    conflicts = (
        Comment.objects.filter(youtube_link__project=project, review_status='conflict')
        .select_related('youtube_link')
        .prefetch_related('annotations__annotator', 'annotations__project_label__label')
        .order_by('-updated_at')
    )
    paginator = Paginator(conflicts, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    project_labels = [{
        'id': str(pl.id),
        'name': pl.display_name,
        'color': pl.display_color,
    } for pl in ProjectLabel.objects.filter(project=project).select_related('label')]

    return render(request, 'comments/project_adjudicate.html', {
        'project': project,
        'is_owner': True,
        'page_obj': page_obj,
        'project_labels': project_labels,
        'project_labels_json': json.dumps(project_labels, ensure_ascii=False),
    })


@login_required
def project_versions(request, project_id):
    """Danh sách phiên bản dataset đã chốt."""
    project = get_project_or_404(request.user, project_id)
    versions = DatasetVersion.objects.filter(project=project).select_related('created_by')
    return render(request, 'comments/project_versions.html', {
        'project': project,
        'versions': versions,
        'is_owner': is_owner_level(project, request.user),
        'is_real_owner': project.owner_id == request.user.id,
    })


def _get_version_or_404(user, project_id, version_id, *, require_owner=False):
    """Lấy DatasetVersion kèm kiểm tra quyền trên dự án cha."""
    project = get_project_or_404(user, project_id, require_owner=require_owner)
    version = get_object_or_404(
        DatasetVersion.objects.select_related('project'),
        id=version_id, project=project,
    )
    return project, version


def _version_file(version, attribute):
    """
    Đường dẫn file của phiên bản, đã kiểm tra nằm trong thư mục xuất.

    file_path do máy chủ tự sinh nên hiện không thể bị người dùng chi phối,
    nhưng vẫn chặn ở đây để một lỗi ở nơi khác không biến thành đọc file tuỳ ý.
    """
    raw = getattr(version, attribute, '')
    if not raw:
        return None
    path = Path(raw).resolve()
    root = Path(settings.EXPORT_ROOT).resolve()
    if not path.is_file() or root not in path.parents:
        return None
    return path


@login_required
def dataset_version_download(request, project_id, version_id):
    """Tải về file của một phiên bản đã chốt."""
    _project, version = _get_version_or_404(request.user, project_id, version_id)

    path = _version_file(version, 'file_path')
    if version.status != 'ready' or path is None:
        messages.error(request, _(
            'Không tìm thấy file của phiên bản này. File có thể đã bị xoá khỏi máy chủ.'
        ))
        return redirect('comments:project_versions', project_id=project_id)

    return FileResponse(
        open(path, 'rb'), as_attachment=True, filename=version.download_name
    )


@login_required
@require_POST
def dataset_version_restore(request, project_id, version_id):
    """
    Đưa nhãn của dự án về đúng trạng thái đã chốt trong phiên bản.

    Chạy đồng bộ (không qua Celery) để người dùng thấy ngay kết quả: đây là
    thao tác cập nhật CSDL theo lô, không phải sinh file, nên tính bằng giây.
    """
    project, version = _get_version_or_404(
        request.user, project_id, version_id, require_owner=True
    )
    if project.is_locked:
        messages.error(request, _('Dự án đang bị khoá nên không thể phục hồi.'))
        return redirect('comments:project_versions', project_id=project_id)

    path = _version_file(version, 'snapshot_path')
    if path is None:
        messages.error(request, _(
            'Phiên bản này không có ảnh chụp nên không phục hồi được. '
            'Chỉ những phiên bản chốt sau khi có tính năng phục hồi mới dùng được.'
        ))
        return redirect('comments:project_versions', project_id=project_id)

    # Chốt một phiên bản dự phòng trước khi ghi đè, để thao tác này đảo ngược
    # được. Chạy đồng bộ vì chạy nền sẽ chụp nhầm trạng thái sau khi phục hồi.
    backup_name = ''
    if request.POST.get('backup') == 'on':
        backup_name = timezone.now().strftime('truoc-phuc-hoi-%Y%m%d-%H%M%S')
        backup = DatasetVersion.objects.create(
            project=project,
            version=backup_name,
            notes=_('Tự động chốt trước khi phục hồi về "%(version)s".') % {
                'version': version.version,
            },
            export_format=version.export_format,
            created_by=request.user,
            status='building',
        )
        if build_version(backup, review_filter='all').get('status') != 'ok':
            messages.error(request, _(
                'Không chốt được phiên bản dự phòng nên đã dừng, chưa phục hồi gì cả.'
            ))
            return redirect('comments:project_versions', project_id=project_id)

    try:
        stats = versioning_service.restore_snapshot(project, path)
    except versioning_service.SnapshotIncompatible as exc:
        messages.error(request, _('Không phục hồi được: %(reason)s') % {'reason': exc})
        return redirect('comments:project_versions', project_id=project_id)

    messages.success(request, _(
        'Đã phục hồi dự án về phiên bản "%(version)s": %(comments)s bình luận, '
        '%(tokens)s token.'
    ) % {
        'version': version.version,
        'comments': stats['comments'],
        'tokens': stats['tokens'],
    })
    if backup_name:
        messages.info(request, _(
            'Trạng thái trước khi phục hồi đã được chốt thành phiên bản "%(name)s".'
        ) % {'name': backup_name})
    if stats['untouched']:
        messages.info(request, _(
            '%(n)s bình luận được thêm sau khi chốt phiên bản nên được giữ nguyên.'
        ) % {'n': stats['untouched']})
    if stats['missing_labels']:
        messages.warning(request, _(
            'Các nhãn sau không còn trong dự án nên đã bị bỏ qua: %(labels)s'
        ) % {'labels': ', '.join(stats['missing_labels'])})

    return redirect('comments:project_versions', project_id=project_id)


@login_required
@require_POST
def dataset_version_delete(request, project_id, version_id):
    """
    Xoá một phiên bản: bản ghi và cả file trên đĩa.

    Chặn khi dự án khoá, giống xoá nguồn dữ liệu: khoá dự án nghĩa là đóng băng
    bộ dữ liệu, mà mỗi phiên bản là một bản đã công bố của bộ dữ liệu đó.
    """
    project, version = _get_version_or_404(
        request.user, project_id, version_id, require_owner=True
    )
    if project.is_locked:
        messages.error(request, _('Dự án đang bị khoá nên không thể xoá phiên bản.'))
        return redirect('comments:project_versions', project_id=project_id)

    for attribute in ('file_path', 'snapshot_path'):
        path = _version_file(version, attribute)
        if path is not None:
            try:
                path.unlink()
            except OSError as exc:
                logger.warning('Không xoá được file %s: %s', path, exc)

    version_name = version.version
    version.delete()
    messages.success(request, _('Đã xoá phiên bản "%(name)s".') % {'name': version_name})
    return redirect('comments:project_versions', project_id=project_id)


# ---------------------------------------------------------------------------
# Xuất dữ liệu
# ---------------------------------------------------------------------------
@login_required
def project_export(request, project_id):
    project = get_project_or_404(request.user, project_id)
    links = YouTubeLink.objects.filter(project=project)

    if request.method == 'POST':
        export_format = request.POST.get('format', 'json_sentence')
        filter_value = request.POST.get('filter', 'all')
        filter_label = ProjectLabel.objects.filter(
            pk=filter_value, project=project
        ).select_related('label').first() if filter_value != 'all' else None
        filter_name = filter_label.label.name if filter_label else 'all'

        link_id = request.POST.get('link_id')
        youtube_link = None
        if link_id:
            youtube_link = get_object_or_404(YouTubeLink, id=link_id, project=project)

        # Xuất chạy nền: dataset lớn mất vài phút, quá thời gian chờ của trình
        # duyệt và reverse proxy. Bản ghi lịch sử được tạo ngay để người dùng
        # theo dõi tiến độ, file sẽ có link tải khi worker ghi xong.
        record = ExportRecord.objects.create(
            project=project,
            youtube_link=youtube_link,
            export_format=export_format[:30],
            filter_toxicity=(filter_name or 'all')[:100],
            review_filter=(request.POST.get('review', 'all') or 'all')[:20],
            requested_by=request.user,
            status='pending',
            current_step=str(_('Đang xếp hàng')),
        )
        run_export.delay(str(record.id))
        return redirect(
            f"{reverse('comments:project_export', args=[project.id])}?job={record.id}"
        )

    project_labels = [{
        'id': str(pl.id),
        'name': pl.display_name,
        'color': pl.display_color,
        'description': pl.display_description,
    } for pl in ProjectLabel.objects.filter(project=project).select_related('label')]

    from .export_service import format_groups

    return render(request, 'comments/export.html', {
        'project': project,
        'is_owner': is_owner_level(project, request.user),
        'links': links,
        'project_labels': project_labels,
        'format_groups': format_groups(),
        # Lịch sử xuất chính là danh sách file tải về.
        'exports': (
            ExportRecord.objects.filter(project=project)
            .select_related('requested_by', 'youtube_link')[:50]
        ),
        'watch_job': request.GET.get('job', ''),
    })


@login_required
def export_download(request, project_id, export_id):
    """Tải file của một lần xuất đã hoàn tất."""
    project = get_project_or_404(request.user, project_id)
    record = get_object_or_404(
        ExportRecord.objects.select_related('project'),
        id=export_id, project=project,
    )

    path = _job_file(record.file_path)
    if record.status != 'ready' or path is None:
        messages.error(request, _(
            'Không tìm thấy file của lần xuất này. File có thể đã bị xoá khỏi máy chủ.'
        ))
        return redirect('comments:project_export', project_id=project_id)

    return FileResponse(
        open(path, 'rb'), as_attachment=True, filename=record.download_name
    )


def _job_file(raw_path):
    """Đường dẫn file kết quả, đã kiểm tra nằm trong thư mục xuất."""
    if not raw_path:
        return None
    path = Path(raw_path).resolve()
    root = Path(settings.EXPORT_ROOT).resolve()
    if not path.is_file() or root not in path.parents:
        return None
    return path


# ---------------------------------------------------------------------------
# Nguồn dữ liệu
# ---------------------------------------------------------------------------
@login_required
def add_youtube_link(request, project_id):
    # Chỉ chủ dự án mới được thêm link: thao tác này tiêu tốn quota YouTube API
    # của chủ dự án, nên annotator không được phép kích hoạt.
    project = get_project_or_404(request.user, project_id, require_owner=True,
                                 require_unlocked=True)

    if request.method != 'POST':
        return redirect('comments:project_detail', project_id=project.id)

    url = request.POST.get('url', '').strip()
    if not url:
        messages.error(request, _('URL YouTube là bắt buộc.'))
        return redirect('comments:project_detail', project_id=project.id)

    video_id = extract_video_id(url)
    if not video_id:
        messages.error(request, _('URL YouTube không hợp lệ.'))
        return redirect('comments:project_detail', project_id=project.id)

    if YouTubeLink.objects.filter(project=project, video_id=video_id).exists():
        messages.warning(request, _('Video này đã có trong dự án.'))
        return redirect('comments:project_detail', project_id=project.id)

    try:
        video_info = get_video_info(
            video_id, api_key=get_owner_youtube_api_key(project)
        ) or {}
    except Exception as exc:
        logger.warning('Không lấy được thông tin video %s: %s', video_id, exc)
        messages.error(request, _(
            'Không lấy được thông tin video từ YouTube API. '
            'Vui lòng kiểm tra link hoặc YouTube API key trong phần Cài đặt.'
        ))
        return redirect('comments:project_detail', project_id=project.id)

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
    messages.success(request, _('Đã bắt đầu tải bình luận cho "%(title)s".') % {
        'title': video_info.get('title', video_id)
    })
    return redirect('comments:project_detail', project_id=project.id)


@login_required
def import_csv(request, project_id):
    """
    Nhập dữ liệu từ file CSV: nguồn dữ liệu ngoài YouTube (Giai đoạn 4 §29).

    CSV cần có cột 'text'; các cột tuỳ chọn: 'id', 'author', 'label'.
    """
    project = get_project_or_404(request.user, project_id, require_owner=True,
                                 require_unlocked=True)

    if request.method != 'POST':
        return render(request, 'comments/import_csv.html', {
            'project': project,
            'is_owner': True,
            'watch_link': request.GET.get('job', ''),
            'recent_imports': YouTubeLink.objects.filter(
                project=project, kind='csv'
            ).order_by('-added_at')[:10],
        })

    upload = request.FILES.get('file')
    if not upload:
        messages.error(request, _('Vui lòng chọn một file CSV.'))
        return redirect('comments:import_csv', project_id=project.id)

    # Chỉ đọc dòng đầu để kiểm tra cột: file lớn không được nạp hết vào RAM của
    # tiến trình web, và việc đọc thật sự là của worker.
    try:
        header = upload.readline().decode('utf-8-sig')
    except UnicodeDecodeError:
        messages.error(request, _('File phải được mã hoá UTF-8.'))
        return redirect('comments:import_csv', project_id=project.id)

    columns = [c.strip().lower() for c in next(csv.reader(io.StringIO(header)), [])]
    if 'text' not in columns:
        messages.error(request, _('File CSV phải có cột "text".'))
        return redirect('comments:import_csv', project_id=project.id)

    dataset_name = request.POST.get('name', '').strip() or upload.name

    link = YouTubeLink.objects.create(
        project=project,
        kind='csv',
        video_id=f'csv:{timezone.now():%Y%m%d%H%M%S}',
        url='',
        title=dataset_name,
        channel='CSV import',
        status='pending',
    )

    # Ghi file lên volume dùng chung web/worker rồi mới giao việc.
    upload_dir = Path(settings.MEDIA_ROOT) / 'imports'
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored = upload_dir / f'{link.id}.csv'
    upload.seek(0)
    with open(stored, 'wb') as handle:
        for chunk in upload.chunks():
            handle.write(chunk)

    run_csv_import.delay(str(link.id), str(stored))
    messages.info(request, _(
        'Đang nhập dữ liệu ở chế độ nền. Trang sẽ tự cập nhật khi xong.'
    ))
    return redirect(
        f"{reverse('comments:import_csv', args=[project.id])}?job={link.id}"
    )


@login_required
def link_detail(request, link_id):
    link = get_link_or_404(request.user, link_id)
    project = link.project

    project_labels = list(
        ProjectLabel.objects.filter(project=project).select_related('label')
    )

    # Tham số phân trang: sai định dạng thì dùng mặc định thay vì 500.
    try:
        page_number = max(1, int(request.GET.get('page', 1)))
    except (TypeError, ValueError):
        page_number = 1
    try:
        per_page = max(1, min(200, int(request.GET.get('per_page', 50))))
    except (TypeError, ValueError):
        per_page = 50

    filter_status = request.GET.get('filter', 'all')
    search = request.GET.get('q', '').strip()

    queryset = link.comments.select_related(
        'ai_label__label', 'manual_label__label', 'gold_label__label'
    ).prefetch_related('tokens')

    # Bộ lọc "đã/chưa gán" và "theo nhãn" tính theo annotation của chính người
    # đang xem. Nếu dùng manual_label (bản sao dùng chung), annotator sẽ thấy
    # câu người khác đã làm nằm trong nhóm "đã gán" của mình.
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

    if search:
        queryset = queryset.filter(
            Q(text__icontains=search) | Q(author__icontains=search)
        )

    # Paginator của Django: chỉ render cửa sổ trang, không phải hàng nghìn link.
    paginator = Paginator(queryset, per_page)
    page_obj = paginator.get_page(page_number)

    counters = link_counters(link, user=request.user)
    label_stats = label_stats_for_link(project_labels, link, user=request.user)
    ai_review = ai_review_service.ai_review_stats(link=link)

    # Góc nhìn cá nhân: mỗi người thấy đúng nhãn mình đã gán, không phải nhãn
    # thủ công gần nhất của cả nhóm. Gán ngược lại vào page_obj để template
    # dùng chính danh sách đã được đính `my_label`/`my_tokens`.
    page_obj.object_list = annotation_service.attach_my_annotations(
        page_obj.object_list, request.user
    )

    project_labels_data = [{
        'id': str(pl.id),
        'name': pl.display_name,
        'color': pl.display_color,
        'description': pl.display_description,
    } for pl in project_labels]

    comment_manual_labels = {
        str(comment.id): comment.my_label
        for comment in page_obj.object_list
        if comment.my_label
    }

    return render(request, 'comments/link_detail.html', {
        'link': link,
        'project': project,
        'comments': page_obj.object_list,
        'page_obj': page_obj,
        'paginator': paginator,
        'page': page_obj.number,
        'per_page': per_page,
        'total_pages': paginator.num_pages,
        'total_comments': counters['total_comments'],
        'annotated_comments': counters['annotated_comments'],
        'unannotated_count': counters['unannotated_count'],
        'manual_pending_count': counters['manual_pending_count'],
        'ai_review': ai_review,
        'filter_status': filter_status,
        'search_query': search,
        'latest_fetch_progress': get_effective_task_progress(str(link.id), 'fetching'),
        'latest_annotate_progress': get_effective_task_progress(str(link.id), 'annotating'),
        'fetch_running': TaskProgress.objects.filter(
            youtube_link=link, task_type='fetching', status='running'
        ).exists(),
        'annotate_running': TaskProgress.objects.filter(
            youtube_link=link, task_type='annotating', status='running'
        ).exists(),
        'is_owner': is_owner_level(project, request.user),
        'is_real_owner': project.owner_id == request.user.id,
        'project_labels_data': project_labels_data,
        'comment_manual_labels': comment_manual_labels,
        'label_stats': label_stats,
    })


@login_required
def annotate_workspace(request, link_id):
    """
    Màn hình gán nhãn tập trung (focus mode).

    Mỗi lần một comment, điều hướng bằng bàn phím, tải sẵn hàng đợi qua API.
    """
    link = get_link_or_404(request.user, link_id)
    project = link.project

    project_labels = [{
        'id': str(pl.id),
        'name': pl.display_name,
        'color': pl.display_color,
        'description': pl.display_description,
    } for pl in ProjectLabel.objects.filter(project=project).select_related('label')]

    return render(request, 'comments/annotate_workspace.html', {
        'link': link,
        'project': project,
        'project_labels': project_labels,
        'project_labels_json': json.dumps(project_labels, ensure_ascii=False),
        'counters': link_counters(link),
    })


@login_required
@require_POST
def delete_youtube_link(request, link_id):
    # get_link_or_404 phải chạy trước khi đọc project_id: link không tồn tại
    # thì không có gì để redirect về.
    link = get_link_or_404(request.user, link_id, require_owner=True,
                           require_unlocked=True)
    project_id = link.project_id

    cancel_tasks_for_link_now(str(link.id))
    link.delete()
    messages.success(request, _('Đã xoá nguồn dữ liệu và toàn bộ dữ liệu liên quan.'))
    return redirect('comments:project_detail', project_id=project_id)


# ---------------------------------------------------------------------------
# SSE tiến độ
# ---------------------------------------------------------------------------
@login_required
def progress_event_stream(request, link_id):
    """
    Server-Sent Events cho tiến độ realtime.

    Stream đóng ngay khi không còn task nào chạy, thay vì giữ kết nối đủ 600
    giây: mỗi kết nối chiếm một worker gevent. Nhịp poll giãn dần 1s -> 5s để
    task chạy dài không nện liên tục vào DB.
    """
    link = get_link_or_404(request.user, link_id)
    link_id_str = str(link.id)

    def event_stream():
        yield 'retry: 3000\n\n'
        last_fetch_state = object()
        last_annotate_state = object()
        start_time = time.time()
        max_wait = 600
        idle_rounds = 0
        interval = 1.0

        def state_of(record):
            if not record:
                return ('pending', 0, '', 0, 0)
            return (
                record.status,
                record.progress_percent,
                record.current_step or '',
                record.total_items,
                record.processed_items,
            )

        while time.time() - start_time < max_wait:
            close_old_connections()
            fetch_progress = get_effective_task_progress(link_id_str, 'fetching')
            annotate_progress = get_effective_task_progress(link_id_str, 'annotating')

            changed = False
            current = state_of(fetch_progress)
            if current != last_fetch_state:
                yield f'data: {json.dumps(_serialize_task_progress(fetch_progress, "fetch"))}\n\n'
                last_fetch_state = current
                changed = True

            current = state_of(annotate_progress)
            if current != last_annotate_state:
                yield f'data: {json.dumps(_serialize_task_progress(annotate_progress, "annotate"))}\n\n'
                last_annotate_state = current
                changed = True

            running = TaskProgress.objects.filter(
                youtube_link_id=link_id_str, status='running'
            ).exists()

            yield ': keepalive\n\n'

            if not running:
                # Không còn tác vụ nào chạy: đóng stream ngay thay vì giữ kết
                # nối rỗng suốt 10 phút (mỗi kết nối chiếm một worker).
                yield 'event: done\ndata: {"status": "idle"}\n\n'
                return

            # Giãn nhịp poll khi không có thay đổi -> giảm tải DB.
            idle_rounds = 0 if changed else idle_rounds + 1
            interval = 1.0 if idle_rounds < 5 else min(5.0, interval * 1.5)
            time.sleep(interval)

        yield 'event: done\ndata: {"status": "timeout"}\n\n'

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response
