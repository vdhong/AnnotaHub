"""Settings dùng cho test: nhanh, không phụ thuộc dịch vụ ngoài."""
import os

os.environ.setdefault('DEBUG', '1')
os.environ.setdefault('SECRET_KEY', 'test-only-secret-key-not-used-in-production-0123456789')

from .settings import *  # noqa: F401,F403,E402

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

CACHES = {
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}
}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = 'memory://'
CELERY_RESULT_BACKEND = 'cache+memory://'

EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

# Tắt whitenoise manifest (chưa chạy collectstatic khi test)
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}

FIELD_ENCRYPTION_KEY = 'ZmFrZS10ZXN0LWtleS1mb3ItZmVybmV0LTMyYnl0ZXM9PQ=='

# Tắt rate limit khi chạy test: nếu để bật, các test lặp nhiều request sẽ ngẫu
# nhiên nhận 429 và che mất lỗi thật. Bản thân cơ chế throttle được kiểm tra
# riêng trong test_throttling.py với ngưỡng đặt lại tường minh.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    'DEFAULT_THROTTLE_RATES': {'annotation': None, 'export': None, 'heavy': None},
}
LOGGING['root']['level'] = 'ERROR'  # noqa: F405
