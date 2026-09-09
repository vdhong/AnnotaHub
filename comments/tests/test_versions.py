"""
Test vòng đời của một phiên bản dataset: chốt → tải về → phục hồi → xoá.

Trọng tâm là phục hồi: đây là thao tác ghi đè dữ liệu nên phải chứng minh được
nó khôi phục đúng cái cần khôi phục và không đụng vào cái không được đụng.
"""
import shutil
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings
from django.urls import reverse

from comments.models import Comment, DatasetVersion, Token
from comments.services import versioning
from comments.tasks import build_version
from comments.tests.factories import (
    make_comment,
    make_link,
    make_project,
    make_project_label,
    make_user,
)

PASSWORD = 'test-pass-12345'


class VersionTestBase(TestCase):
    """Ghi file phiên bản vào thư mục tạm riêng cho mỗi lớp test."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.export_root = Path(tempfile.mkdtemp(prefix='annotahub-versions-'))
        cls._settings = override_settings(EXPORT_ROOT=cls.export_root)
        cls._settings.enable()

    @classmethod
    def tearDownClass(cls):
        cls._settings.disable()
        shutil.rmtree(cls.export_root, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.owner = make_user('ver-owner')
        self.annotator = make_user('ver-annotator')
        self.project = make_project(self.owner, name='Dự án phiên bản',
                                    participants=[self.annotator])
        self.toxic = make_project_label(self.project)
        self.clean = make_project_label(
            self.project, label=make_project_label.__globals__['make_label'](
                self.owner, name='sạch', color='#00FF00'
            )
        )
        self.link = make_link(self.project)
        self.comment = make_comment(self.link, text='câu thử', comment_id='c1')
        self.token = Token.objects.create(
            comment=self.comment, text='câu', position=0,
            start_offset=0, end_offset=3,
        )

    def _make_version(self, name='v1.0'):
        record = DatasetVersion.objects.create(
            project=self.project, version=name,
            export_format='json_token', created_by=self.owner, status='building',
        )
        self.assertEqual(build_version(record).get('status'), 'ok')
        record.refresh_from_db()
        return record


class SnapshotTests(VersionTestBase):
    def test_build_writes_export_and_snapshot(self):
        version = self._make_version()
        self.assertEqual(version.status, 'ready')
        self.assertTrue(version.can_download)
        self.assertTrue(version.can_restore)
        self.assertTrue(Path(version.snapshot_path).is_file())
        self.assertTrue(version.checksum_sha256)

    def test_restore_puts_labels_back(self):
        # Trạng thái lúc chốt: đã chốt nhãn "toxic".
        self.comment.gold_label = self.toxic
        self.comment.review_status = 'adjudicated'
        self.comment.save()
        self.token.gold_label = self.toxic
        self.token.save()
        version = self._make_version()

        # Sau đó có người sửa nhãn.
        self.comment.gold_label = self.clean
        self.comment.review_status = 'conflict'
        self.comment.is_meaningful = False
        self.comment.save()
        self.token.gold_label = self.clean
        self.token.save()

        stats = versioning.restore_snapshot(self.project, version.snapshot_path)

        self.comment.refresh_from_db()
        self.token.refresh_from_db()
        self.assertEqual(self.comment.gold_label_id, self.toxic.id)
        self.assertEqual(self.comment.review_status, 'adjudicated')
        self.assertIsNone(self.comment.is_meaningful)
        self.assertEqual(self.token.gold_label_id, self.toxic.id)
        self.assertEqual(stats['comments'], 1)
        self.assertEqual(stats['skipped'], 0)

    def test_restore_clears_labels_added_after_snapshot(self):
        """Phục hồi phải xoá nhãn gán sau khi chốt, không chỉ ghi đè."""
        version = self._make_version()  # chốt khi chưa có nhãn nào

        self.comment.manual_label = self.toxic
        self.comment.save()
        self.token.manual_label = self.toxic
        self.token.save()

        versioning.restore_snapshot(self.project, version.snapshot_path)

        self.comment.refresh_from_db()
        self.token.refresh_from_db()
        self.assertIsNone(self.comment.manual_label_id)
        self.assertIsNone(self.token.manual_label_id)

    def test_comments_added_later_are_left_alone(self):
        version = self._make_version()
        newer = make_comment(self.link, text='thêm sau', comment_id='c2')
        newer.manual_label = self.toxic
        newer.save()

        stats = versioning.restore_snapshot(self.project, version.snapshot_path)

        newer.refresh_from_db()
        self.assertEqual(newer.manual_label_id, self.toxic.id)
        self.assertEqual(stats['untouched'], 1)

    def test_deleted_comments_are_reported_not_recreated(self):
        version = self._make_version()
        Comment.objects.filter(pk=self.comment.pk).delete()

        stats = versioning.restore_snapshot(self.project, version.snapshot_path)

        self.assertEqual(stats['comments'], 0)
        self.assertEqual(stats['skipped'], 1)
        self.assertFalse(Comment.objects.filter(pk=self.comment.pk).exists())

    def test_removed_label_is_reported(self):
        self.comment.gold_label = self.toxic
        self.comment.save()
        version = self._make_version()
        label_name = self.toxic.display_name
        self.toxic.delete()

        stats = versioning.restore_snapshot(self.project, version.snapshot_path)

        self.assertIn(label_name, stats['missing_labels'])

    def test_snapshot_of_another_project_is_rejected(self):
        version = self._make_version()
        other = make_project(self.owner, name='Dự án khác')
        with self.assertRaises(versioning.SnapshotIncompatible):
            versioning.restore_snapshot(other, version.snapshot_path)


class VersionActionViewTests(VersionTestBase):
    def test_owner_can_download_restore_and_delete(self):
        version = self._make_version()
        self.client.login(username='ver-owner', password=PASSWORD)

        download = self.client.get(reverse(
            'comments:dataset_version_download', args=[self.project.id, version.id]
        ))
        self.assertEqual(download.status_code, 200)
        self.assertIn('attachment', download['Content-Disposition'])
        download.close()

        restore = self.client.post(reverse(
            'comments:dataset_version_restore', args=[self.project.id, version.id]
        ))
        self.assertRedirects(restore, reverse(
            'comments:project_versions', args=[self.project.id]
        ))

        file_path = Path(version.file_path)
        delete = self.client.post(reverse(
            'comments:dataset_version_delete', args=[self.project.id, version.id]
        ))
        self.assertEqual(delete.status_code, 302)
        self.assertFalse(DatasetVersion.objects.filter(pk=version.pk).exists())
        self.assertFalse(file_path.exists())

    def test_restore_with_backup_creates_a_rollback_version(self):
        version = self._make_version()
        self.comment.manual_label = self.toxic
        self.comment.save()

        self.client.login(username='ver-owner', password=PASSWORD)
        self.client.post(
            reverse('comments:dataset_version_restore',
                    args=[self.project.id, version.id]),
            {'backup': 'on'},
        )

        backup = DatasetVersion.objects.exclude(pk=version.pk).get()
        self.assertTrue(backup.version.startswith('truoc-phuc-hoi-'))
        self.assertTrue(backup.can_restore)

        # Nhãn hiện tại đã bị gỡ, nhưng phiên bản dự phòng đưa lại được.
        self.comment.refresh_from_db()
        self.assertIsNone(self.comment.manual_label_id)
        versioning.restore_snapshot(self.project, backup.snapshot_path)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.manual_label_id, self.toxic.id)

    def test_annotator_can_download_but_not_restore_or_delete(self):
        version = self._make_version()
        self.client.login(username='ver-annotator', password=PASSWORD)

        download = self.client.get(reverse(
            'comments:dataset_version_download', args=[self.project.id, version.id]
        ))
        self.assertEqual(download.status_code, 200)
        download.close()

        for name in ('dataset_version_restore', 'dataset_version_delete'):
            with self.subTest(action=name):
                response = self.client.post(
                    reverse(f'comments:{name}', args=[self.project.id, version.id])
                )
                self.assertEqual(response.status_code, 403)
        self.assertTrue(DatasetVersion.objects.filter(pk=version.pk).exists())

    def test_stranger_cannot_reach_version_of_another_project(self):
        version = self._make_version()
        make_user('ver-stranger')
        self.client.login(username='ver-stranger', password=PASSWORD)
        response = self.client.get(reverse(
            'comments:dataset_version_download', args=[self.project.id, version.id]
        ))
        self.assertEqual(response.status_code, 404)

    def test_version_of_other_project_is_not_reachable_via_wrong_project_id(self):
        version = self._make_version()
        other = make_project(self.owner, name='Dự án khác')
        self.client.login(username='ver-owner', password=PASSWORD)
        response = self.client.get(reverse(
            'comments:dataset_version_download', args=[other.id, version.id]
        ))
        self.assertEqual(response.status_code, 404)

    def test_locked_project_blocks_restore(self):
        version = self._make_version()
        self.project.is_locked = True
        self.project.save(update_fields=['is_locked'])

        self.client.login(username='ver-owner', password=PASSWORD)
        response = self.client.post(
            reverse('comments:dataset_version_restore',
                    args=[self.project.id, version.id]),
            follow=True,
        )
        self.assertContains(response, 'bị khoá')

    def test_locked_project_blocks_delete(self):
        version = self._make_version()
        self.project.is_locked = True
        self.project.save(update_fields=['is_locked'])

        self.client.login(username='ver-owner', password=PASSWORD)
        self.client.post(
            reverse('comments:dataset_version_delete',
                    args=[self.project.id, version.id])
        )
        self.assertTrue(DatasetVersion.objects.filter(pk=version.pk).exists())

    def test_version_without_snapshot_cannot_be_restored(self):
        """Phiên bản chốt trước khi có tính năng phục hồi: báo rõ, không nổ."""
        version = self._make_version()
        version.snapshot_path = ''
        version.save(update_fields=['snapshot_path'])

        self.client.login(username='ver-owner', password=PASSWORD)
        response = self.client.post(
            reverse('comments:dataset_version_restore',
                    args=[self.project.id, version.id]),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'không có ảnh chụp')
