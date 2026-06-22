from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.contrib.auth.models import User
import uuid
import hashlib
import secrets


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
    youtube_api_key = models.CharField(
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
    ollama_api_key = models.CharField(
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
        """Check if this label is currently used in any comments or tokens."""
        # Check via ProjectLabel relationships
        project_labels = self.projectlabels.all()
        if project_labels.exists():
            # Check if any token or comment references these project labels
            pl_ids = project_labels.values_list('id', flat=True)
            if Token.objects.filter(ai_label_id__in=pl_ids).exists():
                return True
            if Token.objects.filter(manual_label_id__in=pl_ids).exists():
                return True
            if Comment.objects.filter(ai_label_id__in=pl_ids).exists():
                return True
            if Comment.objects.filter(manual_label_id__in=pl_ids).exists():
                return True
        return False

    def usage_count(self):
        """Count how many times this label is used across all projects."""
        pl_ids = self.projectlabels.values_list('id', flat=True)
        token_count = Token.objects.filter(
            Q(ai_label_id__in=pl_ids) | Q(manual_label_id__in=pl_ids)
        ).count()
        comment_count = Comment.objects.filter(
            Q(ai_label_id__in=pl_ids) | Q(manual_label_id__in=pl_ids)
        ).count()
        return token_count + comment_count


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

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    @property
    def total_links(self):
        return self.youtubelinks.filter(status__in=['completed', 'annotated']).count()

    @property
    def total_comments(self):
        return sum(link.comments.count() for link in self.youtubelinks.all())

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


class TaskProgress(models.Model):
    """Track progress of async tasks (comment fetching, annotation)."""
    TASK_TYPES = (
        ('fetching', 'Fetching Comments'),
        ('annotating', 'Annotating Comments'),
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
    current_step = models.TextField(blank=True, default='')
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
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='youtubelinks'
    )
    video_id = models.CharField(max_length=100, db_index=True)
    url = models.URLField(max_length=2048)
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
    text = models.TextField()
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
        ]
        unique_together = ('youtube_link', 'youtube_comment_id')

    def __str__(self):
        preview = self.text[:50] if self.text else ''
        return f"Comment by {self.author}: {preview}..."

    @property
    def effective_label(self):
        """
        Return the effective label for this comment.
        Priority: manual_label > ai_label
        """
        return self.manual_label or self.ai_label
    @property
    def labeled(self):
        if self.effective_label:
            return self.effective_label
    @property
    def toxicity_label(self):
        if self.effective_label:
            return self.effective_label.display_name
        return 'O'
    @property
    def is_annotated(self):
        return self.effective_label is not None

    @property
    def is_toxic(self):
        """Backward compat: check if effective label name contains 'toxic'."""
        if self.effective_label:
            return self.effective_label.display_name
        return False

    @property
    def is_non_toxic(self):
        """Backward compat: check if effective label is neutral/O."""
        eff = self.effective_label
        return not eff or eff.label.name.upper() == 'O'
        
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

        from .services.ollama_service import tokenize_text

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
            # Backward compat
            'is_toxic': token_rows[idx].is_toxic if idx in token_rows else False,
            'toxicity_score': token_rows[idx].toxicity_score if idx in token_rows else None,
            'annotation_source': token_rows[idx].annotation_source if idx in token_rows else 'manual',
        } for idx, token in enumerate(tokenize_text(text))]

    def ensure_token_inventory(self):
        """
        Ensure all tokens for the current text exist in the database.
        """
        text = (self.text or '').strip()
        if not text:
            return []

        from .services.ollama_service import tokenize_text

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

        from .services.ollama_service import tokenize_text

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

    def update_comment_label(self):
        """Update legacy toxicity_label based on token annotations (backward compat)."""
        # eff = self.effective_label
        # if eff and eff.label.name.upper() != 'O':
        #     self.toxicity_label = 'toxic'
        # elif self.tokens.exists():
        #     self.toxicity_label = 'non_toxic'
        # if self.tokens.exists():
        #     self.is_meaningful = True
        # self.save(update_fields=['toxicity_label', 'is_meaningful', 'updated_at'])


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
        Return the effective label for this token.
        Priority: manual_label > ai_label
        """
        return self.manual_label or self.ai_label

    @property
    def labeled(self):
        if self.effective_label:
            return self.effective_label
    @property
    def toxicity_label(self):
        if self.effective_label:
            return self.effective_label.display_name
        return 'O'

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
    def is_toxic(self):
        """Backward compat: check if effective label is toxic-like."""
        if self.effective_label:
            return self.effective_label is not None
        return False

    @is_toxic.setter
    def is_toxic(self, value):
        """Allow setting is_toxic for backward compat during migration."""
        # Store as private to avoid recursion
        object.__setattr__(self, '_is_toxic_compat', value)

    def _get_is_toxic(self):
        if self.effective_label:
            return self.effective_label is not None
        return False


class ExportRecord(models.Model):
    """Record of data exports."""
    EXPORT_FORMATS = (
        ('json_sentence', 'JSON - Sentence Level'),
        ('json_token', 'JSON - Token Level'),
        ('json_llm', 'JSON - LLM Training'),
        ('xml_conll', 'XML - CoNLL Format'),
        ('csv_sentence', 'CSV - Sentence Level'),
        ('csv_token', 'CSV - Token Level'),
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
    filter_toxicity = models.CharField(
        max_length=20,
        choices=(
            ('all', 'All Comments'),
            ('toxic', 'Toxic Only'),
            ('non_toxic', 'Non-Toxic Only'),
        ),
        default='all'
    )
    comment_count = models.IntegerField(default=0)
    token_count = models.IntegerField(default=0)
    file_size = models.CharField(max_length=50, blank=True, default='')
    generated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-generated_at']

    def __str__(self):
        return f"Export: {self.export_format} - {self.project.name}"