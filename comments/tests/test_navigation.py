"""
Test luồng điều hướng của các trang con thuộc một dự án.

Mọi trang con phải dùng cùng một thanh điều hướng (`comments/_project_nav.html`):
breadcrumb → tên dự án → dải tab. Bộ test này chặn việc một trang quay lại
kiểu điều hướng riêng (nút "Back" tự vẽ, breadcrumb tự viết…).
"""
from django.test import TestCase
from django.urls import reverse

from comments.tests.factories import (
    make_comment,
    make_link,
    make_project,
    make_user,
)

PASSWORD = 'test-pass-12345'

# Các trang con của dự án và tab được kỳ vọng đang mở.
OWNER_PAGES = [
    ('comments:project_detail', 'overview'),
    ('comments:project_edit', 'edit'),
    ('comments:project_labels_settings', 'labels'),
    ('comments:project_manage_participants', 'members'),
    ('comments:project_quality', 'quality'),
    ('comments:project_adjudicate', 'adjudicate'),
    ('comments:project_versions', 'versions'),
    ('comments:import_csv', 'import'),
    ('comments:project_export', 'export'),
]

# Trang con mà annotator cũng vào được.
ANNOTATOR_PAGES = [
    ('comments:project_detail', 'overview'),
    ('comments:project_versions', 'versions'),
    ('comments:project_export', 'export'),
]

# Tab chỉ chủ dự án được thấy.
OWNER_ONLY_URLS = [
    'comments:project_labels_settings',
    'comments:project_manage_participants',
    'comments:project_quality',
    'comments:project_adjudicate',
    'comments:import_csv',
    'comments:project_edit',
]


class ProjectNavigationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('nav-owner')
        cls.annotator = make_user('nav-annotator')
        cls.project = make_project(cls.owner, name='Dự án điều hướng',
                                   participants=[cls.annotator])
        cls.link = make_link(cls.project)
        make_comment(cls.link)

    def _tabs(self, html):
        """Trả về danh sách URL của các tab trong dải điều hướng."""
        import re
        return re.findall(r'<a href="([^"]+)"\s+class="project-tab', html)

    def test_owner_sees_same_nav_on_every_subpage(self):
        self.client.login(username='nav-owner', password=PASSWORD)
        for url_name, _active in OWNER_PAGES:
            with self.subTest(page=url_name):
                response = self.client.get(
                    reverse(url_name, args=[self.project.id])
                )
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertIn('class="project-nav"', html)
                self.assertIn('<div class="project-tabs">', html)
                # Đúng một tab đang mở, và tất cả tab của chủ dự án đều hiện.
                self.assertEqual(html.count('project-tab active'), 1)
                self.assertEqual(len(self._tabs(html)), len(OWNER_PAGES))
                # Breadcrumb luôn dẫn về danh sách dự án.
                self.assertIn(reverse('comments:project_list'), html)

    def test_annotator_nav_hides_owner_only_tabs(self):
        self.client.login(username='nav-annotator', password=PASSWORD)
        for url_name, _active in ANNOTATOR_PAGES:
            with self.subTest(page=url_name):
                response = self.client.get(
                    reverse(url_name, args=[self.project.id])
                )
                self.assertEqual(response.status_code, 200)
                tabs = self._tabs(response.content.decode())
                self.assertEqual(len(tabs), len(ANNOTATOR_PAGES))
                for owner_only in OWNER_ONLY_URLS:
                    self.assertNotIn(
                        reverse(owner_only, args=[self.project.id]), tabs
                    )

    def test_link_pages_keep_project_breadcrumb(self):
        self.client.login(username='nav-owner', password=PASSWORD)

        detail = self.client.get(reverse('comments:link_detail', args=[self.link.id]))
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.content.decode()
        self.assertIn('class="project-nav"', detail_html)
        self.assertIn(reverse('comments:project_detail', args=[self.project.id]),
                      detail_html)
        self.assertIn(self.link.title, detail_html)

        # Focus mode: chỉ breadcrumb, không dải tab, nhưng vẫn cùng một luồng.
        workspace = self.client.get(
            reverse('comments:annotate_workspace', args=[self.link.id])
        )
        self.assertEqual(workspace.status_code, 200)
        workspace_html = workspace.content.decode()
        self.assertIn('project-nav--compact', workspace_html)
        self.assertNotIn('<div class="project-tabs">', workspace_html)
        self.assertIn(reverse('comments:link_detail', args=[self.link.id]),
                      workspace_html)

    def test_locked_project_hides_unusable_tabs(self):
        """
        Dự án khoá: ẩn tab mà mọi thao tác đều bị chặn, để người dùng không bấm
        vào rồi nhận thông báo lỗi. Xem `require_unlocked=True` ở views/api.
        """
        self.project.is_locked = True
        self.project.save(update_fields=['is_locked'])
        self.addCleanup(
            lambda: type(self.project).objects.filter(pk=self.project.pk)
            .update(is_locked=False)
        )

        self.client.login(username='nav-owner', password=PASSWORD)
        html = self.client.get(
            reverse('comments:project_detail', args=[self.project.id])
        ).content.decode()
        tabs = self._tabs(html)

        for hidden in ('comments:project_labels_settings',
                       'comments:project_adjudicate',
                       'comments:import_csv'):
            with self.subTest(tab=hidden):
                self.assertNotIn(reverse(hidden, args=[self.project.id]), tabs)

        # Những tab vẫn dùng được thì phải còn nguyên.
        for kept in ('comments:project_manage_participants',
                     'comments:project_quality',
                     'comments:project_versions',
                     'comments:project_export',
                     'comments:project_edit'):
            with self.subTest(tab=kept):
                self.assertIn(reverse(kept, args=[self.project.id]), tabs)

        # Form thêm nguồn dữ liệu cũng bị chặn khi khoá nên không được hiện.
        self.assertNotIn(
            reverse('comments:add_youtube_link', args=[self.project.id]), html
        )

    def test_unlocked_project_shows_every_owner_tab(self):
        self.client.login(username='nav-owner', password=PASSWORD)
        html = self.client.get(
            reverse('comments:project_detail', args=[self.project.id])
        ).content.decode()
        self.assertEqual(len(self._tabs(html)), len(OWNER_PAGES))
        self.assertIn(
            reverse('comments:add_youtube_link', args=[self.project.id]), html
        )

    def test_subpages_have_no_ad_hoc_back_button(self):
        """Không trang nào tự vẽ lại nút quay lại kiểu riêng."""
        self.client.login(username='nav-owner', password=PASSWORD)
        for url_name, _active in OWNER_PAGES:
            with self.subTest(page=url_name):
                html = self.client.get(
                    reverse(url_name, args=[self.project.id])
                ).content.decode()
                self.assertNotIn('bi-arrow-left', html)


