"""
Mỗi annotator chỉ thấy nhãn của chính mình trên màn hình gán nhãn.

`comment.manual_label` / `token.manual_label` là bản sao tính sẵn dùng chung
cho cả dự án: nó giữ annotation thủ công gần nhất của bất kỳ ai. Màn hình gán
nhãn không được đọc thẳng cột đó, kẻo người này thấy nhãn người kia: vừa gây
nhầm lẫn, vừa hỏng độ đồng thuận vì các annotator thấy đáp án của nhau trước
khi chốt.
"""
from django.test import TestCase
from django.urls import reverse

from comments.models import Token
from comments.services import annotation as annotation_service
from comments.services.stats import label_stats_for_link, link_counters
from comments.tests.factories import (
    make_comment,
    make_label,
    make_link,
    make_project,
    make_project_label,
    make_user,
)

PASSWORD = 'test-pass-12345'


class MyAnnotationViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('mine-owner')
        cls.alice = make_user('mine-alice')
        cls.bob = make_user('mine-bob')
        cls.project = make_project(
            cls.owner, name='Dự án nhiều người', participants=[cls.alice, cls.bob],
            annotators_per_comment=2,
        )
        cls.toxic = make_project_label(cls.project)
        cls.clean = make_project_label(
            cls.project, label=make_label(cls.owner, name='sạch', color='#00FF00')
        )
        cls.link = make_link(cls.project)
        cls.comment = make_comment(cls.link, text='một câu thử', comment_id='c1')
        cls.token = Token.objects.create(
            comment=cls.comment, text='một', position=0,
            start_offset=0, end_offset=3,
        )

    def _label_as(self, user, project_label):
        annotation_service.set_comment_label(self.comment, user, project_label)
        annotation_service.set_token_label(self.comment, user, 0, project_label)

    def test_each_annotator_sees_only_their_own_label(self):
        self._label_as(self.alice, self.toxic)
        self._label_as(self.bob, self.clean)

        for user, mine, theirs in (
            (self.alice, self.toxic, self.clean),
            (self.bob, self.clean, self.toxic),
        ):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(
                    reverse('comments:link_detail', args=[self.link.id])
                )
                comment = response.context['comments'][0]
                self.assertEqual(comment.my_label['id'], str(mine.id))
                self.assertEqual(comment.my_tokens[0]['my_label']['id'], str(mine.id))

                # Trong vùng hiển thị bình luận: chỉ có id nhãn của mình.
                # (Sau vùng này là danh sách nhãn của dự án dùng để dựng nút
                # bấm: ở đó mọi nhãn xuất hiện là đúng.)
                html = response.content.decode()
                cards = html[html.index('id="commentsContainer"'):
                             html.index('<!-- Pagination -->')]
                self.assertIn(str(mine.id), cards)
                self.assertNotIn(str(theirs.id), cards)

    def test_annotator_who_has_not_labelled_sees_nothing(self):
        self._label_as(self.alice, self.toxic)

        self.client.force_login(self.bob)
        response = self.client.get(reverse('comments:link_detail', args=[self.link.id]))
        comment = response.context['comments'][0]
        self.assertIsNone(comment.my_label)
        self.assertIsNone(comment.my_tokens[0]['my_label'])
        self.assertContains(response, 'Bạn chưa gán')

    def test_deliberate_no_label_differs_from_not_labelled(self):
        """Chọn "không nhãn" là một quyết định, khác hẳn với chưa xét."""
        annotation_service.set_comment_label(self.comment, self.alice, None)

        self.assertEqual(
            annotation_service.my_comment_label(self.comment, self.alice),
            annotation_service.NO_LABEL_DATA,
        )
        self.assertIsNone(
            annotation_service.my_comment_label(self.comment, self.bob)
        )

    def test_ai_label_stays_visible_to_everyone(self):
        """Gợi ý của máy không phải việc của người khác: vẫn phải thấy."""
        annotation_service.set_comment_label(
            self.comment, None, self.toxic, source='ai'
        )
        annotation_service.set_token_label(
            self.comment, None, 0, self.toxic, source='ai'
        )

        self.client.force_login(self.bob)
        response = self.client.get(reverse('comments:link_detail', args=[self.link.id]))
        token = response.context['comments'][0].my_tokens[0]
        self.assertEqual(token['ai_label']['id'], str(self.toxic.id))
        self.assertIsNone(token['my_label'])


class MyAnnotationFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('flt-owner')
        cls.alice = make_user('flt-alice')
        cls.bob = make_user('flt-bob')
        cls.project = make_project(cls.owner, name='Dự án bộ lọc',
                                   participants=[cls.alice, cls.bob])
        cls.toxic = make_project_label(cls.project)
        cls.link = make_link(cls.project)
        cls.mine = make_comment(cls.link, text='alice làm câu này', comment_id='c1')
        cls.theirs = make_comment(cls.link, text='bob làm câu này', comment_id='c2')
        cls.untouched = make_comment(cls.link, text='chưa ai làm', comment_id='c3')

        annotation_service.set_comment_label(cls.mine, cls.alice, cls.toxic)
        annotation_service.set_comment_label(cls.theirs, cls.bob, cls.toxic)

    def _ids(self, response):
        return {str(c.id) for c in response.context['comments']}

    def test_annotated_filter_only_lists_my_work(self):
        self.client.force_login(self.alice)
        response = self.client.get(
            reverse('comments:link_detail', args=[self.link.id]), {'filter': 'annotated'}
        )
        self.assertEqual(self._ids(response), {str(self.mine.id)})

    def test_unannotated_filter_includes_what_others_did(self):
        """Câu Bob đã làm vẫn là việc chưa làm của Alice."""
        self.client.force_login(self.alice)
        response = self.client.get(
            reverse('comments:link_detail', args=[self.link.id]),
            {'filter': 'unannotated'},
        )
        self.assertEqual(
            self._ids(response), {str(self.theirs.id), str(self.untouched.id)}
        )

    def test_label_filter_is_per_user(self):
        self.client.force_login(self.bob)
        response = self.client.get(
            reverse('comments:link_detail', args=[self.link.id]),
            {'filter': f'label_{self.toxic.id}'},
        )
        self.assertEqual(self._ids(response), {str(self.theirs.id)})

    def test_counters_and_label_stats_are_per_user(self):
        for user, annotated, pending in ((self.alice, 1, 2), (self.bob, 1, 2)):
            with self.subTest(user=user.username):
                counters = link_counters(self.link, user=user)
                self.assertEqual(counters['annotated_comments'], annotated)
                self.assertEqual(counters['manual_pending_count'], pending)
                self.assertEqual(counters['total_comments'], 3)

                stats = label_stats_for_link([self.toxic], self.link, user=user)
                self.assertEqual(stats[0]['count'], 1)

    def test_counters_without_user_stay_project_wide(self):
        """Không truyền user thì vẫn là con số của cả dự án (dùng cho báo cáo)."""
        counters = link_counters(self.link)
        self.assertEqual(counters['annotated_comments'], 2)
        self.assertEqual(counters['manual_pending_count'], 1)


class MyAnnotationApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('api-owner')
        cls.alice = make_user('api-alice')
        cls.bob = make_user('api-bob')
        cls.project = make_project(cls.owner, name='Dự án API',
                                   participants=[cls.alice, cls.bob],
                                   annotators_per_comment=2)
        cls.toxic = make_project_label(cls.project)
        cls.link = make_link(cls.project)
        cls.comment = make_comment(cls.link, text='câu api', comment_id='c1')
        Token.objects.create(comment=cls.comment, text='câu', position=0,
                             start_offset=0, end_offset=3)

    def test_token_api_hides_other_annotators_labels(self):
        annotation_service.set_token_label(self.comment, self.alice, 0, self.toxic)

        self.client.force_login(self.bob)
        data = self.client.get(
            reverse('api:api_comment_tokens', args=[self.comment.id])
        ).json()
        self.assertIsNone(data['tokens'][0]['my_label'])
        self.assertIsNone(data['tokens'][0]['manual_label'])
        self.assertIsNone(data['comment']['my_label'])

        self.client.force_login(self.alice)
        data = self.client.get(
            reverse('api:api_comment_tokens', args=[self.comment.id])
        ).json()
        self.assertEqual(data['tokens'][0]['my_label']['id'], str(self.toxic.id))

    def test_queue_hides_other_annotators_token_labels(self):
        annotation_service.set_token_label(self.comment, self.alice, 0, self.toxic)

        self.client.force_login(self.bob)
        data = self.client.get(
            reverse('api:api_annotation_queue', args=[self.link.id])
        ).json()
        self.assertEqual(len(data['comments']), 1)
        self.assertIsNone(data['comments'][0]['tokens'][0]['my_label'])

    def test_status_api_reports_my_progress(self):
        annotation_service.set_comment_label(self.comment, self.alice, self.toxic)

        self.client.force_login(self.alice)
        alice_stats = self.client.get(
            reverse('api:api_link_status', args=[self.link.id])
        ).json()['stats']
        self.client.force_login(self.bob)
        bob_stats = self.client.get(
            reverse('api:api_link_status', args=[self.link.id])
        ).json()['stats']

        self.assertEqual(alice_stats['annotated_comments'], 1)
        self.assertEqual(alice_stats['manual_pending_count'], 0)
        self.assertEqual(bob_stats['annotated_comments'], 0)
        self.assertEqual(bob_stats['manual_pending_count'], 1)
