"""URLs cho app comments.

Chia làm 3 nhóm:
- machine_urls : endpoint máy gọi máy (health, SSE), không có tiền tố ngôn ngữ.
- api_urls     : REST API (JSON), không có tiền tố ngôn ngữ, đều yêu cầu đăng nhập.
- web_urls     : trang HTML, có tiền tố ngôn ngữ (/vi/..., /en/...).
"""
from django.urls import path

from . import api_views, views

app_name = 'comments'

# ---------------------------------------------------------------------------
# Endpoint máy gọi máy: không phụ thuộc ngôn ngữ
# ---------------------------------------------------------------------------
machine_urls = [
    path('health/', views.health_check, name='health_check'),
    path('sse/progress/<uuid:link_id>/', views.progress_event_stream,
         name='progress_event_stream'),
]

# ---------------------------------------------------------------------------
# Trang HTML
# ---------------------------------------------------------------------------
web_urls = [
    # Xác thực
    path('login/', views.custom_login, name='login'),
    path('logout/', views.custom_logout, name='logout'),
    path('register/', views.register, name='register'),
    path('verify-email/<str:token>/', views.verify_email, name='verify_email'),
    path('resend-verification/', views.resend_verification, name='resend_verification'),
    path('invite/<str:token>/', views.accept_invitation, name='accept_invitation'),

    # Cài đặt người dùng
    path('settings/', views.user_settings, name='user_settings'),

    # Dự án
    path('projects/', views.project_list, name='project_list'),
    path('projects/create/', views.project_create, name='project_create'),
    path('projects/<uuid:project_id>/', views.project_detail, name='project_detail'),
    path('projects/<uuid:project_id>/edit/', views.project_edit, name='project_edit'),
    path('projects/<uuid:project_id>/delete/', views.project_delete, name='project_delete'),
    path('projects/<uuid:project_id>/export/', views.project_export, name='project_export'),
    path('projects/<uuid:project_id>/labels/', views.project_labels_settings,
         name='project_labels_settings'),
    path('projects/<uuid:project_id>/participants/', views.project_manage_participants,
         name='project_manage_participants'),
    path('projects/<uuid:project_id>/lock/', views.project_lock, name='project_lock'),

    # Chất lượng gán nhãn (Giai đoạn 3)
    path('projects/<uuid:project_id>/quality/', views.project_quality,
         name='project_quality'),
    path('projects/<uuid:project_id>/adjudicate/', views.project_adjudicate,
         name='project_adjudicate'),
    path('projects/<uuid:project_id>/versions/', views.project_versions,
         name='project_versions'),
    path('projects/<uuid:project_id>/exports/<uuid:export_id>/download/',
         views.export_download, name='export_download'),
    path('projects/<uuid:project_id>/versions/<uuid:version_id>/download/',
         views.dataset_version_download, name='dataset_version_download'),
    path('projects/<uuid:project_id>/versions/<uuid:version_id>/restore/',
         views.dataset_version_restore, name='dataset_version_restore'),
    path('projects/<uuid:project_id>/versions/<uuid:version_id>/delete/',
         views.dataset_version_delete, name='dataset_version_delete'),

    # Màn hình gán nhãn tập trung (focus mode)
    path('links/<uuid:link_id>/annotate/', views.annotate_workspace,
         name='annotate_workspace'),

    # Nhãn
    path('labels/', views.label_list, name='label_list'),
    path('labels/create/', views.label_create, name='label_create'),
    path('labels/<uuid:label_id>/edit/', views.label_edit, name='label_edit'),
    path('labels/<uuid:label_id>/delete/', views.label_delete, name='label_delete'),

    # Nguồn dữ liệu
    path('projects/<uuid:project_id>/links/add/', views.add_youtube_link,
         name='add_youtube_link'),
    path('projects/<uuid:project_id>/import/', views.import_csv, name='import_csv'),
    path('links/<uuid:link_id>/detail/', views.link_detail, name='link_detail'),
    path('links/<uuid:link_id>/delete/', views.delete_youtube_link,
         name='delete_youtube_link'),
]