class CsvSourcePageTests(TestCase):
    """
    Nguồn CSV vẫn là một "link" để thao tác, nhưng không có video và không tải
    được bình luận từ đâu cả. Những phần chỉ đúng với YouTube phải biến mất,
    kẻo người dùng bấm vào rồi mở ra một trang YouTube trống.
    """

    # Bám vào thuộc tính HTML và URL, không bám vào chữ hiển thị (trang render
    # theo ngôn ngữ đang hoạt động) cũng không bám vào tên id trần: các hàm JS
    # vẫn nhắc tới id trong getElementById dù phần tử đã bị bỏ.
    YOUTUBE_ONLY = (
        'youtube.com/watch',
        'id="videoToggleBtn"',
        'id="youtubePlayer"',
        'id="fetchRefetchBtn"',
        'id="fetchClearRefetchBtn"',
    )

    def setUp(self):
        self.owner = make_user('csv-owner')
        self.project = make_project(self.owner, name='Nguồn hỗn hợp')
        self.youtube = make_link(self.project, video_id='abc12345678')
        self.csv = make_link(self.project, video_id='csv:20260101000000',
                             kind='csv', title='dataset.csv', channel='CSV import')
        make_comment(self.csv, text='một dòng từ CSV', comment_id='row-0')
        self.client.login(username='csv-owner', password=PASSWORD)

    def _html(self, link):
        return self.client.get(
            reverse('comments:link_detail', args=[link.id])
        ).content.decode()

    def test_trang_csv_khong_co_phan_danh_rieng_cho_youtube(self):
        html = self._html(self.csv)
        for needle in self.YOUTUBE_ONLY:
            self.assertNotIn(needle, html,
                             f'"{needle}" không nên xuất hiện trên trang nguồn CSV.')

    def test_trang_youtube_van_day_du(self):
        html = self._html(self.youtube)
        for needle in self.YOUTUBE_ONLY:
            self.assertIn(needle, html,
                          f'"{needle}" phải còn trên trang nguồn YouTube.')

    def test_csv_van_giu_phan_gan_nhan_ai(self):
        """Ẩn phần YouTube không được kéo theo phần gán nhãn AI."""
        html = self._html(self.csv)
        self.assertIn('id="continueAnnotationBtn"', html)
        self.assertIn('id="annotateActionsBtns"', html)
