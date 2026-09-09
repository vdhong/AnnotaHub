"""Test tokenizer và việc căn chỉnh nhãn của LLM theo offset ký tự."""
from django.test import SimpleTestCase

from comments.services.ollama_service import (
    create_token_annotations,
    map_spans_to_tokens,
    normalize_spans,
)
from comments.services.tokenization import get_nlp, tokenize_text


class TokenizerTests(SimpleTestCase):
    def test_offsets_map_back_to_original_text(self):
        text = 'cái video này rác rưởi vcl 😂'
        for token in tokenize_text(text):
            self.assertEqual(text[token['start']:token['end']], token['text'])

    def test_pipeline_is_reused(self):
        """spaCy phải khởi tạo một lần, không phải mỗi lần gọi tokenize."""
        first = get_nlp()
        second = get_nlp()
        if first is not None:
            self.assertIs(first, second)

    def test_empty_text(self):
        self.assertEqual(tokenize_text(''), [])


class SpanAlignmentTests(SimpleTestCase):
    """
    Ghép nhãn của LLM theo chỉ số mảng là lỗi âm thầm nguy hiểm nhất: lệch một
    token thì toàn bộ nhãn phía sau gán sai mà không có gì báo ra.
    """

    TEXT = 'cái video này rác rưởi vcl'
    VALID = {'offensive'}

    def test_valid_span_is_kept(self):
        start = self.TEXT.index('rác rưởi')
        spans, warnings = normalize_spans(self.TEXT, [{
            'start': start, 'end': start + len('rác rưởi'),
            'text': 'rác rưởi', 'label': 'offensive', 'score': 0.9,
        }], self.VALID)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]['text'], 'rác rưởi')
        self.assertEqual(warnings, [])

    def test_wrong_offset_is_realigned_by_text(self):
        """LLM đếm sai vị trí -> hệ thống tự tìm lại theo nội dung, có cảnh báo."""
        spans, warnings = normalize_spans(self.TEXT, [{
            'start': 0, 'end': 3, 'text': 'rác rưởi',
            'label': 'offensive', 'score': 0.9,
        }], self.VALID)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]['text'], 'rác rưởi')
        self.assertTrue(any('căn lại' in w for w in warnings))

    def test_hallucinated_span_is_rejected(self):
        """Đoạn không tồn tại trong văn bản bị loại, không gán bừa."""
        spans, warnings = normalize_spans(self.TEXT, [{
            'start': 0, 'end': 5, 'text': 'không hề có trong câu',
            'label': 'offensive',
        }], self.VALID)
        self.assertEqual(spans, [])
        self.assertTrue(any('không có trong văn bản' in w for w in warnings))

    def test_unknown_label_is_rejected(self):
        spans, warnings = normalize_spans(self.TEXT, [{
            'start': 0, 'end': 3, 'text': 'cái', 'label': 'nhãn-lạ',
        }], self.VALID)
        self.assertEqual(spans, [])
        self.assertTrue(any('nhãn lạ' in w for w in warnings))

    def test_overlapping_spans_deduped(self):
        start = self.TEXT.index('rác rưởi')
        spans, warnings = normalize_spans(self.TEXT, [
            {'start': start, 'end': start + 8, 'text': 'rác rưởi', 'label': 'offensive'},
            {'start': start, 'end': start + 3, 'text': 'rác', 'label': 'offensive'},
        ], self.VALID)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]['text'], 'rác rưởi')

    def test_span_maps_to_every_overlapping_token(self):
        """
        Kết quả không phụ thuộc cách tách token: có pyvi thì "rác rưởi" là một
        token, không có thì là HAI. Cả hai trường hợp đều phải phủ đúng cụm từ.
        """
        start = self.TEXT.index('rác rưởi')
        mapping = map_spans_to_tokens(self.TEXT, [{
            'start': start, 'end': start + len('rác rưởi'),
            'label': 'offensive', 'score': 0.9,
        }])
        tokens = tokenize_text(self.TEXT)
        covered = ''.join(tokens[i]['text'] for i in sorted(mapping))
        self.assertEqual(covered.replace(' ', ''), 'rácrưởi')
        # Không token nào nằm ngoài span bị gán nhãn.
        for index in mapping:
            self.assertLess(tokens[index]['start'], start + len('rác rưởi'))
            self.assertGreater(tokens[index]['end'], start)

    def test_token_annotations_leave_unlabeled_tokens_empty(self):
        start = self.TEXT.index('vcl')
        rows = create_token_annotations(self.TEXT, {
            'is_meaningful': True,
            'spans': [{'start': start, 'end': start + 3, 'text': 'vcl',
                       'label': 'offensive', 'score': 1.0}],
        }, labels_info=[{'name': 'offensive'}])
        labeled = [r for r in rows if r['assigned_label']]
        self.assertEqual(len(labeled), 1)
        self.assertEqual(labeled[0]['text'], 'vcl')
        # Các token còn lại không bị gán nhãn nhầm.
        self.assertTrue(all(r['assigned_label'] is None
                            for r in rows if r['text'] != 'vcl'))

    def test_legacy_token_labels_format_is_converted(self):
        from comments.services.ollama_service import _spans_from_legacy_token_labels

        spans, warnings = _spans_from_legacy_token_labels(self.TEXT, [
            {'text': 'cái', 'label': 'O'},
            {'text': 'video', 'label': 'O'},
            {'text': 'này', 'label': 'O'},
            {'text': 'rác', 'label': 'offensive'},
        ])
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]['text'], 'rác')
        self.assertEqual(self.TEXT[spans[0]['start']:spans[0]['end']], 'rác')
        self.assertTrue(warnings)


class VietnameseWordSegmentationTests(SimpleTestCase):
    """
    Tách từ tiếng Việt gộp nhiều âm tiết thành một token ("rác rưởi", "đại học").

    Đây là lý do không ghép nhãn theo chỉ số mảng được: LLM tách theo khoảng
    trắng nên đếm ra số token khác hẳn server. Ánh xạ theo offset ký tự miễn
    nhiễm với khác biệt đó.
    """

    def test_offsets_correct_for_compound_words(self):
        text = 'học sinh trường đại học rất giỏi'
        tokens = tokenize_text(text)
        for token in tokens:
            self.assertEqual(text[token['start']:token['end']], token['text'])

    def test_span_hits_compound_token_regardless_of_segmentation(self):
        """
        Span "rác rưởi" phải gán đúng dù server coi đó là 1 token (có pyvi) hay
        2 token (fallback regex).
        """
        text = 'cái video này rác rưởi vcl'
        start = text.index('rác rưởi')
        mapping = map_spans_to_tokens(text, [{
            'start': start, 'end': start + len('rác rưởi'),
            'label': 'offensive', 'score': 0.9,
        }])
        tokens = tokenize_text(text)
        covered = ''.join(tokens[i]['text'] for i in sorted(mapping)).replace(' ', '')
        self.assertEqual(covered, 'rácrưởi')
        # Không token nào ngoài span bị gán nhầm.
        for index in mapping:
            self.assertTrue(tokens[index]['start'] < start + len('rác rưởi'))
            self.assertTrue(tokens[index]['end'] > start)

    def test_urls_stay_whole(self):
        text = 'check https://example.com/abc nhé'
        tokens = tokenize_text(text)
        self.assertIn('https://example.com/abc', [t['text'] for t in tokens])
