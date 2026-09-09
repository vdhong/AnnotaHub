"""Kiểm tra cơ chế giới hạn tần suất: được tắt ở các test khác nên phải test riêng."""
from django.test import TestCase, override_settings

from comments.tests.factories import (
    make_comment,
    make_link,
    make_project,
    make_user,
)

PASSWORD = 'test-pass-12345'


class ExportThrottleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('throttle_owner')
        cls.project = make_project(cls.owner, name='Giới hạn tần suất')
        cls.link = make_link(cls.project)
        make_comment(cls.link, comment_id='thr-1')

    def setUp(self):
        self.client.login(username='throttle_owner', password=PASSWORD)
        # Throttle của DRF lưu đếm trong cache; dọn trước mỗi test.
        from django.core.cache import cache

        cache.clear()

    def _post_export(self):
        return self.client.post(
            f'/api/links/{self.link.id}/export/',
            data={'format': 'csv_sentence'}, content_type='application/json',
        )

    def test_export_is_throttled_after_limit(self):
        """
        Kiểm tra trực tiếp lớp throttle: DRF ghim `rate` vào thể hiện throttle
        lúc khởi tạo, nên override_settings không thay đổi được ngưỡng của một
        view đã nạp. Gọi thẳng allow_request() là cách kiểm chứng đáng tin.
        """
        from rest_framework.throttling import ScopedRateThrottle

        from comments.api_views import LinkExportView

        limit = 3

        class _FixedRateThrottle(ScopedRateThrottle):
            THROTTLE_RATES = {'export': f'{limit}/min'}

        throttle = _FixedRateThrottle()
        view = LinkExportView()
        view.throttle_scope = 'export'

        request = self.client.request().wsgi_request
        request.user = self.owner

        results = [throttle.allow_request(request, view) for _ in range(limit + 2)]
        self.assertEqual(results[:limit], [True] * limit)
        self.assertIn(False, results[limit:],
                      'Vượt ngưỡng phải bị throttle chặn lại')

    def test_export_view_declares_throttle_scope(self):
        """View xuất dữ liệu phải khai báo scope, nếu không throttle vô tác dụng."""
        from comments.api_views import LinkExportView

        self.assertEqual(LinkExportView.throttle_scope, 'export')

    def test_no_throttle_when_rate_is_none(self):
        """Cấu hình test tắt throttle -> nhiều request liên tiếp vẫn qua."""
        statuses = [self._post_export().status_code for _ in range(12)]
        self.assertEqual(set(statuses), {200})


class LoginRateLimitTests(TestCase):
    """Giới hạn đăng nhập dùng cache riêng, không phải throttle của DRF."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        make_user('brute_target')

    @override_settings(LOGIN_RATELIMIT_ATTEMPTS=3, LOGIN_RATELIMIT_WINDOW=300)
    def test_repeated_failed_logins_are_blocked(self):
        from django.urls import reverse

        url = reverse('comments:login')
        statuses = []
        for _ in range(5):
            response = self.client.post(
                url, {'username': 'brute_target', 'password': 'sai-mat-khau'}
            )
            statuses.append(response.status_code)

        self.assertIn(429, statuses, 'Brute-force phải bị chặn sau vài lần thử')
