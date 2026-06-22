"""annotahub URL Configuration"""
from django.contrib import admin
from django.urls import path, include
from django.conf.urls.i18n import i18n_patterns
from django.conf import settings
from django.conf.urls.static import static

from comments import views as comment_views
from comments.urls import api_urls as comments_api_urls

from django.conf.urls.i18n import set_language

urlpatterns = [
    path('admin/', admin.site.urls),
    path('i18n/setlang/', set_language, name='set_language'),
    # API endpoints
    path('api/', include(comments_api_urls)),
] + i18n_patterns(
    path('', comment_views.dashboard, name='dashboard'),
    # Web views for comments app
    path('', include('comments.urls')),
) 
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    for dirlink in settings.STATICFILES_DIRS:
        print(settings.STATIC_URL, 'add dir', dirlink)
        urlpatterns += static(settings.STATIC_URL, document_root=dirlink)
