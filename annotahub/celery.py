import os
from celery import Celery
from celery.schedules import crontab

# Set the default Django settings module
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'annotahub.settings')

app = Celery('annotahub')

# Using config_from_object to read configuration from django settings
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks from all registered Django apps
app.autodiscover_tasks()

# Celery Beat schedule for periodic tasks
app.conf.beat_schedule = {
    # Clean up old task results every day at midnight
    'cleanup-old-results': {
        'task': 'comments.tasks.cleanup_old_results',
        'schedule': crontab(hour=0, minute=0),
    },
}

@app.task(bind=True, verbose=True)
def debug_task(self):
    print(f'Request: {self.request!r}')