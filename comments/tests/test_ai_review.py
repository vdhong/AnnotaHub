"""
Quy trình "AI gán trước: người soát lại".

Ba điều phải đúng:
1. Đề xuất của AI phải hiện ra để người dùng soát, nhưng không được giả làm
   nhãn của họ.
2. Bấm "Đồng ý" thì đề xuất trở thành nhãn của chính người đó: cả câu lẫn
   token, và từ đó sửa chỗ nào chỉ chỗ đó đổi.
3. Số liệu AI đúng / bị sửa được tính lại từ dữ liệu, nên đúng cả với phần đã
   gán từ trước và không sai lệch khi người dùng sửa đi sửa lại.
"""
import re

from django.test import TestCase
from django.urls import reverse

from comments.models import AnnotationEvent, CommentAnnotation, Token, TokenAnnotation
from comments.services import ai_review
from comments.services import annotation as annotation_service
from comments.tests.factories import (
    make_comment,
    make_label,
    make_link,
    make_project,
    make_project_label,
    make_user,
)


class AiReviewTestBase(TestCase):
    def setUp(self):
        self.owner = make_user('ai-owner')
        self.alice = make_user('ai-alice')
        self.project = make_project(self.owner, name='Dự án soát AI',
                                    participants=[self.alice])
        self.toxic = make_project_label(self.project)
        self.clean = make_project_label(
            self.project, label=make_label(self.owner, name='sạch', color='#0F0')
        )
        self.link = make_link(self.project)
        self.comment = make_comment(self.link, text='đồ ngu thật', comment_id='c1')
        self.tokens = [
            Token.objects.create(comment=self.comment, text=text, position=i,
                                 start_offset=0, end_offset=len(text))
            for i, text in enumerate(['đồ', 'ngu', 'thật'])
        ]

    def _ai_labels(self, comment_label, token_labels=()):
        """Ghi đề xuất của AI y như task Celery vẫn làm."""
        annotation_service.store_ai_comment_annotation(
            self.comment, comment_label, confidence=0.8
        )
        self.comment.ai_label = comment_label
        self.comment.ai_processed = True
        self.comment.save(update_fields=['ai_label', 'ai_processed'])
        for position, label in token_labels:
            token = self.tokens[position]
            annotation_service.store_ai_token_annotations([(token, label, 0.9)])
            token.ai_label = label
            token.save(update_fields=['ai_label'])


class AcceptSuggestionTests(AiReviewTestBase):
    def test_ai_suggestion_is_not_shown_as_my_own_label(self):
        self._ai_labels(self.toxic, [(1, self.toxic)])
        annotation_service.attach_my_annotations([self.comment], self.alice)

        self.assertIsNone(self.comment.my_label)
        self.assertEqual(self.comment.ai_token_count, 1)
        suggested = [t for t in self.comment.my_tokens if t['is_ai_suggestion']]
        self.assertEqual(len(suggested), 1)
        self.assertIsNone(suggested[0]['my_label'])
        self.assertEqual(suggested[0]['ai_label']['id'], str(self.toxic.id))

    def test_accept_copies_comment_and_token_labels_to_the_user(self):
        self._ai_labels(self.toxic, [(0, self.toxic), (1, self.toxic)])

        result = ai_review.accept_ai_suggestion(self.comment, self.alice)

        self.assertTrue(result['accepted'])
        self.assertEqual(result['tokens_accepted'], 2)
        self.assertEqual(
            annotation_service.my_comment_label(self.comment, self.alice)['id'],
            str(self.toxic.id),
        )
        mine = TokenAnnotation.objects.filter(
            token__comment=self.comment, annotator=self.alice, source='manual'
        )
        self.assertEqual(
            sorted(a.token.position for a in mine), [0, 1]
        )

    def test_accept_keeps_the_ai_span_in_one_piece(self):
        """Cụm nhiều token của AI phải ở lại thành một cụm sau khi nhận."""
        for position in (0, 1):
            self.tokens[position].span_group = self.tokens[0].id
            self.tokens[position].save(update_fields=['span_group'])
        self._ai_labels(self.toxic, [(0, self.toxic), (1, self.toxic)])

        ai_review.accept_ai_suggestion(self.comment, self.alice)

        groups = {
            Token.objects.get(pk=self.tokens[i].pk).span_group for i in (0, 1)
        }
        self.assertEqual(len(groups), 1)
        self.assertIsNotNone(groups.pop())

    def test_ai_suggestion_survives_acceptance_so_stats_stay_comparable(self):
        self._ai_labels(self.toxic, [(1, self.toxic)])
        ai_review.accept_ai_suggestion(self.comment, self.alice)

        self.assertTrue(
            CommentAnnotation.objects.filter(comment=self.comment, source='ai').exists()
        )
        self.assertTrue(
            TokenAnnotation.objects.filter(
                token__comment=self.comment, source='ai'
            ).exists()
        )

    def test_editing_after_accept_only_changes_what_was_touched(self):
        self._ai_labels(self.toxic, [(0, self.toxic), (1, self.toxic)])
        ai_review.accept_ai_suggestion(self.comment, self.alice)

        # Sửa đúng một token; token còn lại vẫn giữ nhãn đã nhận từ AI.
        annotation_service.set_token_label(self.comment, self.alice, 0, self.clean)

        labels = {
            a.token.position: a.project_label_id
            for a in TokenAnnotation.objects.filter(
                token__comment=self.comment, annotator=self.alice, source='manual'
            ).select_related('token')
        }
        self.assertEqual(labels[0], self.clean.id)
        self.assertEqual(labels[1], self.toxic.id)

    def test_accept_is_recorded_in_the_audit_trail(self):
        self._ai_labels(self.toxic)
        ai_review.accept_ai_suggestion(self.comment, self.alice)

        self.assertTrue(
            AnnotationEvent.objects.filter(
                comment=self.comment, actor=self.alice, action='accept_ai'
            ).exists()
        )

    def test_accept_without_a_suggestion_is_refused(self):
        result = ai_review.accept_ai_suggestion(self.comment, self.alice)
        self.assertFalse(result['accepted'])
        self.assertFalse(
            CommentAnnotation.objects.filter(
                comment=self.comment, annotator=self.alice
            ).exists()
        )


