"""
Chặn lỗi "bản dịch làm hỏng khối JavaScript".

Chuỗi dịch nhét thẳng vào một chuỗi JS mà chứa đúng dấu nháy bao ngoài sẽ đóng
chuỗi sớm -> SyntaxError -> toàn bộ khối <script> không chạy. Trang vẫn hiện ra
bình thường nên lỗi rất khó thấy: nút mất, click không phản ứng, và chỉ xảy ra
ở đúng một ngôn ngữ. Tiếng Anh đặc biệt dễ dính vì có dấu nháy sở hữu
("the AI's labels").

Cách chặn: render các trang có JavaScript ở mọi ngôn ngữ và tìm chuỗi mở mà hết
dòng vẫn chưa đóng: chuỗi JS bình thường không xuống dòng được.
"""
import re

from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from comments.models import Token
from comments.services import annotation as annotation_service
from comments.tests.factories import (
    make_comment,
    make_link,
    make_project,
    make_project_label,
    make_user,
)

SCRIPT_RE = re.compile(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', re.S)


def unterminated_strings(script: str) -> list[tuple[int, str]]:
    """Các dòng có chuỗi mở mà chưa đóng trước khi hết dòng."""
    problems = []
    for lineno, line in enumerate(script.split('\n'), 1):
        index, length = 0, len(line)
        while index < length:
            char = line[index]
            if char == '/' and index + 1 < length and line[index + 1] == '/':
                break
            if char in '"\'':
                quote, cursor, closed = char, index + 1, False
                while cursor < length:
                    if line[cursor] == '\\':
                        cursor += 2
                        continue
                    if line[cursor] == quote:
                        closed = True
                        break
                    cursor += 1
                if not closed:
                    problems.append((lineno, line.strip()[:120]))
                    break
                index = cursor + 1
                continue
            index += 1
    return problems


class TemplateJavaScriptTests(TestCase):
    def setUp(self):
        self.owner = make_user('js-owner')
        self.project = make_project(self.owner, name='Dự án kiểm JS')
        self.label = make_project_label(self.project)
        self.link = make_link(self.project)
        self.comment = make_comment(self.link, text='câu thử', comment_id='c1')
        Token.objects.create(comment=self.comment, text='câu', position=0,
                             start_offset=0, end_offset=3)
        # Có đề xuất của AI thì các khối liên quan tới AI mới được render ra.
        annotation_service.store_ai_comment_annotation(
            self.comment, self.label, confidence=0.9
        )
        self.comment.ai_label = self.label
        self.comment.ai_processed = True
        self.comment.save(update_fields=['ai_label', 'ai_processed'])
        self.client.force_login(self.owner)

    PAGES = (
        ('link_detail', 'comments:link_detail', 'link'),
        ('annotate_workspace', 'comments:annotate_workspace', 'link'),
        ('project_detail', 'comments:project_detail', 'project'),
        ('project_quality', 'comments:project_quality', 'project'),
        ('project_versions', 'comments:project_versions', 'project'),
        ('project_export', 'comments:project_export', 'project'),
        ('import_csv', 'comments:import_csv', 'project'),
    )

    def _fetch(self, route, target, language):
        """
        Tải trang ở đúng ngôn ngữ cần kiểm.

        URL phải được dựng bên trong translation.override: dự án dùng
        i18n_patterns nên ngôn ngữ nằm ở tiền tố URL, và LocaleMiddleware kích
        hoạt ngôn ngữ theo tiền tố đó chứ không theo override. Reverse ở ngoài
        thì mọi vòng lặp đều rơi về cùng một ngôn ngữ mặc định và test xanh giả.
        """
        pk = self.link.id if target == 'link' else self.project.id
        with translation.override(language):
            url = reverse(route, args=[pk])
        self.assertTrue(
            url.startswith(f'/{language}/'),
            f'URL {url} không mang tiền tố ngôn ngữ {language}',
        )
        return url, self.client.get(url)

    def test_no_translation_breaks_a_script_block(self):
        for language in ('vi', 'en'):
            for page, route, target in self.PAGES:
                with self.subTest(language=language, page=page):
                    url, response = self._fetch(route, target, language)
                    self.assertEqual(response.status_code, 200, url)
                    html = response.content.decode()
                    for script in SCRIPT_RE.findall(html):
                        broken = unterminated_strings(script)
                        self.assertEqual(
                            broken, [],
                            f'Chuỗi JS bị cắt ngang ở {page} ({language}): {broken}',
                        )

    def test_detector_actually_catches_a_broken_string(self):
        """Nếu bộ dò này hỏng thì test trên sẽ xanh giả: kiểm tra lại nó."""
        self.assertTrue(unterminated_strings("var a = 'the AI's labels';"))
        self.assertFalse(unterminated_strings("var a = 'the AI\\'s labels';"))
        self.assertFalse(unterminated_strings('var a = "the AI\'s labels";'))
