"""
Hàng đợi của màn hình gán nhãn nhanh có ba chế độ.

Nếu hàng đợi chỉ trả về việc còn lại thì gán nhãn xong là câu đó rời hàng đợi
vĩnh viễn, tải lại trang cũng không quay lại sửa được ngay trong màn hình này.
Ba chế độ todo / done / all ghép thành một dòng thời gian liền mạch để đi lui
đi tới.
"""
from django.test import TestCase
from django.urls import reverse

from comments.models import Token
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


class QueueModeTests(TestCase):
    def setUp(self):
        self.owner = make_user('q-owner')
        self.alice = make_user('q-alice')
        self.bob = make_user('q-bob')
        self.project = make_project(self.owner, name='Dự án hàng đợi',
                                    participants=[self.alice, self.bob])
        self.toxic = make_project_label(self.project)
        self.clean = make_project_label(
            self.project, label=make_label(self.owner, name='sạch', color='#0F0')
        )
        self.link = make_link(self.project)
        self.comments = [
            make_comment(self.link, text=f'câu số {i}', comment_id=f'c{i}')
            for i in range(5)
        ]
        self.client.force_login(self.alice)

    def _queue(self, **params):
        return self.client.get(
            reverse('api:api_annotation_queue', args=[self.link.id]), params
        ).json()

    def _ids(self, payload):
        return [c['id'] for c in payload['comments']]

    def test_labelled_comment_leaves_todo_but_appears_in_done(self):
        target = self.comments[0]
        annotation_service.set_comment_label(target, self.alice, self.toxic)

        todo = self._queue()
        self.assertNotIn(str(target.id), self._ids(todo))
        self.assertEqual(todo['counts']['done'], 1)

        done = self._queue(mode='done')
        self.assertEqual(self._ids(done), [str(target.id)])
        self.assertEqual(done['comments'][0]['my_label']['id'], str(self.toxic.id))

    def test_done_is_ordered_oldest_first_so_last_is_the_newest(self):
        for comment in self.comments[:3]:
            annotation_service.set_comment_label(comment, self.alice, self.toxic)

        done = self._queue(mode='done')
        self.assertEqual(
            self._ids(done), [str(c.id) for c in self.comments[:3]]
        )

    def test_editing_an_old_label_does_not_reorder_done(self):
        """Sửa nhãn câu cũ không được đẩy nó xuống cuối: mất dấu chỗ đang đứng."""
        for comment in self.comments[:3]:
            annotation_service.set_comment_label(comment, self.alice, self.toxic)
        before = self._ids(self._queue(mode='done'))

        annotation_service.set_comment_label(self.comments[0], self.alice, self.clean)

        self.assertEqual(self._ids(self._queue(mode='done')), before)

    def test_done_only_contains_my_own_work(self):
        annotation_service.set_comment_label(self.comments[0], self.alice, self.toxic)
        annotation_service.set_comment_label(self.comments[1], self.bob, self.toxic)

        self.assertEqual(
            self._ids(self._queue(mode='done')), [str(self.comments[0].id)]
        )
        self.client.force_login(self.bob)
        self.assertEqual(
            self._ids(self._queue(mode='done')), [str(self.comments[1].id)]
        )

    def test_all_mode_reaches_comments_todo_cannot(self):
        """
        Câu đã đủ số annotator rơi khỏi todo. Với annotators_per_comment=1, chỉ
        cần một người gán là người còn lại không thấy câu đó ở đâu nữa.
        """
        annotation_service.set_comment_label(self.comments[0], self.bob, self.toxic)

        self.assertNotIn(str(self.comments[0].id), self._ids(self._queue()))
        self.assertIn(str(self.comments[0].id), self._ids(self._queue(mode='all')))
        self.assertEqual(self._queue(mode='all')['counts']['all'], 5)

    def test_skipped_comment_is_reachable_again_in_all_mode(self):
        annotation_service.skip_comment(self.comments[0], self.alice, skipped=True)

        self.assertNotIn(str(self.comments[0].id), self._ids(self._queue()))
        self.assertIn(str(self.comments[0].id), self._ids(self._queue(mode='all')))

    def test_remaining_always_counts_my_todo_whatever_the_mode(self):
        annotation_service.set_comment_label(self.comments[0], self.alice, self.toxic)
        for mode in ('todo', 'done', 'all'):
            with self.subTest(mode=mode):
                payload = self._queue(mode=mode)
                self.assertEqual(payload['remaining'], 4)
                self.assertEqual(payload['counts'], {'todo': 4, 'done': 1, 'all': 5})

    def test_paging_reports_offset_and_total(self):
        payload = self._queue(mode='all', limit=2, offset=2)
        self.assertEqual(payload['offset'], 2)
        self.assertEqual(payload['total'], 5)
        self.assertEqual(len(payload['comments']), 2)

    def test_offset_past_the_end_falls_back_to_the_last_page(self):
        """Gán xong câu cuối rồi F5: offset cũ vượt quá danh sách đã co lại."""
        payload = self._queue(mode='all', limit=2, offset=99)
        self.assertEqual(payload['offset'], 4)
        self.assertEqual(len(payload['comments']), 1)

    def test_unknown_mode_falls_back_to_todo(self):
        self.assertEqual(self._queue(mode='linh-tinh')['mode'], 'todo')

    def test_token_labels_in_done_mode_are_my_own(self):
        target = self.comments[0]
        Token.objects.create(comment=target, text='câu', position=0,
                             start_offset=0, end_offset=3)
        annotation_service.set_comment_label(target, self.alice, self.toxic)
        annotation_service.set_token_label(target, self.alice, 0, self.toxic)
        annotation_service.set_comment_label(target, self.bob, self.clean)
        annotation_service.set_token_label(target, self.bob, 0, self.clean)

        done = self._queue(mode='done')
        self.assertEqual(
            done['comments'][0]['tokens'][0]['my_label']['id'], str(self.toxic.id)
        )

        self.client.force_login(self.bob)
        done = self._queue(mode='done')
        self.assertEqual(
            done['comments'][0]['tokens'][0]['my_label']['id'], str(self.clean.id)
        )

    def test_comments_with_an_ai_suggestion_come_first(self):
        """
        Câu AI đã có ý kiến phải lên trước câu AI không đụng tới.

        `toxicity_confidence` mặc định là 0.0, nên nếu chỉ xếp theo confidence
        tăng dần thì câu không có dự đoán nào lại luôn đứng đầu: người dùng
        lướt hết hàng đợi mà không gặp đề xuất nào của AI để soát.
        """
        with_ai = self.comments[4]
        annotation_service.store_ai_comment_annotation(
            with_ai, self.toxic, confidence=0.95
        )
        with_ai.ai_label = self.toxic
        with_ai.toxicity_confidence = 0.95
        with_ai.save(update_fields=['ai_label', 'toxicity_confidence'])

        first = self._queue()['comments'][0]
        self.assertEqual(first['id'], str(with_ai.id))
        self.assertIsNotNone(first['ai_label'])

    def test_among_ai_suggestions_the_least_confident_comes_first(self):
        """Trong nhóm đã có dự đoán, vẫn giữ tinh thần active learning."""
        for comment, confidence in ((self.comments[0], 0.9), (self.comments[1], 0.6)):
            annotation_service.store_ai_comment_annotation(
                comment, self.toxic, confidence=confidence
            )
            comment.ai_label = self.toxic
            comment.toxicity_confidence = confidence
            comment.save(update_fields=['ai_label', 'toxicity_confidence'])

        ids = self._ids(self._queue())[:2]
        self.assertEqual(ids, [str(self.comments[1].id), str(self.comments[0].id)])

    def test_stranger_cannot_read_the_queue(self):
        make_user('q-stranger')
        self.client.force_login(make_user('q-outsider'))
        response = self.client.get(
            reverse('api:api_annotation_queue', args=[self.link.id])
        )
        self.assertEqual(response.status_code, 404)
