"""
Test màn hình gán nhãn nhanh: thống kê cá nhân, nhãn của chính mình, và i18n.
"""
from django.test import TestCase

from comments.models import CommentAnnotation
from comments.services import annotation as annotation_service
from comments.tests.factories import (
    make_comment,
    make_label,
    make_link,
    make_project,
    make_project_label,
    make_user,
)

PASSWORD = 'test-pass-12345'


class QueueProgressTests(TestCase):
    """Hàng đợi phải trả về tiến độ của chính người dùng hiện tại."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('wsp_owner')
        cls.mate = make_user('wsp_mate')
        cls.project = make_project(cls.owner, name='Workspace',
                                   participants=[cls.mate])
        cls.label = make_project_label(cls.project, make_label(cls.owner, 'toxic'))
        cls.link = make_link(cls.project)
        for i in range(5):
            make_comment(cls.link, text=f'bình luận số {i}', comment_id=f'ws-{i}')

    def setUp(self):
        self.client.login(username='wsp_owner', password=PASSWORD)

    def _queue(self):
        return self.client.get(f'/api/links/{self.link.id}/queue/?limit=10').json()

    def test_queue_reports_zero_progress_initially(self):
        progress = self._queue()['progress']
        self.assertEqual(progress['my_annotated_link'], 0)
        self.assertEqual(progress['link_total'], 5)
        self.assertEqual(progress['percent'], 0.0)

    def test_progress_counts_only_my_own_annotations(self):
        """Người khác gán nhãn không được tính vào tiến độ của mình."""
        comment = self.link.comments.first()
        annotation_service.set_comment_label(comment, self.mate, self.label)

        progress = self._queue()['progress']
        self.assertEqual(progress['my_annotated_link'], 0,
                         'Nhãn của người khác không phải tiến độ của tôi.')

        annotation_service.set_comment_label(
            self.link.comments.exclude(pk=comment.pk).first(), self.owner, self.label
        )
        self.assertEqual(self._queue()['progress']['my_annotated_link'], 1)

    def test_progress_updates_in_label_response(self):
        comment_id = self._queue()['comments'][0]['id']
        response = self.client.post(
            f'/api/comments/{comment_id}/set-comment-labels/',
            data={'label_id': str(self.label.id)}, content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['progress']['my_annotated_link'], 1)

    def test_project_wide_count_included(self):
        other_link = make_link(self.project, video_id='second12345')
        other = make_comment(other_link, comment_id='ws-other')
        annotation_service.set_comment_label(other, self.owner, self.label)

        progress = self._queue()['progress']
        self.assertEqual(progress['my_annotated_link'], 0)
        self.assertEqual(progress['my_annotated_project'], 1)


class MyLabelTests(TestCase):
    """
    Khi quay lại câu trước phải thấy nhãn mình đã gán, không phải trạng thái
    đồng thuận của nhóm ("Agreed").
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('ml_owner')
        cls.mate = make_user('ml_mate')
        cls.project = make_project(cls.owner, name='Nhãn của tôi',
                                   participants=[cls.mate],
                                   annotators_per_comment=2)
        cls.toxic = make_project_label(cls.project, make_label(cls.owner, 'toxic'))
        cls.clean = make_project_label(cls.project, make_label(cls.owner, 'clean'))
        cls.link = make_link(cls.project)
        cls.comment = make_comment(cls.link, text='nội dung thử', comment_id='ml-1')

    def setUp(self):
        self.client.login(username='ml_owner', password=PASSWORD)

    def test_my_label_returned_after_labelling(self):
        response = self.client.post(
            f'/api/comments/{self.comment.id}/set-comment-labels/',
            data={'label_id': str(self.toxic.id)}, content_type='application/json',
        )
        my_label = response.json()['my_label']
        self.assertEqual(my_label['id'], str(self.toxic.id))
        self.assertEqual(my_label['name'], 'toxic')
        self.assertTrue(my_label['color'])

    def test_my_label_persists_on_revisit(self):
        """Đọc lại câu (bấm 'Trước') vẫn thấy nhãn mình đã chọn."""
        self.client.post(
            f'/api/comments/{self.comment.id}/set-comment-labels/',
            data={'label_id': str(self.toxic.id)}, content_type='application/json',
        )
        data = self.client.get(f'/api/comments/{self.comment.id}/tokens/').json()
        self.assertEqual(data['comment']['my_label']['name'], 'toxic')

    def test_my_label_is_mine_not_someone_elses(self):
        """Hai người gán nhãn khác nhau: mỗi người thấy nhãn của chính mình."""
        annotation_service.set_comment_label(self.comment, self.mate, self.clean)
        annotation_service.set_comment_label(self.comment, self.owner, self.toxic)

        data = self.client.get(f'/api/comments/{self.comment.id}/tokens/').json()
        self.assertEqual(data['comment']['my_label']['name'], 'toxic')

        self.client.login(username='ml_mate', password=PASSWORD)
        data = self.client.get(f'/api/comments/{self.comment.id}/tokens/').json()
        self.assertEqual(data['comment']['my_label']['name'], 'clean')
        # Trạng thái nhóm là 'conflict' cho cả hai: không cho biết ai chọn gì.
        self.assertEqual(data['comment']['review_status'], 'conflict')

    def test_my_label_none_when_not_annotated(self):
        data = self.client.get(f'/api/comments/{self.comment.id}/tokens/').json()
        self.assertIsNone(data['comment']['my_label'])


