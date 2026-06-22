"""Admin configuration for AnnotaHub models."""
from django.contrib import admin
from .models import (
    Project, YouTubeLink, Comment, Token, TaskProgress,
    ExportRecord, Label, ProjectLabel, UserSettings, UserInvitation
)


class UserSettingsAdmin(admin.ModelAdmin):
    """Admin interface for managing user API settings."""
    list_display = ('user', 'username', 'has_youtube_key', 'has_ollama', 'ollama_model_display', 'updated_at')
    search_fields = ('user__username', 'user__email')
    readonly_fields = ('created_at', 'updated_at')
    fields = ('user', 'youtube_api_key', 'ollama_base_url', 'ollama_api_key',
              'ollama_model', 'created_at', 'updated_at')

    def username(self, obj):
        return obj.user.username
    username.short_description = 'Username'

    def has_youtube_key(self, obj):
        return 'Yes' if obj.has_youtube_api_key else 'No'
    has_youtube_key.short_description = 'YouTube API Key'

    def has_ollama(self, obj):
        return 'Yes' if obj.has_ollama_config else 'No'
    has_ollama.short_description = 'Ollama Configured'

    def ollama_model_display(self, obj):
        return obj.ollama_model or 'Default'
    ollama_model_display.short_description = 'Ollama Model'


class LabelAdmin(admin.ModelAdmin):
    """Admin interface for managing labels."""
    list_display = ('name', 'owner', 'color_display', 'is_active', 'created_at')
    list_filter = ('is_active', 'owner')
    search_fields = ('name', 'description')
    readonly_fields = ('created_at', 'updated_at',)

    def color_display(self, obj):
        return f'<span style="color: {obj.color}; font-weight: bold;">{obj.name} ({obj.color})</span>'
    color_display.short_description = 'Label'
    color_display.allow_tags = True


class ProjectLabelInlineAdmin(admin.TabularInline):
    """Inline admin for viewing/editing project labels."""
    model = ProjectLabel
    extra = 0
    raw_id_fields = ('label',)
    fields = ('label', 'override_name', 'override_description', 'override_color', 'display_name', 'display_color')
    readonly_fields = ('display_name', 'display_color')

    def has_add_permission(self, request, obj=None):
        return True

    def has_delete_permission(self, request, obj=None):
        return True


class ProjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'total_links', 'total_comments', 'created_at')
    search_fields = ('name', 'description')
    list_filter = ('owner',)
    readonly_fields = ('created_at', 'updated_at', 'total_links', 'total_comments')
    filter_horizontal = ('participants',)
    inlines = [ProjectLabelInlineAdmin]


class TokenInline(admin.TabularInline):
    model = Token
    extra = 0
    readonly_fields = ('text', 'position', 'start_offset', 'end_offset',
                       'effective_label_display', 'ai_label_display', 'manual_label_display')
    can_delete = True

    def has_add_permission(self, request, obj=None):
        return False

    def effective_label_display(self, obj):
        eff = obj.effective_label
        return eff.display_name if eff else 'none'
    effective_label_display.short_description = 'Effective Label'

    def ai_label_display(self, obj):
        return obj.ai_label.display_name if obj.ai_label else 'none'
    ai_label_display.short_description = 'AI Label'

    def manual_label_display(self, obj):
        return obj.manual_label.display_name if obj.manual_label else 'none'
    manual_label_display.short_description = 'Manual Label'


class CommentAdmin(admin.ModelAdmin):
    list_display = ('author', 'text_preview', 'annotation_source',
                    'effective_label_display', 'ai_label_display', 'manual_label_display', 'fetched_at')
    list_filter = ('annotation_source', 'ai_label')
    search_fields = ('text', 'author')
    readonly_fields = ('youtube_comment_id', 'text', 'author', 'fetched_at',
                       'effective_label_display', 'ai_label_display', 'manual_label_display')
    inlines = [TokenInline]

    def text_preview(self, obj):
        return obj.text[:80] + '..' if len(obj.text) > 80 else obj.text
    text_preview.short_description = 'Text'

    def effective_label_display(self, obj):
        eff = obj.effective_label
        return eff.display_name if eff else 'none'
    effective_label_display.short_description = 'Effective Label'

    def ai_label_display(self, obj):
        return obj.ai_label.display_name if obj.ai_label else 'none'
    ai_label_display.short_description = 'AI Label'

    def manual_label_display(self, obj):
        return obj.manual_label.display_name if obj.manual_label else 'none'
    manual_label_display.short_description = 'Manual Label'