class AcceptEndpointTests(AiReviewTestBase):
    def _accept(self):
        return self.client.post(
            reverse('api:api_accept_ai', args=[self.comment.id]),
            {}, content_type='application/json',
        )

    def test_endpoint_returns_my_new_labels(self):
        self._ai_labels(self.toxic, [(1, self.toxic)])
        self.client.force_login(self.alice)

        response = self._accept()

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['my_label']['id'], str(self.toxic.id))
        self.assertEqual(data['tokens_accepted'], 1)
        mine = [t for t in data['tokens'] if t['my_label']]
        self.assertEqual(len(mine), 1)
        self.assertFalse(any(t['is_ai_suggestion'] for t in data['tokens']))

    def test_endpoint_refuses_when_there_is_no_suggestion(self):
        self.client.force_login(self.alice)
        response = self._accept()
        self.assertEqual(response.status_code, 400)

    def test_locked_project_blocks_accepting(self):
        self._ai_labels(self.toxic)
        self.project.is_locked = True
        self.project.save(update_fields=['is_locked'])
        self.client.force_login(self.alice)

        self.assertEqual(self._accept().status_code, 403)

    def test_stranger_cannot_accept(self):
        self._ai_labels(self.toxic)
        self.client.force_login(make_user('ai-stranger'))
        self.assertEqual(self._accept().status_code, 404)


class DisplayContractTests(AiReviewTestBase):
    """
    Những gì giao diện dựa vào để hiển thị đúng đề xuất của AI.
    """

    def _html(self):
        self.client.force_login(self.alice)
        response = self.client.get(
            reverse('comments:link_detail', args=[self.link.id])
        )
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_ai_panel_is_not_a_dismissible_alert(self):
        """
        Ô "AI đề xuất" là bảng điều khiển thường trực, không được mang class
        .alert: main.js tự đóng mọi thông báo sau 5 giây, làm nút "Đồng ý với
        AI" biến mất ngay trước mắt người dùng.
        """
        self._ai_labels(self.toxic, [(1, self.toxic)])
        html = self._html()

        panel = re.search(
            r'<div[^>]*id="aiSuggestion-[^"]+"[^>]*>', html
        ) or re.search(r'<div[^>]*class="[^"]*"[^>]*id="aiSuggestion-', html)
        self.assertIsNotNone(panel, 'Không thấy ô đề xuất của AI')
        classes = re.search(r'class="([^"]*)"', panel.group(0))
        self.assertNotIn('alert', classes.group(1).split(),
                         'Ô đề xuất mang class .alert sẽ bị main.js tự đóng')

    def test_ai_badge_carries_its_own_colours(self):
        """
        Huy hiệu AI phải tự mang màu bằng inline style.

        collectstatic chỉ chạy lúc build image, nên style.css trong container
        rất dễ là bản đã cũ. Huy hiệu chỉ dựa vào class thì hiện ra trắng trơn,
        trông như câu chưa có nhãn nào.
        """
        self._ai_labels(self.toxic)
        html = self._html()

        badge = re.search(
            rf'id="commentLabel-{self.comment.id}"(.*?)</span>', html, re.S
        )
        self.assertIsNotNone(badge)
        self.assertIn('border', badge.group(1))
        self.assertIn(self.toxic.display_color, badge.group(1))

    def test_comment_without_any_ai_suggestion_is_counted_separately(self):
        """Số câu AI không đụng tới phải hiện ra, tránh bị hiểu là mất nhãn."""
        make_comment(self.link, text='câu AI bỏ qua', comment_id='c2')
        self._ai_labels(self.toxic)

        stats = ai_review.ai_review_stats(link=self.link)
        self.assertEqual(stats['no_ai_comments'], 1)
        self.assertEqual(stats['total_comments'], 2)
        self.assertIn('AI', self._html())

    def test_unlabelled_comment_sends_no_label_to_the_javascript(self):
        """
        Chưa gán gì thì bản đồ nhãn không được chứa comment đó.

        Giao diện phân biệt "chưa gán" với "đã xét, kết luận không nhãn" bằng
        đúng chỗ này. Gộp lại thì nút "Không có" luôn sáng sẵn, người dùng bấm
        vào để xác nhận và vô tình tạo ra một nhãn O thật trong dữ liệu.
        """
        self._ai_labels(self.toxic)
        self.client.force_login(self.alice)
        response = self.client.get(
            reverse('comments:link_detail', args=[self.link.id])
        )
        self.assertNotIn(str(self.comment.id), response.context['comment_manual_labels'])

        annotation_service.set_comment_label(self.comment, self.alice, None)
        response = self.client.get(
            reverse('comments:link_detail', args=[self.link.id])
        )
        mine = response.context['comment_manual_labels'][str(self.comment.id)]
        self.assertIsNone(mine['id'])
        self.assertEqual(mine['name'], 'O')


