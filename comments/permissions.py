"""
Tầng phân quyền dùng chung cho web view, REST API và Celery task.

Nguyên tắc: mọi truy cập vào Project / YouTubeLink / Comment / Token đều phải đi
qua một trong các hàm ở đây. Không view nào được tự ý `get_object_or_404(Project, id=...)`.

Vai trò:
- owner       : người sở hữu thật (Project.owner). Toàn quyền.
- admin       : superuser trên dự án của người khác. Có quyền can thiệp để hỗ
                trợ/khắc phục sự cố, nhưng không phải chủ sở hữu.
- annotator   : thành viên tham gia, chỉ được xem và gán nhãn.
- (không có)  : 404, không phải 403, để tránh lộ sự tồn tại của tài nguyên.

Phân biệt "sở hữu" với "có quyền". Superuser có quyền trên mọi dự án nhưng
không sở hữu chúng. Gộp hai khái niệm này lại thì giao diện liệt kê toàn bộ dự
án của người khác vào mục "dự án của tôi" và hiển thị nút Xoá như thể đó là dự
án của mình, rất dễ dẫn tới xoá nhầm dữ liệu người khác. Vì vậy
owned_projects() không có ngoại lệ cho superuser.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import permissions

from .models import Comment, Project, YouTubeLink

ROLE_OWNER = 'owner'
ROLE_ADMIN = 'admin'
ROLE_ANNOTATOR = 'annotator'

# Các vai trò được phép thực hiện hành động cấp chủ sở hữu.
OWNER_LEVEL_ROLES = (ROLE_OWNER, ROLE_ADMIN)


class ProjectLocked(PermissionDenied):
    """Dự án đã bị khoá: không cho phép thao tác ghi."""

    def __init__(self, message='Dự án đã bị khoá, không thể chỉnh sửa.'):
        super().__init__(message)


# ---------------------------------------------------------------------------
# Truy vấn có lọc quyền
# ---------------------------------------------------------------------------
def visible_projects(user):
    """QuerySet các dự án mà user được phép nhìn thấy (sở hữu hoặc tham gia)."""
    if not user or not user.is_authenticated:
        return Project.objects.none()
    if user.is_superuser:
        return Project.objects.all()
    return Project.objects.filter(
        Q(owner=user) | Q(participants=user)
    ).distinct()


def owned_projects(user):
    """
    QuerySet các dự án mà user thực sự sở hữu.

    Không có ngoại lệ cho superuser: sở hữu là quan hệ dữ liệu (Project.owner),
    không phải mức quyền. Dùng admin_projects() nếu cần các dự án mà superuser
    có quyền quản trị.
    """
    if not user or not user.is_authenticated:
        return Project.objects.none()
    return Project.objects.filter(owner=user)


def admin_projects(user):
    """
    Dự án mà superuser có quyền quản trị nhưng không sở hữu.

    Rỗng với người dùng thường.
    """
    if not user or not user.is_authenticated or not user.is_superuser:
        return Project.objects.none()
    return Project.objects.exclude(owner=user)


def visible_links(user):
    """QuerySet các YouTubeLink thuộc dự án mà user được phép nhìn thấy."""
    if not user or not user.is_authenticated:
        return YouTubeLink.objects.none()
    if user.is_superuser:
        return YouTubeLink.objects.all()
    return YouTubeLink.objects.filter(
        Q(project__owner=user) | Q(project__participants=user)
    ).distinct()


def visible_comments(user):
    """QuerySet các Comment thuộc dự án mà user được phép nhìn thấy."""
    if not user or not user.is_authenticated:
        return Comment.objects.none()
    if user.is_superuser:
        return Comment.objects.all()
    return Comment.objects.filter(
        Q(youtube_link__project__owner=user)
        | Q(youtube_link__project__participants=user)
    ).distinct()


# ---------------------------------------------------------------------------
# Xác định vai trò
# ---------------------------------------------------------------------------
def role_for(project: Project, user) -> str | None:
    """
    Vai trò của user trên dự án: 'owner', 'admin', 'annotator' hoặc None.

    Superuser trên dự án của người khác nhận 'admin', không phải 'owner', để
    giao diện nói đúng sự thật về quyền sở hữu.
    """
    if not user or not user.is_authenticated:
        return None
    if project.owner_id == user.id:
        return ROLE_OWNER
    if project.participants.filter(pk=user.pk).exists():
        return ROLE_ANNOTATOR
    if user.is_superuser:
        return ROLE_ADMIN
    return None


def is_owner_level(project: Project, user) -> bool:
    """True nếu user được phép thực hiện hành động cấp chủ sở hữu."""
    return role_for(project, user) in OWNER_LEVEL_ROLES


# ---------------------------------------------------------------------------
# Lấy đối tượng kèm kiểm tra quyền (dùng chung cho web view + API)
# ---------------------------------------------------------------------------
def get_project_or_404(user, project_id, *, require_owner=False, require_unlocked=False) -> Project:
    """
    Lấy Project mà user có quyền truy cập.

    - Không có quyền xem  -> Http404 (không lộ sự tồn tại).
    - Cần owner mà chỉ là annotator -> PermissionDenied (403).
    - require_unlocked và dự án đang khoá -> ProjectLocked (403).
    """
    if isinstance(project_id, Project):
        project = project_id
    else:
        project = get_object_or_404(Project, id=project_id)

    role = role_for(project, user)
    if role is None:
        raise Http404('Project not found')
    if require_owner and role not in OWNER_LEVEL_ROLES:
        raise PermissionDenied(
            'Chỉ chủ sở hữu dự án mới có thể thực hiện hành động này.'
        )
    if require_unlocked and project.is_locked:
        raise ProjectLocked()
    return project


def get_link_or_404(user, link_id, *, require_owner=False, require_unlocked=False) -> YouTubeLink:
    """Lấy YouTubeLink kèm kiểm tra quyền trên dự án cha."""
    if isinstance(link_id, YouTubeLink):
        link = link_id
    else:
        link = get_object_or_404(
            YouTubeLink.objects.select_related('project'), id=link_id
        )
    get_project_or_404(
        user,
        link.project,
        require_owner=require_owner,
        require_unlocked=require_unlocked,
    )
    return link


def get_comment_or_404(user, comment_id, *, require_owner=False, require_unlocked=False) -> Comment:
    """Lấy Comment kèm kiểm tra quyền trên dự án cha."""
    if isinstance(comment_id, Comment):
        comment = comment_id
    else:
        comment = get_object_or_404(
            Comment.objects.select_related('youtube_link__project'), id=comment_id
        )
    get_project_or_404(
        user,
        comment.youtube_link.project,
        require_owner=require_owner,
        require_unlocked=require_unlocked,
    )
    return comment


# ---------------------------------------------------------------------------
# Permission class cho Django REST Framework
# ---------------------------------------------------------------------------
class IsProjectMember(permissions.BasePermission):
    """Cho phép owner hoặc annotator của dự án."""

    message = 'Bạn không có quyền truy cập dự án này.'

    def has_object_permission(self, request, view, obj):
        project = _project_of(obj)
        return project is not None and role_for(project, request.user) is not None


class IsProjectOwner(permissions.BasePermission):
    """Cho phép chủ sở hữu dự án (và superuser với vai trò quản trị)."""

    message = 'Chỉ chủ sở hữu dự án mới có thể thực hiện hành động này.'

    def has_object_permission(self, request, view, obj):
        project = _project_of(obj)
        return project is not None and is_owner_level(project, request.user)


class IsProjectOwnerOrReadOnly(permissions.BasePermission):
    """Annotator được đọc; chỉ owner được ghi."""

    message = 'Chỉ chủ sở hữu dự án mới có thể thay đổi dữ liệu này.'

    def has_object_permission(self, request, view, obj):
        project = _project_of(obj)
        if project is None:
            return False
        role = role_for(project, request.user)
        if role is None:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        return role in OWNER_LEVEL_ROLES


def _project_of(obj) -> Project | None:
    """Suy ra Project từ một đối tượng bất kỳ trong domain."""
    if isinstance(obj, Project):
        return obj
    if isinstance(obj, YouTubeLink):
        return obj.project
    if isinstance(obj, Comment):
        return obj.youtube_link.project
    project = getattr(obj, 'project', None)
    if isinstance(project, Project):
        return project
    comment = getattr(obj, 'comment', None)
    if isinstance(comment, Comment):
        return comment.youtube_link.project
    return None
