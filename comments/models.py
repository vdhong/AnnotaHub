import hashlib
import secrets
import uuid
from pathlib import Path

from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .fields import EncryptedCharField, mask_secret


class EmailVerification(models.Model):
    """
    Stores email verification tokens for new user registrations.
    When a user registers, a verification email is sent with a one-time token.
    The user must click the verification link before they can log in.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='email_verification',
        help_text='The user account waiting for email verification'
    )
    token = models.CharField(max_length=64, unique=True, db_index=True)
    is_verified = models.BooleanField(default=False, help_text='Whether this email has been verified')
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(
        help_text='Expiration time for the verification link',
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Verification for {self.user.email} - {'Verified' if self.is_verified else 'Pending'}"

    @staticmethod
    def generate_token():
        """Generate a secure random token."""
        return hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest()

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = self.generate_token()
        if not self.expires_at:
            self.expires_at = timezone.now() + timezone.timedelta(days=7)
        super().save(*args, **kwargs)

    def is_expired(self):
        """Check if this verification has expired."""
        return timezone.now() > self.expires_at


class UserInvitation(models.Model):
    """
    Stores invitation tokens for inviting users to the platform.
    When a project owner adds an email that doesn't belong to an existing user,
    an invitation is created with a one-time token.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(max_length=254, db_index=True)
    token = models.CharField(max_length=64, unique=True, db_index=True)
    project = models.ForeignKey(
        'Project',
        on_delete=models.CASCADE,
        related_name='invitations',
        help_text='The project that triggered this invitation'
    )
    inviter = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='sent_invitations',
        help_text='User who sent the invitation'
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='invitations',
        null=True,
        blank=True,
        help_text='The user account associated with this invitation'
    )
    is_used = models.BooleanField(default=False, help_text='Whether this invitation has been used')
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(
        help_text='Expiration time for the invitation link',
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Invitation for {self.email} - {'Used' if self.is_used else 'Active'}"

    @staticmethod
    def generate_token():
        """Generate a secure random token."""
        return hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest()

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = self.generate_token()
        if not self.expires_at:
            self.expires_at = timezone.now() + timezone.timedelta(days=7)
        super().save(*args, **kwargs)

    def is_expired(self):
        """Check if this invitation has expired."""
        return timezone.now() > self.expires_at


class UserSettings(models.Model):
    """
    Per-user API configuration settings.
    Each user can store their own YouTube API key and Ollama configuration.
    When a project owner uses AI labeling or adds YouTube links, the system
    uses the owner's API keys.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='settings',
        help_text='User who owns these settings'
    )
    # YouTube API Configuration
    # Mã hoá at-rest: bản dump SQL không còn lộ khoá của người dùng.
    youtube_api_key = EncryptedCharField(
        max_length=500,
        blank=True,
        default='',
        help_text='YouTube Data API v3 key for fetching comments'
    )
    # Ollama Configuration
    ollama_base_url = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text='Ollama API base URL (e.g., http://localhost:11434)'
    )
    ollama_api_key = EncryptedCharField(
        max_length=500,
        blank=True,
        default='',
        help_text='Ollama API authentication key'
    )
    ollama_model = models.CharField(
        max_length=200,
        blank=True,
        default='',
        help_text='Ollama model name (e.g., llama3, qwen3.6:27b)'
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"Settings for {self.user.username}"

    @property
    def has_youtube_api_key(self):
        """Check if YouTube API key is configured."""
        return bool(self.youtube_api_key and self.youtube_api_key.strip())

    @property
    def has_ollama_config(self):
        """Check if Ollama is fully configured."""
        return bool(
            self.ollama_base_url and self.ollama_base_url.strip()
            and self.ollama_api_key and self.ollama_api_key.strip()
            and self.ollama_model and self.ollama_model.strip()
        )

    def get_youtube_api_key(self):
        """Get YouTube API key, falling back to project settings if empty."""
        from django.conf import settings
        return self.youtube_api_key.strip() or settings.YOUTUBE_API_KEY or ''

    def get_ollama_base_url(self):
        """Get Ollama base URL, falling back to project settings if empty."""
        from django.conf import settings
        return self.ollama_base_url.strip() or settings.OLLAMA_BASE_URL or ''

    def get_ollama_api_key(self):
        """Get Ollama API key, falling back to project settings if empty."""
        from django.conf import settings
        return self.ollama_api_key.strip() or settings.OLLAMA_API_KEY or ''

    def get_ollama_model(self):
        """Get Ollama model, falling back to project settings if empty."""
        from django.conf import settings
        return self.ollama_model.strip() or settings.OLLAMA_MODEL or ''

    @property
    def youtube_api_key_masked(self):
        return mask_secret(self.youtube_api_key)

    @property
    def ollama_api_key_masked(self):
        return mask_secret(self.ollama_api_key)


class Label(models.Model):
    """
    Label owned by a user, can be assigned to projects.
    Each label has a name, description, display color, and an owner.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='labels',
        help_text='The user who created and owns this label'
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default='', help_text='Description of when to use this label')
    color = models.CharField(
        max_length=7, default='#FF0000',
        help_text='Hex color code for display (e.g., #FF0000)'
    )
    is_active = models.BooleanField(default=True, help_text='Whether this label can be assigned to new projects')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        unique_together = ('owner', 'name')

    def __str__(self):
        return f"{self.name} (by {self.owner.username})"

    def is_in_use(self):
        """True nếu nhãn đang được dùng ở bất kỳ comment hay token nào."""
        pl_ids = list(self.projectlabels.values_list('id', flat=True))
        if not pl_ids:
            return False
        label_filter = Q(ai_label_id__in=pl_ids) | Q(manual_label_id__in=pl_ids) | Q(gold_label_id__in=pl_ids)
        return (
            Comment.objects.filter(label_filter).exists()
            or Token.objects.filter(label_filter).exists()
        )

    def usage_count(self):
        """Số lần nhãn này được dùng trên toàn hệ thống."""
        return type(self).usage_counts_for([self]).get(self.id, 0)

    @classmethod
    def usage_counts_for(cls, labels):
        """
        Đếm mức sử dụng cho nhiều nhãn trong 2 truy vấn, thay vì 4-6 truy vấn
        COUNT toàn bảng cho mỗi nhãn như trước.
        Trả về {label_id: tổng số lần dùng}.
        """
        labels = list(labels)
        if not labels:
            return {}

        label_ids = [label.id for label in labels]
        pl_rows = ProjectLabel.objects.filter(label_id__in=label_ids).values_list(
            'id', 'label_id'
        )
        pl_to_label = dict(pl_rows)
        if not pl_to_label:
            return {label.id: 0 for label in labels}

        pl_ids = list(pl_to_label)
        counts = {label.id: 0 for label in labels}

        for model in (Comment, Token):
            rows = (
                model.objects.filter(
                    Q(ai_label_id__in=pl_ids) | Q(manual_label_id__in=pl_ids)
                )
                .values('ai_label_id', 'manual_label_id')
                .annotate(n=models.Count('id'))
            )
            for row in rows:
                for key in ('ai_label_id', 'manual_label_id'):
                    pl_id = row[key]
                    if pl_id in pl_to_label:
                        counts[pl_to_label[pl_id]] += row['n']

        return counts