class AiStatsTests(AiReviewTestBase):
    def test_untouched_suggestions_are_counted_as_pending(self):
        self._ai_labels(self.toxic, [(1, self.toxic)])
        stats = ai_review.ai_review_stats(link=self.link)

        self.assertEqual(stats['pending_ai_comments'], 1)
        self.assertEqual(stats['comments']['reviewed'], 0)
        self.assertIsNone(stats['comments']['accuracy'])

    def test_accepting_counts_as_ai_being_right(self):
        self._ai_labels(self.toxic, [(1, self.toxic)])
        ai_review.accept_ai_suggestion(self.comment, self.alice)

        stats = ai_review.ai_review_stats(link=self.link)
        self.assertEqual(stats['comments'],
                         {'human_total': 1, 'reviewed': 1, 'matched': 1,
                          'corrected': 0, 'ai_missed': 0, 'accuracy': 100.0})
        self.assertEqual(stats['tokens']['matched'], 1)
        self.assertEqual(stats['pending_ai_comments'], 0)

    def test_correcting_after_accept_moves_the_number_to_corrected(self):
        self._ai_labels(self.toxic, [(1, self.toxic)])
        ai_review.accept_ai_suggestion(self.comment, self.alice)
        annotation_service.set_comment_label(self.comment, self.alice, self.clean)
        annotation_service.set_token_label(self.comment, self.alice, 1, self.clean)

        stats = ai_review.ai_review_stats(link=self.link)
        self.assertEqual(stats['comments']['matched'], 0)
        self.assertEqual(stats['comments']['corrected'], 1)
        self.assertEqual(stats['comments']['accuracy'], 0.0)
        self.assertEqual(stats['tokens']['corrected'], 1)

    def test_label_the_ai_never_touched_counts_as_a_miss(self):
        self._ai_labels(self.toxic)   # AI chỉ gán cấp câu, không gán token nào
        ai_review.accept_ai_suggestion(self.comment, self.alice)
        annotation_service.set_token_label(self.comment, self.alice, 2, self.toxic)

        stats = ai_review.ai_review_stats(link=self.link)
        self.assertEqual(stats['tokens']['reviewed'], 0)
        self.assertEqual(stats['tokens']['ai_missed'], 1)

    def test_both_sides_saying_no_label_is_a_match_not_a_correction(self):
        """NULL = NULL trong SQL không bao giờ đúng: dễ đếm nhầm thành 'bị sửa'."""
        self._ai_labels(None)
        ai_review.accept_ai_suggestion(self.comment, self.alice)

        stats = ai_review.ai_review_stats(link=self.link)
        self.assertEqual(stats['comments']['matched'], 1)
        self.assertEqual(stats['comments']['corrected'], 0)

    def test_stats_work_for_labels_made_before_the_feature_existed(self):
        """Số liệu tính lại từ dữ liệu nên không cần ai bấm nút 'Đồng ý'."""
        self._ai_labels(self.toxic)
        annotation_service.set_comment_label(self.comment, self.alice, self.toxic)

        stats = ai_review.ai_review_stats(project=self.project)
        self.assertEqual(stats['comments']['reviewed'], 1)
        self.assertEqual(stats['comments']['matched'], 1)

    def test_project_scope_covers_every_link(self):
        other_link = make_link(self.project, video_id='xyz98765432')
        other = make_comment(other_link, text='câu khác', comment_id='c2')
        annotation_service.store_ai_comment_annotation(other, self.toxic)
        annotation_service.set_comment_label(other, self.alice, self.clean)

        self._ai_labels(self.toxic)
        ai_review.accept_ai_suggestion(self.comment, self.alice)

        stats = ai_review.ai_review_stats(project=self.project)
        self.assertEqual(stats['comments']['reviewed'], 2)
        self.assertEqual(stats['comments']['matched'], 1)
        self.assertEqual(stats['comments']['corrected'], 1)
        self.assertEqual(stats['comments']['accuracy'], 50.0)

        # Lọc theo một link thì chỉ thấy phần của link đó.
        self.assertEqual(
            ai_review.ai_review_stats(link=other_link)['comments']['corrected'], 1
        )