class RemoveVsNoLabelTests(TestCase):
    """
    Phân biệt "gỡ bỏ quyết định" với "quyết định là không nhãn (O)".
    Nút "Xoá nhãn" phải thực sự gỡ bỏ, nếu không con số thống kê sẽ sai.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('rm_owner')
        cls.project = make_project(cls.owner, name='Gỡ nhãn')
        cls.label = make_project_label(cls.project, make_label(cls.owner, 'toxic'))
        cls.link = make_link(cls.project)
        cls.comment = make_comment(cls.link, comment_id='rm-1')

    def setUp(self):
        self.client.login(username='rm_owner', password=PASSWORD)

    def _post(self, payload):
        return self.client.post(
            f'/api/comments/{self.comment.id}/set-comment-labels/',
            data=payload, content_type='application/json',
        ).json()

    def test_remove_deletes_annotation_and_decrements_count(self):
        self._post({'label_id': str(self.label.id)})
        self.assertEqual(
            CommentAnnotation.objects.filter(
                comment=self.comment, annotator=self.owner, source='manual'
            ).count(), 1)

        result = self._post({'remove': True})
        self.assertTrue(result['removed'])
        self.assertIsNone(result['my_label'])
        self.assertEqual(result['progress']['my_annotated_link'], 0)
        self.assertEqual(
            CommentAnnotation.objects.filter(
                comment=self.comment, annotator=self.owner, source='manual'
            ).count(), 0)

    def test_null_label_is_a_deliberate_O_decision(self):
        result = self._post({'label_id': None})
        self.assertEqual(result['my_label']['name'], 'O')
        self.assertEqual(result['progress']['my_annotated_link'], 1,
                         'Chọn "O" là một quyết định, vẫn tính là đã gán nhãn.')

    def test_removed_comment_returns_to_queue(self):
        self._post({'label_id': str(self.label.id)})
        before = self.client.get(f'/api/links/{self.link.id}/queue/').json()['remaining']
        self._post({'remove': True})
        after = self.client.get(f'/api/links/{self.link.id}/queue/').json()['remaining']
        self.assertEqual(after, before + 1)


class WorkspaceI18nTests(TestCase):
    """Giao diện phải hiển thị đúng ngôn ngữ đang chọn."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('i18n_owner')
        cls.project = make_project(cls.owner, name='Ngôn ngữ')
        make_project_label(cls.project, make_label(cls.owner, 'toxic'))
        cls.link = make_link(cls.project)
        make_comment(cls.link, comment_id='i18n-1')

    def setUp(self):
        self.client.login(username='i18n_owner', password=PASSWORD)

    def _body(self, lang):
        response = self.client.get(f'/{lang}/links/{self.link.id}/annotate/')
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_vietnamese_strings_present(self):
        body = self._body('vi')
        for text in ('bạn đã gán ở nguồn này', 'trong phiên này', 'Bạn đã gán'):
            self.assertIn(text, body)

    def test_english_strings_present(self):
        body = self._body('en')
        for text in ('you annotated in this source', 'this session', 'You labelled'):
            self.assertIn(text, body)

    def test_english_page_has_no_untranslated_vietnamese(self):
        """Chuỗi tiếng Việt lọt vào trang tiếng Anh nghĩa là thiếu bản dịch."""
        body = self._body('en')
        for text in ('bạn đã gán ở nguồn này', 'trong phiên này',
                     'Bạn chưa gán nhãn câu này', 'Nhóm đã đồng thuận'):
            self.assertNotIn(text, body, f'Còn sót tiếng Việt: {text!r}')

    def test_project_list_separates_sections_in_both_languages(self):
        for lang, needle in (('vi', 'Dự án do chính bạn tạo và sở hữu'),
                             ('en', 'Projects you created and own')):
            response = self.client.get(f'/{lang}/projects/')
            self.assertIn(needle, response.content.decode())