class Project(models.Model):
    """Project for organizing YouTube comment collections."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(blank=True, default='')
    # Owner: the user who created and fully owns this project
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='owned_projects',
        help_text='The user who owns this project'
    )
    # Participants: users who can participate (label-only access)
    participants = models.ManyToManyField(
        User,
        related_name='participated_projects',
        blank=True,
        help_text='Users who can participate in this project (label-only access)'
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    is_locked = models.BooleanField(default=False)

    # --- Cấu hình quy trình gán nhãn ---
    guideline = models.TextField(
        blank=True, default='',
        help_text='Hướng dẫn gán nhãn hiển thị cho annotator (hỗ trợ xuống dòng)'
    )
    annotators_per_comment = models.PositiveSmallIntegerField(
        default=1,
        help_text='Số annotator cần gán nhãn mỗi comment trước khi chốt nhãn vàng'
    )
    auto_adjudicate = models.BooleanField(
        default=True,
        help_text='Tự động chốt nhãn vàng khi các annotator đồng thuận tuyệt đối'
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    @property
    def total_links(self):
        return self.youtubelinks.filter(status__in=['completed', 'annotated']).count()

    @property
    def total_comments(self):
        # Một truy vấn COUNT, không lặp qua từng link.
        return Comment.objects.filter(youtube_link__project=self).count()

    @property
    def available_labels(self):
        """Return all labels available for this project."""
        return ProjectLabel.objects.filter(project=self).select_related('label')

    def is_owner(self, user):
        """Check if the given user is the owner of this project."""
        return self.owner == user

    def is_participant(self, user):
        """Check if the given user is a participant (not owner) of this project."""
        return self.participants.filter(pk=user.pk).exists()

    def can_edit(self, user):
        """Check if the user can edit project info (only owner)."""
        return self.is_owner(user)

    def can_label(self, user):
        """Check if the user can assign labels (owner or participant)."""
        return self.is_owner(user) or self.is_participant(user)


class ProjectLabel(models.Model):
    """
    Links a Label to a Project, optionally overriding name/description/color
    for project-specific usage.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='projectlabels'
    )
    label = models.ForeignKey(
        Label, on_delete=models.CASCADE, related_name='projectlabels'
    )
    # Project-specific overrides (null = use public label value)
    override_name = models.CharField(max_length=100, blank=True, null=True)
    override_description = models.TextField(blank=True, null=True)
    override_color = models.CharField(max_length=7, blank=True, null=True)

    class Meta:
        unique_together = ('project', 'label')
        ordering = ['id']

    def __str__(self):
        return f"{self.project.name}: {self.display_name}"

    @property
    def display_name(self):
        return self.override_name or self.label.name

    @property
    def display_description(self):
        return self.override_description or self.label.description or ''

    @property
    def display_color(self):
        return self.override_color or self.label.color

    @classmethod
    def usage_counts_for(cls, project_labels):
        """
        Đếm số comment/token dùng mỗi ProjectLabel trong 2 truy vấn gộp.
        Trả về {project_label_id: {'comments': n, 'tokens': m}}.
        """
        project_labels = list(project_labels)
        if not project_labels:
            return {}

        pl_ids = [pl.id for pl in project_labels]
        result = {pl.id: {'comments': 0, 'tokens': 0} for pl in project_labels}

        for model, key in ((Comment, 'comments'), (Token, 'tokens')):
            rows = (
                model.objects.filter(
                    Q(ai_label_id__in=pl_ids) | Q(manual_label_id__in=pl_ids)
                )
                .values('ai_label_id', 'manual_label_id')
                .annotate(n=models.Count('id'))
            )
            for row in rows:
                for field in ('ai_label_id', 'manual_label_id'):
                    pl_id = row[field]
                    if pl_id in result:
                        result[pl_id][key] += row['n']

        return result


