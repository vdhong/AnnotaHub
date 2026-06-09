"""
Web views for AnnotaHub - Project management, comment viewing, annotation editing
"""
import json
import logging
import time
import redis
from django.shortcuts import render, get_object_or_404, redirect
from django.conf import settings
from django.db import connection, close_old_connections
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse, HttpResponseForbidden
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Count
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils.translation import gettext_lazy as _

from .models import Project, YouTubeLink, Comment, Token, TaskProgress, Label, ProjectLabel, UserSettings, UserInvitation, EmailVerification
from django.contrib.auth.models import User
from .services.youtube_service import extract_video_id, get_video_info
from .tasks import (
    fetch_comments_task, cancel_tasks_for_link_now, annotate_comments_task,
    enqueue_fetch_comments_task, enqueue_annotation_task,
    reannotate_all_comments, get_effective_task_progress,
    clear_link_data_for_refetch,
)
from .services.invitation_service import send_invitation_email
from .services.email_verification_service import send_verification_email
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.contrib.auth.forms import AuthenticationForm
from .export_service import generate_export

logger = logging.getLogger(__name__)


# ========================
# Authentication Views (Login/Logout)
# ========================

def custom_login(request):
    """
    Custom login view that works for all users (staff and non-staff).
    Django's admin login redirect fails for non-staff users.
    """
    # If user is already logged in, redirect to dashboard
    if request.user.is_authenticated:
        return redirect('comments:project_list')

    # Get the next URL from GET params (added by @login_required decorator)
    next_url = request.GET.get('next', '')

    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            auth_login(request, user)
            
            # Redirect to next URL if specified, otherwise to dashboard
            next_url = request.POST.get('next', request.GET.get('next', ''))
            if next_url:
                return redirect(next_url)
            return redirect('comments:project_list')
        else:
            messages.error(request, 'Tên đăng nhập hoặc mật khẩu không đúng.')
    else:
        form = AuthenticationForm()

    return render(request, 'comments/login.html', {'form': form, 'next': next_url})


def custom_logout(request):
    """Custom logout view."""
    auth_logout(request)
    messages.info(request, 'Bạn đã đăng xuất thành công.')
    return redirect('comments:login')


# ========================
# Registration & Email Verification Views
# ========================

def register(request):
    """
    User registration view.
    Creates a new user account with username, full name, email and password.
    Sends verification email. User must verify email before logging in.
    """
    # If user is already logged in, redirect to dashboard
    if request.user.is_authenticated:
        return redirect('comments:project_list')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        email = request.POST.get('email', '').strip().lower()
        password = request.POST.get('password', '')
        password_confirm = request.POST.get('password_confirm', '')

        # Validation errors
        errors = []

        # Validate username
        if not username:
            errors.append('Tên đăng nhập là bắt buộc.')
        elif len(username) < 3:
            errors.append('Tên đăng nhập phải có ít nhất 3 ký tự.')
        elif len(username) > 150:
            errors.append('Tên đăng nhập phải có tối đa 150 ký tự.')
        elif User.objects.filter(username=username).exists():
            errors.append(f'Tên đăng nhập "{username}" đã được sử dụng.')

        # Validate full name
        full_name = f"{first_name} {last_name}".strip()
        if not full_name:
            errors.append('Họ và tên là bắt buộc.')

        # Validate email
        if not email:
            errors.append('Địa chỉ email là bắt buộc.')
        elif User.objects.filter(email=email).exists():
            errors.append(f'Địa chỉ email "{email}" đã được đăng ký.')

        # Validate password
        if not password:
            errors.append('Mật khẩu là bắt buộc.')
        elif len(password) < 8:
            errors.append('Mật khẩu phải có ít nhất 8 ký tự.')
        elif password != password_confirm:
            errors.append('Hai mật khẩu nhập lại không khớp.')

        if errors:
            for error in errors:
                messages.error(request, error)
            return render(request, 'comments/register.html', {
                'username': username,
                'first_name': first_name,
                'last_name': last_name,
                'email': email,
            })

        # Create user (inactive until email verified)
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password
        )
        user.first_name = first_name
        user.last_name = last_name
        user.is_active = False  # User cannot login until email is verified
        user.save()

        # Create email verification token
        verification = EmailVerification.objects.create(user=user)

        # Send verification email
        email_sent = send_verification_email(email, verification.token)

        if email_sent:
            messages.success(
                request,
                f'Dăng ký tài khoản thành công! Vui lòng kiểm tra hộp thư {email} '
                'để xác thực địa chỉ email. Liên kết xác thực sẽ hết hạn sau 7 ngày.'
            )
        else:
            messages.warning(
                request,
                f'Dăng ký tài khoản thành công nhưng không thể gửi email xác thực. '
                f'Vui lòng nhấn nút "Gửi lại email" bên dưới hoặc liên hệ quản trị viên.'
            )

        return render(request, 'comments/verification_sent.html', {
            'email': email,
            'user': user,
        })

    return render(request, 'comments/register.html', {
        'username': '',
        'first_name': '',
        'last_name': '',
        'email': '',
    })


def verify_email(request, token):
    """
    Handle email verification link click.
    - Validates the verification token
    - Activates the user account
    - Auto-logs in the user
    - Redirects to dashboard
    """
    try:
        verification = EmailVerification.objects.get(token=token)
    except EmailVerification.DoesNotExist:
        messages.error(request, 'Liên kết xác thực không hợp lệ.')
        return redirect('comments:login')

    # Check if already verified
    if verification.is_verified:
        messages.info(request, 'Email đã được xác thực. Vui lòng đăng nhập.')
        return redirect('comments:login')

    # Check if expired
    if verification.is_expired():
        messages.error(request, 'Liên kết xác thực đã hết hạn. Vui lòng đăng ký lại hoặc liên hệ quản trị viên.')
        return redirect('comments:register')

    user = verification.user

    # Activate user and mark verification as done
    user.is_active = True
    user.save()
    verification.is_verified = True
    verification.save()

    # Auto-login the user
    auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')

    messages.success(request, 'Xác thực email thành công! Chào mừng bạn đến với AnnotaHub.')
    return redirect('comments:project_list')


