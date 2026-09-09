"""
Tách token tiếng Việt, có cache pipeline.

spacy.blank("vi") phải được khởi tạo một lần rồi dùng lại. Gọi nó trong
tokenize_text() nghĩa là dựng lại toàn bộ pipeline cho từng comment: trang chi
tiết 50 comment dựng 50 lần, export 100k comment dựng 100k lần, và đó là nút
thắt hiệu năng lớn nhất của hệ thống.

Việc dùng lại được bảo vệ bằng lock, vì Celery worker chạy gevent có thể gọi
đồng thời.
"""
from __future__ import annotations

import logging
import re
import threading
from functools import lru_cache

logger = logging.getLogger(__name__)

_nlp = None
_nlp_lock = threading.Lock()
_spacy_unavailable = False

# Regex dự phòng khi không có spaCy: URL, từ (kể cả có dấu tiếng Việt),
# số, và ký tự đơn không phải chữ/khoảng trắng.
_FALLBACK_PATTERN = re.compile(
    r'https?://\S+|www\.\S+'
    r'|[\wÀ-ỹ]+(?:[\'’_-][\wÀ-ỹ]+)*'
    r'|\d+(?:[.,:/-]\d+)*'
    r'|[^\w\s]'
)
_SYMBOL_RUN = re.compile(r'[^\w\s]+', flags=re.UNICODE)


def get_nlp():
    """Trả về pipeline spaCy dùng chung; None nếu spaCy không khả dụng."""
    global _nlp, _spacy_unavailable
    if _spacy_unavailable:
        return None
    if _nlp is not None:
        return _nlp
    with _nlp_lock:
        if _nlp is None and not _spacy_unavailable:
            try:
                import spacy

                _nlp = spacy.blank('vi')
                logger.info('Đã khởi tạo pipeline spaCy tiếng Việt (dùng chung).')
            except Exception as exc:
                logger.warning(
                    'Không dùng được spaCy (%s) — chuyển sang tokenizer regex.', exc
                )
                _spacy_unavailable = True
                return None
    return _nlp


def tokenize_text(text: str) -> list[dict]:
    """
    Tách `text` thành token kèm offset ký tự.
    Trả về list các dict {text, start, end}.
    """
    if not text:
        return []

    nlp = get_nlp()
    if nlp is not None:
        raw_tokens = [
            {'text': token.text, 'start': token.idx, 'end': token.idx + len(token.text)}
            for token in nlp(text)
            if not token.is_space
        ]
    else:
        raw_tokens = [
            {'text': match.group(0), 'start': match.start(), 'end': match.end()}
            for match in _FALLBACK_PATTERN.finditer(text)
        ]

    # Gộp các ký hiệu/emoji liền nhau thành một token (ví dụ "!!!", "😂🔥").
    tokens: list[dict] = []
    for token in raw_tokens:
        if tokens:
            previous = tokens[-1]
            if (
                previous['end'] == token['start']
                and _SYMBOL_RUN.fullmatch(previous['text'])
                and _SYMBOL_RUN.fullmatch(token['text'])
            ):
                previous['text'] += token['text']
                previous['end'] = token['end']
                continue
        tokens.append(token)

    return tokens


@lru_cache(maxsize=4096)
def tokenize_cached(text: str) -> tuple:
    """
    Bản có cache cho các văn bản lặp lại (render danh sách, export).
    Trả về tuple để hashable; gọi list(...) nếu cần mutable.
    """
    return tuple(
        (token['text'], token['start'], token['end']) for token in tokenize_text(text)
    )


def tokens_from_cache(text: str) -> list[dict]:
    return [
        {'text': item[0], 'start': item[1], 'end': item[2]}
        for item in tokenize_cached(text)
    ]