class TaskProgress(models.Model):
    """Track progress of async tasks (comment fetching, annotation)."""
    TASK_TYPES = (
        ('fetching', 'Fetching Comments'),
        ('annotating', 'Annotating Comments'),
        ('importing', 'Importing CSV'),
    )
    TASK_STATUSES = (
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    youtube_link = models.ForeignKey(
        'YouTubeLink', on_delete=models.CASCADE, related_name='task_progresses'
    )
    task_type = models.CharField(max_length=20, choices=TASK_TYPES)
    task_id = models.CharField(max_length=255, blank=True, default='', db_index=True)
    status = models.CharField(max_length=20, choices=TASK_STATUSES, default='pending')
    progress_percent = models.IntegerField(default=0)
    # Khoá thông điệp (comments.task_messages), không phải câu viết sẵn: worker
    # không biết người xem dùng ngôn ngữ nào.
    current_step = models.TextField(blank=True, default='')
    step_params = models.JSONField(default=dict, blank=True)
    total_items = models.IntegerField(default=0)
    processed_items = models.IntegerField(default=0)
    error_message = models.TextField(blank=True, default='')
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['youtube_link', 'status']),
        ]

    def __str__(self):
        return f"{self.task_type} - {self.status} ({self.progress_percent}%)"


class YouTubeLink(models.Model):
    """Stores YouTube video links associated with a project."""
    KIND_CHOICES = (
        ('youtube', 'YouTube video'),
        ('csv', 'CSV import'),
        ('manual', 'Nhập thủ công'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='youtubelinks'
    )
    # kind cho phép cùng một bảng chứa nguồn dữ liệu ngoài YouTube (CSV, nhập tay)
    # mà không phải viết lại toàn bộ quan hệ Comment/Token.
    kind = models.CharField(
        max_length=20, choices=KIND_CHOICES, default='youtube', db_index=True,
        help_text='Loại nguồn dữ liệu'
    )
    video_id = models.CharField(max_length=100, db_index=True)
    url = models.URLField(max_length=2048, blank=True, default='')
    title = models.CharField(max_length=500, blank=True, default='')
    channel = models.CharField(max_length=255, blank=True, default='')
    thumbnail = models.URLField(max_length=1024, blank=True, default='')
    status = models.CharField(
        max_length=20,
        choices=(
            ('pending', 'Pending'),
            ('fetching', 'Fetching Comments'),
            ('completed', 'Comments Fetched'),
            ('annotating', 'Annotating'),
            ('annotated', 'Fully Annotated'),
            ('failed', 'Failed'),
        ),
        default='pending'
    )
    comment_count = models.IntegerField(default=0)
    view_count = models.IntegerField(default=0)
    like_count = models.IntegerField(default=0)
    added_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('project', 'video_id')
        ordering = ['-added_at']
        indexes = [
            models.Index(fields=['project', 'status']),
        ]

    def __str__(self):
        return f"{self.title or self.video_id} ({self.project.name})"


class Comment(models.Model):
    """Individual YouTube comment with toxicity annotation."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    youtube_link = models.ForeignKey(
        YouTubeLink, on_delete=models.CASCADE, related_name='comments'
    )
    youtube_comment_id = models.CharField(max_length=255, db_index=True)
    author = models.CharField(max_length=255, blank=True, default='')
    author_channel_url = models.URLField(max_length=1024, blank=True, default='')
    avatar_url = models.URLField(max_length=1024, blank=True, default='')
    # text: bản chuẩn hoá dùng để hiển thị và gán nhãn (có thể là bản dịch).
    text = models.TextField()
    # source_text: văn bản gốc lấy từ nguồn, không bao giờ bị ghi đè.
    # Đây là bản sao bất biến để có thể tái lập lại toàn bộ pipeline.
    source_text = models.TextField(
        blank=True, default='',
        help_text='Văn bản gốc từ nguồn dữ liệu — bất biến, không bao giờ ghi đè'
    )
    original_text = models.TextField(
        blank=True, default='',
        help_text='Original comment text if it was translated (non-Vietnamese)'
    )
    is_meaningful = models.BooleanField(
        null=True,
        blank=True,
        default=None,
        help_text='Whether the comment contains meaningful content that should be labeled'
    )
    # Phân biệt "AI đã xử lý và kết luận nhãn O" với "AI chưa xử lý". Gộp cả
    # hai vào ai_label=NULL thì mỗi lần chạy lại, comment AI đã kết luận O lại
    # bị gửi sang LLM một lần nữa.
    ai_processed = models.BooleanField(
        default=False, db_index=True,
        help_text='AI đã xử lý comment này (kể cả khi kết luận là nhãn O)'
    )
    ai_processed_at = models.DateTimeField(null=True, blank=True)
    like_count = models.IntegerField(default=0)
    published_at = models.DateTimeField(null=True, blank=True)
    updated_at_source = models.DateTimeField(null=True, blank=True)
    is_public = models.BooleanField(default=True)

    # --- Dual-label system: AI label + Manual label ---
    # AI-assigned label (from Ollama annotation)
    ai_label = models.ForeignKey(
        'ProjectLabel',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='comments_ai_labeled',
        help_text='Label assigned by AI'
    )
    # User-assigned label (manual override; takes priority for display/export)
    manual_label = models.ForeignKey(
        'ProjectLabel',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='comments_manual_labeled',
        help_text='Label assigned by user (overrides AI label for display)'
    )

    # Nhãn vàng: kết quả chốt sau khi tổng hợp/phân xử nhiều annotator.
    # manual_label được giữ lại như bản sao tương thích ngược của gold_label.
    gold_label = models.ForeignKey(
        'ProjectLabel',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='comments_gold_labeled',
        help_text='Nhãn chốt cuối cùng sau khi phân xử'
    )
    REVIEW_STATUSES = (
        ('pending', 'Chưa đủ annotator'),
        ('agreed', 'Đồng thuận'),
        ('conflict', 'Bất đồng — cần phân xử'),
        ('adjudicated', 'Đã phân xử'),
    )
    review_status = models.CharField(
        max_length=20, choices=REVIEW_STATUSES, default='pending', db_index=True
    )
    manual_annotation_count = models.PositiveSmallIntegerField(
        default=0, help_text='Số annotator đã gán nhãn comment này'
    )

    toxicity_confidence = models.FloatField(
        null=True, blank=True,
        help_text='Confidence score from the model (0.0 - 1.0)'
    )
    annotation_source = models.CharField(
        max_length=20,
        choices=(
            ('auto', 'Automatic (AI)'),
            ('manual', 'Manual'),
            ('mixed', 'Mixed (AI + Manual)'),
        ),
        null=True, blank=True
    )
    model_response = models.JSONField(
        null=True, blank=True,
        help_text='Raw model response for debugging'
    )

    # Timestamps
    fetched_at = models.DateTimeField(default=timezone.now)
    annotated_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-fetched_at']
        indexes = [
            models.Index(fields=['youtube_link', 'ai_label']),
            models.Index(fields=['youtube_link', 'youtube_comment_id']),
            # Các truy vấn đếm/lọc nóng trên trang link_detail và API status.
            models.Index(fields=['youtube_link', 'manual_label']),
            models.Index(fields=['youtube_link', 'is_meaningful']),
            models.Index(fields=['youtube_link', 'ai_processed']),
            models.Index(fields=['youtube_link', 'review_status']),
        ]
        unique_together = ('youtube_link', 'youtube_comment_id')

    def __str__(self):
        preview = self.text[:50] if self.text else ''
        return f"Comment by {self.author}: {preview}..."

    @property
    def effective_label(self):
        """
        Nhãn hiệu lực để hiển thị và xuất dữ liệu.
        Thứ tự ưu tiên: gold_label (đã phân xử) > manual_label > ai_label.
        """
        return self.gold_label or self.manual_label or self.ai_label

    @property
    def labeled(self):
        return self.effective_label

    @property
    def toxicity_label(self):
        eff = self.effective_label
        return eff.display_name if eff else 'O'

    @property
    def is_annotated(self):
        return self.effective_label is not None

    @property
    def is_toxic(self) -> bool:
        """
        True khi nhãn hiệu lực khác nhãn trung tính 'O'.
        """
        eff = self.effective_label
        if not eff:
            return False
        return eff.label.name.strip().upper() != 'O'

    @property
    def is_non_toxic(self) -> bool:
        return not self.is_toxic


    @property
    def was_translated(self):
        """True if the comment was likely translated from Vietnamese."""
        return self.original_text and self.original_text.strip().lower() != self.text.strip().lower()

    @property
    def effective_label_data(self):
        """Return dict with label info for the effective (display) label."""
        pl = self.effective_label
        if not pl:
            return None
        return {
            'id': str(pl.id),
            'name': pl.display_name,
            'color': pl.display_color,
            'description': pl.display_description,
        }

    @property
    def ai_label_data(self):
        """Return dict with label info for the AI-assigned label."""
        if not self.ai_label:
            return None
        return {
            'id': str(self.ai_label.id),
            'name': self.ai_label.display_name,
            'color': self.ai_label.display_color,
        }

    @property
    def manual_label_data(self):
        """Return dict with label info for the manual label."""
        if not self.manual_label:
            return None
        return {
            'id': str(self.manual_label.id),
            'name': self.manual_label.display_name,
            'color': self.manual_label.display_color,
        }

    @property
    def display_tokens(self):
        """
        Return token data for display.
        Always tokenize the current comment text so missing tokens do not
        disappear after partial manual labeling.
        """
        text = (self.text or '').strip()
        if not text:
            return []

        from .services.tokenization import tokens_from_cache

        token_rows = {token.position: token for token in self.tokens.all()}
        return [{
            'id': str(token_rows[idx].id) if idx in token_rows else None,
            'text': token['text'],
            'position': idx,
            'start_offset': token['start'],
            'end_offset': token['end'],
            'ai_label': token_rows[idx].ai_label_data if idx in token_rows else None,
            'manual_label': token_rows[idx].manual_label_data if idx in token_rows else None,
            'effective_label': token_rows[idx].effective_label_data if idx in token_rows else None,
            'span_group': (
                str(token_rows[idx].span_group) if idx in token_rows and token_rows[idx].span_group
                else None
            ),
            # Backward compat
            'is_toxic': token_rows[idx].is_toxic if idx in token_rows else False,
            'toxicity_score': token_rows[idx].toxicity_score if idx in token_rows else None,
            'annotation_source': token_rows[idx].annotation_source if idx in token_rows else 'manual',
        } for idx, token in enumerate(tokens_from_cache(text))]

    def ensure_token_inventory(self):
        """
        Ensure all tokens for the current text exist in the database.
        """
        text = (self.text or '').strip()
        if not text:
            return []

        from .services.tokenization import tokenize_text

        token_data_list = tokenize_text(text)
        existing = {token.position: token for token in self.tokens.all()}
        created_tokens = []

        for idx, token_data in enumerate(token_data_list):
            if idx in existing:
                continue
            created_tokens.append(Token.objects.create(
                comment=self,
                text=token_data['text'],
                position=idx,
                start_offset=token_data['start'],
                end_offset=token_data['end'],
                annotated_at=timezone.now(),
                annotation_source='manual',
            ))

        return created_tokens

    def get_or_create_token_for_position(self, position):
        """
        Get an existing token by position or create it from the current text.
        """
        self.ensure_token_inventory()
        existing = self.tokens.filter(position=position).first()
        if existing:
            return existing

        text = (self.text or '').strip()
        if not text:
            return None

        from .services.tokenization import tokenize_text

        tokens = tokenize_text(text)
        if position < 0 or position >= len(tokens):
            return None

        token_data = tokens[position]
        return Token.objects.create(
            comment=self,
            text=token_data['text'],
            position=position,
            start_offset=token_data['start'],
            end_offset=token_data['end'],
            annotated_at=timezone.now(),
            annotation_source='manual',
        )

    @property
    def needs_manual_label(self):
        """True when the comment still needs a user label."""
        return self.manual_label is None and self.is_meaningful is not False

    @property
    def is_skipped(self):
        """True when the comment was deemed not meaningful and skipped."""
        return self.is_meaningful is False



class Token(models.Model):
    """Individual token/word within a comment with toxicity annotation."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    comment = models.ForeignKey(
        Comment, on_delete=models.CASCADE, related_name='tokens'
    )
    text = models.CharField(max_length=255)
    position = models.IntegerField(help_text='Position of the token in the comment')
    start_offset = models.IntegerField(help_text='Start character offset in the original text')
    end_offset = models.IntegerField(help_text='End character offset in the original text')

    # --- Dual-label system: AI label + Manual label ---
    # AI-assigned label (from Ollama annotation)
    ai_label = models.ForeignKey(
        'ProjectLabel',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='tokens_ai_labeled',
        help_text='Label assigned by AI'
    )
    # User-assigned label (manual override; takes priority for display/export)
    manual_label = models.ForeignKey(
        'ProjectLabel',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='tokens_manual_labeled',
        help_text='Label assigned by user (overrides AI label for display)'
    )

    gold_label = models.ForeignKey(
        'ProjectLabel',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='tokens_gold_labeled',
        help_text='Nhãn chốt cuối cùng sau khi phân xử'
    )

    # Định danh cụm (span) mà token này thuộc về.
    # Không có trường này thì hai token cạnh nhau cùng nhãn là mơ hồ: không biết
    # đó là một cụm hai từ hay HAI cụm một từ. Mọi bộ công cụ sequence-labeling
    # (BIO/IOB2) đều cần phân biệt này, nên phải lưu tường minh chứ không suy đoán.
    span_group = models.UUIDField(
        null=True, blank=True, db_index=True,
        help_text='Các token cùng một cụm chia sẻ chung giá trị này'
    )

    # Legacy fields
    toxicity_score = models.FloatField(
        null=True, blank=True,
        help_text='Toxicity score for this specific token (0.0 - 1.0)'
    )
    annotated_at = models.DateTimeField(null=True, blank=True)
    annotation_source = models.CharField(
        max_length=20,
        choices=(
            ('auto', 'Automatic (AI)'),
            ('manual', 'Manual'),
        ),
        default='auto'
    )

    class Meta:
        ordering = ['position']
        indexes = [
            models.Index(fields=['comment', 'position']),
        ]

    def __str__(self):
        eff = self.effective_label
        label_name = eff.display_name if eff else 'none'
        return f'"{self.text}" label=[{label_name}] at pos {self.position}'

    @property
    def effective_label(self):
        """
        Nhãn hiệu lực của token.
        Thứ tự ưu tiên: gold_label > manual_label > ai_label.
        """
        return self.gold_label or self.manual_label or self.ai_label

    @property
    def labeled(self):
        return self.effective_label

    @property
    def toxicity_label(self):
        eff = self.effective_label
        return eff.display_name if eff else 'O'

    @property
    def effective_label_data(self):
        """Return dict with label info for the effective (display) label."""
        pl = self.effective_label
        if not pl:
            return None
        return {
            'id': str(pl.id),
            'name': pl.display_name,
            'color': pl.display_color,
        }

    @property
    def ai_label_data(self):
        """Return dict with label info for the AI-assigned label."""
        if not self.ai_label:
            return None
        return {
            'id': str(self.ai_label.id),
            'name': self.ai_label.display_name,
            'color': self.ai_label.display_color,
        }

    @property
    def manual_label_data(self):
        """Return dict with label info for the manual label."""
        if not self.manual_label:
            return None
        return {
            'id': str(self.manual_label.id),
            'name': self.manual_label.display_name,
            'color': self.manual_label.display_color,
        }

    @property
    def is_toxic(self) -> bool:
        """
        True khi token mang nhãn khác 'O'.
        """
        eff = self.effective_label
        if not eff:
            return False
        return eff.label.name.strip().upper() != 'O'


