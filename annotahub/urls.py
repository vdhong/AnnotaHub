"""Cấu hình URL gốc.

Endpoint cho máy gọi máy (health check, REST API, SSE) phải nằm ngoài
i18n_patterns. Nằm trong thì /health/ bị chuyển hướng thành /vi/health/, và
healthcheck của Docker lẫn load balancer nhận 302 rồi kết luận service đã
chết. Chỉ trang HTML cho người dùng mới đi qua i18n_patterns.
"""
from django.conf import settings
from django.conf.urls.i18n import i18n_patterns, set_language
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from comments import views as comment_views
from comments.urls import api_urls as comments_api_urls
from comments.urls import machine_urls as comments_machine_urls

# --- Không phụ thuộc ngôn ngữ (máy gọi máy) ---
urlpatterns = [
    path('admin/', admin.site.urls),
    path('i18n/setlang/', set_language, name='set_language'),
    path('', include(comments_machine_urls)),
    # Namespace 'api' để template và view có thể reverse tên URL của REST API
    # (ví dụ {% url 'api:api_dataset_versions' project.id %}).
    path('api/', include((comments_api_urls, 'comments'), namespace='api')),
]

# --- Trang HTML cho người dùng (có tiền tố ngôn ngữ) ---
urlpatterns += i18n_patterns(
    path('', comment_views.dashboard, name='dashboard'),
    path('', include('comments.urls')),
)

handler403 = 'comments.views.handler403'
handler404 = 'comments.views.handler404'
handler500 = 'comments.views.handler500'

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    for static_dir in settings.STATICFILES_DIRS:
        urlpatterns += static(settings.STATIC_URL, document_root=static_dir)
