"""
Thông điệp tiến độ của task nền.

Worker chạy ngoài request nên không biết người xem đang dùng ngôn ngữ nào; gọi
gettext ở đó chỉ cho ra ngôn ngữ mặc định của server. Vì vậy task chỉ ghi lại
một khoá cùng vài con số, còn câu chữ được dựng lúc trả về cho trình duyệt,
theo đúng ngôn ngữ của phiên đó.

Bản ghi cũ chứa câu viết sẵn vẫn hiển thị nguyên như trước: khoá nào không có
trong bảng thì trả lại chính nó.
"""
from django.utils.translation import gettext as _

# --- Bước ---------------------------------------------------------------
PREPARING = 'preparing'
QUEUED_FETCH = 'queued_fetch'
FETCHING = 'fetching'
FETCH_DONE = 'fetch_done'
NOT_YOUTUBE = 'not_youtube'
QUEUED_ANNOTATION = 'queued_annotation'
ANNOTATING = 'annotating'
NO_PENDING = 'no_pending'
CANCELLED = 'cancelled'
IMPORT_DONE = 'import_done'
FAILED = 'failed'

# --- Lỗi ----------------------------------------------------------------
ERR_PROJECT_LOCKED = 'err_project_locked'
ERR_NO_LABELS = 'err_no_labels'
ERR_OLLAMA_MISSING = 'err_ollama_missing'


def _catalog():
    """Dựng bảng khi được gọi, để gettext bắt đúng ngôn ngữ đang hoạt động."""
    return {
        PREPARING: _('Đang chuẩn bị'),
        QUEUED_FETCH: _('Đã xếp hàng chờ tải bình luận'),
        FETCHING: _('Đã tải %(processed)s bình luận'),
        FETCH_DONE: _('Đã thêm %(created)s bình luận mới, cập nhật %(updated)s'),
        NOT_YOUTUBE: _('Nguồn không phải YouTube, bỏ qua bước tải'),
        QUEUED_ANNOTATION: _('Đã xếp hàng chờ gán nhãn'),
        ANNOTATING: _('Đã gán nhãn %(processed)s/%(total)s bình luận'),
        NO_PENDING: _('Không có bình luận nào cần gán nhãn'),
        CANCELLED: _('Đã dừng theo yêu cầu'),
        IMPORT_DONE: _('Đã nhập %(rows)s dòng'),
        FAILED: _('Thất bại'),
        ERR_PROJECT_LOCKED: _('Dự án đã bị khoá.'),
        ERR_NO_LABELS: _('Chưa thiết lập nhãn cho dự án này.'),
        ERR_OLLAMA_MISSING: _('Chưa thiết lập OLLAMA URL, API KEY và MODEL '
                              '(trong Cài đặt cá nhân hoặc biến môi trường).'),
    }


def render(key, params=None, *, processed=0, total=0):
    """
    Dựng câu hiển thị từ khoá đã lưu.

    Khoá lạ được trả nguyên văn: bản ghi từ phiên bản trước lưu sẵn cả câu, và
    thông báo lỗi kỹ thuật từ LLM hay YouTube API cũng đi qua đây.
    """
    if not key:
        return ''
    template = _catalog().get(key)
    if template is None:
        return key
    values = {'processed': processed, 'total': total}
    values.update(params or {})
    try:
        return template % values
    except (KeyError, TypeError, ValueError):
        return template