class ExportRecord(models.Model):
    """Lịch sử xuất dữ liệu."""

    # Danh sách này chỉ để Admin hiển thị nhãn dễ đọc. Nguồn sự thật về các định
    # dạng khả dụng là comments.export_service.EXPORT_FORMATS: không import ở
    # đây để tránh vòng lặp import (export_service import từ models).
    EXPORT_FORMATS = (
        ('conll', 'CoNLL-2003 (token + BIO)'),
        ('conll_full', 'CoNLL extended (with offsets)'),
        ('hf_jsonl', 'HuggingFace datasets JSONL'),
        ('spacy_json', 'spaCy training JSON'),
        ('doccano_jsonl', 'Doccano JSONL'),
        ('label_studio_json', 'Label Studio JSON'),
        ('json_sentence', 'JSON - sentence level'),
        ('json_token', 'JSON - token level'),
        ('jsonl', 'JSONL - token level'),
        ('xml', 'XML - full structure'),
        ('csv_sentence', 'CSV - sentence level'),
        ('csv_token', 'CSV - token level'),
        ('csv_spans', 'CSV - labelled spans only'),
        ('csv_annotations', 'CSV - per annotation'),
        ('xlsx', 'Excel workbook'),
        ('json_llm', 'JSONL - LLM fine-tuning'),
        # Tên cũ, giữ lại để đọc được các bản ghi lịch sử.
        ('xml_conll', 'XML (legacy name)'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='exports'
    )
    youtube_link = models.ForeignKey(
        YouTubeLink, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='exports'
    )
    export_format = models.CharField(max_length=30, choices=EXPORT_FORMATS)
    # Lưu 'all' hoặc tên nhãn được lọc. Không khai báo choices cố định vì giá
    # trị thực tế là tên nhãn của từng dự án, không phải một tập đóng.
    filter_toxicity = models.CharField(
        max_length=100, default='all', blank=True,
        help_text="'all' hoặc tên nhãn được dùng để lọc"
    )
    comment_count = models.IntegerField(default=0)
    token_count = models.IntegerField(default=0)
    file_size = models.CharField(max_length=50, blank=True, default='')
    generated_at = models.DateTimeField(default=timezone.now)

    # --- Chạy nền -----------------------------------------------------------
    # Xuất dữ liệu lớn mất vài phút, vượt quá thời gian chờ của trình duyệt và
    # của reverse proxy. Giờ mỗi lần xuất là một công việc chạy nền, và chính
    # bản ghi lịch sử này mang trạng thái + tiến độ + đường dẫn file kết quả.
    STATUSES = (
        ('pending', 'Chờ xử lý'),
        ('running', 'Đang xuất'),
        ('ready', 'Sẵn sàng tải'),
        ('failed', 'Thất bại'),
    )

    # Mặc định 'ready': các bản ghi lịch sử tạo trước khi có tính năng chạy nền
    # đều đã xuất xong (chỉ là không còn file để tải).
    status = models.CharField(max_length=20, choices=STATUSES, default='ready')
    progress_percent = models.IntegerField(default=0)
    current_step = models.TextField(blank=True, default='')
    file_path = models.CharField(max_length=500, blank=True, default='')
    file_bytes = models.BigIntegerField(default=0)
    error_message = models.TextField(blank=True, default='')
    review_filter = models.CharField(max_length=20, default='all', blank=True)
    requested_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='export_records'
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-generated_at']
        indexes = [
            models.Index(fields=['project', 'status']),
        ]

    def __str__(self):
        return f"Export: {self.export_format} - {self.project.name}"

    @property
    def is_running(self) -> bool:
        return self.status in ('pending', 'running')

    @property
    def can_download(self) -> bool:
        return (
            self.status == 'ready'
            and bool(self.file_path)
            and Path(self.file_path).is_file()
        )

    @property
    def download_name(self) -> str:
        safe = ''.join(
            ch if ch.isalnum() or ch in '-_' else '-' for ch in self.project.name
        )[:60]
        return (
            f'{safe}_{self.export_format}_'
            f'{self.generated_at:%Y%m%d-%H%M%S}{Path(self.file_path).suffix}'
        )

# ---------------------------------------------------------------------------
# Hệ thống gán nhãn đa người (multi-annotator)
# ---------------------------------------------------------------------------
# Mỗi Comment/Token chỉ có một manual_label thì người gán nhãn sau ghi đè im
# lặng lên người trước: không ai biết ai đã gán gì, và không tính được độ đồng
# thuận giữa các annotator (IAA).
#
# Các bảng dưới đây lưu annotation của từng người. Cột manual_label/gold_label
# trên Comment/Token được giữ lại như bản sao đã tính sẵn (denormalised) để
# truy vấn hiển thị và export nhanh, đồng bộ qua recompute_gold_label().

ANNOTATION_SOURCES = (
    ('ai', 'AI'),
    ('manual', 'Người gán nhãn'),
    ('adjudicated', 'Phân xử bởi chủ dự án'),
)


class CommentAnnotation(models.Model):
    """Nhãn cấp câu do một annotator (hoặc AI) gán cho một comment."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    comment = models.ForeignKey(
        Comment, on_delete=models.CASCADE, related_name='annotations'
    )
    annotator = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='comment_annotations',
        help_text='NULL nghĩa là do AI gán'
    )
    source = models.CharField(max_length=20, choices=ANNOTATION_SOURCES, default='manual')
    project_label = models.ForeignKey(
        'ProjectLabel',
        null=True, blank=True,
        on_delete=models.CASCADE,
        related_name='comment_annotations',
        help_text='NULL nghĩa là annotator chọn "không nhãn" (tương đương O)'
    )
    is_meaningful = models.BooleanField(null=True, blank=True, default=None)
    confidence = models.FloatField(null=True, blank=True)
    note = models.TextField(blank=True, default='', help_text='Ghi chú của annotator')
    time_spent_ms = models.PositiveIntegerField(
        default=0, help_text='Thời gian gán nhãn (ms) — dùng đo năng suất'
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['comment', 'annotator', 'source'],
                name='uniq_comment_annotation_per_annotator',
            ),
        ]
        indexes = [
            models.Index(fields=['comment', 'source']),
            models.Index(fields=['annotator', 'created_at']),
        ]

    def __str__(self):
        who = self.annotator.username if self.annotator else 'AI'
        label = self.project_label.display_name if self.project_label else 'O'
        return f'{who} -> {label}'

    @property
    def label_name(self) -> str:
        return self.project_label.display_name if self.project_label else 'O'


class TokenAnnotation(models.Model):
    """Nhãn cấp token do một annotator (hoặc AI) gán."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token = models.ForeignKey(
        'Token', on_delete=models.CASCADE, related_name='annotations'
    )
    annotator = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='token_annotations',
    )
    source = models.CharField(max_length=20, choices=ANNOTATION_SOURCES, default='manual')
    project_label = models.ForeignKey(
        'ProjectLabel',
        null=True, blank=True,
        on_delete=models.CASCADE,
        related_name='token_annotations',
    )
    score = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['token', 'annotator', 'source'],
                name='uniq_token_annotation_per_annotator',
            ),
        ]
        indexes = [
            models.Index(fields=['token', 'source']),
            models.Index(fields=['annotator', 'created_at']),
        ]

    def __str__(self):
        who = self.annotator.username if self.annotator else 'AI'
        label = self.project_label.display_name if self.project_label else 'O'
        return f'{who} -> {label} @{self.token_id}'


