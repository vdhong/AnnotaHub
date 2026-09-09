"""
Test hồi quy cho các lỗi cụ thể đã phát hiện trong phân tích.

Mỗi test ở đây tương ứng một mục trong phan_tich.md và sẽ thất bại nếu lỗi cũ
quay trở lại.
"""
from unittest.mock import patch

from django.db.models import Count
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from comments import views
from comments.models import Comment, TaskProgress, Token, TokenAnnotation
from comments.tests.factories import (
    make_comment,
    make_label,
    make_link,
    make_project,
    make_project_label,
    make_user,
)

PASSWORD = 'test-pass-12345'


class GettextShadowingTests(SimpleTestCase):
    """
    §3.1: `_` là alias gettext. Gán `project, _, _ = result` biến `_` thành
    biến cục bộ, mọi lời gọi `_('...')` sau đó ném TypeError.
    """

    OFFENDERS = ['project_delete', 'project_manage_participants', 'project_lock']

    def test_underscore_is_never_a_local_variable(self):
        for name in self.OFFENDERS:
            func = getattr(views, name)
            while hasattr(func, '__wrapped__'):
                func = func.__wrapped__
            self.assertNotIn(
                '_', func.__code__.co_varnames,
                msg=f'views.{name} dùng `_` làm biến cục bộ — sẽ phá gettext.',
            )


class ProjectDeleteTests(TestCase):
    """§3.1: project_delete từng xoá dự án XONG rồi mới crash 500."""

    def setUp(self):
        self.owner = make_user('del_owner')
        self.project = make_project(self.owner, name='Sắp xoá')
        self.client.login(username='del_owner', password=PASSWORD)

    def test_delete_completes_without_error(self):
        from comments.models import Project

        response = self.client.post(
            reverse('comments:project_delete', args=[self.project.id])
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())


class ParticipantManagementTests(TestCase):
    """§3.1: mọi POST vào trang quản lý thành viên đều 500."""

    def setUp(self):
        self.owner = make_user('pm_owner')
        self.other = make_user('pm_other')
        self.project = make_project(self.owner, name='Quản lý thành viên')
        self.client.login(username='pm_owner', password=PASSWORD)

    def test_add_participant_by_email_works(self):
        response = self.client.post(
            reverse('comments:project_manage_participants', args=[self.project.id]),
            data={'action': 'add_participant', 'email': self.other.email},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.project.participants.filter(pk=self.other.pk).exists())

    def test_remove_participant_works(self):
        self.project.participants.add(self.other)
        response = self.client.post(
            reverse('comments:project_manage_participants', args=[self.project.id]),
            data={'action': 'remove_participant', 'user_id': self.other.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.project.participants.filter(pk=self.other.pk).exists())


class LockedProjectTaskTests(TestCase):
    """
    §3.2: nhánh "dự án bị khoá" tham chiếu task_progress trước khi gán
    -> UnboundLocalError, task chết và bản ghi tiến độ treo ở 'running'.
    """

    def setUp(self):
        self.owner = make_user('lock_task_owner')
        self.project = make_project(self.owner, name='Khoá task', is_locked=True)
        self.link = make_link(self.project)

    def test_fetch_task_on_locked_project_returns_cleanly(self):
        from comments.tasks import fetch_comments_task

        result = fetch_comments_task.apply(args=[str(self.link.id)]).get()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['message'], 'Project is locked')

        progress = TaskProgress.objects.filter(
            youtube_link=self.link, task_type='fetching'
        ).first()
        self.assertIsNotNone(progress, 'Phải có bản ghi tiến độ, không được rơi vào UnboundLocalError')
        self.assertEqual(progress.status, 'failed')

    def test_annotate_task_on_locked_project_returns_cleanly(self):
        from comments.tasks import annotate_comments_task

        result = annotate_comments_task.apply(args=[str(self.link.id)]).get()
        self.assertEqual(result['status'], 'error')
        progress = TaskProgress.objects.filter(
            youtube_link=self.link, task_type='annotating'
        ).first()
        self.assertIsNotNone(progress)
        self.assertEqual(progress.status, 'failed')


