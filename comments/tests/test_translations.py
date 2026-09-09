"""
Chặn việc chuỗi tiếng Việt lọt sang giao diện tiếng Anh.

Chuỗi gốc của dự án viết bằng tiếng Việt, nên một mục chưa dịch trong
`locale/en` không gây lỗi gì cả: gettext lặng lẽ trả về chính msgid. Người dùng
chọn tiếng Anh vẫn thấy tiếng Việt, và không có gì trong log báo hiệu.

Test này đọc thẳng file .po nên bắt được cả những mục mà không test giao diện
nào chạm tới.
"""
import re
import unicodedata

from django.conf import settings
from django.test import SimpleTestCase
from django.utils import translation


def _has_vietnamese(text):
    """Chữ cái có dấu hoặc các chữ riêng của tiếng Việt."""
    for char in text:
        if char in 'đĐơƠưƯăĂêÊôÔâÂ':
            return True
        name = unicodedata.name(char, '')
        if name.startswith('LATIN') and 'WITH' in name:
            return True
    return False


def _entries(path):
    """(msgid, [msgstr...]) cho từng mục trong file .po."""
    text = path.read_text(encoding='utf-8')
    for block in text.split('\n\n'):
        match = re.search(r'^msgid\s+"(.*)"$', block, re.M)
        if not match or 'Project-Id-Version' in block:
            continue
        values = re.findall(r'^msgstr(?:\[\d\])?\s+"(.*)"$', block, re.M)
        yield match.group(1), values


class EnglishCatalogTests(SimpleTestCase):

    @property
    def catalog(self):
        return settings.BASE_DIR / 'locale' / 'en' / 'LC_MESSAGES' / 'django.po'

    def test_moi_chuoi_tieng_viet_deu_co_ban_dich_tieng_anh(self):
        missing = [
            msgid for msgid, values in _entries(self.catalog)
            if _has_vietnamese(msgid) and any(v == '' for v in values)
        ]
        self.assertEqual(
            missing, [],
            'Các chuỗi sau sẽ hiện tiếng Việt trên giao diện tiếng Anh:\n  '
            + '\n  '.join(missing),
        )

    def test_bo_dem_dang_thuc_su_hoat_dong(self):
        """Nếu file .mo chưa biên dịch lại thì test trên vẫn xanh mà giao diện vẫn sai."""
        from django.utils.translation import ngettext

        msgid = 'Có %(n)s bình luận mà các annotator không thống nhất.'
        with translation.override('en'):
            rendered = ngettext(msgid, msgid, 3) % {'n': 3}
        self.assertNotIn('bình luận', rendered)
        self.assertIn('comments', rendered)