def resend_verification(request):
    """
    Resend verification email.
    """
    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()

        if not email:
            messages.error(request, 'Vui lòng nhập địa chỉ email.')
            return redirect('comments:register')

        # Find user with unverified email
        user = User.objects.filter(email=email).first()

        if not user:
            messages.error(request, f'Không tìm thấy tài khoản với email "{email}".')
            return redirect('comments:register')

        if user.is_active:
            messages.info(request, f'Tài khoản với email "{email}" đã được xác thực. Vui lòng đăng nhập.')
            return redirect('comments:login')

        # Get or create verification
        try:
            verification = user.email_verification
        except EmailVerification.DoesNotExist:
            verification = EmailVerification.objects.create(user=user)

        # Refresh expiry
        verification.expires_at = timezone.now() + timezone.timedelta(days=7)
        verification.save()

        # Send verification email
        email_sent = send_verification_email(email, verification.token)

        if email_sent:
            messages.success(request, f'Email xác thực đã được gửi lại đến {email}.')
        else:
            messages.error(request, 'Không thể gửi email xác thực. Vui lòng thử lại sau hoặc liên hệ quản trị viên.')

        return redirect('comments:register')

    return redirect('comments:register')


# ========================
# Invitation Views
# ========================

def accept_invitation(request, token):
    """
    Handle invitation link click.
    - Validates the invitation token
    - Activates the user account
    - Auto-logs in the user
    - Adds user to project participants
    - Redirects to user edit page (forced profile completion)
    """
    try:
        invitation = UserInvitation.objects.get(token=token)
    except UserInvitation.DoesNotExist:
        messages.error(request, 'Liên kết mời không hợp lệ hoặc đã hết hạn.')
        return redirect('comments:project_list')
    
    # Check if invitation is already used
    if invitation.is_used:
        messages.error(request, 'Liên kết mời này đã được sử dụng.')
        return redirect('comments:project_list')
    
    # Check if invitation is expired
    if invitation.is_expired():
        messages.error(request, 'Liên kết mời đã hết hạn. Vui lòng liên hệ chủ dự án để gửi lại lời mời.')
        return redirect('comments:project_list')
    
    # Check if user exists
    if not invitation.user:
        messages.error(request, 'Không tìm thấy tài khoản liên kết với lời mời này.')
        return redirect('comments:project_list')
    
    user = invitation.user
    
    if request.method == 'POST':
        # Handle form submission - update user profile
        username = request.POST.get('username', '').strip()
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        password = request.POST.get('password', '')
        password_confirm = request.POST.get('password_confirm', '')
        
        # Validate username
        if not username:
            messages.error(request, 'Tên đăng nhập là bắt buộc.')
        elif User.objects.filter(username=username).exclude(pk=user.pk).exists():
            messages.error(request, f'Tên đăng nhập "{username}" đã được sử dụng.')
        # Validate password
        elif not password:
            messages.error(request, 'Mật khẩu là bắt buộc.')
        elif password != password_confirm:
            messages.error(request, 'Hai mật khẩu không khớp.')
        elif len(password) < 8:
            messages.error(request, 'Mật khẩu phải có ít nhất 8 ký tự.')
        else:
            # Update user profile
            user.username = username
            user.first_name = first_name
            user.last_name = last_name
            user.set_password(password)
            user.is_active = True
            user.save()
            
            # Mark invitation as used
            invitation.is_used = True
            invitation.save()
            
            # Add user to project participants
            project = invitation.project
            if not project.participants.filter(pk=user.pk).exists():
                project.participants.add(user)
            
            # Auto-login the user
            auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            
            messages.success(request, 'Tài khoản đã được kích hoạt thành công! Bạn đã được thêm vào dự án.')
            return redirect('comments:project_list')
    
    # GET request - show the profile completion form
    return render(request, 'comments/accept_invitation.html', {
        'invitation': invitation,
        'user': user,
        'project_name': invitation.project.name,
        'inviter_name': invitation.inviter.get_full_name() or invitation.inviter.username,
    })


# ========================
# Permission Helper
# ========================

def check_project_access(request, project_id, require_owner=False):
    """
    Check if the current user has access to the project.
    - Owner: full access
    - Participant: label-only access (can view, can label)
    Returns (project, is_owner, is_participant) tuple or redirects with error.
    """
    project = get_object_or_404(Project, id=project_id)

    if project.is_owner(request.user):
        return (project, True, False)
    elif project.is_participant(request.user):
        if require_owner:
            messages.error(request, 'Bạn không có quyền thực hiện hành động này. Chỉ chủ sở hữu dự án mới có thể thực hiện.')
            return redirect('comments:project_list')
        return (project, False, True)
    else:
        messages.error(request, 'Bạn không có quyền truy cập dự án này.')
        return redirect('comments:project_list')


# ========================
# User Settings View
# ========================

@login_required
def user_settings(request):
    """View and edit current user's API settings."""
    user = request.user

    # Get or create settings for this user
    settings_obj, created = UserSettings.objects.get_or_create(user=user)

    if request.method == 'POST':
        # YouTube API Key
        youtube_api_key = request.POST.get('youtube_api_key', '').strip()
        # Ollama config
        ollama_base_url = request.POST.get('ollama_base_url', '').strip()
        ollama_api_key = request.POST.get('ollama_api_key', '').strip()
        ollama_model = request.POST.get('ollama_model', '').strip()

        settings_obj.youtube_api_key = youtube_api_key if youtube_api_key else ''
        settings_obj.ollama_base_url = ollama_base_url if ollama_base_url else ''
        settings_obj.ollama_api_key = ollama_api_key if ollama_api_key else ''
        settings_obj.ollama_model = ollama_model if ollama_model else ''
        settings_obj.save()

        messages.success(request, 'Cài đặt đã được lưu thành công.')
        return redirect('comments:user_settings')

    # Show global defaults for reference
    from django.conf import settings as django_settings
    return render(request, 'comments/user_settings.html', {
        'user_settings': settings_obj,
        'global_youtube_api_key': bool(django_settings.YOUTUBE_API_KEY),
        'global_ollama_base_url': django_settings.OLLAMA_BASE_URL,
        'global_ollama_model': django_settings.OLLAMA_MODEL,
        'global_has_ollama_api_key': bool(django_settings.OLLAMA_API_KEY),
    })