# ---------------------------------------------------------------------------
# REST API: mọi endpoint đều yêu cầu đăng nhập + kiểm tra quyền trên dự án
# ---------------------------------------------------------------------------
api_urls = [
    # Dự án
    path('projects/', api_views.ProjectListView.as_view(), name='api_project_list'),
    path('projects/create/', api_views.ProjectCreateView.as_view(), name='api_project_create'),
    path('projects/<uuid:project_id>/', api_views.ProjectDetailView.as_view(),
         name='api_project_detail'),
    path('projects/<uuid:project_id>/links/', api_views.LinkManageView.as_view(),
         name='api_link_manage'),
    path('projects/<uuid:project_id>/labels/', api_views.ProjectLabelsView.as_view(),
         name='api_project_labels'),

    # Link
    path('links/<uuid:link_id>/status/', api_views.LinkStatusView.as_view(),
         name='api_link_status'),
    path('links/<uuid:link_id>/comments/', api_views.LinkCommentsView.as_view(),
         name='api_link_comments'),
    path('links/<uuid:link_id>/export/', api_views.LinkExportView.as_view(),
         name='api_link_export'),
    path('export-formats/', api_views.ExportFormatsView.as_view(),
         name='api_export_formats'),

    # Gán nhãn
    path('comments/<uuid:comment_id>/tokens/', api_views.CommentTokensView.as_view(),
         name='api_comment_tokens'),
    path('comments/<uuid:comment_id>/set-token-labels/<int:token_position>/',
         api_views.SetTokenLabelView.as_view(), name='api_set_token_labels'),
    path('comments/<uuid:comment_id>/set-comment-labels/',
         api_views.SetCommentLabelView.as_view(), name='api_set_comment_labels'),
    path('comments/<uuid:comment_id>/set-token-span/',
         api_views.SetTokenSpanLabelView.as_view(), name='api_set_token_span'),
    path('comments/<uuid:comment_id>/skip/', api_views.SkipCommentView.as_view(),
         name='api_skip_comment'),
    path('comments/<uuid:comment_id>/accept-ai/',
         api_views.AcceptAiSuggestionView.as_view(), name='api_accept_ai'),
    path('comments/<uuid:comment_id>/annotations/',
         api_views.CommentAnnotationsView.as_view(), name='api_comment_annotations'),

    # Hàng đợi gán nhãn (focus mode / phân công)
    path('links/<uuid:link_id>/queue/', api_views.AnnotationQueueView.as_view(),
         name='api_annotation_queue'),
    path('projects/<uuid:project_id>/assign/', api_views.AssignWorkView.as_view(),
         name='api_assign_work'),

    # Chất lượng & phân xử
    path('projects/<uuid:project_id>/agreement/', api_views.AgreementView.as_view(),
         name='api_agreement'),
    path('projects/<uuid:project_id>/conflicts/', api_views.ConflictListView.as_view(),
         name='api_conflicts'),
    path('comments/<uuid:comment_id>/adjudicate/', api_views.AdjudicateView.as_view(),
         name='api_adjudicate'),
    path('projects/<uuid:project_id>/progress/', api_views.AnnotatorProgressView.as_view(),
         name='api_annotator_progress'),

    # Điều khiển task
    path('links/<uuid:link_id>/stop-fetch/', api_views.StopFetchTaskView.as_view(),
         name='api_stop_fetch'),
    path('links/<uuid:link_id>/stop-annotate/', api_views.StopAnnotationTaskView.as_view(),
         name='api_stop_annotate'),
    path('links/<uuid:link_id>/retry-fetch/', api_views.RetryFetchView.as_view(),
         name='api_retry_fetch'),
    path('links/<uuid:link_id>/clear-refetch/', api_views.ClearAndRefetchView.as_view(),
         name='api_clear_and_refetch'),
    path('links/<uuid:link_id>/continue-annotate/', api_views.ContinueAnnotationView.as_view(),
         name='api_continue_annotate'),
    path('links/<uuid:link_id>/reannotate/', api_views.ReannotateLinkView.as_view(),
         name='api_reannotate'),

    # Nhãn
    path('labels/', api_views.LabelListView.as_view(), name='api_label_list'),
    path('labels/create/', api_views.LabelCreateView.as_view(), name='api_label_create'),

    # Phiên bản dataset
    path('projects/<uuid:project_id>/exports/', api_views.ExportJobsView.as_view(),
         name='api_export_jobs'),
    path('projects/<uuid:project_id>/versions/', api_views.DatasetVersionView.as_view(),
         name='api_dataset_versions'),
]

urlpatterns = web_urls
