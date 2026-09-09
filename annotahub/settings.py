"""
Django settings for AnnotaHub project.

Nguyên tắc:
- Mặc định an toàn: DEBUG tắt, không có SECRET_KEY hợp lệ thì từ chối khởi động.
- Mọi giá trị nhạy cảm đọc từ biến môi trường (.env chỉ dùng cho môi trường dev/local).
"""
import os
import re
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')


def env_bool(name, default=False):
    """Đọc biến môi trường dạng boolean, chấp nhận 1/true/yes/on."""
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'y', 'on')


def env_int(name, default=0):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_list(name, default=''):
    raw = os.environ.get(name, default) or ''
    return [item.strip() for item in raw.split(',') if item.strip()]


# ---------------------------------------------------------------------------
# Core security
# ---------------------------------------------------------------------------
# Mặc định là False. Muốn bật debug phải khai báo tường minh DEBUG=1.
DEBUG = env_bool('DEBUG', False)

_PLACEHOLDER_SECRETS = {
    '',
    'your-secret-key-here-change-in-production',
    'django-insecure-change-me-in-production',
}
SECRET_KEY = os.environ.get('SECRET_KEY', '').strip()

if SECRET_KEY in _PLACEHOLDER_SECRETS or SECRET_KEY.startswith('django-insecure-'):
    if DEBUG:
        # Chỉ chấp nhận khoá tạm khi chạy dev; không bao giờ dùng lại giá trị cố định.
        from django.core.management.utils import get_random_secret_key

        SECRET_KEY = get_random_secret_key()
    else:
        raise ImproperlyConfigured(
            'SECRET_KEY chưa được cấu hình (hoặc còn là giá trị mẫu). '
            'Sinh khoá mới bằng: python -c "from django.core.management.utils import '
            'get_random_secret_key; print(get_random_secret_key())" rồi đặt vào biến '
            'môi trường SECRET_KEY.'
        )

if len(SECRET_KEY) < 50 and not DEBUG:
    raise ImproperlyConfigured('SECRET_KEY phải dài tối thiểu 50 ký tự.')

ALLOWED_HOSTS = env_list('ALLOWED_HOSTS', 'localhost,127.0.0.1')
if not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ['localhost', '127.0.0.1']
if '*' in ALLOWED_HOSTS and not DEBUG:
    raise ImproperlyConfigured(
        "ALLOWED_HOSTS='*' không được phép khi DEBUG=False. "
        'Hãy liệt kê tên miền cụ thể.'
    )

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_celery_beat',
    'django_celery_results',
    'rest_framework',
    'comments',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'annotahub.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'django.template.context_processors.i18n',
            ],
        },
    },
]

WSGI_APPLICATION = 'annotahub.wsgi.application'

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get('DATABASE_URL', '')
url_pattern = re.compile(
    r'postgres(?:ql)?://(?P<user>[^:]+):(?P<password>[^@]+)@'
    r'(?P<host>[\w.-]+):(?P<port>\d+)/(?P<database>[\w-]+)'
)
url_match = url_pattern.match(DATABASE_URL) if DATABASE_URL else None

if url_match:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': url_match.group('database'),
            'USER': url_match.group('user'),
            'PASSWORD': url_match.group('password'),
            'HOST': url_match.group('host'),
            'PORT': url_match.group('port'),
            'CONN_MAX_AGE': env_int('DB_CONN_MAX_AGE', 60),
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ.get('POSTGRES_DB', 'annotahub'),
            'USER': os.environ.get('POSTGRES_USER', 'annotahub_user'),
            'PASSWORD': os.environ.get('POSTGRES_PASSWORD', ''),
            'HOST': os.environ.get('DB_HOST', 'localhost'),
            'PORT': os.environ.get('DB_PORT', '5432'),
            'CONN_MAX_AGE': env_int('DB_CONN_MAX_AGE', 60),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------