# ========================
# Label Management Views
# ========================

@login_required
def label_list(request):
    """List all labels owned by the current user."""
    labels = Label.objects.filter(owner=request.user).prefetch_related('projectlabels').order_by('name')
    # Add usage info for each label
    for label in labels:
        label.usage_count_val = label.usage_count()
        label.is_used = label.is_in_use()
    return render(request, 'comments/label_list.html', {'labels': labels})


@login_required
def label_create(request):
    """Create a new label owned by the current user."""
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        color = request.POST.get('color', '#FF0000').strip()

        if not name:
            messages.error(request, 'Tên nhãn là bắt buộc.')
            return redirect('comments:label_list')

        if Label.objects.filter(owner=request.user, name=name).exists():
            messages.error(request, f'Bạn đã có nhãn "{name}".')
            return redirect('comments:label_list')

        Label.objects.create(
            owner=request.user,
            name=name,
            description=description,
            color=color
        )
        messages.success(request, f'Nhãn "{name}" đã được tạo.')
        return redirect('comments:label_list')

    return render(request, 'comments/label_form.html', {'action': 'Create'})


@login_required
def label_edit(request, label_id):
    """Edit an existing label. Only the owner can edit."""
    label = get_object_or_404(Label, id=label_id)
    
    # Only owner can edit
    if label.owner != request.user:
        messages.error(request, 'Bạn không có quyền chỉnh sửa nhãn này.')
        return redirect('comments:label_list')

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        color = request.POST.get('color', '#FF0000').strip()
        is_active = request.POST.get('is_active') == 'on'

        if not name:
            messages.error(request, 'Tên nhãn là bắt buộc.')
        elif Label.objects.filter(owner=request.user, name=name).exclude(pk=label.pk).exists():
            messages.error(request, f'Bạn đã có nhãn "{name}".')
        else:
            label.name = name
            label.description = description
            label.color = color
            label.is_active = is_active
            label.save()
            messages.success(request, f'Nhãn "{name}" đã được cập nhật.')
            return redirect('comments:label_list')

    return render(request, 'comments/label_form.html', {'label': label, 'action': 'Edit'})


@login_required
@require_POST
def label_delete(request, label_id):
    """Delete a label. Only the owner can delete, and only if not in use."""
    label = get_object_or_404(Label, id=label_id)
    
    # Only owner can delete
    if label.owner != request.user:
        messages.error(request, 'Bạn không có quyền xoá nhãn này.')
        return redirect('comments:label_list')
    
    # Cannot delete if label is in use
    if label.is_in_use():
        messages.error(request, f'Không thể xoá nhãn "{label.name}" vì đang được sử dụng trong các bình luận hoặc token.')
        return redirect('comments:label_list')
    
    label_name = label.name
    label.delete()
    messages.success(request, f'Nhãn "{label_name}" đã được xoá.')
    return redirect('comments:label_list')


# ========================
# Project Label Settings
# ========================

@login_required
def project_labels_settings(request, project_id):
    """
    View and manage labels for a project.
    Only the project owner can manage labels.
    Displays labels owned by the project owner that can be added/removed.
    """
    project = get_object_or_404(Project, id=project_id)
    
    # Only project owner can manage labels
    if project.owner != request.user:
        messages.error(request, 'Chỉ chủ sở hữu dự án mới có thể quản lý nhãn.')
        return redirect('comments:project_detail', project_id=project.id)

    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'add_label':
            label_id = request.POST.get('label_id')
            if label_id:
                # Only allow adding labels owned by the project owner
                label = get_object_or_404(
                    Label, id=label_id, owner=project.owner
                )
                ProjectLabel.objects.get_or_create(project=project, label=label)
                messages.success(request, f'Đã thêm nhãn "{label.name}" vào dự án.')

        elif action == 'remove_label':
            project_label_id = request.POST.get('project_label_id')
            if project_label_id:
                pl = get_object_or_404(ProjectLabel, id=project_label_id, project=project)
                pl.delete()
                messages.success(request, 'Đã bỏ nhãn khỏi dự án.')

        elif action == 'update_override':
            project_label_id = request.POST.get('project_label_id')
            if project_label_id:
                pl = get_object_or_404(ProjectLabel, id=project_label_id, project=project)
                pl.override_name = request.POST.get('override_name') or None
                pl.override_description = request.POST.get('override_description') or None
                pl.override_color = request.POST.get('override_color') or None
                pl.save()
                messages.success(request, 'Đã cập nhật nhãn.')

        elif action == 'add_custom_label':
            name = request.POST.get('custom_name', '').strip()
            description = request.POST.get('custom_description', '').strip()
            color = request.POST.get('custom_color', '#FF0000').strip()
            if name:
                # Create label owned by the project owner
                label, created = Label.objects.get_or_create(
                    owner=project.owner,
                    name=name,
                    defaults={'description': description, 'color': color}
                )
                if created:
                    label.description = description
                    label.color = color
                    label.save()
                ProjectLabel.objects.get_or_create(project=project, label=label)
                messages.success(request, f'Đã thêm nhãn "{name}" vào dự án.')
            else:
                messages.error(request, 'Tên nhãn là bắt buộc.')

        return redirect('comments:project_labels_settings', project_id=project.id)

    # Get project labels with usage info
    project_labels = ProjectLabel.objects.filter(project=project).select_related('label')
    
    # Only show labels owned by the project owner that are NOT yet assigned to this project
    assigned_label_ids = project_labels.values_list('label_id', flat=True)
    available_labels = Label.objects.filter(
        owner=project.owner,
        is_active=True
    ).exclude(
        id__in=assigned_label_ids
    ).order_by('name')

    # Count usage per project label
    for pl in project_labels:
        pl.token_usage = pl.tokens_ai_labeled.count() + pl.tokens_manual_labeled.count()
        pl.comment_usage = pl.comments_ai_labeled.count() + pl.comments_manual_labeled.count()

    return render(request, 'comments/project_labels_settings.html', {
        'project': project,
        'project_labels': project_labels,
        'available_labels': available_labels,
    })


