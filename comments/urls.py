"""URLs for the comments app"""
from django.urls import path
from . import views, api_views

app_name = 'comments'

# Web views (HTML pages, form submissions, SSE)
web_urls = [
    path('health/', views.health_check, name='health_check'),

    # Authentication views (no login required)
    path('login/', views.custom_login, name='login'),
    path('logout/', views.custom_logout, name='logout'),
    path('register/', views.register, name='register'),
    path('verify-email/<str:token>/', views.verify_email, name='verify_email'),
    path('resend-verification/', views.resend_verification, name='resend_verification'),

    # Invitation views (no login required)
    path('invite/<str:token>/', views.accept_invitation, name='accept_invitation'),

    # User Settings
    path('settings/', views.user_settings, name='user_settings'),

    # Dashboard & Project views
    path('projects/', views.project_list, name='project_list'),
    path('projects/create/', views.project_create, name='project_create'),
    path('projects/<uuid:project_id>/', views.project_detail, name='project_detail'),
    path('projects/<uuid:project_id>/edit/', views.project_edit, name='project_edit'),
    path('projects/<uuid:project_id>/delete/', views.project_delete, name='project_delete'),
    path('projects/<uuid:project_id>/export/', views.project_export, name='project_export'),
    path('projects/<uuid:project_id>/labels/', views.project_labels_settings, name='project_labels_settings'),
    path('projects/<uuid:project_id>/participants/', views.project_manage_participants, name='project_manage_participants'),

    # Label Management views
    path('labels/', views.label_list, name='label_list'),
    path('labels/create/', views.label_create, name='label_create'),
    path('labels/<uuid:label_id>/edit/', views.label_edit, name='label_edit'),
    path('labels/<uuid:label_id>/delete/', views.label_delete, name='label_delete'),

    # YouTube Link views
    path('projects/<uuid:project_id>/links/add/', views.add_youtube_link, name='add_youtube_link'),
    path('links/<uuid:link_id>/detail/', views.link_detail, name='link_detail'),
    path('links/<uuid:link_id>/delete/', views.delete_youtube_link, name='delete_youtube_link'),
    path('links/<uuid:link_id>/reannotate/', views.reannotate_link, name='reannotate_link'),
    path('links/<uuid:link_id>/retry/', views.retry_fetch_link, name='retry_fetch_link'),
    path('links/<uuid:link_id>/clear-refetch/', views.clear_and_refetch_link, name='clear_and_refetch_link'),
    path('links/<uuid:link_id>/stop-fetch/', views.stop_fetch_task, name='stop_fetch_task'),
    path('links/<uuid:link_id>/stop-annotate/', views.stop_annotation_task, name='stop_annotation_task'),
    path('links/<uuid:link_id>/continue-annotate/', views.continue_annotation, name='continue_annotation'),

    # SSE for real-time progress
    path('sse/progress/<uuid:link_id>/', views.progress_event_stream, name='progress_event_stream'),
]

# API endpoints (JSON responses)
api_urls = [
    path('projects/', api_views.ProjectListView.as_view(), name='api_project_list'),
    path('projects/create/', api_views.ProjectCreateView.as_view(), name='api_project_create'),
    path('projects/<uuid:project_id>/', api_views.ProjectDetailView.as_view(), name='api_project_detail'),
    path('projects/<uuid:project_id>/links/', api_views.LinkManageView.as_view(), name='api_link_manage'),
    path('projects/<uuid:project_id>/labels/', api_views.ProjectLabelsView.as_view(), name='api_project_labels'),
    path('links/<uuid:link_id>/status/', api_views.LinkStatusView.as_view(), name='api_link_status'),
    path('links/<uuid:link_id>/comments/', api_views.LinkCommentsView.as_view(), name='api_link_comments'),
    path('links/<uuid:link_id>/export/', api_views.LinkExportView.as_view(), name='api_link_export'),
    path('comments/<uuid:comment_id>/tokens/', api_views.CommentTokensView.as_view(), name='api_comment_tokens'),
    path('comments/<uuid:comment_id>/toggle-token/<int:token_position>/',
         api_views.ToggleTokenView.as_view(), name='api_toggle_token'),
    path('comments/<uuid:comment_id>/set-token-labels/<int:token_position>/',
         views.set_token_labels, name='api_set_token_labels'),
    path('comments/<uuid:comment_id>/set-comment-labels/',
         views.set_comment_labels, name='api_set_comment_labels'),
    path('comments/<uuid:comment_id>/manual-label/',
         api_views.ManualLabelView.as_view(), name='api_manual_label'),
    # Task control endpoints
    path('links/<uuid:link_id>/stop-fetch/',
         api_views.StopFetchTaskView.as_view(), name='api_stop_fetch'),
    path('links/<uuid:link_id>/stop-annotate/',
         api_views.StopAnnotationTaskView.as_view(), name='api_stop_annotate'),
    path('links/<uuid:link_id>/retry-fetch/',
         api_views.RetryFetchView.as_view(), name='api_retry_fetch'),
    path('links/<uuid:link_id>/clear-refetch/',
         api_views.ClearAndRefetchView.as_view(), name='api_clear_and_refetch'),
    path('links/<uuid:link_id>/continue-annotate/',
         api_views.ContinueAnnotationView.as_view(), name='api_continue_annotate'),
    path('links/<uuid:link_id>/reannotate/',
         api_views.ReannotateLinkView.as_view(), name='api_reannotate'),
    # Label management API
    path('labels/', api_views.LabelListView.as_view(), name='api_label_list'),
    path('labels/create/', api_views.LabelCreateView.as_view(), name='api_label_create'),
]

# Default urlpatterns for backward compatibility
urlpatterns = web_urls