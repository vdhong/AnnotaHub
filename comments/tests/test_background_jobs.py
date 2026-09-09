"""
Ba việc nặng phải chạy nền: nhập CSV, xuất dữ liệu, chốt phiên bản.

Chạy thẳng trong request thì với dataset lớn, trình duyệt hoặc reverse proxy
ngắt kết nối giữa chừng và người dùng nhận file cụt, hoặc thấy trang treo mà
không biết việc đã chạy tới đâu. Test ở đây khoá lại ba điều:

1. Request trả về ngay, việc thật giao cho worker.
2. Tiến độ ghi được vào CSDL nên giao diện hỏi lại được.
3. Kết quả tải về được, và chỉ người trong dự án mới tải được.

Celery chạy ở chế độ eager trong test nên `.delay()` thực thi ngay tại chỗ:
đủ để kiểm tra đầu-cuối mà không cần worker thật.
"""
import shutil
import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from comments.models import (
    Comment,
    DatasetVersion,
    ExportRecord,
    TaskProgress,
    YouTubeLink,
)
from comments.tasks import build_version, run_csv_import, run_export
from comments.tests.factories import (
    make_comment,
    make_link,
    make_project,
    make_project_label,
    make_user,
)


class JobTestBase(TestCase):
    """Mỗi lớp test ghi file vào thư mục tạm riêng."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.root = Path(tempfile.mkdtemp(prefix='annotahub-jobs-'))
        cls._settings = override_settings(
            EXPORT_ROOT=cls.root / 'exports', MEDIA_ROOT=cls.root / 'media'
        )
        cls._settings.enable()

    @classmethod
    def tearDownClass(cls):
        cls._settings.disable()
        shutil.rmtree(cls.root, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.owner = make_user('job-owner')
        self.annotator = make_user('job-annotator')
        self.project = make_project(self.owner, name='Dự án chạy nền',
                                    participants=[self.annotator])
        self.label = make_project_label(self.project)
        self.link = make_link(self.project)
        for i in range(3):
            make_comment(self.link, text=f'câu số {i}', comment_id=f'j{i}')
        self.client.force_login(self.owner)


class ExportJobTests(JobTestBase):
    def _start(self, **extra):
        payload = {'format': 'json_sentence', 'filter': 'all', 'review': 'all'}
        payload.update(extra)
        return self.client.post(
            reverse('comments:project_export', args=[self.project.id]), payload
        )

    def test_export_redirects_instead_of_streaming_the_file(self):
        """Request phải trả về ngay, không kéo dài suốt thời gian sinh file."""
        response = self._start()

        self.assertEqual(response.status_code, 302)
        record = ExportRecord.objects.get(project=self.project)
        self.assertIn(f'job={record.id}', response['Location'])

    def test_the_worker_produces_a_downloadable_file(self):
        self._start()
        record = ExportRecord.objects.get(project=self.project)

        self.assertEqual(record.status, 'ready')
        self.assertEqual(record.progress_percent, 100)
        self.assertEqual(record.comment_count, 3)
        self.assertTrue(record.can_download)
        self.assertTrue(Path(record.file_path).is_file())
        self.assertTrue(record.file_size)

        response = self.client.get(reverse(
            'comments:export_download', args=[self.project.id, record.id]
        ))
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response['Content-Disposition'])
        body = b''.join(response.streaming_content)
        response.close()
        self.assertIn('câu số 0', body.decode('utf-8'))

    def test_progress_is_written_while_the_export_runs(self):
        record = ExportRecord.objects.create(
            project=self.project, export_format='json_sentence',
            filter_toxicity='all', review_filter='all', status='pending',
        )
        seen = []
        original = ExportRecord.save

        def spy(self, *args, **kwargs):
            seen.append(self.progress_percent)
            return original(self, *args, **kwargs)

        with self.settings(EXPORT_ROOT=self.root / 'exports'):
            ExportRecord.save = spy
            try:
                run_export(str(record.id))
            finally:
                ExportRecord.save = original

        self.assertTrue(any(0 < p < 100 for p in seen),
                        f'Không thấy tiến độ trung gian: {seen}')

    def test_two_exports_in_the_same_second_keep_separate_files(self):
        """Tên file chỉ theo thời gian thì hai lần xuất liền nhau đè lên nhau."""
        self._start()
        self._start()

        paths = set(
            ExportRecord.objects.filter(project=self.project)
            .values_list('file_path', flat=True)
        )
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(Path(p).is_file() for p in paths))

    def test_a_failing_export_is_recorded_not_swallowed(self):
        record = ExportRecord.objects.create(
            project=self.project, export_format='dinh-dang-khong-co',
            filter_toxicity='all', status='pending',
        )
        run_export(str(record.id))

        record.refresh_from_db()
        self.assertEqual(record.status, 'failed')
        self.assertTrue(record.error_message)

    def test_export_history_is_the_download_list(self):
        self._start()
        response = self.client.get(
            reverse('comments:project_export', args=[self.project.id])
        )
        record = ExportRecord.objects.get(project=self.project)
        self.assertIn(record, list(response.context['exports']))

    def test_jobs_api_reports_status_and_download_url(self):
        self._start()
        record = ExportRecord.objects.get(project=self.project)

        data = self.client.get(
            reverse('api:api_export_jobs', args=[self.project.id])
        ).json()

        job = next(j for j in data['exports'] if j['id'] == str(record.id))
        self.assertEqual(job['status'], 'ready')
        self.assertTrue(job['can_download'])
        self.assertIn(str(record.id), job['download_url'])

    def test_annotator_can_download_but_stranger_cannot(self):
        self._start()
        record = ExportRecord.objects.get(project=self.project)
        url = reverse('comments:export_download',
                      args=[self.project.id, record.id])

        self.client.force_login(self.annotator)
        allowed = self.client.get(url)
        self.assertEqual(allowed.status_code, 200)
        allowed.close()

        self.client.force_login(make_user('job-stranger'))
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_missing_file_reports_instead_of_crashing(self):
        self._start()
        record = ExportRecord.objects.get(project=self.project)
        Path(record.file_path).unlink()

        response = self.client.get(
            reverse('comments:export_download', args=[self.project.id, record.id]),
            follow=True,
        )
        self.assertContains(response, 'Không tìm thấy file')


class ImportJobTests(JobTestBase):
    def _upload(self, body=b'text,author\nxin chao,an\nchao ban,binh\n', name='du-lieu.csv'):
        return self.client.post(
            reverse('comments:import_csv', args=[self.project.id]),
            {'name': 'Bộ thử', 'file': SimpleUploadedFile(name, body, 'text/csv')},
        )

    def test_import_returns_immediately_and_worker_creates_rows(self):
        response = self._upload()

        link = YouTubeLink.objects.get(project=self.project, kind='csv')
        self.assertEqual(response.status_code, 302)
        self.assertIn(f'job={link.id}', response['Location'])

        link.refresh_from_db()
        self.assertEqual(link.status, 'completed')
        self.assertEqual(link.comment_count, 2)
        self.assertEqual(
            sorted(Comment.objects.filter(youtube_link=link)
                   .values_list('text', flat=True)),
            ['chao ban', 'xin chao'],
        )

    def test_import_progress_is_tracked(self):
        self._upload()
        link = YouTubeLink.objects.get(project=self.project, kind='csv')

        progress = TaskProgress.objects.get(youtube_link=link, task_type='importing')
        self.assertEqual(progress.status, 'completed')
        self.assertEqual(progress.progress_percent, 100)
        self.assertEqual(progress.processed_items, 2)

    def test_uploaded_file_is_cleaned_up(self):
        """File tải lên chỉ là dữ liệu trung gian, giữ lại thì volume phình dần."""
        self._upload()
        upload_dir = Path(self.root) / 'media' / 'imports'
        self.assertEqual(list(upload_dir.glob('*.csv')), [])

    def test_csv_without_a_text_column_is_refused_before_any_work(self):
        response = self._upload(body=b'noi_dung,author\nxin chao,an\n', name='sai.csv')

        self.assertEqual(response.status_code, 302)
        self.assertFalse(YouTubeLink.objects.filter(
            project=self.project, kind='csv').exists())

    def test_failed_import_marks_the_link_and_the_progress(self):
        link = YouTubeLink.objects.create(
            project=self.project, kind='csv', video_id='csv:loi',
            title='Hỏng', status='pending',
        )
        run_csv_import(str(link.id), str(self.root / 'khong-ton-tai.csv'))

        link.refresh_from_db()
        self.assertEqual(link.status, 'failed')
        progress = TaskProgress.objects.get(youtube_link=link, task_type='importing')
        self.assertEqual(progress.status, 'failed')
        self.assertTrue(progress.error_message)

    def test_link_status_api_exposes_the_import_task(self):
        self._upload()
        link = YouTubeLink.objects.get(project=self.project, kind='csv')

        data = self.client.get(
            reverse('api:api_link_status', args=[link.id])
        ).json()

        kinds = [task['type'] for task in data['tasks']]
        self.assertIn('importing', kinds)


class VersionProgressTests(JobTestBase):
    def test_building_a_version_records_progress(self):
        record = DatasetVersion.objects.create(
            project=self.project, version='v1.0',
            export_format='json_sentence', status='building',
        )
        self.assertEqual(build_version(record)['status'], 'ok')

        record.refresh_from_db()
        self.assertEqual(record.status, 'ready')
        self.assertEqual(record.progress_percent, 100)
        self.assertTrue(record.current_step)

    def test_versions_api_exposes_progress_for_the_table(self):
        DatasetVersion.objects.create(
            project=self.project, version='v-dang-chay',
            export_format='json_sentence', status='building',
            progress_percent=42, current_step='Đang ghi 42/100 bình luận',
        )
        data = self.client.get(
            reverse('api:api_dataset_versions', args=[self.project.id])
        ).json()

        row = next(v for v in data['versions'] if v['version'] == 'v-dang-chay')
        self.assertEqual(row['progress_percent'], 42)
        self.assertIn('42/100', row['current_step'])

    def test_a_failed_build_keeps_the_reason_visible(self):
        record = DatasetVersion.objects.create(
            project=self.project, version='v-loi',
            export_format='dinh-dang-khong-co', status='building',
        )
        build_version(record)

        record.refresh_from_db()
        self.assertEqual(record.status, 'failed')
        self.assertTrue(record.current_step)


class AnnotationProgressTests(TestCase):
    """
    Thanh tiến độ phải nhích theo từng bình luận.

    Batch mặc định 50 bình luận. Nếu chỉ báo tiến độ khi xong cả batch thì
    người dùng nhìn thấy 0/221 suốt mấy phút trong khi worker đang gọi LLM liên
    tục, và tưởng hệ thống treo.
    """

    def setUp(self):
        from comments.tests.factories import (
            make_comment,
            make_label,
            make_link,
            make_project,
            make_project_label,
            make_user,
        )
        self.owner = make_user('prog_owner')
        self.project = make_project(self.owner, name='Tiến độ')
        make_project_label(self.project, make_label(self.owner, 'toxic'))
        self.link = make_link(self.project)
        for i in range(3):
            make_comment(self.link, text='thằng ngu này', comment_id=f'pg-{i}')

    def test_tien_do_nhich_sau_tung_binh_luan(self):
        from unittest.mock import patch

        from comments.tasks import annotate_comments_task

        seen = []

        def fake_llm(text, **kwargs):
            record = TaskProgress.objects.filter(
                youtube_link=self.link, task_type='annotating').first()
            seen.append(record.processed_items if record else 0)
            return {
                'annotation': {
                    'is_meaningful': True, 'comment_label': 'toxic',
                    'confidence': 0.9, 'source_is_vietnamese': True,
                    'vietnamese_text': text, 'spans': [], 'warnings': [],
                },
                'vietnamese_text': text, 'original_text': '',
                'was_translated': False, 'is_meaningful': True,
            }

        with patch('comments.tasks.process_comment', side_effect=fake_llm), \
                patch('comments.tasks.get_owner_ollama_config',
                      return_value=('http://x', 'k', 'm')):
            annotate_comments_task(str(self.link.id))

        self.assertEqual(len(seen), 3)
        self.assertEqual(
            seen, [0, 1, 2],
            'Tiến độ chỉ được cập nhật khi xong cả batch, không phải từng bình luận.',
        )


class TaskStepLanguageTests(TestCase):
    """Câu mô tả tiến độ phải theo ngôn ngữ người xem, không theo ngôn ngữ worker."""

    def test_dung_ngon_ngu_dang_kich_hoat(self):
        from django.utils import translation

        from comments import task_messages

        with translation.override('en'):
            english = task_messages.render(
                task_messages.ANNOTATING, {}, processed=5, total=10)
        with translation.override('vi'):
            vietnamese = task_messages.render(
                task_messages.ANNOTATING, {}, processed=5, total=10)

        self.assertEqual(english, 'Annotated 5/10 comments')
        self.assertNotEqual(english, vietnamese)
        self.assertIn('5/10', vietnamese)

    def test_khoa_la_tra_ve_nguyen_van(self):
        """Bản ghi cũ lưu sẵn cả câu, và lỗi kỹ thuật, vẫn hiển thị được."""
        from comments import task_messages

        self.assertEqual(
            task_messages.render('API Error: quota exceeded'),
            'API Error: quota exceeded',
        )
        self.assertEqual(task_messages.render(''), '')


class ContinueAnnotationButtonTests(TestCase):
    """
    Nút "Continue Annotation" chỉ hiện khi còn việc cho AI.

    Điều kiện hiện nút phải trùng với điều kiện chia batch của task. Lệch nhau
    thì nút biến mất trong khi vẫn còn hàng trăm bình luận chưa chạy, và người
    dùng buộc phải bấm "Re-Annotate All" để chạy lại từ đầu.
    """

    def setUp(self):
        from comments.tests.factories import (
            make_comment,
            make_label,
            make_link,
            make_project,
            make_project_label,
            make_user,
        )
        self.owner = make_user('btn_owner')
        self.project = make_project(self.owner, name='Nút chạy tiếp')
        make_project_label(self.project, make_label(self.owner, 'toxic'))
        self.link = make_link(self.project)
        for i in range(3):
            make_comment(self.link, text='thằng ngu này', comment_id=f'btn-{i}')
        self.client.login(username='btn_owner', password='test-pass-12345')

    def _pending_theo_task(self):
        return Comment.objects.filter(
            youtube_link=self.link, ai_processed=False
        ).exclude(is_meaningful=False).count()

    def test_dem_khop_voi_dieu_kien_chia_batch(self):
        from comments.services.stats import link_counters

        # AI đã chạy xong một lượt: is_meaningful không còn là None nữa.
        Comment.objects.filter(youtube_link=self.link).update(
            ai_processed=True, is_meaningful=True)
        # Rồi bấm chạy lại, cờ ai_processed quay về False.
        Comment.objects.filter(youtube_link=self.link).update(ai_processed=False)

        counters = link_counters(self.link, user=self.owner)
        self.assertEqual(
            counters['unannotated_count'], self._pending_theo_task(),
            'Số hiện trên giao diện lệch với số bình luận task sẽ xử lý.',
        )
        self.assertEqual(counters['unannotated_count'], 3)

    def test_bo_qua_khong_tinh_la_con_cho(self):
        from comments.services.stats import link_counters

        Comment.objects.filter(youtube_link=self.link).update(
            ai_processed=False, is_meaningful=False)
        counters = link_counters(self.link, user=self.owner)
        self.assertEqual(counters['unannotated_count'], 0)
        self.assertEqual(counters['unannotated_count'], self._pending_theo_task())

    def test_nut_hien_tren_trang(self):
        from django.urls import reverse

        Comment.objects.filter(youtube_link=self.link).update(
            ai_processed=False, is_meaningful=True)
        html = self.client.get(
            reverse('comments:link_detail', args=[self.link.id])
        ).content.decode()

        anchor = html.index('id="continueAnnotationBtn"')
        tag = html[anchor:html.index('>', anchor)]
        self.assertNotIn('display: none', tag,
                         'Nút chạy tiếp bị ẩn dù còn bình luận chưa gán nhãn.')


class CsvImportIdTests(TestCase):
    """
    Cột `id` trong CSV đi vào `Comment.youtube_comment_id`.

    Ràng buộc duy nhất là ('youtube_link', 'youtube_comment_id'), phạm vi theo
    từng nguồn dữ liệu chứ không phải toàn hệ thống. Nhờ vậy file xuất ra từ
    một dự án nhập được sang dự án khác mà không đụng id, và nhập ngược lại
    chính dự án cũ cũng không ghi đè dữ liệu đang có.
    """

    def setUp(self):
        from comments.tests.factories import make_comment, make_link, make_project, make_user
        self.owner = make_user('csvid')
        self.source = make_project(self.owner, name='Nguồn')
        self.target = make_project(self.owner, name='Đích')
        self.link = make_link(self.source, video_id='vid00000001')
        for i in range(3):
            make_comment(self.link, text=f'bình luận {i}', comment_id=f'ytid-{i}')
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _csv(self, name):
        """File mới cho mỗi lần nhập: run_csv_import xoá file nguồn khi xong."""
        import csv as csv_module
        path = self.tmp / f'{name}.csv'
        with open(path, 'w', encoding='utf-8', newline='') as handle:
            writer = csv_module.writer(handle)
            writer.writerow(['id', 'text', 'author'])
            for comment in Comment.objects.filter(youtube_link=self.link):
                writer.writerow([comment.youtube_comment_id, comment.text, ''])
        return path

    def _import(self, project, name, link=None):
        target = link or YouTubeLink.objects.create(
            project=project, kind='csv', video_id=f'csv:{name}',
            url='', title=name, channel='CSV import')
        run_csv_import(str(target.id), str(self._csv(name)))
        return target

    def test_nhap_sang_du_an_khac_giu_nguyen_id(self):
        other = self._import(self.target, 'sangdichs')
        self.assertEqual(Comment.objects.filter(youtube_link=other).count(), 3)
        self.assertEqual(
            sorted(Comment.objects.filter(youtube_link=other)
                   .values_list('youtube_comment_id', flat=True)),
            ['ytid-0', 'ytid-1', 'ytid-2'],
        )

    def test_nhap_nguoc_lai_khong_dung_du_lieu_cu(self):
        again = self._import(self.source, 'venguon')
        self.assertEqual(Comment.objects.filter(youtube_link=again).count(), 3)
        self.assertEqual(Comment.objects.filter(youtube_link=self.link).count(), 3)
        # Cùng youtube_comment_id nhưng là các bản ghi riêng biệt.
        rows = Comment.objects.filter(youtube_comment_id='ytid-0')
        self.assertEqual(rows.count(), 2)
        self.assertEqual(len({row.pk for row in rows}), 2)

    def test_nhap_lai_cung_file_vao_cung_nguon_khong_nhan_doi(self):
        link = self._import(self.source, 'lan1')
        self._import(self.source, 'lan2', link=link)
        self.assertEqual(Comment.objects.filter(youtube_link=link).count(), 3)
