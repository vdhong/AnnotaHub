"""
Celery task: tải bình luận từ nguồn dữ liệu và gán nhãn bằng LLM.

Vài chỗ dễ vấp khi sửa file này:

Việc gán nhãn phải chia batch. Chạy tuần tự mười nghìn comment trong một task
mất chừng tám tiếng, vượt time limit một giờ; task bị giết, rồi vì acks_late
nên được giao lại và chạy lại từ đầu, đốt tiền API gấp bội.

Cờ ai_processed giữ cho task idempotent: chạy lại không xử lý lại comment đã
xong.

Huỷ task bằng cờ trong Redis chứ đừng dùng revoke(terminate=True). revoke gửi
SIGTERM giết cả tiến trình worker, kéo theo mọi task khác đang chạy chung.

task_progress phải được khởi tạo trước mọi nhánh dùng đến nó, kể cả nhánh dự án
bị khoá. Thiếu một nhánh là task chết vì UnboundLocalError và bản ghi tiến độ
treo ở trạng thái running vĩnh viễn.

Bảng tra nhãn dựng một lần thành dict, không truy vấn DB cho từng token.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.template.defaultfilters import filesizeformat
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from . import task_messages as msg
from .models import (
    Comment,
    DatasetVersion,
    ExportRecord,
    Project,
    ProjectLabel,
    TaskProgress,
    Token,
    TokenAnnotation,
    UserSettings,
    YouTubeLink,
)
from .services.annotation import (
    store_ai_comment_annotation,
    store_ai_token_annotations,
)
from .services.ollama_service import (
    LLMError,
    QuotaExhaustedError,
    create_token_annotations,
    get_comment_label_name,
    process_comment,
)
from .services.stats import project_label_map
from .services.youtube_service import fetch_comments

logger = logging.getLogger(__name__)

TERMINAL_TASK_STATUSES = ('completed', 'failed', 'cancelled')
CANCEL_FLAG_TTL = 60 * 60 * 6


# ---------------------------------------------------------------------------
# Cấu hình theo chủ dự án
# ---------------------------------------------------------------------------
def _owner_settings(project: Project) -> UserSettings:
    """
    Lấy UserSettings của chủ dự án, tạo mới nếu chưa có.

    Luôn get_or_create: chủ dự án chưa từng mở trang Cài đặt thì vẫn phải rơi
    về cấu hình toàn cục, chứ không phải báo "Chưa thiết lập API KEY".
    """
    obj, _created = UserSettings.objects.get_or_create(user=project.owner)
    return obj


def get_owner_youtube_api_key(project: Project):
    """YouTube API key của chủ dự án, tự động lùi về cấu hình toàn cục."""
    return _owner_settings(project).get_youtube_api_key() or None


def get_owner_ollama_config(project: Project):
    """(base_url, api_key, model) của chủ dự án, tự động lùi về cấu hình toàn cục."""
    obj = _owner_settings(project)
    return (
        obj.get_ollama_base_url(),
        obj.get_ollama_api_key(),
        obj.get_ollama_model(),
    )


def _gather_labels_info(youtube_link: YouTubeLink):
    """Danh sách định nghĩa nhãn của dự án để đưa vào prompt."""
    project_labels = ProjectLabel.objects.filter(
        project=youtube_link.project
    ).select_related('label')
    return [{
        'name': pl.display_name,
        'description': pl.display_description or '',
        'color': pl.display_color,
    } for pl in project_labels]


# ---------------------------------------------------------------------------
# Cờ huỷ mềm
# ---------------------------------------------------------------------------
def _cancel_key(link_id: str) -> str:
    return f'cancel:link:{link_id}'


def request_cancel(link_id: str) -> None:
    cache.set(_cancel_key(link_id), True, CANCEL_FLAG_TTL)


def clear_cancel(link_id: str) -> None:
    cache.delete(_cancel_key(link_id))


def is_cancelled(link_id: str) -> bool:
    try:
        return bool(cache.get(_cancel_key(link_id)))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Tiến độ
# ---------------------------------------------------------------------------
def _bootstrap_task_progress(youtube_link: YouTubeLink, task_type: str,
                             task_id: str, current_step: str) -> TaskProgress:
    return TaskProgress.objects.update_or_create(
        task_id=task_id,
        task_type=task_type,
        defaults={
            'youtube_link': youtube_link,
            'status': 'running',
            'progress_percent': 0,
            'current_step': current_step,
            'step_params': {},
            'total_items': 0,
            'processed_items': 0,
            'error_message': '',
            'started_at': timezone.now(),
            'completed_at': None,
        },
    )[0]


def get_effective_task_progress(youtube_link_id: str, task_type: str):
    """Bản ghi tiến độ đang chạy, nếu không có thì bản ghi mới nhất cùng loại."""
    base = TaskProgress.objects.filter(
        youtube_link_id=youtube_link_id, task_type=task_type
    )
    return (
        base.filter(status='running').order_by('-created_at').first()
        or base.order_by('-created_at').first()
    )


def _finish_progress(task_progress, status, step='', error='', params=None):
    if task_progress is None:
        return
    task_progress.status = status
    if step:
        task_progress.current_step = step
        task_progress.step_params = params or {}
    if error:
        task_progress.error_message = error[:2000]
    if status == 'completed':
        task_progress.progress_percent = 100
    task_progress.completed_at = timezone.now()
    task_progress.save(update_fields=[
        'status', 'current_step', 'step_params', 'error_message',
        'progress_percent', 'completed_at',
    ])
def _update_progress(task_progress, progress_percent, step_key, total, processed,
                     params=None):
    if task_progress is None:
        return
    try:
        task_progress.progress_percent = progress_percent
        task_progress.current_step = step_key
        task_progress.step_params = params or {}
        task_progress.total_items = total
        task_progress.processed_items = processed
        task_progress.save(update_fields=[
            'progress_percent', 'current_step', 'step_params',
            'total_items', 'processed_items',
        ])
    except Exception as exc:
        logger.error('Không cập nhật được tiến độ: %s', exc)


def enqueue_fetch_comments_task(youtube_link: YouTubeLink,
                                current_step=msg.QUEUED_FETCH) -> str:
    task_id = str(uuid.uuid4())
    clear_cancel(str(youtube_link.id))
    _bootstrap_task_progress(youtube_link, 'fetching', task_id, current_step)
    fetch_comments_task.apply_async((str(youtube_link.id),), task_id=task_id)
    return task_id


def enqueue_annotation_task(youtube_link: YouTubeLink,
                            current_step=msg.QUEUED_ANNOTATION) -> str:
    task_id = str(uuid.uuid4())
    clear_cancel(str(youtube_link.id))
    _bootstrap_task_progress(youtube_link, 'annotating', task_id, current_step)
    annotate_comments_task.apply_async((str(youtube_link.id),), task_id=task_id)
    return task_id


def _derive_link_status(youtube_link: YouTubeLink) -> str:
    """Trạng thái link suy ra từ dữ liệu thực tế đang lưu."""
    if not youtube_link.comments.exists():
        return 'pending'
    # ai_processed thay cho ai_label__isnull: comment được AI kết luận là 'O'
    # vẫn tính là đã xử lý.
    if youtube_link.comments.filter(ai_processed=False).exclude(
        is_meaningful=False
    ).exists():
        return 'completed'
    return 'annotated'


def cancel_tasks_for_link_now(youtube_link_id: str) -> dict:
    """
    Yêu cầu dừng mọi task của một link.

    Đặt cờ huỷ mềm (task tự thoát ở điểm an toàn) và revoke không terminate,
    để không giết tiến trình worker đang chạy các task khác.
    """
    from annotahub.celery import app

    request_cancel(youtube_link_id)

    running = list(TaskProgress.objects.filter(
        youtube_link_id=youtube_link_id, status__in=['pending', 'running']
    ))
    for task in running:
        if task.task_id:
            app.control.revoke(task.task_id, terminate=False)
        task.status = 'cancelled'
        task.current_step = task.current_step or 'Task cancelled by user'
        task.completed_at = timezone.now()
        task.save(update_fields=['status', 'current_step', 'completed_at'])

    link_status = None
    link = YouTubeLink.objects.filter(id=youtube_link_id).first()
    if link is not None:
        link.status = _derive_link_status(link)
        link.save(update_fields=['status', 'updated_at'])
        link_status = link.status

    logger.info('Đã huỷ %s task cho link %s', len(running), youtube_link_id)
    return {'cancelled': len(running), 'status': link_status}


def clear_link_data_for_refetch(youtube_link_id: str) -> dict:
    link = YouTubeLink.objects.filter(id=youtube_link_id).first()
    if link is None:
        logger.error('Không tìm thấy link %s để xoá dữ liệu', youtube_link_id)
        return {'status': 'error', 'message': 'YouTubeLink not found'}

    cancel_result = cancel_tasks_for_link_now(youtube_link_id)

    deleted = link.comments.count()
    link.comments.all().delete()
    TaskProgress.objects.filter(youtube_link=link).delete()

    link.comment_count = 0
    link.status = 'pending'
    link.save(update_fields=['comment_count', 'status', 'updated_at'])

    logger.info('Đã xoá %s bình luận và đặt lại link %s', deleted, youtube_link_id)
    return {
        'status': 'reset',
        'deleted_comments': deleted,
        'cancelled_tasks': cancel_result.get('cancelled', 0),
    }


# ---------------------------------------------------------------------------
# Task: tải bình luận
# ---------------------------------------------------------------------------
@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def fetch_comments_task(self, youtube_link_id: str):
    link = YouTubeLink.objects.select_related('project').filter(id=youtube_link_id).first()
    if link is None:
        logger.error('Không tìm thấy link %s', youtube_link_id)
        return {'status': 'error', 'message': 'YouTubeLink not found'}

    # Bản ghi tiến độ được tạo trước mọi nhánh return, kể cả nhánh lỗi.
    task_progress = _bootstrap_task_progress(
        link, 'fetching', self.request.id or str(uuid.uuid4()),
        'Fetching comments from YouTube'
    )

    if link.project.is_locked:
        _finish_progress(task_progress, 'failed', error=msg.ERR_PROJECT_LOCKED)
        link.status = _derive_link_status(link)
        link.save(update_fields=['status', 'updated_at'])
        return {'status': 'error', 'message': 'Project is locked'}

    if link.kind != 'youtube':
        _finish_progress(task_progress, 'completed',
                         step=msg.NOT_YOUTUBE)
        return {'status': 'skipped', 'reason': 'not a youtube source'}

    logger.info('Bắt đầu tải bình luận cho video %s', link.video_id)

    try:
        link.status = 'fetching'
        link.save(update_fields=['status', 'updated_at'])

        def on_progress(percent, step, total, processed):
            _update_progress(task_progress, percent, step, total, processed)

        comment_data = fetch_comments(
            link.video_id,
            max_results=None,
            on_progress=on_progress,
            api_key=get_owner_youtube_api_key(link.project),
            should_stop=lambda: is_cancelled(youtube_link_id),
        )

        if is_cancelled(youtube_link_id):
            _finish_progress(task_progress, 'cancelled', step=msg.CANCELLED)
            return {'status': 'cancelled'}

        created = 0
        updated = 0
        with transaction.atomic():
            for data in comment_data:
                text = data.get('text', '')
                _obj, was_created = Comment.objects.update_or_create(
                    youtube_link=link,
                    youtube_comment_id=data['youtube_comment_id'],
                    defaults={
                        'author': data.get('author', ''),
                        'author_channel_url': data.get('author_channel_url', ''),
                        'avatar_url': data.get('avatar_url', ''),
                        'text': text,
                        # source_text chỉ ghi khi tạo mới -> giữ bản gốc bất biến.
                        'like_count': data.get('like_count', 0),
                        'published_at': data.get('published_at'),
                        'updated_at_source': data.get('updated_at'),
                        'is_public': data.get('is_public', True),
                    },
                )
                if was_created:
                    created += 1
                    # Ghi bản gốc một lần duy nhất, ngay lúc tạo.
                    Comment.objects.filter(pk=_obj.pk).update(
                        source_text=data.get('text_original') or text
                    )
                else:
                    updated += 1

        total_stored = link.comments.count()
        link.comment_count = total_stored
        link.status = 'completed'
        link.save(update_fields=['comment_count', 'status', 'updated_at'])

        fetch_summary = {'created': created, 'updated': updated}
        _update_progress(task_progress, 100, msg.FETCH_DONE,
                         total_stored, total_stored, params=fetch_summary)
        _finish_progress(task_progress, 'completed',
                         step=msg.FETCH_DONE, params=fetch_summary)

        logger.info('Đã lưu %s bình luận cho video %s', total_stored, link.video_id)

        link.status = 'annotating'
        link.save(update_fields=['status', 'updated_at'])
        enqueue_annotation_task(link, msg.QUEUED_ANNOTATION)

        return {
            'status': 'success',
            'comments_created': created,
            'comments_updated': updated,
            'youtube_link_id': youtube_link_id,
        }

    except Exception as exc:
        logger.error('Lỗi tải bình luận cho %s: %s', link.video_id, exc)
        _finish_progress(task_progress, 'failed', error=str(exc))
        link.status = 'failed'
        link.save(update_fields=['status', 'updated_at'])

        message = str(exc)
        # Lỗi cấu hình/khoá API thì retry không giúp gì.
        if 'API' in message or 'quota' in message.lower():
            return {'status': 'error', 'message': message}
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc) from exc
        return {'status': 'error', 'message': message}


# ---------------------------------------------------------------------------
# Task: gán nhãn bằng LLM
# ---------------------------------------------------------------------------
@shared_task(bind=True, max_retries=2, default_retry_delay=120)
def annotate_comments_task(self, youtube_link_id: str):
    """
    Điều phối gán nhãn: chia comment chưa xử lý thành các batch nhỏ và giao cho
    annotate_batch_task. Task này không tự gọi LLM nên không bao giờ chạm
    time limit dù dữ liệu lớn cỡ nào.
    """
    link = YouTubeLink.objects.select_related('project').filter(id=youtube_link_id).first()
    if link is None:
        return {'status': 'error', 'message': 'YouTubeLink not found'}

    task_progress = _bootstrap_task_progress(
        link, 'annotating', self.request.id or str(uuid.uuid4()),
        msg.PREPARING
    )

    if link.project.is_locked:
        _finish_progress(task_progress, 'failed', error=msg.ERR_PROJECT_LOCKED)
        return {'status': 'error', 'message': 'Project is locked'}

    labels_info = _gather_labels_info(link)
    if not labels_info:
        _finish_progress(task_progress, 'failed',
                         error=msg.ERR_NO_LABELS)
        link.status = 'failed'
        link.save(update_fields=['status', 'updated_at'])
        return {'status': 'error', 'message': 'no labels configured'}

    base_url, api_key, model = get_owner_ollama_config(link.project)
    if not (base_url and api_key and model):
        _finish_progress(
            task_progress, 'failed',
            error=msg.ERR_OLLAMA_MISSING
        )
        link.status = 'failed'
        link.save(update_fields=['status', 'updated_at'])
        return {'status': 'error', 'message': 'ollama not configured'}

    pending_ids = list(
        link.comments.filter(ai_processed=False)
        .exclude(is_meaningful=False)
        .values_list('id', flat=True)
    )
    total = len(pending_ids)

    if total == 0:
        _finish_progress(task_progress, 'completed', step=msg.NO_PENDING)
        link.status = _derive_link_status(link)
        link.save(update_fields=['status', 'updated_at'])
        return {'status': 'success', 'annotated': 0}

    task_progress.total_items = total
    task_progress.save(update_fields=['total_items'])

    link.status = 'annotating'
    link.save(update_fields=['status', 'updated_at'])

    batch_size = max(1, settings.ANNOTATION_BATCH_SIZE)
    batches = [pending_ids[i:i + batch_size] for i in range(0, total, batch_size)]

    for batch in batches:
        annotate_batch_task.apply_async((
            youtube_link_id,
            [str(cid) for cid in batch],
            str(task_progress.id),
        ))

    logger.info('Đã chia %s bình luận thành %s batch cho link %s',
                total, len(batches), youtube_link_id)
    _update_progress(task_progress, 0, msg.QUEUED_ANNOTATION, total, 0)

    return {'status': 'queued', 'total': total, 'batches': len(batches)}


@shared_task(bind=True, max_retries=2, default_retry_delay=60,
             soft_time_limit=1800, time_limit=1900)
def annotate_batch_task(self, youtube_link_id: str, comment_ids: list, progress_id: str):
    """Gán nhãn một batch comment. Idempotent: bỏ qua comment đã xử lý."""
    link = YouTubeLink.objects.select_related('project').filter(id=youtube_link_id).first()
    if link is None:
        return {'status': 'error', 'message': 'YouTubeLink not found'}

    task_progress = TaskProgress.objects.filter(id=progress_id).first()

    if is_cancelled(youtube_link_id):
        logger.info('Batch bị huỷ cho link %s', youtube_link_id)
        return {'status': 'cancelled'}

    project = link.project
    labels_info = _gather_labels_info(link)
    base_url, api_key, model = get_owner_ollama_config(project)

    # Bảng tra nhãn dựng một lần cho cả batch, thay vì truy vấn DB cho mỗi token.
    label_map = project_label_map(project)

    comments = list(
        Comment.objects.filter(id__in=comment_ids, ai_processed=False)
        .exclude(is_meaningful=False)
    )

    annotated = 0
    failed = 0

    for comment in comments:
        if is_cancelled(youtube_link_id):
            logger.info('Dừng batch giữa chừng theo yêu cầu huỷ.')
            break
        try:
            _annotate_one(comment, project, labels_info, label_map,
                          base_url, api_key, model)
            annotated += 1
        except QuotaExhaustedError as exc:
            # Hết tiền: dừng toàn bộ, không retry, không đốt thêm.
            logger.error('Hết quota LLM: %s', exc)
            request_cancel(youtube_link_id)
            _finish_progress(task_progress, 'failed', error=str(exc))
            link.status = 'failed'
            link.save(update_fields=['status', 'updated_at'])
            return {'status': 'quota_exhausted'}
        except (LLMError, Exception) as exc:
            failed += 1
            logger.warning('Lỗi gán nhãn comment %s: %s', comment.id, exc)

        # Nhịp cập nhật là từng bình luận, không phải từng batch. Batch mặc định
        # 50 bình luận và mỗi bình luận là một lượt gọi LLM, nên báo theo batch
        # thì thanh tiến độ đứng im hàng phút trong khi log worker chạy ầm ầm.
        # Thêm một COUNT cho mỗi lượt gọi LLM là chi phí không đáng kể.
        _sync_batch_progress(link, task_progress)

    _sync_batch_progress(link, task_progress)
    return {'status': 'success', 'annotated': annotated, 'failed': failed}


def _annotate_one(comment: Comment, project, labels_info, label_map,
                  base_url, api_key, model) -> None:
    """Gán nhãn một comment và ghi kết quả (annotation AI + token)."""
    # Luôn gán nhãn dựa trên văn bản gốc nếu có, để kết quả tái lập được.
    source_text = comment.source_text or comment.text
    previous_text = comment.text or ''

    result = process_comment(
        source_text,
        labels_info=labels_info,
        ollama_base_url=base_url,
        ollama_api_key=api_key,
        ollama_model=model,
    )
    annotation = result['annotation']

    vietnamese_text = result.get('vietnamese_text') or source_text
    was_translated = result.get('was_translated', False)

    with transaction.atomic():
        comment_fields = ['ai_processed', 'ai_processed_at', 'model_response',
                          'annotated_at', 'is_meaningful', 'toxicity_confidence',
                          'annotation_source', 'ai_label']

        # Không ghi đè source_text. text chỉ đổi khi thực sự là bản dịch.
        if not comment.source_text:
            comment.source_text = source_text
            comment_fields.append('source_text')

        if was_translated:
            comment.text = vietnamese_text
            comment.original_text = source_text
            comment_fields += ['text', 'original_text']

        # Chạy lại thường cho ra đúng bản dịch cũ, và khi đó token không đổi.
        # So chính văn bản chứ không so bộ token: cùng một chuỗi thì
        # tokenize_text() luôn cắt như nhau.
        text_changed = (comment.text or '').strip() != previous_text.strip()

        comment.model_response = annotation
        comment.annotated_at = timezone.now()
        comment.ai_processed = True
        comment.ai_processed_at = timezone.now()
        comment.is_meaningful = annotation.get('is_meaningful', True)
        comment.toxicity_confidence = annotation.get('confidence')

        if comment.is_meaningful is False:
            comment.ai_label = None
            comment.annotation_source = 'auto'
            comment.save(update_fields=comment_fields)
            # Gỡ nhãn AI khỏi token nhưng giữ token lại. AI kết luận bình luận
            # không có nội dung không có nghĩa là công gán nhãn của người trên
            # đó là rác.
            comment.tokens.update(ai_label=None, toxicity_score=None)
            _clear_ai_token_annotations(comment)
            store_ai_comment_annotation(comment, None, confidence=None,
                                        is_meaningful=False)
            return

        label_name = get_comment_label_name(annotation)
        ai_project_label = label_map.get((label_name or '').lower())
        comment.ai_label = ai_project_label
        comment.annotation_source = 'auto'
        comment.save(update_fields=comment_fields)

        store_ai_comment_annotation(
            comment, ai_project_label,
            confidence=annotation.get('confidence'),
            is_meaningful=True,
        )

        _apply_ai_tokens(comment, annotation, labels_info, label_map,
                         text_changed=text_changed)


def _clear_ai_token_annotations(comment: Comment) -> None:
    """
    Dọn annotation token của AI cho một comment.

    Ràng buộc duy nhất (token, annotator, source) không chặn được trùng lặp ở
    đây: annotator của AI là NULL, mà trong PostgreSQL hai giá trị NULL không
    bằng nhau nên mỗi lần ghi lại đẻ thêm một dòng. Đếm IAA sẽ coi AI như
    nhiều người khác nhau.
    """
    TokenAnnotation.objects.filter(token__comment=comment, source='ai').delete()


def _apply_ai_tokens(comment: Comment, annotation, labels_info, label_map,
                     *, text_changed: bool) -> None:
    """
    Ghi nhãn AI lên token của comment.

    Chỉ đụng vào `ai_label` và `toxicity_score`. Nhãn thủ công, nhãn đã chốt và
    `span_group` là của người gán, không phải chỗ của AI.

    `span_group` để trống: ranh giới cụm do LLM tự cắt không đáng tin, và
    export đã gộp token liền kề cùng nhãn khi thiếu trường này. Người kéo chọn
    cụm thì vẫn ghi span_group của mình và được tôn trọng.

    `text_changed` chỉ đúng khi bản dịch mới khác bản đang lưu. Lúc đó token cũ
    neo vào văn bản không còn tồn tại nên phải dựng lại, và nhãn thủ công trên
    đó cũng mất theo — không có cách nào giữ.
    """
    token_rows = create_token_annotations(
        comment.text, annotation, labels_info=labels_info
    )

    if text_changed:
        Token.objects.filter(comment=comment).delete()
    _clear_ai_token_annotations(comment)

    # Truy vấn thẳng, không qua comment.tokens: nếu chỗ gọi có prefetch_related
    # thì related manager trả về cache đã cũ sau khi xoá ở trên.
    existing = {t.position: t for t in Token.objects.filter(comment=comment)}
    now = timezone.now()
    to_create, to_update, labelled = [], [], []

    for row in token_rows:
        position = row['position']
        label = label_map.get((row.get('assigned_label') or '').lower())
        score = row.get('toxicity_score')
        token = existing.get(position)

        if token is None:
            token = Token(
                comment=comment,
                text=row['text'][:255],
                position=position,
                start_offset=row['start_offset'],
                end_offset=row['end_offset'],
                ai_label=label,
                toxicity_score=score,
                annotated_at=now,
                annotation_source='auto',
            )
            to_create.append(token)
        else:
            # annotation_source và annotated_at giữ nguyên: chúng nói ai chạm
            # vào token này gần nhất, và AI ghi đè nhãn của mình không đổi
            # điều đó.
            token.ai_label = label
            token.toxicity_score = score
            to_update.append(token)

        if label is not None:
            labelled.append((token, label, score))

    if to_create:
        Token.objects.bulk_create(to_create)
    if to_update:
        Token.objects.bulk_update(to_update, ['ai_label', 'toxicity_score'])

    store_ai_token_annotations(labelled)


def _sync_batch_progress(link: YouTubeLink, task_progress) -> None:
    """Cập nhật tiến độ tổng và chốt trạng thái link khi mọi batch đã xong."""
    if task_progress is None:
        return

    total = task_progress.total_items or link.comments.count()
    remaining = link.comments.filter(ai_processed=False).exclude(
        is_meaningful=False
    ).count()
    processed = max(0, total - remaining)
    percent = int((processed / total) * 100) if total else 100

    _update_progress(task_progress, percent, msg.ANNOTATING, total, processed)

    if remaining == 0:
        _finish_progress(task_progress, 'completed', step=msg.ANNOTATING)
        link.status = _derive_link_status(link)
        link.save(update_fields=['status', 'updated_at'])


# ---------------------------------------------------------------------------
# Task bảo trì
# ---------------------------------------------------------------------------
@shared_task
def cleanup_old_results(days: int = 30):
    """Dọn bản ghi tiến độ và kết quả Celery cũ."""
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(days=days)

    old_progress = TaskProgress.objects.filter(
        status__in=TERMINAL_TASK_STATUSES, completed_at__lt=cutoff
    )
    progress_count = old_progress.count()
    old_progress.delete()

    # django_celery_results không tự dọn; không cắt bớt thì bảng phình vô hạn.
    result_count = 0
    try:
        from django_celery_results.models import TaskResult

        old_results = TaskResult.objects.filter(date_done__lt=cutoff)
        result_count = old_results.count()
        old_results.delete()
    except Exception as exc:
        logger.warning('Không dọn được TaskResult: %s', exc)

    logger.info('Đã dọn %s TaskProgress và %s TaskResult', progress_count, result_count)
    return {'task_progress': progress_count, 'task_results': result_count}


@shared_task
def reap_stale_task_progress(stale_minutes: int = 60):
    """
    Đánh dấu thất bại cho các task treo ở 'running' quá lâu.

    Xảy ra khi worker bị giết đột ngột: bản ghi tiến độ ở lại trạng thái running
    vĩnh viễn và UI hiển thị "đang chạy" mãi mãi.
    """
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(minutes=stale_minutes)
    stale = TaskProgress.objects.filter(status='running', started_at__lt=cutoff)
    count = stale.count()
    stale.update(
        status='failed',
        error_message='Task không phản hồi quá lâu, đã được đánh dấu thất bại tự động.',
        completed_at=timezone.now(),
    )
    if count:
        logger.warning('Đã dọn %s task treo', count)
    return {'reaped': count}


@shared_task
def scheduled_database_backup():
    """Sao lưu DB theo lịch của Celery beat."""
    from django.core.management import call_command

    try:
        call_command('db_command', 'backup', '--output-dir', '/app/backups')
        logger.info('Sao lưu DB định kỳ thành công')
        return {'status': 'ok'}
    except Exception as exc:
        logger.error('Sao lưu DB định kỳ thất bại: %s', exc)
        return {'status': 'error', 'message': str(exc)}


@shared_task
def cancel_tasks_for_link(youtube_link_id: str):
    return cancel_tasks_for_link_now(youtube_link_id)


@shared_task
def reannotate_all_comments(youtube_link_id: str):
    """Đặt lại nhãn AI và chạy lại. không đụng tới nhãn do người gán."""
    link = YouTubeLink.objects.filter(id=youtube_link_id).first()
    if link is None:
        return {'status': 'error', 'message': 'YouTubeLink not found'}

    from .models import CommentAnnotation, TokenAnnotation

    CommentAnnotation.objects.filter(comment__youtube_link=link, source='ai').delete()
    TokenAnnotation.objects.filter(token__comment__youtube_link=link, source='ai').delete()
    link.comments.update(
        ai_label=None,
        toxicity_confidence=None,
        model_response=None,
        ai_processed=False,
        ai_processed_at=None,
    )
    Token.objects.filter(comment__youtube_link=link).update(
        ai_label=None, toxicity_score=None
    )

    enqueue_annotation_task(link, msg.QUEUED_ANNOTATION)
    return {'status': 'started', 'youtube_link_id': youtube_link_id}


# ---------------------------------------------------------------------------
# Xuất dữ liệu chạy nền
# ---------------------------------------------------------------------------
@shared_task(bind=True, soft_time_limit=3000)
def run_export(self, export_id: str):
    """
    Sinh file xuất cho một ExportRecord.

    File được ghi ra đĩa ở chế độ nền rồi mới đưa link tải. Xuất thẳng trong
    request theo kiểu streaming thì với dataset lớn, trình duyệt hoặc reverse
    proxy ngắt kết nối giữa chừng và người dùng nhận file cụt mà không biết.
    """
    from .export_service import export_to_file

    record = (
        ExportRecord.objects.filter(id=export_id)
        .select_related('project', 'youtube_link').first()
    )
    if record is None:
        return {'status': 'error', 'message': 'ExportRecord not found'}

    record.status = 'running'
    record.current_step = str(_('Đang bắt đầu'))
    record.save(update_fields=['status', 'current_step'])

    try:
        export_dir = Path(settings.EXPORT_ROOT)
        export_dir.mkdir(parents=True, exist_ok=True)
        safe_name = ''.join(
            ch if ch.isalnum() or ch in '-_' else '-' for ch in record.project.name
        )[:60]
        # Gắn thêm phần đầu của id: hai lần xuất cùng dự án, cùng định dạng
        # trong cùng một giây sẽ ghi đè file của nhau nếu chỉ dựa vào thời gian.
        target = export_dir / (
            f'{safe_name}_{record.export_format}_'
            f'{record.generated_at:%Y%m%d-%H%M%S}_{str(record.id)[:8]}'
        )

        stats = export_to_file(
            record.project, record.youtube_link, record.export_format,
            record.filter_toxicity or 'all', target,
            review_filter=record.review_filter or 'all',
            progress=_progress_reporter(record),
        )

        path = Path(stats['path'])
        record.file_path = str(path)
        record.file_bytes = path.stat().st_size
        record.file_size = filesizeformat(record.file_bytes)
        record.comment_count = stats['comment_count']
        record.token_count = stats['token_count']
        record.status = 'ready'
        record.progress_percent = 100
        record.current_step = str(_('Sẵn sàng tải về'))
        record.completed_at = timezone.now()
        record.save()

        logger.info('Đã xuất %s cho dự án %s (%s bình luận)',
                    record.export_format, record.project.name, stats['comment_count'])
        return {'status': 'ok', 'export_id': str(record.id)}

    except Exception as exc:
        logger.error('Xuất dữ liệu thất bại: %s', exc)
        record.status = 'failed'
        record.error_message = str(exc)[:2000]
        record.current_step = str(_('Thất bại'))
        record.completed_at = timezone.now()
        record.save(update_fields=[
            'status', 'error_message', 'current_step', 'completed_at'
        ])
        return {'status': 'error', 'message': str(exc)}


# ---------------------------------------------------------------------------
# Nhập CSV chạy nền
# ---------------------------------------------------------------------------
CSV_BATCH = 1000


@shared_task(bind=True, soft_time_limit=3000)
def run_csv_import(self, link_id: str, source_path: str):
    """
    Đọc file CSV đã tải lên và tạo bình luận cho `link`.

    File nằm trên volume dùng chung giữa web và worker (xem docker-compose),
    nên worker đọc được đúng file mà web vừa nhận.
    """
    import csv as csv_module

    link = YouTubeLink.objects.filter(id=link_id).select_related('project').first()
    if link is None:
        return {'status': 'error', 'message': 'YouTubeLink not found'}

    progress = TaskProgress.objects.create(
        youtube_link=link, task_type='importing', status='running',
        task_id=self.request.id or '', started_at=timezone.now(),
        current_step=str(_('Đang đọc file')),
    )
    path = Path(source_path)

    try:
        # Đếm trước số dòng để thanh tiến độ có mẫu số thật. File CSV đọc hai
        # lượt vẫn rẻ hơn nhiều so với việc ghi CSDL.
        with open(path, encoding='utf-8-sig', newline='') as handle:
            total = max(1, sum(1 for _line in handle) - 1)
        progress.total_items = total
        progress.save(update_fields=['total_items'])

        created = 0
        rows = []
        with open(path, encoding='utf-8-sig', newline='') as handle:
            reader = csv_module.DictReader(handle)
            for index, row in enumerate(reader):
                normalized = {
                    (k or '').strip().lower(): (v or '') for k, v in row.items()
                }
                text = normalized.get('text', '').strip()
                if not text:
                    continue
                rows.append(Comment(
                    youtube_link=link,
                    youtube_comment_id=normalized.get('id') or f'row-{index}',
                    author=normalized.get('author', '')[:255],
                    text=text,
                    source_text=text,
                ))
                if len(rows) >= CSV_BATCH:
                    Comment.objects.bulk_create(rows, ignore_conflicts=True)
                    created += len(rows)
                    rows = []
                    _report_import(progress, created, total)
            if rows:
                Comment.objects.bulk_create(rows, ignore_conflicts=True)
                created += len(rows)

        stored = link.comments.count()
        link.comment_count = stored
        link.status = 'completed'
        link.save(update_fields=['comment_count', 'status', 'updated_at'])

        # Lưu trước khi gọi _finish_progress: hàm đó chỉ ghi các trường trạng
        # thái, không ghi processed_items.
        progress.processed_items = stored
        progress.progress_percent = 100
        progress.save(update_fields=['processed_items', 'progress_percent'])
        _finish_progress(progress, 'completed', step=msg.IMPORT_DONE,
                         params={'rows': stored})
        logger.info('Đã nhập %s dòng CSV vào %s', stored, link.title)
        return {'status': 'ok', 'created': stored, 'link_id': str(link.id)}

    except Exception as exc:
        logger.error('Nhập CSV thất bại: %s', exc)
        link.status = 'failed'
        link.save(update_fields=['status', 'updated_at'])
        _finish_progress(progress, 'failed', step=msg.FAILED, error=str(exc))
        return {'status': 'error', 'message': str(exc)}

    finally:
        # File tải lên chỉ là dữ liệu trung gian; giữ lại thì volume phình dần.
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning('Không xoá được file tạm %s: %s', path, exc)


def _report_import(progress, done, total):
    percent = min(99, int(done / total * 100))
    if percent == progress.progress_percent:
        return
    progress.progress_percent = percent
    progress.processed_items = done
    progress.current_step = str(_('Đã nhập %(done)s/%(total)s dòng') % {
        'done': done, 'total': total,
    })
    progress.save(update_fields=[
        'progress_percent', 'processed_items', 'current_step'
    ])


# ---------------------------------------------------------------------------
# Phiên bản dataset (Giai đoạn 4)
# ---------------------------------------------------------------------------
def _progress_reporter(record, *, ceiling=100, field='progress_percent',
                       step_field='current_step'):
    """
    Trả về hàm `report(percent, step)` ghi tiến độ vào một bản ghi.

    Chỉ ghi CSDL khi phần trăm thực sự đổi: một lần xuất có thể gọi hàm này
    hàng chục nghìn lần, ghi mỗi lần sẽ biến thanh tiến độ thành nút thắt cổ
    chai nặng hơn cả việc xuất dữ liệu.
    """
    state = {'percent': -1}

    def report(percent, step=''):
        scaled = int(percent * ceiling / 100)
        if scaled == state['percent']:
            return
        state['percent'] = scaled
        setattr(record, field, scaled)
        if step:
            setattr(record, step_field, str(step)[:500])
        record.save(update_fields=[field, step_field])

    return report


def build_version(record: DatasetVersion, review_filter: str = 'gold') -> dict:
    """
    Sinh file cho một DatasetVersion: bản xuất + ảnh chụp + checksum.

    Tách khỏi task Celery để chỗ khác gọi đồng bộ được: cụ thể là khi chốt
    phiên bản dự phòng ngay trước lúc phục hồi, nơi không được phép chạy nền
    (chạy nền sẽ chụp nhầm trạng thái SAU khi đã phục hồi).
    """
    from .export_service import export_to_file
    from .services.agreement import project_agreement_report
    from .services.versioning import snapshot_path_for, write_snapshot

    try:
        export_dir = Path(settings.EXPORT_ROOT)
        export_dir.mkdir(parents=True, exist_ok=True)
        safe_name = ''.join(
            ch if ch.isalnum() or ch in '-_' else '-' for ch in record.project.name
        )[:60]
        target = export_dir / f'{safe_name}_{record.version}_{record.export_format}'

        report = _progress_reporter(record, ceiling=80)

        # Mặc định chỉ lấy nhãn đã chốt: phiên bản dataset là bản công bố,
        # không nên chứa bình luận chưa ai gán nhãn.
        stats = export_to_file(
            record.project, None, record.export_format, 'all', target,
            review_filter=review_filter, progress=report,
        )

        # Ảnh chụp luôn lấy toàn bộ bình luận, không theo review_filter: nó
        # phục vụ việc phục hồi, không phải công bố.
        report(85, str(_('Đang ghi ảnh chụp để phục hồi')))
        write_snapshot(record.project, snapshot_path_for(target))
        report(92, str(_('Đang tính checksum và độ đồng thuận')))

        digest = hashlib.sha256()
        with open(stats['path'], 'rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)

        agreement = project_agreement_report(record.project)

        record.file_path = str(stats['path'])
        record.snapshot_path = str(snapshot_path_for(target))
        record.checksum_sha256 = digest.hexdigest()
        record.comment_count = stats['comment_count']
        record.token_count = stats['token_count']
        record.annotator_count = agreement.get('annotator_count', 0)
        record.agreement_score = agreement.get('krippendorff_alpha')
        record.status = 'ready'
        record.progress_percent = 100
        record.current_step = str(_('Hoàn tất'))
        record.save()

        logger.info('Đã tạo phiên bản dataset %s cho dự án %s',
                    record.version, record.project.name)
        return {'status': 'ok', 'version': record.version}

    except Exception as exc:
        logger.error('Tạo phiên bản dataset thất bại: %s', exc)
        record.status = 'failed'
        record.current_step = str(exc)[:500]
        record.notes = f'{record.notes}\n[LỖI] {exc}'.strip()
        record.save(update_fields=['status', 'notes', 'current_step'])
        return {'status': 'error', 'message': str(exc)}


@shared_task(bind=True, soft_time_limit=3000)
def build_dataset_version(self, version_id: str, review_filter: str = 'gold'):
    """
    Tạo snapshot dataset ở chế độ nền.

    Chạy nền vì với dataset lớn việc sinh file có thể mất vài phút.
    """
    record = DatasetVersion.objects.filter(id=version_id).select_related('project').first()
    if record is None:
        return {'status': 'error', 'message': 'DatasetVersion not found'}
    return build_version(record, review_filter=review_filter)
