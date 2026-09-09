import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'annotahub.settings')

app = Celery('annotahub')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

# Lịch chạy định kỳ. DatabaseScheduler sẽ đồng bộ các mục này vào DB lần đầu,
# sau đó có thể chỉnh trực tiếp trong Django Admin mà không cần deploy lại.
app.conf.beat_schedule = {
    'cleanup-old-results': {
        'task': 'comments.tasks.cleanup_old_results',
        'schedule': crontab(hour=0, minute=0),
    },
    # Sao lưu DB hằng ngày lúc 02:00.
    'daily-database-backup': {
        'task': 'comments.tasks.scheduled_database_backup',
        'schedule': crontab(hour=2, minute=0),
    },
    # Dọn các task treo ở trạng thái running quá lâu do worker bị giết.
    'reap-stale-tasks': {
        'task': 'comments.tasks.reap_stale_task_progress',
        'schedule': crontab(minute='*/15'),
    },
}


@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