class TaskProgressInline(admin.TabularInline):
    model = TaskProgress
    extra = 0
    readonly_fields = ('task_type', 'status', 'progress_percent', 'current_step', 'total_items', 'processed_items')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class YouTubeLinkAdmin(admin.ModelAdmin):
    list_display = ('title_preview', 'project', 'status', 'comment_count', 'added_at')
    list_filter = ('status', 'project')
    search_fields = ('title', 'video_id')
    readonly_fields = ('video_id', 'added_at')
    inlines = [TaskProgressInline]

    def title_preview(self, obj):
        return obj.title[:50] or obj.video_id
    title_preview.short_description = 'Title'


class TokenAdmin(admin.ModelAdmin):
    list_display = ('text', 'comment_text_preview', 'effective_label_display',
                    'ai_label_display', 'manual_label_display', 'position')
    search_fields = ('text',)

    def comment_text_preview(self, obj):
        return obj.comment.text[:50]
    comment_text_preview.short_description = 'Comment'

    def effective_label_display(self, obj):
        eff = obj.effective_label
        return eff.display_name if eff else 'none'
    effective_label_display.short_description = 'Effective Label'

    def ai_label_display(self, obj):
        return obj.ai_label.display_name if obj.ai_label else 'none'
    ai_label_display.short_description = 'AI Label'

    def manual_label_display(self, obj):
        return obj.manual_label.display_name if obj.manual_label else 'none'
    manual_label_display.short_description = 'Manual Label'


class TaskProgressAdmin(admin.ModelAdmin):
    list_display = ('task_type', 'youtube_link_title', 'status', 'progress_percent', 'current_step')
    list_filter = ('task_type', 'status')
    readonly_fields = ('created_at',)

    def youtube_link_title(self, obj):
        return obj.youtube_link.title or obj.youtube_link.video_id
    youtube_link_title.short_description = 'Video'


class ExportRecordAdmin(admin.ModelAdmin):
    list_display = ('project', 'export_format', 'filter_toxicity', 'comment_count', 'file_size', 'generated_at')
    list_filter = ('export_format', 'filter_toxicity')
    readonly_fields = ('generated_at',)


admin.site.register(Label, LabelAdmin)
admin.site.register(Project, ProjectAdmin)
admin.site.register(YouTubeLink, YouTubeLinkAdmin)
admin.site.register(Comment, CommentAdmin)
admin.site.register(Token, TokenAdmin)
admin.site.register(TaskProgress, TaskProgressAdmin)
admin.site.register(ExportRecord, ExportRecordAdmin)
admin.site.register(UserSettings, UserSettingsAdmin)


class UserInvitationAdmin(admin.ModelAdmin):
    """Admin interface for managing user invitations."""
    list_display = ('email', 'project', 'inviter', 'user', 'is_used', 'is_expired_display', 'created_at', 'expires_at')
    list_filter = ('is_used', 'project', 'inviter')
    search_fields = ('email', 'user__username', 'user__email', 'token')
    readonly_fields = ('token', 'created_at', 'expires_at')
    fields = ('email', 'token', 'project', 'inviter', 'user', 'is_used', 'created_at', 'expires_at')

    def is_expired_display(self, obj):
        return 'Yes' if obj.is_expired() else 'No'
    is_expired_display.short_description = 'Expired'
    # is_expired_display.boolean = True


admin.site.register(UserInvitation, UserInvitationAdmin)
