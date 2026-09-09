"""
Test phân quyền: mỗi endpoint × mỗi vai trò.

Đây là bộ test quan trọng nhất: trước khi nâng cấp, toàn bộ /api/* không có
xác thực, ai cũng đọc/xoá/xuất được dữ liệu của mọi dự án. Bộ test này chặn
việc lỗ hổng đó quay lại.
"""
from django.test import TestCase
from django.urls import reverse

from comments.models import Comment, Project
from comments.tests.factories import (
    make_comment,
    make_link,
    make_project,
    make_project_label,
    make_user,
)

PASSWORD = 'test-pass-12345'


class PermissionMatrixTests(TestCase):
    """Ma trận: ẩn danh / người lạ / annotator / chủ dự án."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('owner')
        cls.annotator = make_user('annotator')
        cls.stranger = make_user('stranger')

        cls.project = make_project(cls.owner, participants=[cls.annotator])
        cls.project_label = make_project_label(cls.project)
        cls.link = make_link(cls.project)
        cls.comment = make_comment(cls.link)

    def login(self, user):
        self.assertTrue(self.client.login(username=user.username, password=PASSWORD))

    # -- API đọc -----------------------------------------------------------
    def test_api_project_list_requires_auth(self):
        response = self.client.get('/api/projects/')
        self.assertIn(response.status_code, (401, 403))

    def test_api_project_list_only_shows_own_projects(self):
        other_owner = make_user('other')
        make_project(other_owner, name='Dự án của người khác')

        self.login(self.stranger)
        response = self.client.get('/api/projects/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['projects'], [])

        self.login(self.owner)
        names = [p['name'] for p in self.client.get('/api/projects/').json()['projects']]
        self.assertEqual(names, [self.project.name])

    def test_stranger_gets_404_not_403_on_project_detail(self):
        """404 thay vì 403 để không lộ sự tồn tại của tài nguyên."""
        self.login(self.stranger)
        response = self.client.get(f'/api/projects/{self.project.id}/')
        self.assertEqual(response.status_code, 404)

    def test_annotator_can_read_project(self):
        self.login(self.annotator)
        response = self.client.get(f'/api/projects/{self.project.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['role'], 'annotator')

    # -- API ghi / phá huỷ --------------------------------------------------
    def test_anonymous_cannot_delete_project(self):
        response = self.client.delete(f'/api/projects/{self.project.id}/')
        self.assertIn(response.status_code, (401, 403))
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())

    def test_stranger_cannot_delete_project(self):
        self.login(self.stranger)
        response = self.client.delete(f'/api/projects/{self.project.id}/')
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())

    def test_annotator_cannot_delete_project(self):
        self.login(self.annotator)
        response = self.client.delete(f'/api/projects/{self.project.id}/')
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())

    def test_owner_can_delete_project(self):
        self.login(self.owner)
        response = self.client.delete(f'/api/projects/{self.project.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())

    def test_anonymous_cannot_export_dataset(self):
        """Xuất dữ liệu là hành vi đánh cắp dataset nguy hiểm nhất."""
        response = self.client.post(
            f'/api/links/{self.link.id}/export/',
            data={'format': 'csv_sentence'}, content_type='application/json',
        )
        self.assertIn(response.status_code, (401, 403))

    def test_stranger_cannot_export_dataset(self):
        self.login(self.stranger)
        response = self.client.post(
            f'/api/links/{self.link.id}/export/',
            data={'format': 'csv_sentence'}, content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)

    def test_annotator_cannot_reannotate(self):
        """Reannotate từng xoá trắng nhãn thủ công; chỉ chủ dự án được gọi."""
        self.login(self.annotator)
        response = self.client.post(f'/api/links/{self.link.id}/reannotate/')
        self.assertEqual(response.status_code, 403)

    def test_stranger_cannot_clear_and_refetch(self):
        self.login(self.stranger)
        response = self.client.post(f'/api/links/{self.link.id}/clear-refetch/')
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Comment.objects.filter(pk=self.comment.pk).exists())

    # -- Web view -----------------------------------------------------------
    def test_stranger_cannot_view_link_detail(self):
        self.login(self.stranger)
        response = self.client.get(
            reverse('comments:link_detail', args=[self.link.id])
        )
        self.assertEqual(response.status_code, 404)

    def test_annotator_can_view_link_detail(self):
        self.login(self.annotator)
        response = self.client.get(
            reverse('comments:link_detail', args=[self.link.id])
        )
        self.assertEqual(response.status_code, 200)

    def test_stranger_cannot_export_project_page(self):
        self.login(self.stranger)
        response = self.client.get(
            reverse('comments:project_export', args=[self.project.id])
        )
        self.assertEqual(response.status_code, 404)

    def test_sse_requires_login(self):
        response = self.client.get(f'/sse/progress/{self.link.id}/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_sse_rejects_stranger(self):
        self.login(self.stranger)
        response = self.client.get(f'/sse/progress/{self.link.id}/')
        self.assertEqual(response.status_code, 404)

    def test_annotator_cannot_manage_participants(self):
        self.login(self.annotator)
        response = self.client.get(
            reverse('comments:project_manage_participants', args=[self.project.id])
        )
        self.assertEqual(response.status_code, 403)

    def test_annotator_cannot_add_link(self):
        """Thêm link tiêu tốn quota YouTube API của chủ dự án."""
        self.login(self.annotator)
        response = self.client.post(
            reverse('comments:add_youtube_link', args=[self.project.id]),
            data={'url': 'https://youtube.com/watch?v=zzzzzzzzzzz'},
        )
        self.assertEqual(response.status_code, 403)

    def test_health_endpoint_is_public_and_not_language_prefixed(self):
        """/health/ phải trả 200 trực tiếp, không bị i18n đổi thành 302."""
        response = self.client.get('/health/')
        self.assertIn(response.status_code, (200, 503))


class LabelOwnershipTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('label_owner')
        cls.stranger = make_user('label_stranger')

    def test_api_label_list_scoped_to_user(self):
        from comments.tests.factories import make_label

        make_label(self.owner, name='riêng-của-owner')

        self.client.login(username='label_stranger', password=PASSWORD)
        response = self.client.get('/api/labels/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['labels'], [])

    def test_api_label_list_does_not_crash(self):
        """Endpoint này từng trả HTTP 500 vì tham chiếu thuộc tính không tồn tại."""
        from comments.tests.factories import make_label

        make_label(self.owner, name='nhãn-a')
        self.client.login(username='label_owner', password=PASSWORD)
        response = self.client.get('/api/labels/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['labels']), 1)
        self.assertIn('assignment_count', response.json()['labels'][0])

    def test_api_create_label_assigns_owner(self):
        """Tạo Label qua API phải tự gán owner, không để rơi vào IntegrityError."""
        self.client.login(username='label_owner', password=PASSWORD)
        response = self.client.post(
            '/api/labels/create/',
            data={'name': 'nhãn mới', 'color': '#00FF00'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)

    def test_api_create_project_assigns_owner(self):
        """Tạo Project qua API phải tự gán owner, không để rơi vào IntegrityError."""
        self.client.login(username='label_owner', password=PASSWORD)
        response = self.client.post(
            '/api/projects/create/',
            data={'name': 'Dự án qua API'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            Project.objects.get(name='Dự án qua API').owner, self.owner
        )


class LockedProjectTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('lock_owner')
        cls.annotator = make_user('lock_annotator')
        cls.project = make_project(cls.owner, name='Dự án khoá',
                                   participants=[cls.annotator], is_locked=True)
        cls.project_label = make_project_label(cls.project)
        cls.link = make_link(cls.project)
        cls.comment = make_comment(cls.link)

    def test_locked_project_blocks_labeling(self):
        self.client.login(username='lock_annotator', password=PASSWORD)
        response = self.client.post(
            f'/api/comments/{self.comment.id}/set-comment-labels/',
            data={'label_id': str(self.project_label.id)},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_locked_project_still_readable(self):
        self.client.login(username='lock_annotator', password=PASSWORD)
        response = self.client.get(f'/api/links/{self.link.id}/status/')
        self.assertEqual(response.status_code, 200)


class SuperuserOwnershipTests(TestCase):
    """
    Hồi quy: superuser không được coi là chủ sở hữu dự án của người khác.

    Lỗi đã xảy ra: owned_projects() có ngoại lệ `if user.is_superuser: return
    Project.objects.all()`, khiến mọi dự án hiện trong mục "dự án tôi sở hữu"
    của quản trị viên. Ngoài việc hiển thị sai, nó còn nguy hiểm: quản trị viên
    thấy nút "Xoá dự án" và tưởng đó là dự án của mình.
    """

    @classmethod
    def setUpTestData(cls):
        cls.real_owner = make_user('real_owner')
        cls.admin = make_user('site_admin', is_superuser=True, is_staff=True)
        cls.outsider = make_user('outsider')
        cls.project = make_project(cls.real_owner, name='Dự án của người khác')

    def test_owned_projects_excludes_superuser(self):
        from comments.permissions import owned_projects

        self.assertNotIn(
            self.project, owned_projects(self.admin),
            'Superuser không sở hữu dự án của người khác.',
        )
        self.assertIn(self.project, owned_projects(self.real_owner))

    def test_admin_projects_lists_only_others_projects(self):
        from comments.permissions import admin_projects

        own = make_project(self.admin, name='Dự án của chính admin')
        administered = admin_projects(self.admin)
        self.assertIn(self.project, administered)
        self.assertNotIn(own, administered,
                         'Dự án admin tự sở hữu không thuộc nhóm "quản trị".')
        self.assertEqual(list(admin_projects(self.outsider)), [])

    def test_role_for_returns_admin_not_owner(self):
        from comments.permissions import ROLE_ADMIN, ROLE_OWNER, role_for

        self.assertEqual(role_for(self.project, self.real_owner), ROLE_OWNER)
        self.assertEqual(role_for(self.project, self.admin), ROLE_ADMIN)
        self.assertIsNone(role_for(self.project, self.outsider))

    def test_superuser_still_has_owner_level_permission(self):
        """Vẫn phải can thiệp được để hỗ trợ: chỉ là không được gọi là 'chủ'."""
        from comments.permissions import is_owner_level

        self.assertTrue(is_owner_level(self.project, self.admin))
        self.assertTrue(is_owner_level(self.project, self.real_owner))
        self.assertFalse(is_owner_level(self.project, self.outsider))

    def test_project_list_page_separates_owned_from_administered(self):
        self.client.login(username='site_admin', password=PASSWORD)
        response = self.client.get(reverse('comments:project_list'))
        self.assertEqual(response.status_code, 200)

        owned = list(response.context['owned_projects'])
        administered = list(response.context['administered_projects'])

        self.assertNotIn(self.project, owned,
                         'Dự án của người khác không được nằm trong mục "sở hữu".')
        self.assertIn(self.project, administered)

    def test_project_list_for_real_owner_unchanged(self):
        self.client.login(username='real_owner', password=PASSWORD)
        response = self.client.get(reverse('comments:project_list'))
        self.assertIn(self.project, list(response.context['owned_projects']))
        self.assertEqual(list(response.context['administered_projects']), [])

    def test_api_reports_accurate_role_and_owner(self):
        self.client.login(username='site_admin', password=PASSWORD)
        rows = self.client.get('/api/projects/').json()['projects']
        row = next(r for r in rows if r['id'] == str(self.project.id))

        self.assertEqual(row['role'], 'admin')
        self.assertFalse(row['is_owner'])
        self.assertEqual(row['owner'], 'real_owner')

    def test_api_reports_owner_for_real_owner(self):
        self.client.login(username='real_owner', password=PASSWORD)
        rows = self.client.get('/api/projects/').json()['projects']
        row = next(r for r in rows if r['id'] == str(self.project.id))

        self.assertEqual(row['role'], 'owner')
        self.assertTrue(row['is_owner'])

    def test_detail_page_flags_admin_view(self):
        self.client.login(username='site_admin', password=PASSWORD)
        response = self.client.get(
            reverse('comments:project_detail', args=[self.project.id])
        )
        self.assertTrue(response.context['viewing_as_admin'])
        self.assertFalse(response.context['is_real_owner'])
        # Vẫn có quyền thao tác cấp chủ sở hữu.
        self.assertTrue(response.context['is_owner'])

    def test_detail_page_for_real_owner_has_no_admin_banner(self):
        self.client.login(username='real_owner', password=PASSWORD)
        response = self.client.get(
            reverse('comments:project_detail', args=[self.project.id])
        )
        self.assertFalse(response.context['viewing_as_admin'])
        self.assertTrue(response.context['is_real_owner'])