LANGUAGE_CODE = 'vi'
LANGUAGES = [
    ('vi', 'Tiếng Việt'),
    ('en', 'English'),
]
LOCALE_PATHS = [BASE_DIR / 'locale']
TIME_ZONE = 'Asia/Ho_Chi_Minh'
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static & media
# ---------------------------------------------------------------------------
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
# Manifest storage băm tên file để cache vĩnh viễn. Cần chạy collectstatic
# trước (image build làm việc này). Tách khỏi DEBUG để build có thể ép bật.
USE_STATIC_MANIFEST = env_bool('USE_STATIC_MANIFEST', not DEBUG)
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {
        'BACKEND': (
            'whitenoise.storage.CompressedManifestStaticFilesStorage'
            if USE_STATIC_MANIFEST
            else 'django.contrib.staticfiles.storage.StaticFilesStorage'
        )
    },
}
WHITENOISE_MAX_AGE = 60 * 60 * 24 * 365 if USE_STATIC_MANIFEST else 0

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Nơi lưu file của DatasetVersion. Là volume dùng chung giữa web và worker
# (xem docker-compose.yml), nên phải cấu hình được để test ghi ra thư mục tạm.
EXPORT_ROOT = Path(os.environ.get('EXPORT_ROOT') or (BASE_DIR / 'exports'))

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
DATA_UPLOAD_MAX_MEMORY_SIZE = env_int('DATA_UPLOAD_MAX_MEMORY_SIZE', 10 * 1024 * 1024)

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = 'django-db'
CELERY_CACHE_BACKEND = 'django-cache'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_MAX_TASKS_PER_CHILD = 100
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = False
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = env_int('CELERY_TASK_TIME_LIMIT', 3600)
CELERY_TASK_SOFT_TIME_LIMIT = env_int('CELERY_TASK_SOFT_TIME_LIMIT', 3300)
CELERY_RESULT_EXTENDED = True
CELERY_RESULT_EXPIRES = 60 * 60 * 24 * 7

# Redis dùng chung cho cache + cờ huỷ task (soft cancel)
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': REDIS_URL,
        'KEY_PREFIX': 'annotahub',
    }
}

# ---------------------------------------------------------------------------
# Tích hợp ngoài
# ---------------------------------------------------------------------------
YOUTUBE_API_KEY = os.environ.get('YOUTUBE_API_KEY', '')

OLLAMA_BASE_URL = os.environ.get('OLLAMA_BASE_URL', '')
OLLAMA_API_KEY = os.environ.get('OLLAMA_API_KEY', '')
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', '')

# Khoá mã hoá API key lưu trong DB (Fernet, base64 32 byte).
# Rỗng => lưu plaintext kèm cảnh báo (chỉ chấp nhận ở môi trường dev).
FIELD_ENCRYPTION_KEY = os.environ.get('FIELD_ENCRYPTION_KEY', '').strip()

# Số request LLM đồng thời cho mỗi batch annotate.
ANNOTATION_BATCH_SIZE = env_int('ANNOTATION_BATCH_SIZE', 50)
ANNOTATION_CONCURRENCY = env_int('ANNOTATION_CONCURRENCY', 4)
ANNOTATION_LLM_TEMPERATURE = float(os.environ.get('ANNOTATION_LLM_TEMPERATURE', '0'))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{asctime} [{levelname}] {name}.{funcName}:{lineno} - {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': os.environ.get('DJANGO_LOG_LEVEL', 'WARNING'),
            'propagate': False,
        },
        'comments': {
            'handlers': ['console'],
            'level': os.environ.get('APP_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
    },
}

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/projects/'
LOGOUT_REDIRECT_URL = '/login/'

SITE_URL = os.environ.get('SITE_URL', 'http://localhost:8000')

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
EMAIL_BACKEND = os.environ.get(
    'EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend'
)
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = env_int('EMAIL_PORT', 587)
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', True)
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.environ.get(
    'DEFAULT_FROM_EMAIL', f'AnnotaHub <{EMAIL_HOST_USER}>' if EMAIL_HOST_USER else 'AnnotaHub <no-reply@localhost>'
)

# ---------------------------------------------------------------------------
# HTTPS / cookie / header security
# ---------------------------------------------------------------------------
CSRF_TRUSTED_ORIGINS = env_list(
    'CSRF_TRUSTED_ORIGINS', 'http://localhost:6868,http://127.0.0.1:6868'
)

SESSION_COOKIE_SECURE = env_bool('SESSION_COOKIE_SECURE', not DEBUG)
CSRF_COOKIE_SECURE = env_bool('CSRF_COOKIE_SECURE', not DEBUG)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_AGE = env_int('SESSION_COOKIE_AGE', 60 * 60 * 24 * 14)

SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', False)
SECURE_HSTS_SECONDS = env_int('SECURE_HSTS_SECONDS', 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
X_FRAME_OPTIONS = 'DENY'

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.LimitOffsetPagination',
    'PAGE_SIZE': 50,
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.ScopedRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'annotation': '600/min',
        'export': '10/min',
        'heavy': '20/min',
    },
    'UNAUTHENTICATED_USER': 'django.contrib.auth.models.AnonymousUser',
}

# Giới hạn tần suất đăng nhập/đăng ký (áp dụng ở tầng view, dùng cache Redis).
LOGIN_RATELIMIT_ATTEMPTS = env_int('LOGIN_RATELIMIT_ATTEMPTS', 10)
LOGIN_RATELIMIT_WINDOW = env_int('LOGIN_RATELIMIT_WINDOW', 300)