class AnnotationAssignment(models.Model):
    """
    Phân công một comment cho một annotator.

    Cho phép chia đều khối lượng công việc và biết ai còn nợ bao nhiêu comment.
    """

    STATUSES = (
        ('pending', 'Chưa làm'),
        ('done', 'Đã gán nhãn'),
        ('skipped', 'Bỏ qua'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='assignments'
    )
    comment = models.ForeignKey(
        Comment, on_delete=models.CASCADE, related_name='assignments'
    )
    annotator = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='annotation_assignments'
    )
    status = models.CharField(max_length=20, choices=STATUSES, default='pending', db_index=True)
    assigned_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['assigned_at']
        constraints = [
            models.UniqueConstraint(
                fields=['comment', 'annotator'], name='uniq_assignment_per_annotator'
            ),
        ]
        indexes = [
            models.Index(fields=['annotator', 'status']),
            models.Index(fields=['project', 'status']),
        ]

    def __str__(self):
        return f'{self.annotator.username}: {self.comment_id} [{self.status}]'


class AnnotationEvent(models.Model):
    """
    Nhật ký kiểm toán (audit trail): ghi lại mọi thay đổi nhãn.

    Cho phép truy vết ai đã đổi gì, khi nào, và khôi phục khi có người gán sai
    hàng loạt.
    """

    ACTIONS = (
        ('comment_label', 'Gán nhãn câu'),
        ('token_label', 'Gán nhãn token'),
        ('adjudicate', 'Phân xử'),
        ('ai_annotate', 'AI gán nhãn'),
        ('accept_ai', 'Chấp nhận đề xuất của AI'),
        ('reset', 'Xoá nhãn hàng loạt'),
        ('skip', 'Đánh dấu không có nghĩa'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='annotation_events'
    )
    comment = models.ForeignKey(
        Comment, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='events'
    )
    token_position = models.IntegerField(null=True, blank=True)
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='annotation_events'
    )
    action = models.CharField(max_length=30, choices=ACTIONS)
    old_value = models.CharField(max_length=200, blank=True, default='')
    new_value = models.CharField(max_length=200, blank=True, default='')
    detail = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['project', '-created_at']),
            models.Index(fields=['comment', '-created_at']),
            models.Index(fields=['actor', '-created_at']),
        ]

    def __str__(self):
        who = self.actor.username if self.actor else 'system'
        return f'[{self.created_at:%Y-%m-%d %H:%M}] {who} {self.action}: {self.old_value} -> {self.new_value}'


