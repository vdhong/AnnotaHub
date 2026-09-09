"""
Trường mã hoá at-rest cho bí mật lưu trong DB, hiện là API key của người dùng.

Bản dump SQL trong backups/ đi ra ngoài dễ hơn ta tưởng, nên khoá của người
dùng không nên nằm nguyên văn trong đó.

Có FIELD_ENCRYPTION_KEY thì giá trị được mã hoá bằng Fernet (AES-128-CBC +
HMAC) và lưu dưới dạng "enc:<ciphertext>". Không có khoá thì lưu nguyên văn
kèm cảnh báo trong log; chỉ chấp nhận được ở máy dev.

Lúc đọc, trường tự nhận biết tiền tố nên giá trị cũ chưa mã hoá vẫn đọc bình
thường, và việc bật mã hoá không cần migrate dữ liệu.
"""
from __future__ import annotations

import base64
import hashlib
import logging

from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)

PREFIX = 'enc:'
_fernet = None
_warned = False


def _get_fernet():
    global _fernet, _warned
    if _fernet is not None:
        return _fernet

    raw_key = (getattr(settings, 'FIELD_ENCRYPTION_KEY', '') or '').strip()
    if not raw_key:
        if not _warned:
            logger.warning(
                'FIELD_ENCRYPTION_KEY chưa được đặt — API key sẽ lưu KHÔNG mã hoá. '
                'Chỉ chấp nhận ở môi trường phát triển.'
            )
            _warned = True
        return None

    try:
        from cryptography.fernet import Fernet
    except ImportError:
        logger.error('Thiếu thư viện cryptography — không thể mã hoá trường.')
        return None

    try:
        # Chấp nhận cả khoá Fernet chuẩn lẫn passphrase tuỳ ý.
        if len(raw_key) == 44 and raw_key.endswith('='):
            key = raw_key.encode()
        else:
            digest = hashlib.sha256(raw_key.encode()).digest()
            key = base64.urlsafe_b64encode(digest)
        _fernet = Fernet(key)
    except Exception as exc:
        logger.error('FIELD_ENCRYPTION_KEY không hợp lệ: %s', exc)
        return None

    return _fernet


def encrypt_value(value: str) -> str:
    if not value:
        return ''
    fernet = _get_fernet()
    if fernet is None:
        return value
    return PREFIX + fernet.encrypt(value.encode()).decode()


def decrypt_value(value: str) -> str:
    if not value:
        return ''
    if not value.startswith(PREFIX):
        # Dữ liệu cũ chưa mã hoá: trả về nguyên trạng.
        return value
    fernet = _get_fernet()
    if fernet is None:
        logger.error('Có dữ liệu đã mã hoá nhưng thiếu FIELD_ENCRYPTION_KEY.')
        return ''
    try:
        return fernet.decrypt(value[len(PREFIX):].encode()).decode()
    except Exception as exc:
        logger.error('Không giải mã được giá trị: %s', exc)
        return ''


class EncryptedCharField(models.CharField):
    """CharField tự mã hoá khi ghi và giải mã khi đọc."""

    def from_db_value(self, value, expression, connection):
        return decrypt_value(value) if value else value

    def to_python(self, value):
        if value is None:
            return value
        return decrypt_value(value) if isinstance(value, str) and value.startswith(PREFIX) else value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value:
            return value
        if value.startswith(PREFIX):
            return value
        return encrypt_value(value)


def mask_secret(value: str, visible: int = 4) -> str:
    """Che bí mật để hiển thị: chỉ lộ vài ký tự cuối."""
    if not value:
        return ''
    if len(value) <= visible:
        return '•' * len(value)
    return '•' * 8 + value[-visible:]