# ========================
# Token/Comment Label Actions (Multi-Label)
# ========================

@login_required
@require_POST
def set_token_labels(request, comment_id, token_position):
    """
    Set manual label for a token.
    Each token has ONE manual_label (user-assigned) that overrides ai_label.
    """
    comment = get_object_or_404(Comment, id=comment_id)
    token = comment.get_or_create_token_for_position(token_position)
    if token is None:
        return JsonResponse({'success': False, 'error': 'Token not found'}, status=404)

    # Parse label ID from request body (JSON)
    import json as json_lib
    body = request.body.decode('utf-8')
    try:
        data = json_lib.loads(body) if body else {}
    except json_lib.JSONDecodeError:
        data = {}

    # Accept either label_id (single) or label_ids (first one used)
    label_id = data.get('label_id') or (data.get('label_ids', [None])[0] if data.get('label_ids') else None)
    project = comment.youtube_link.project

    # Set manual_label (single FK)
    if label_id:
        project_label = ProjectLabel.objects.filter(
            id=label_id,
            project=project
        ).first()
        token.manual_label = project_label
    else:
        token.manual_label = None

    token.annotation_source = 'manual'
    token.save(update_fields=['manual_label', 'annotation_source'])

    # Update comment
    comment.update_comment_label()
    comment.is_meaningful = True
    comment.annotated_at = timezone.now()
    if comment.annotation_source == 'auto':
        comment.annotation_source = 'mixed'
    elif comment.annotation_source is None:
        comment.annotation_source = 'manual'
    comment.save(update_fields=['annotation_source', 'annotated_at', 'is_meaningful'])

    return JsonResponse({
        'success': True,
        'token_text': token.text,
        'is_toxic': token.is_toxic,
        'comment_label': comment.toxicity_label,
        'effective_label': token.effective_label_data,
        'ai_label': token.ai_label_data,
        'manual_label': token.manual_label_data,
    })


@login_required
@require_POST
def set_comment_labels(request, comment_id):
    """
    Set manual label for a comment.
    Each comment has ONE manual_label (user-assigned) that overrides ai_label.
    """
    comment = get_object_or_404(Comment, id=comment_id)

    import json as json_lib
    body = request.body.decode('utf-8')
    try:
        data = json_lib.loads(body) if body else {}
    except json_lib.JSONDecodeError:
        data = {}

    # Accept either label_id (single) or label_ids (first one used)
    label_id = data.get('label_id') or (data.get('label_ids', [None])[0] if data.get('label_ids') else None)
    project = comment.youtube_link.project

    # Set manual_label (single FK)
    if label_id:
        project_label = ProjectLabel.objects.filter(
            id=label_id,
            project=project
        ).first()
        comment.manual_label = project_label
    else:
        comment.manual_label = None

    # Update legacy toxicity_label based on effective label
    comment.update_comment_label()

    comment.annotation_source = 'manual'
    comment.annotated_at = timezone.now()
    comment.is_meaningful = True
    comment.save(update_fields=[
        'manual_label', 'toxicity_label', 'annotation_source', 'annotated_at', 'is_meaningful'
    ])

    return JsonResponse({
        'success': True,
        'label': comment.toxicity_label,
        'effective_label': comment.effective_label_data,
        'ai_label': comment.ai_label_data,
        'manual_label': comment.manual_label_data,
    })


TASK_LABELS = {
    'pending': 'Pending',
    'running': 'Running',
    'completed': 'Completed',
    'failed': 'Failed',
    'cancelled': 'Cancelled',
}


def _serialize_task_progress(progress_record, task_type):
    """Return a consistent JSON-serializable task snapshot."""
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
        'step': progress_record.current_step,
        'total': progress_record.total_items,
        'processed': progress_record.processed_items,
    }


def dashboard(request):
    """Main dashboard showing all projects."""
    projects = Project.objects.all().annotate(
        link_count=Count('youtubelinks', distinct=True),
        comment_count=Count('youtubelinks__comments', distinct=True),
        annotated_count=Count('youtubelinks__comments', filter=Q(youtubelinks__comments__toxicity_label__isnull=False), distinct=True),
    )
    return render(request, 'comments/dashboard.html', {'projects': projects})


def health_check(request):
    """Lightweight health endpoint for container monitoring."""
    components = {'database': {'status': 'unknown'}, 'redis': {'status': 'unknown'}}

    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        components['database'] = {'status': 'ok'}
    except Exception as exc:
        components['database'] = {'status': 'error', 'error': str(exc)}

    try:
        redis_client = redis.Redis.from_url(settings.CELERY_BROKER_URL)
        redis_client.ping()
        components['redis'] = {'status': 'ok'}
    except Exception as exc:
        components['redis'] = {'status': 'error', 'error': str(exc)}

    overall_ok = all(component['status'] == 'ok' for component in components.values())
    return JsonResponse({
        'status': 'ok' if overall_ok else 'degraded',
        'components': components,
        'timestamp': timezone.now().isoformat(),
    }, status=200 if overall_ok else 503)