class DatasetVersion(models.Model):
    """
    Ảnh chụp (snapshot) một phiên bản dataset đã xuất.

    Cho phép tái lập kết quả nghiên cứu: mỗi lần công bố dataset sẽ ghim lại
    số lượng, checksum và file kết quả.
    """

    STATUSES = (
        ('building', 'Đang tạo'),
        ('ready', 'Sẵn sàng'),
        ('failed', 'Thất bại'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='dataset_versions'
    )
    version = models.CharField(max_length=50, help_text='Ví dụ: v1.0, 2026-09-03')
    notes = models.TextField(blank=True, default='')
    export_format = models.CharField(max_length=30, default='json_token')
    status = models.CharField(max_length=20, choices=STATUSES, default='building')
    progress_percent = models.IntegerField(default=0)
    current_step = models.TextField(blank=True, default='')
    file_path = models.CharField(max_length=500, blank=True, default='')
    snapshot_path = models.CharField(
        max_length=500, blank=True, default='',
        help_text='Ảnh chụp nhãn ở dạng chuẩn, dùng để phục hồi dự án về '
                  'phiên bản này. Khác file_path: file_path là bản xuất cho '
                  'người dùng cuối, có định dạng do người tạo chọn.'
    )
    checksum_sha256 = models.CharField(max_length=64, blank=True, default='')
    comment_count = models.IntegerField(default=0)
    token_count = models.IntegerField(default=0)
    annotator_count = models.IntegerField(default=0)
    agreement_score = models.FloatField(
        null=True, blank=True,
        help_text="Krippendorff's alpha tại thời điểm chốt phiên bản"
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='dataset_versions'
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['project', 'version'], name='uniq_dataset_version_per_project'
            ),
        ]

    def __str__(self):
        return f'{self.project.name} @ {self.version} ({self.status})'

    @property
    def file_exists(self) -> bool:
        """File xuất còn nằm trên đĩa hay không (volume có thể đã bị xoá)."""
        return bool(self.file_path) and Path(self.file_path).is_file()

    @property
    def can_download(self) -> bool:
        return self.status == 'ready' and self.file_exists

    @property
    def can_restore(self) -> bool:
        """
        Chỉ phục hồi được khi có ảnh chụp chuẩn.

        Các phiên bản chốt trước khi tính năng phục hồi ra đời không có
        snapshot_path: nút phục hồi sẽ bị vô hiệu hoá thay vì báo lỗi.
        """
        return (
            self.status == 'ready'
            and bool(self.snapshot_path)
            and Path(self.snapshot_path).is_file()
        )

    @property
    def download_name(self) -> str:
        """Tên file khi tải về: có tên dự án và tên phiên bản."""
        safe = ''.join(
            ch if ch.isalnum() or ch in '-_' else '-' for ch in self.project.name
        )[:60]
        return f'{safe}_{self.version}{Path(self.file_path).suffix}'

    @property
    def file_size_bytes(self) -> int:
        try:
            return Path(self.file_path).stat().st_size
        except OSError:
            return 0