class BrokenEndpointTests(TestCase):
    """§3.3: các endpoint từng trả HTTP 500 do lỗi lập trình."""

    def setUp(self):
        self.owner = make_user('be_owner')
        self.project = make_project(self.owner, name='Endpoint hỏng')
        self.link = make_link(self.project)
        make_comment(self.link, comment_id='be-1')
        self.client.login(username='be_owner', password=PASSWORD)

    def test_link_comments_default_filter_does_not_crash(self):
        """Nhánh mặc định filter=all từng rơi vào so sánh field với QuerySet."""
        response = self.client.get(f'/api/links/{self.link.id}/comments/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['total'], 1)

    def test_link_comments_all_filters(self):
        for value in ('all', 'annotated', 'unannotated', 'conflict', 'skipped'):
            with self.subTest(filter=value):
                response = self.client.get(
                    f'/api/links/{self.link.id}/comments/?filter={value}'
                )
                self.assertEqual(response.status_code, 200)

    def test_delete_missing_link_returns_404_not_nameerror(self):
        """§3.6: nhánh link=None tham chiếu biến project_id chưa gán."""
        import uuid

        response = self.client.post(
            reverse('comments:delete_youtube_link', args=[uuid.uuid4()])
        )
        self.assertEqual(response.status_code, 404)

    def test_invalid_page_param_does_not_crash(self):
        """§3.6: int(request.GET['page']) không bọc try -> 500 với ?page=abc."""
        response = self.client.get(
            reverse('comments:link_detail', args=[self.link.id]) + '?page=abc&per_page=xyz'
        )
        self.assertEqual(response.status_code, 200)


class BackwardCompatPropertyTests(TestCase):
    """§3.6: is_toxic trả sai kiểu / luôn True kể cả với nhãn trung tính 'O'."""

    def setUp(self):
        self.owner = make_user('bc_owner')
        self.project = make_project(self.owner, name='Thuộc tính')
        self.neutral = make_project_label(self.project, make_label(self.owner, 'O'))
        self.toxic = make_project_label(self.project, make_label(self.owner, 'toxic'))
        self.link = make_link(self.project)

    def test_is_toxic_is_bool_and_false_for_neutral_label(self):
        comment = make_comment(self.link, comment_id='bc-1', manual_label=self.neutral)
        self.assertIsInstance(comment.is_toxic, bool)
        self.assertFalse(comment.is_toxic)

    def test_is_toxic_true_for_real_label(self):
        comment = make_comment(self.link, comment_id='bc-2', manual_label=self.toxic)
        self.assertTrue(comment.is_toxic)

    def test_is_toxic_false_without_label(self):
        comment = make_comment(self.link, comment_id='bc-3')
        self.assertFalse(comment.is_toxic)

    def test_gold_label_wins_over_manual_and_ai(self):
        comment = make_comment(self.link, comment_id='bc-4',
                               ai_label=self.toxic, manual_label=self.toxic,
                               gold_label=self.neutral)
        self.assertEqual(comment.effective_label.id, self.neutral.id)


class AiProcessedSemanticsTests(TestCase):
    """
    Comment được AI kết luận là 'O' phải phân biệt được với comment chưa xử lý.
    Gộp cả hai vào ai_label=NULL thì mỗi lần chạy lại đều gọi LLM cho những
    comment đã xong, đốt tiền API mà không thêm thông tin gì.
    """

    def setUp(self):
        self.owner = make_user('ai_owner')
        self.project = make_project(self.owner, name='Ngữ nghĩa O')
        make_project_label(self.project, make_label(self.owner, 'toxic'))
        self.link = make_link(self.project)

    def test_processed_comment_without_label_is_not_requeued(self):
        make_comment(self.link, comment_id='ai-1', ai_processed=True, ai_label=None)
        pending = Comment.objects.filter(
            youtube_link=self.link, ai_processed=False
        ).exclude(is_meaningful=False).count()
        self.assertEqual(pending, 0)

    def test_continue_annotation_reports_nothing_to_do(self):
        make_comment(self.link, comment_id='ai-2', ai_processed=True, ai_label=None)
        self.client.login(username='ai_owner', password=PASSWORD)
        response = self.client.post(f'/api/links/{self.link.id}/continue-annotate/')
        self.assertEqual(response.status_code, 400)


def _llm_result(text, spans=(), *, meaningful=True, label='toxic', translated=False):
    """Dựng đúng shape mà process_comment trả về, khỏi phải gọi LLM thật."""
    return {
        'annotation': {
            'is_meaningful': meaningful,
            'comment_label': label,
            'confidence': 0.9,
            'source_is_vietnamese': not translated,
            'vietnamese_text': text,
            'spans': list(spans),
            'warnings': [],
        },
        'vietnamese_text': text,
        'original_text': '',
        'was_translated': translated,
        'is_meaningful': meaningful,
    }


class ReannotatePreservesManualTests(TestCase):
    """
    Chạy lại gán nhãn AI không được đụng tới công sức của người gán.

    Các test ở đây cố tình cho task chạy thật thay vì mock enqueue: chỗ từng
    xoá sạch nhãn token nằm trong task, mock nó đi thì không kiểm được gì.
    """

    TEXT = 'thằng ngu này nói bậy'
    SPAN = [{'start': 0, 'end': 9, 'text': 'thằng ngu', 'label': 'toxic', 'score': 0.9}]

    def setUp(self):
        self.owner = make_user('re_owner')
        self.project = make_project(self.owner, name='Giữ nhãn tay')
        self.label = make_project_label(self.project, make_label(self.owner, 'toxic'))
        self.link = make_link(self.project)
        self.comment = make_comment(self.link, text=self.TEXT, comment_id='re-1')
        self.client.login(username='re_owner', password=PASSWORD)

    def _reannotate(self, **data):
        return self.client.post(
            f'/api/links/{self.link.id}/reannotate/',
            data=data,
            content_type='application/json',
        )

    def _human_labels_token_zero(self):
        from comments.services import annotation as annotation_service
        annotation_service.set_token_label(self.comment, self.owner, 0, self.label)
        annotation_service.set_comment_label(self.comment, self.owner, self.label)

    @patch('comments.tasks.process_comment')
    def test_nhan_cap_cau_van_con(self, mock_llm):
        mock_llm.return_value = _llm_result(self.TEXT, self.SPAN)
        self._human_labels_token_zero()

        self.assertEqual(self._reannotate().status_code, 200)

        self.comment.refresh_from_db()
        self.assertIsNotNone(
            self.comment.manual_label_id,
            'Nhãn thủ công cấp câu phải được giữ nguyên.',
        )

    @patch('comments.tasks.process_comment')
    def test_nhan_cap_token_van_con(self, mock_llm):
        mock_llm.return_value = _llm_result(self.TEXT, self.SPAN)
        self._human_labels_token_zero()

        self.assertEqual(self._reannotate().status_code, 200)

        self.assertEqual(
            TokenAnnotation.objects.filter(
                token__comment=self.comment, source='manual').count(),
            1,
            'Nhãn token do người gán bị xoá khi chạy lại gán nhãn AI.',
        )
        self.assertEqual(
            Token.objects.filter(
                comment=self.comment, manual_label__isnull=False).count(),
            1,
        )

    @patch('comments.tasks.process_comment')
    def test_nhan_token_da_chot_van_con(self, mock_llm):
        mock_llm.return_value = _llm_result(self.TEXT, self.SPAN)
        self._human_labels_token_zero()
        Token.objects.filter(comment=self.comment, position=0).update(
            gold_label=self.label)

        self._reannotate()

        self.assertEqual(
            Token.objects.filter(
                comment=self.comment, gold_label__isnull=False).count(),
            1,
            'Nhãn token đã phân xử không được mất khi chạy lại AI.',
        )

    @patch('comments.tasks.process_comment')
    def test_cum_token_nguoi_keo_chon_van_con(self, mock_llm):
        from comments.services import annotation as annotation_service
        mock_llm.return_value = _llm_result(self.TEXT, self.SPAN)
        annotation_service.set_token_span_label(
            self.comment, self.owner, 0, 1, self.label)
        groups = set(
            Token.objects.filter(comment=self.comment, position__in=(0, 1))
            .values_list('span_group', flat=True)
        )
        self.assertEqual(len(groups), 1)

        self._reannotate()

        after = set(
            Token.objects.filter(comment=self.comment, position__in=(0, 1))
            .values_list('span_group', flat=True)
        )
        self.assertEqual(after, groups, 'AI đã ghi đè cụm token của người gán.')

    @patch('comments.tasks.process_comment')
    def test_khong_nhan_ban_annotation_ai(self, mock_llm):
        mock_llm.return_value = _llm_result(self.TEXT, self.SPAN)

        for _ in range(3):
            self._reannotate()

        counts = (
            TokenAnnotation.objects
            .filter(token__comment=self.comment, source='ai')
            .values('token_id').annotate(n=Count('token_id'))
        )
        self.assertTrue(
            all(row['n'] == 1 for row in counts),
            'Mỗi token chỉ được có một annotation AI, chạy lại không đẻ thêm.',
        )

    @patch('comments.tasks.process_comment')
    def test_ai_ket_luan_vo_nghia_khong_xoa_nhan_nguoi(self, mock_llm):
        mock_llm.return_value = _llm_result(self.TEXT, self.SPAN)
        self._human_labels_token_zero()
        mock_llm.return_value = _llm_result(self.TEXT, meaningful=False, label='O')

        self._reannotate()

        self.assertEqual(
            TokenAnnotation.objects.filter(
                token__comment=self.comment, source='manual').count(),
            1,
            'AI kết luận bình luận vô nghĩa không được xoá nhãn người đã gán.',
        )

    @patch('comments.tasks.process_comment')
    def test_ban_dich_khong_doi_thi_giu_token(self, mock_llm):
        """Chạy lại ra đúng bản dịch cũ thì token không bị dựng lại."""
        mock_llm.return_value = _llm_result(
            self.TEXT, self.SPAN, translated=True)
        self._reannotate()
        self._human_labels_token_zero()
        ids_before = set(
            Token.objects.filter(comment=self.comment).values_list('id', flat=True))

        self._reannotate()

        ids_after = set(
            Token.objects.filter(comment=self.comment).values_list('id', flat=True))
        self.assertEqual(ids_before, ids_after, 'Token bị dựng lại dù text không đổi.')
        self.assertEqual(
            TokenAnnotation.objects.filter(
                token__comment=self.comment, source='manual').count(), 1)

    @patch('comments.tasks.process_comment')
    def test_reset_manual_van_xoa_het(self, mock_llm):
        mock_llm.return_value = _llm_result(self.TEXT, self.SPAN)
        self._human_labels_token_zero()

        self.assertEqual(self._reannotate(reset_manual=True).status_code, 200)

        self.comment.refresh_from_db()
        self.assertIsNone(self.comment.manual_label_id)

class ExportTests(TestCase):
    """§5.4: export phải là streaming và tên file phải an toàn."""

    def setUp(self):
        self.owner = make_user('ex_owner')
        # Tên dự án chứa ký tự có thể phá header Content-Disposition.
        self.project = make_project(self.owner, name='Dự án "nguy hiểm"\nX')
        self.link = make_link(self.project)
        make_comment(self.link, comment_id='ex-1')
        self.client.login(username='ex_owner', password=PASSWORD)

    def test_filename_is_sanitised(self):
        response = self.client.post(
            f'/api/links/{self.link.id}/export/',
            data={'format': 'csv_sentence'}, content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        disposition = response['Content-Disposition']
        self.assertNotIn('\n', disposition)
        self.assertEqual(disposition.count('"'), 2)

    def test_export_is_streaming(self):
        response = self.client.post(
            f'/api/links/{self.link.id}/export/',
            data={'format': 'csv_sentence'}, content_type='application/json',
        )
        self.assertTrue(response.streaming)
        body = b''.join(response.streaming_content).decode()
        self.assertIn('ex-1', body)

    def test_all_export_formats_produce_output(self):
        from comments.export_service import EXPORT_FORMATS

        for fmt in EXPORT_FORMATS:
            with self.subTest(format=fmt):
                response = self.client.post(
                    f'/api/links/{self.link.id}/export/',
                    data={'format': fmt}, content_type='application/json',
                )
                self.assertEqual(response.status_code, 200)
                # xlsx phải dựng trọn gói (định dạng zip) nên không streaming.
                body = (
                    b''.join(response.streaming_content)
                    if response.streaming else response.content
                )
                self.assertTrue(len(body) > 0)


class HealthCheckTests(SimpleTestCase):
    def test_serialize_task_progress_falls_back_to_pending(self):
        data = views._serialize_task_progress(None, 'fetch')
        self.assertEqual(data['status'], 'pending')
        self.assertEqual(data['progress'], 0)

    @patch('comments.views.connection')
    @patch('comments.views.redis.Redis.from_url')
    def test_health_check_ok(self, mock_redis, mock_connection):
        cursor = mock_connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (1,)
        mock_redis.return_value.ping.return_value = True

        response = views.health_check(RequestFactory().get('/health/'))
        self.assertEqual(response.status_code, 200)

    @patch('comments.views.connection')
    @patch('comments.views.redis.Redis.from_url')
    def test_health_check_does_not_leak_error_detail(self, mock_redis, mock_connection):
        """Thông báo lỗi hạ tầng không được lộ ra ngoài."""
        import json

        mock_connection.cursor.side_effect = Exception('password=supersecret')
        mock_redis.return_value.ping.return_value = True

        response = views.health_check(RequestFactory().get('/health/'))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('supersecret', json.loads(response.content).__str__())


class EncryptedFieldTests(TestCase):
    """§2.4: API key phải được mã hoá at-rest."""

    def test_api_key_is_encrypted_in_database(self):
        from django.db import connection

        from comments.models import UserSettings

        user = make_user('enc_user')
        settings_obj = UserSettings.objects.create(
            user=user, ollama_api_key='sk-super-secret-value'
        )

        # Đọc qua ORM: giải mã trong suốt.
        settings_obj.refresh_from_db()
        self.assertEqual(settings_obj.ollama_api_key, 'sk-super-secret-value')

        # Đọc thẳng từ DB: phải là ciphertext.
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT ollama_api_key FROM comments_usersettings WHERE user_id = %s',
                [user.pk],
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row, 'Không đọc được bản ghi thô từ DB')
        raw = row[0]
        self.assertNotEqual(raw, 'sk-super-secret-value')
        self.assertTrue(raw.startswith('enc:'))

    def test_masking_hides_most_of_key(self):
        from comments.fields import mask_secret

        masked = mask_secret('sk-1234567890abcdef')
        self.assertNotIn('1234567890', masked)
        self.assertTrue(masked.endswith('cdef'))