@login_required
def project_list(request):
    """
    List projects for the current user.
    Shows two sections: owned projects and participated projects.
    """
    # Owned projects (user is the owner)
    owned_projects = Project.objects.filter(owner=request.user).annotate(
        link_count=Count('youtubelinks', distinct=True),
        comment_count=Count('youtubelinks__comments', distinct=True),
    )

    # Participated projects (user is a participant, not owner)
    participated_projects = Project.objects.filter(participants=request.user).annotate(
        link_count=Count('youtubelinks', distinct=True),
        comment_count=Count('youtubelinks__comments', distinct=True),
    )

    return render(request, 'comments/project_list.html', {
        'owned_projects': owned_projects,
        'participated_projects': participated_projects,
    })


@login_required
def project_create(request):
    """Create a new project."""
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()

        if not name:
            messages.error(request, _('Project name is required.'))
            return redirect('comments:project_create')

        # Check for duplicate project name
        if Project.objects.filter(name=name).exists():
            messages.error(request, _('A project with the name "%(name)s" already exists. Please choose a different name.') % {'name': name})
            return redirect('comments:project_create')

        # Set the current user as the owner
        Project.objects.create(
            name=name,
            description=description,
            owner=request.user
        )
        messages.success(request, _('Project "%(name)s" created successfully.') % {'name': name})
        return redirect('comments:project_list')

    return render(request, 'comments/project_form.html', {'action': 'Create'})


@login_required
def project_detail(request, project_id):
    """View project details with all YouTube links."""
    result = check_project_access(request, project_id)
    if not isinstance(result, tuple):
        return result  # redirect

    project, is_owner, is_participant = result
    links = YouTubeLink.objects.filter(project=project).select_related('project')

    # Get latest task progress for each link
    for link in links:
        latest_progress = get_effective_task_progress(str(link.id), 'annotating') or get_effective_task_progress(str(link.id), 'fetching')
        if latest_progress is None:
            latest_progress = TaskProgress.objects.filter(youtube_link=link).order_by('-created_at').first()
        link.latest_progress = latest_progress

    # Get participants list for owner
    participants = []
    if is_owner:
        participants = project.participants.all()

    return render(request, 'comments/project_detail.html', {
        'project': project,
        'links': links,
        'is_owner': is_owner,
        'is_participant': is_participant,
        'participants': participants,
    })


@login_required
def project_edit(request, project_id):
    """Edit a project. Only owner can edit."""
    result = check_project_access(request, project_id, require_owner=True)
    if not isinstance(result, tuple):
        return result  # redirect

    project, is_owner, is_participant = result

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()

        if not name:
            messages.error(request, _('Project name is required.'))
        elif Project.objects.filter(name=name).exclude(pk=project.pk).exists():
            messages.error(request, _('A project with the name "%(name)s" already exists. Please choose a different name.') % {'name': name})
        else:
            project.name = name
            project.description = description
            project.save()
            messages.success(request, _('Project "%(name)s" updated successfully.') % {'name': name})
            return redirect('comments:project_detail', project_id=project.id)

    return render(request, 'comments/project_form.html', {'project': project, 'action': 'Edit'})


@login_required
@require_POST
def project_delete(request, project_id):
    """Delete a project and all its data. Only owner can delete."""
    result = check_project_access(request, project_id, require_owner=True)
    if not isinstance(result, tuple):
        return result  # redirect

    project, is_owner, is_participant = result

    # Cancel all running tasks for all links in this project
    for link in project.youtubelinks.all():
        cancel_tasks_for_link_now(str(link.id))

    project_name = project.name
    project.delete()
    messages.success(request, f'Project "{project_name}" deleted.')
    return redirect('comments:project_list')


