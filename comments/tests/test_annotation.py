"""Test logic gán nhãn đa người, đồng thuận và phân xử."""
from django.test import TestCase

from comments.models import CommentAnnotation
from comments.services import annotation as annotation_service
from comments.services.agreement import (
    cohen_kappa,
    krippendorff_alpha,
    percent_agreement,
    project_agreement_report,
)
from comments.tests.factories import (
    make_comment,
    make_label,
    make_link,
    make_project,
    make_project_label,
    make_user,
)


class MultiAnnotatorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('ma_owner')
        cls.alice = make_user('alice')
        cls.bob = make_user('bob')
        cls.project = make_project(cls.owner, name='Đa annotator',
                                   participants=[cls.alice, cls.bob],
                                   annotators_per_comment=2)
        cls.toxic = make_project_label(cls.project, make_label(cls.owner, 'toxic'))
        cls.clean = make_project_label(cls.project, make_label(cls.owner, 'clean'))
        cls.link = make_link(cls.project)

    def test_second_annotator_does_not_overwrite_first(self):
        """Lỗi nghiêm trọng nhất của mô hình cũ: người sau ghi đè người trước."""
        comment = make_comment(self.link, comment_id='c-overwrite')

        annotation_service.set_comment_label(comment, self.alice, self.toxic)
        annotation_service.set_comment_label(comment, self.bob, self.clean)

        annotations = CommentAnnotation.objects.filter(comment=comment, source='manual')
        self.assertEqual(annotations.count(), 2)
        self.assertEqual(
            {a.annotator.username for a in annotations}, {'alice', 'bob'}
        )

    def test_agreement_sets_gold_label(self):
        comment = make_comment(self.link, comment_id='c-agree')
        annotation_service.set_comment_label(comment, self.alice, self.toxic)
        result = annotation_service.set_comment_label(comment, self.bob, self.toxic)

        comment.refresh_from_db()
        self.assertEqual(result['review_status'], 'agreed')
        self.assertEqual(comment.review_status, 'agreed')
        self.assertEqual(comment.gold_label_id, self.toxic.id)

    def test_disagreement_marks_conflict_and_leaves_gold_empty(self):
        comment = make_comment(self.link, comment_id='c-conflict')
        annotation_service.set_comment_label(comment, self.alice, self.toxic)
        result = annotation_service.set_comment_label(comment, self.bob, self.clean)

        comment.refresh_from_db()
        self.assertEqual(result['review_status'], 'conflict')
        self.assertIsNone(comment.gold_label_id)

    def test_pending_until_enough_annotators(self):
        comment = make_comment(self.link, comment_id='c-pending')
        result = annotation_service.set_comment_label(comment, self.alice, self.toxic)
        self.assertEqual(result['review_status'], 'pending')
        self.assertEqual(result['required'], 2)

    def test_adjudication_resolves_conflict(self):
        comment = make_comment(self.link, comment_id='c-adj')
        annotation_service.set_comment_label(comment, self.alice, self.toxic)
        annotation_service.set_comment_label(comment, self.bob, self.clean)

        annotation_service.adjudicate_comment(comment, self.owner, self.toxic)

        comment.refresh_from_db()
        self.assertEqual(comment.review_status, 'adjudicated')
        self.assertEqual(comment.gold_label_id, self.toxic.id)

    def test_audit_trail_records_changes(self):
        comment = make_comment(self.link, comment_id='c-audit')
        annotation_service.set_comment_label(comment, self.alice, self.toxic)

        events = comment.events.filter(action='comment_label')
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().actor, self.alice)
        self.assertEqual(events.first().new_value, str(self.toxic.id))

    def test_span_labeling_covers_all_tokens_in_range(self):
        comment = make_comment(self.link, text='đây là một câu thử nghiệm dài',
                               comment_id='c-span')
        tokens = annotation_service.set_token_span_label(
            comment, self.alice, 1, 3, self.toxic
        )
        self.assertEqual(len(tokens), 3)
        for token in tokens:
            self.assertEqual(token.manual_label_id, self.toxic.id)

    def test_skip_marks_not_meaningful(self):
        comment = make_comment(self.link, comment_id='c-skip')
        annotation_service.skip_comment(comment, self.alice)
        comment.refresh_from_db()
        self.assertFalse(comment.is_meaningful)


class AgreementMetricTests(TestCase):
    """Kiểm tra công thức IAA trên các trường hợp đã biết kết quả."""

    def test_percent_agreement_perfect(self):
        matrix = {'c1': {'a': 'X', 'b': 'X'}, 'c2': {'a': 'Y', 'b': 'Y'}}
        value, units = percent_agreement(matrix)
        self.assertEqual(value, 1.0)
        self.assertEqual(units, 2)

    def test_percent_agreement_half(self):
        matrix = {'c1': {'a': 'X', 'b': 'X'}, 'c2': {'a': 'X', 'b': 'Y'}}
        value, _units = percent_agreement(matrix)
        self.assertEqual(value, 0.5)

    def test_cohen_kappa_zero_when_only_chance(self):
        # Cả hai gán ngẫu nhiên độc lập, đồng thuận đúng bằng mức kỳ vọng.
        matrix = {
            'c1': {'a': 'X', 'b': 'X'},
            'c2': {'a': 'X', 'b': 'Y'},
            'c3': {'a': 'Y', 'b': 'X'},
            'c4': {'a': 'Y', 'b': 'Y'},
        }
        kappa, n = cohen_kappa(matrix, 'a', 'b')
        self.assertEqual(n, 4)
        self.assertAlmostEqual(kappa, 0.0, places=6)

    def test_cohen_kappa_perfect(self):
        matrix = {'c1': {'a': 'X', 'b': 'X'}, 'c2': {'a': 'Y', 'b': 'Y'}}
        kappa, _n = cohen_kappa(matrix, 'a', 'b')
        self.assertAlmostEqual(kappa, 1.0, places=6)

    def test_krippendorff_alpha_perfect_agreement(self):
        matrix = {
            'c1': {'a': 'X', 'b': 'X'},
            'c2': {'a': 'Y', 'b': 'Y'},
            'c3': {'a': 'X', 'b': 'X'},
        }
        alpha, units = krippendorff_alpha(matrix)
        self.assertEqual(units, 3)
        self.assertAlmostEqual(alpha, 1.0, places=6)

    def test_krippendorff_alpha_none_without_multi_annotation(self):
        matrix = {'c1': {'a': 'X'}, 'c2': {'b': 'Y'}}
        alpha, units = krippendorff_alpha(matrix)
        self.assertIsNone(alpha)
        self.assertEqual(units, 0)

    def test_project_report_shape(self):
        owner = make_user('rep_owner')
        project = make_project(owner, name='Báo cáo')
        make_link(project)
        report = project_agreement_report(project)
        for key in ('total_comments', 'krippendorff_alpha', 'pairwise_kappa',
                    'annotator_count', 'alpha_interpretation'):
            self.assertIn(key, report)