@login_required
def project_manage_participants(request, project_id):
    """Manage participants for a project. Only owner can manage participants."""
    result = check_project_access(request, project_id, require_owner=True)
    if not isinstance(result, tuple):
        return result  # redirect

    project, is_owner, is_participant = result

    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'add_participant':
            email = request.POST.get('email', '').strip().lower()
            if email:
                # Try to find existing user by email
                user = User.objects.filter(email=email).first()
                
                if user:
                    # User exists with this email
                    if user == request.user:
                        messages.error(request, 'Ban khong the them chinh minh vao danh sach tham gia.')
                    elif project.participants.filter(pk=user.pk).exists():
                        messages.warning(request, f'User "{user.username}" da la thanh vien tham gia.')
                    elif project.owner == user:
                        messages.warning(request, f'User "{user.username}" la chu so hữu dự án, không cần thêm.')
                    else:
                        project.participants.add(user)
                        messages.success(request, f'User "{user.username}" (email: {email}) da duoc them vao danh sach tham gia.')
                else:
                    # User does not exist - create inactive user and send invitation
                    # Create a new inactive user with this email
                    new_username = email.split('@')[0]
                    # Ensure username is unique
                    base_username = new_username
                    counter = 1
                    while User.objects.filter(username=new_username).exists():
                        new_username = f"{base_username}{counter}"
                        counter += 1
                    
                    new_user = User.objects.create_user(
                        username=new_username,
                        email=email,
                        password=User.objects.make_random_password(length=32)
                    )
                    new_user.is_active = False
                    new_user.save()
                    
                    # Create invitation
                    invitation = UserInvitation.objects.create(
                        email=email,
                        project=project,
                        inviter=request.user,
                        user=new_user,
                    )
                    
                    # Send invitation email
                    email_sent = send_invitation_email(email, invitation.token)
                    
                    if email_sent:
                        messages.success(
                            request, 
                            f'Email mời đã được gửi đến {email}. User sẽ được thêm vào dự án sau khi xác nhận email.'
                        )
                    else:
                        # If email fails, still show the link for manual sharing
                        from django.conf import settings as django_settings
                        site_url = getattr(django_settings, 'SITE_URL', 'http://localhost:8000')
                        invitation_link = f"{site_url}/invite/{invitation.token}/"
                        messages.warning(
                            request,
                            f'Không thể gửi email mời. Vui lòng gửi liên kết này manually: {invitation_link}'
                        )
            else:
                messages.error(request, 'Vui long nhap email.')

        elif action == 'remove_participant':
            user_id = request.POST.get('user_id')
            if user_id:
                user = User.objects.filter(pk=user_id).first()
                if user and project.participants.filter(pk=user.pk).exists():
                    project.participants.remove(user)
                    messages.success(request, f'User "{user.username}" da duoc xoa khoi danh sach tham gia.')
                else:
                    messages.error(request, 'User khong ton tai trong danh sach tham gia.')
            else:
                messages.error(request, 'Thieu user_id.')

        elif action == 'delete_invitation':
            invitation_id = request.POST.get('invitation_id')
            if invitation_id:
                invitation = UserInvitation.objects.filter(
                    id=invitation_id,
                    project=project,
                    is_used=False
                ).first()
                if invitation:
                    email = invitation.email
                    invitation.delete()
                    messages.success(request, f'Da xoa loi moi cho {email}.')
                else:
                    messages.error(request, 'Khong tim thay loi moi hoac da duoc su dung.')
            else:
                messages.error(request, 'Thieu invitation_id.')

        elif action == 'resend_invitation':
            invitation_id = request.POST.get('invitation_id')
            if invitation_id:
                invitation = UserInvitation.objects.filter(
                    id=invitation_id,
                    project=project,
                    is_used=False
                ).first()
                if invitation:
                    # Check if expired, refresh expiry
                    if invitation.is_expired():
                        invitation.expires_at = timezone.now() + timezone.timedelta(days=7)
                        invitation.save()
                    
                    email_sent = send_invitation_email(invitation.email, invitation.token)
                    if email_sent:
                        messages.success(request, f'Email moi da duoi gui lai den {invitation.email}.')
                    else:
                        messages.error(request, f'Khong the gui lai email moi den {invitation.email}.')
                else:
                    messages.error(request, 'Khong tim thay loi moi hoac da duoc su dung.')
            else:
                messages.error(request, 'Thieu invitation_id.')

        return redirect('comments:project_manage_participants', project_id=project.id)

    participants = project.participants.all()
    # Get pending (active, unused, non-expired) invitations for this project
    pending_invitations = UserInvitation.objects.filter(
        project=project,
        is_used=False
    ).select_related('user', 'inviter').order_by('-created_at')
    
    return render(request, 'comments/project_participants.html', {
        'project': project,
        'participants': participants,
        'pending_invitations': pending_invitations,
    })


@login_required
def project_export(request, project_id):
    """Export page for a project."""
    project = get_object_or_404(Project, id=project_id)
    links = YouTubeLink.objects.filter(project=project)

    if request.method == 'POST':
        export_format = request.POST.get('format', 'json_sentence')
        filter_toxicity = request.POST.get('filter', 'all')
        link_id = request.POST.get('link_id')

        if link_id:
            youtube_link = get_object_or_404(YouTubeLink, id=link_id, project=project)
            return generate_export(project, youtube_link, export_format, filter_toxicity)
        else:
            return generate_export(project, None, export_format, filter_toxicity)

    return render(request, 'comments/export.html', {
        'project': project,
        'links': links,
    })


@login_required
def add_youtube_link(request, project_id):
    """Add a YouTube link to a project. Only owner can add links."""
    result = check_project_access(request, project_id, require_owner=True)
    if not isinstance(result, tuple):
        return result  # redirect

    project, is_owner, is_participant = result

    if request.method == 'POST':
        url = request.POST.get('url', '').strip()

        if not url:
            messages.error(request, 'YouTube URL is required.')
            return redirect('comments:project_detail', project_id=project.id)

        video_id = extract_video_id(url)
        if not video_id:
            messages.error(request, 'Invalid YouTube URL. Please provide a valid YouTube video link.')
            return redirect('comments:project_detail', project_id=project.id)

        # Check if already exists
        if YouTubeLink.objects.filter(project=project, video_id=video_id).exists():
            messages.warning(request, 'This video is already in the project.')
            return redirect('comments:project_detail', project_id=project.id)

        # Get owner's YouTube API key (falls back to global settings if not set)
        owner_settings = None
        try:
            owner_settings = UserSettings.objects.get(user=project.owner)
        except UserSettings.DoesNotExist:
            pass
        owner_youtube_api_key = None
        if owner_settings and owner_settings.has_youtube_api_key:
            owner_youtube_api_key = owner_settings.youtube_api_key

        # Get video info using owner's API key
        video_info = get_video_info(video_id, api_key=owner_youtube_api_key) or {}

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

        # Start fetching comments in background
        enqueue_fetch_comments_task(link, 'Starting comment fetch')
        messages.success(request, f'Started fetching comments for "{video_info.get("title", video_id)}".')
        return redirect('comments:project_detail', project_id=project.id)

    return render(request, 'comments/add_link.html', {'project': project})


@login_required
def link_detail(request, link_id):
    """View detailed comments for a YouTube link."""
    import json as json_mod
    
    link = get_object_or_404(YouTubeLink, id=link_id)
    project = link.project

    # Get project labels
    project_labels = ProjectLabel.objects.filter(project=project).select_related('label')

    # Pagination
    page = max(1, int(request.GET.get('page', 1)))
    per_page = max(1, min(200, int(request.GET.get('per_page', 50))))
    filter_status = request.GET.get('filter', 'all')

    comments_query = link.comments.prefetch_related('tokens')

    # Dynamic filtering: support both legacy filters and project label filters
    if filter_status == 'annotated':
        comments_query = comments_query.filter(toxicity_label__isnull=False)
    elif filter_status == 'unannotated':
        comments_query = comments_query.filter(toxicity_label__isnull=True).exclude(is_meaningful=False)
    elif filter_status.startswith('label_'):
        # Filter by project label ID (effective label: manual_label > ai_label)
        label_id = filter_status.replace('label_', '', 1)
        comments_query = comments_query.filter(
            Q(manual_label_id=label_id) | Q(ai_label_id=label_id)
        )

    total = comments_query.count()
    start = (page - 1) * per_page
    comments = comments_query[start:start + per_page]

    # Get latest task progress
    latest_fetch = get_effective_task_progress(str(link.id), 'fetching')
    latest_annotate = get_effective_task_progress(str(link.id), 'annotating')

    # Check if fetch task is running
    fetch_running = TaskProgress.objects.filter(
        youtube_link=link, task_type='fetching', status='running'
    ).exists()

    # Check if annotation task is running
    annotate_running = TaskProgress.objects.filter(
        youtube_link=link, task_type='annotating', status='running'
    ).exists()

    # Count unannotated comments (for "Continue annotation" button)
    unannotated_count = link.comments.filter(toxicity_label__isnull=True).exclude(is_meaningful=False).count()

    # Statistics - dynamic counts per project label
    total_comments = link.comments.count()
    annotated_comments = link.comments.filter(toxicity_label__isnull=False).count()
    toxic_comments = link.comments.filter(toxicity_label='toxic').count()

    # Compute per-label statistics (count comments whose effective label matches)
    label_stats = []
    for pl in project_labels:
        labeled_count = link.comments.filter(
            Q(manual_label_id=pl.id) | Q(ai_label_id=pl.id)
        ).distinct().count()
        label_stats.append({
            'id': str(pl.id),
            'name': pl.display_name,
            'color': pl.display_color,
            'count': labeled_count,
        })

    # Project labels data for JavaScript
    project_labels_data = [{
        'id': str(pl.id),
        'name': pl.display_name,
        'color': pl.display_color,
        'description': pl.display_description,
    } for pl in project_labels]

    # Build comment manual label mapping for JavaScript initialization
    comment_manual_labels = {}
    for comment in comments:
        ml = comment.manual_label_data
        if ml:
            comment_manual_labels[str(comment.id)] = ml
    comment_manual_labels_json = json_mod.dumps(comment_manual_labels)

    return render(request, 'comments/link_detail.html', {
        'link': link,
        'comments': comments,
        'page': page,
        'per_page': per_page,
        'total_pages': (total + per_page - 1) // per_page,
        'pages': range(1, ((total + per_page - 1) // per_page) + 1),
        'total_comments': total_comments,
        'annotated_comments': annotated_comments,
        'toxic_comments': toxic_comments,
        'filter_status': filter_status,
        'latest_fetch_progress': latest_fetch,
        'latest_annotate_progress': latest_annotate,
        'fetch_running': fetch_running,
        'annotate_running': annotate_running,
        'unannotated_count': unannotated_count,
        'project_labels_data': json_mod.dumps(project_labels_data),
        'comment_manual_labels_json': comment_manual_labels_json,
        'label_stats': json_mod.dumps(label_stats),
    })


@login_required
@require_POST
def delete_youtube_link(request, link_id):
    """Delete a YouTube link and all its comments. Only owner can delete."""
    link = get_object_or_404(YouTubeLink, id=link_id)

    # Check owner access
    result = check_project_access(request, link.project.id, require_owner=True)
    if not isinstance(result, tuple):
        return result

    project_id = link.project.id

    # Cancel running tasks
    cancel_tasks_for_link_now(str(link.id))

    link.delete()
    messages.success(request, 'YouTube link and all its data deleted.')
    return redirect('comments:project_detail', project_id=project_id)


@login_required
@require_POST
def reannotate_link(request, link_id):
    """Re-run annotation for all comments in a link. Only owner can reannotate."""
    link = get_object_or_404(YouTubeLink, id=link_id)

    # Check owner access
    result = check_project_access(request, link.project.id, require_owner=True)
    if not isinstance(result, tuple):
        return result

    # Cancel any running annotation task first
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
    messages.info(request, 'Re-annotation started in background. All labels have been reset.')
    return redirect('comments:link_detail', link_id=link.id)


@login_required
@require_POST
def retry_fetch_link(request, link_id):
    """Refetch comments without deleting existing stored comments."""
    link = get_object_or_404(YouTubeLink, id=link_id)

    # Cancel any running tasks for this link first
    cancel_tasks_for_link_now(str(link.id))

    # Reset link status to allow refetching
    link.status = 'pending'
    link.save(update_fields=['status', 'updated_at'])

    # Start fetching comments again without clearing existing data
    enqueue_fetch_comments_task(link, 'Refetching comments without clearing existing data')
    messages.success(
        request,
        f'Refetching comments for "{link.title or link.video_id}". Existing comments will be kept.'
    )
    return redirect('comments:link_detail', link_id=link.id)


@login_required
@require_POST
def clear_and_refetch_link(request, link_id):
    """Clear existing comments and refetch comments from YouTube."""
    link = get_object_or_404(YouTubeLink, id=link_id)

    clear_result = clear_link_data_for_refetch(str(link.id))
    if clear_result.get('status') == 'error':
        messages.error(request, clear_result.get('message', 'Failed to clear link data.'))
        return redirect('comments:link_detail', link_id=link.id)

    enqueue_fetch_comments_task(link, 'Clearing old comments and refetching')
    messages.success(
        request,
        f'Cleared {clear_result.get("deleted_comments", 0)} comments and started refetching for "{link.title or link.video_id}".'
    )
    return redirect('comments:link_detail', link_id=link.id)


@login_required
@require_POST
def stop_fetch_task(request, link_id):
    """Stop the fetch comments task."""
    link = get_object_or_404(YouTubeLink, id=link_id)

    # Cancel running tasks
    cancel_tasks_for_link_now(str(link.id))
    messages.info(request, 'Fetch task has been stopped.')
    return redirect('comments:link_detail', link_id=link.id)


@login_required
@require_POST
def stop_annotation_task(request, link_id):
    """Stop the AI annotation task."""
    link = get_object_or_404(YouTubeLink, id=link_id)

    # Cancel running tasks
    cancel_tasks_for_link_now(str(link.id))
    messages.info(request, 'Annotation task has been stopped.')
    return redirect('comments:link_detail', link_id=link.id)


@login_required
@require_POST
def continue_annotation(request, link_id):
    """Continue annotation for unannotated comments."""
    link = get_object_or_404(YouTubeLink, id=link_id)

    # Check if there are unannotated comments
    unannotated = link.comments.filter(toxicity_label__isnull=True).exclude(is_meaningful=False).count()
    if unannotated == 0:
        messages.info(request, 'No unannotated comments found.')
        return redirect('comments:link_detail', link_id=link.id)

    # Check if annotation is already running
    running_task = get_effective_task_progress(str(link.id), 'annotating')
    if running_task and running_task.status == 'running':
        messages.info(request, 'Annotation task is already running. Progress is being updated.')
        return redirect('comments:link_detail', link_id=link.id)

    # Start annotation task (it will only process unannotated comments)
    enqueue_annotation_task(link, 'Continuing annotation')
    messages.success(request, f'Continuing annotation for {unannotated} unannotated comments.')
    return redirect('comments:link_detail', link_id=link.id)


@login_required
@require_POST
def toggle_token_toxicity(request, comment_id, token_position):
    """
    Toggle token label between toxic-like and O (neutral).
    Sets manual_label on the token (overrides ai_label for display).
    """
    comment = get_object_or_404(Comment, id=comment_id)
    token = comment.get_or_create_token_for_position(token_position)
    if token is None:
        return JsonResponse({'success': False, 'error': 'Token not found'}, status=404)

    project = comment.youtube_link.project

    # Determine current effective state
    currently_toxic = token.is_toxic

    if currently_toxic:
        # Toggle OFF: set manual_label to 'O' / neutral label
        o_label = None
        for pl in ProjectLabel.objects.filter(project=project).select_related('label'):
            if pl.label.name.upper() == 'O' or pl.label.name.lower() == 'non_toxic':
                o_label = pl
                break
        token.manual_label = o_label  # Could be None if no O label exists
    else:
        # Toggle ON: set manual_label to toxic-like label
        toxic_label = None
        for pl in ProjectLabel.objects.filter(project=project).select_related('label'):
            if pl.label.name.lower() in ('toxic', 'offensive', 'abusive', 'hate'):
                toxic_label = pl
                break
        token.manual_label = toxic_label

    token.annotation_source = 'manual'
    token.save(update_fields=['manual_label', 'annotation_source'])

    # Update comment
    comment.update_comment_label()
    comment.is_meaningful = True
    comment.annotated_at = timezone.now()
    if comment.annotation_source == 'auto':
        comment.annotation_source = 'mixed'
    elif comment.annotation_source is None:
        comment.annotation_source = 'manual'
    comment.save(update_fields=['annotation_source', 'annotated_at', 'is_meaningful'])

    return JsonResponse({
        'success': True,
        'token_text': token.text,
        'is_toxic': token.is_toxic,
        'comment_label': comment.toxicity_label,
        'effective_label': token.effective_label_data,
        'ai_label': token.ai_label_data,
        'manual_label': token.manual_label_data,
    })


@login_required
@require_POST
def manual_label_comment(request, comment_id):
    """Manually set the toxicity label for a comment."""
    comment = get_object_or_404(Comment, id=comment_id)
    label = request.POST.get('label', '')

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


def progress_event_stream(request, link_id):
    """Server-Sent Events endpoint for real-time progress updates."""
    link = get_object_or_404(YouTubeLink, id=link_id)

    def event_stream():
        yield "retry: 3000\n\n"
        last_fetch_state = object()
        last_annotate_state = object()
        start_time = time.time()
        max_wait = 600

        def get_progress_state(progress_record):
            if not progress_record:
                return ('pending', 0, '', 0, 0)
            return (
                progress_record.status,
                progress_record.progress_percent,
                progress_record.current_step,
                progress_record.total_items,
                progress_record.processed_items,
            )

        while time.time() - start_time < max_wait:
            close_old_connections()
            fetch_progress = get_effective_task_progress(str(link.id), 'fetching')
            annotate_progress = get_effective_task_progress(str(link.id), 'annotating')

            current_fetch_state = get_progress_state(fetch_progress)
            if current_fetch_state != last_fetch_state:
                data = _serialize_task_progress(fetch_progress, 'fetch')
                yield f"data: {json.dumps(data)}\n\n"
                last_fetch_state = current_fetch_state

            current_annotate_state = get_progress_state(annotate_progress)
            if current_annotate_state != last_annotate_state:
                data = _serialize_task_progress(annotate_progress, 'annotate')
                yield f"data: {json.dumps(data)}\n\n"
                last_annotate_state = current_annotate_state

            fetch_running = TaskProgress.objects.filter(
                youtube_link=link, task_type='fetching', status='running'
            ).exists()
            annotate_running = TaskProgress.objects.filter(
                youtube_link=link, task_type='annotating', status='running'
            ).exists()

            fetch_terminal = bool(fetch_progress and fetch_progress.status in ('completed', 'failed', 'cancelled'))
            annotate_terminal = bool(annotate_progress and annotate_progress.status in ('completed', 'failed', 'cancelled'))

            yield ": keepalive\n\n"

            if fetch_terminal and (
                (annotate_progress and annotate_terminal)
                or (link.status == 'annotated' and not annotate_running)
            ):
                yield "event: done\ndata: {\"status\": \"complete\"}\n\n"
                break

            time.sleep(1)

        yield "event: done\ndata: {\"status\": \"timeout\"}\n\n"

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response
