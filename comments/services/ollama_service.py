"""
Gán nhãn bằng LLM qua Ollama, hoặc bất kỳ endpoint nào tương thích OpenAI.

Điểm quan trọng nhất của module: LLM được yêu cầu trả về span [start, end)
theo offset ký tự, không phải chỉ số token. Lý do là LLM tách token theo
khoảng trắng còn server tách bằng spaCy, nên nếu ghép nhãn theo vị trí trong
mảng thì chỉ cần lệch một token là toàn bộ nhãn phía sau trượt theo: từ vô
hại bị gán nhãn độc hại mà không có lỗi nào báo ra. Server ánh xạ span sang
token bằng giao nhau về offset.

Lỗi hết quota có lớp exception riêng để `except Exception` ở cuối không nuốt
mất rồi retry vô ích. Vòng retry có trần cả về số lần lẫn tổng thời gian chờ.
temperature mặc định 0, vì đây là tác vụ trích xuất có cấu trúc chứ không phải
sinh văn bản.
"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx
from django.conf import settings

from .tokenization import tokenize_text

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_BASE_URL = settings.OLLAMA_BASE_URL
DEFAULT_OLLAMA_API_KEY = settings.OLLAMA_API_KEY
DEFAULT_OLLAMA_MODEL = settings.OLLAMA_MODEL

MAX_TOTAL_WAIT_SECONDS = 300
NO_LABEL = 'O'


class LLMError(Exception):
    """Lỗi chung khi gọi LLM."""


class QuotaExhaustedError(LLMError):
    """Hết quota/credit: không được retry, phải dừng toàn bộ tiến trình."""


class LLMResponseError(LLMError):
    """Response sai định dạng."""


def _resolve_ollama_config(base_url=None, api_key=None, model=None):
    return (
        (base_url or '').strip() or DEFAULT_OLLAMA_BASE_URL,
        (api_key or '').strip() or DEFAULT_OLLAMA_API_KEY,
        (model or '').strip() or DEFAULT_OLLAMA_MODEL,
    )


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
SPAN_SCHEMA_BLOCK = """{
  "comment_label": "tên_nhãn hoặc O",
  "confidence": 0.0,
  "source_is_vietnamese": true,
  "is_meaningful": true,
  "vietnamese_text": "",
  "spans": [
    {"start": 0, "end": 0, "text": "", "label": "tên_nhãn", "score": 0.0}
  ]
}"""

ANNOTATION_PROMPT_TEMPLATE = """Bạn là hệ thống phân tích ngữ nghĩa và gán nhãn nội dung cho bình luận mạng xã hội tiếng Việt.

Nhiệm vụ: phân tích DUY NHẤT một bình luận và trả về CHỈ một chuỗi JSON hợp lệ.
Tuyệt đối không dùng markdown (```json), không giải thích, không thêm chữ nào ngoài JSON.

========================
DANH SÁCH NHÃN
========================
Chỉ được dùng các nhãn sau:
{LABEL_LIST}

Nếu nội dung không khớp nhãn nào, dùng nhãn mặc định "O".

========================
QUY TẮC NGÔN NGỮ
========================
- Bình luận tiếng Việt: source_is_vietnamese = true, vietnamese_text = giữ NGUYÊN VĂN đầu vào.
  KHÔNG sửa chính tả, KHÔNG viết lại, KHÔNG chuẩn hoá dấu câu.
- Bình luận ngoại ngữ: source_is_vietnamese = false, vietnamese_text = bản dịch tiếng Việt
  tự nhiên, giữ nguyên sắc thái và mức độ gay gắt của bản gốc.
- Mọi phân tích bên dưới dựa trên vietnamese_text.

========================
TÍNH CÓ NGHĨA
========================
is_meaningful = false khi bình luận chỉ có emoji, ký tự ngẫu nhiên (asdfgh),
spam vô nghĩa (kkkkk), hoặc quá ngắn không rõ nội dung.
Khi đó: comment_label = "O", confidence = 0.0, spans = [].

========================
GÁN NHÃN CẤP CỤM TỪ (QUAN TRỌNG NHẤT)
========================
Trả về "spans": danh sách các ĐOẠN VĂN BẢN mang nhãn, mỗi đoạn gồm:
- "start": chỉ số KÝ TỰ bắt đầu trong vietnamese_text (đếm từ 0).
- "end"  : chỉ số ký tự kết thúc (KHÔNG bao gồm ký tự tại vị trí này).
- "text" : đúng đoạn văn bản vietnamese_text[start:end] — dùng để kiểm tra chéo.
- "label": tên nhãn lấy từ DANH SÁCH NHÃN.
- "score": độ chắc chắn 0.0–1.0.

QUY TẮC BẮT BUỘC:
- CHỈ liệt kê các đoạn CÓ nhãn khác "O". Không liệt kê từ bình thường.
- start/end phải đếm theo KÝ TỰ của vietnamese_text, kể cả dấu cách và emoji.
- vietnamese_text[start:end] phải khớp CHÍNH XÁC với trường "text".
- Các đoạn không được chồng lấn nhau.
- Nếu không có đoạn nào mang nhãn, trả spans = [].

========================
VÍ DỤ
========================
Input: "cái video này rác rưởi vcl 😂"
(giả sử danh sách nhãn có "offensive")
Các vị trí ký tự: c=0, á=1, i=2, ' '=3, v=4 ... "rác rưởi" bắt đầu ở 14, kết thúc ở 22; "vcl" từ 23 đến 26.
Output:
{
  "comment_label": "offensive",
  "confidence": 0.95,
  "source_is_vietnamese": true,
  "is_meaningful": true,
  "vietnamese_text": "cái video này rác rưởi vcl 😂",
  "spans": [
    {"start": 14, "end": 22, "text": "rác rưởi", "label": "offensive", "score": 0.9},
    {"start": 23, "end": 26, "text": "vcl", "label": "offensive", "score": 0.98}
  ]
}

========================
ĐỊNH DẠNG OUTPUT
========================
""" + SPAN_SCHEMA_BLOCK


def _build_annotation_prompt(labels_info=None) -> str:
    labels_info = labels_info or []
    if not labels_info:
        # Không có nhãn nào được cấu hình -> dùng bộ nhãn tối thiểu.
        labels_info = [
            {'name': 'toxic', 'description': 'Nội dung độc hại: chửi thề, xúc phạm, '
                                             'miệt thị, đe doạ, phân biệt đối xử.'},
        ]
    label_definitions = '\n'.join(
        f'  - "{item["name"]}": {item.get("description") or "(không có mô tả)"}'
        for item in labels_info
    )
    return ANNOTATION_PROMPT_TEMPLATE.replace('{LABEL_LIST}', label_definitions)


# ---------------------------------------------------------------------------
# Gọi LLM
# ---------------------------------------------------------------------------
def _chat_url(base_url: str) -> str:
    base = base_url.rstrip('/')
    if base.endswith('/v1/chat/completions'):
        return base
    if base.endswith('/v1'):
        return f'{base}/chat/completions'
    return f'{base}/v1/chat/completions'


def _is_quota_error(text: str) -> bool:
    lowered = (text or '').lower()
    return any(marker in lowered for marker in (
        'insufficient_quota', 'insufficient quota', 'exceeded your current quota',
        'billing_hard_limit_reached', 'credit balance is too low',
    ))


def _make_chat_request(prompt: str, system: str, *, max_retries: int = 3,
                       timeout: int = 120, base_url: str = None,
                       api_key: str = None, model: str = None) -> str | None:
    """
    Gọi endpoint chat completion.

    Chiến lược lỗi:
    - Hết quota            -> QuotaExhaustedError, không retry.
    - 429 rate limit        -> chờ theo Retry-After, có trần tổng thời gian chờ.
    - 5xx / timeout mạng    -> retry với backoff luỹ thừa, tối đa max_retries.
    - 4xx khác              -> LLMError ngay, retry không giúp ích.
    """
    resolved_url, resolved_key, resolved_model = _resolve_ollama_config(
        base_url, api_key, model
    )
    if not resolved_url:
        raise LLMError('Chưa cấu hình OLLAMA_BASE_URL.')

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {resolved_key}' if resolved_key else 'Bearer ollama',
    }
    payload = {
        'model': resolved_model,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': prompt},
        ],
        'stream': False,
        # Tác vụ trích xuất có cấu trúc: cần kết quả tái lập được, không sáng tạo.
        'temperature': settings.ANNOTATION_LLM_TEMPERATURE,
        'max_tokens': 2048,
        # Ép định dạng JSON ở phía máy chủ khi nhà cung cấp hỗ trợ.
        'response_format': {'type': 'json_object'},
    }

    url = _chat_url(resolved_url)
    attempt = 0
    total_wait = 0.0

    while True:
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, json=payload, headers=headers)

            if response.status_code == 429:
                body = response.text
                if _is_quota_error(body):
                    raise QuotaExhaustedError(
                        f'Tài khoản LLM đã hết quota/credit. Chi tiết: {body[:300]}'
                    )
                retry_after = response.headers.get('Retry-After')
                wait = int(retry_after) + 1 if (retry_after or '').isdigit() else 5
                total_wait += wait
                if total_wait > MAX_TOTAL_WAIT_SECONDS:
                    raise LLMError(
                        f'Bị giới hạn tốc độ liên tục quá {MAX_TOTAL_WAIT_SECONDS}s, dừng lại.'
                    )
                logger.warning('Rate limit, chờ %ss (tổng đã chờ %ss).', wait, total_wait)
                time.sleep(wait)
                continue

            if response.status_code >= 400:
                body = response.text
                if _is_quota_error(body):
                    raise QuotaExhaustedError(
                        f'Tài khoản LLM đã hết quota (HTTP {response.status_code}). {body[:300]}'
                    )
                if response.status_code >= 500:
                    attempt += 1
                    if attempt > max_retries:
                        raise LLMError(f'LLM lỗi {response.status_code}: {body[:300]}')
                    time.sleep(min(30, 2 ** attempt))
                    continue
                raise LLMError(f'LLM từ chối yêu cầu ({response.status_code}): {body[:300]}')

            result = response.json()
            choices = result.get('choices') or []
            if not choices:
                raise LLMResponseError(f'Response không có "choices": {str(result)[:300]}')
            return choices[0].get('message', {}).get('content', '')

        except (QuotaExhaustedError, LLMError):
            # Lỗi nghiệp vụ đã phân loại: để nó bay lên, đừng retry.
            raise
        except httpx.TimeoutException:
            attempt += 1
            if attempt > max_retries:
                raise LLMError(
                    f'Hết thời gian chờ mạng sau {max_retries} lần thử.'
                ) from None
            logger.warning('Timeout mạng, thử lại lần %s/%s.', attempt, max_retries)
            time.sleep(min(30, 2 ** attempt))
        except httpx.HTTPError as exc:
            attempt += 1
            if attempt > max_retries:
                raise LLMError(f'Lỗi mạng khi gọi LLM: {exc}') from exc
            time.sleep(min(30, 2 ** attempt))


# ---------------------------------------------------------------------------
# Phân tích response
# ---------------------------------------------------------------------------
def _parse_json_response(response_text: str) -> dict | None:
    if not response_text:
        return None

    candidates = [response_text]

    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', response_text, re.DOTALL)
    if match:
        candidates.append(match.group(1))

    match = re.search(r'\{.*\}', response_text, re.DOTALL)
    if match:
        candidates.append(match.group(0))

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    logger.error('Không phân tích được JSON từ LLM: %s', response_text[:300])
    return None


def _coerce_bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ('true', '1', 'yes', 'y', 'on'):
            return True
        if normalized in ('false', '0', 'no', 'n', 'off'):
            return False
    return default


def _coerce_score(value):
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return min(1.0, max(0.0, score))


# ---------------------------------------------------------------------------
# Chuẩn hoá span
# ---------------------------------------------------------------------------
def normalize_spans(text: str, raw_spans, valid_label_names) -> tuple[list[dict], list[str]]:
    """
    Kiểm tra và sửa các span do LLM trả về.

    Trả về (spans hợp lệ, danh sách cảnh báo). Cảnh báo được lưu lại để có thể
    đánh dấu comment cần người kiểm tra lại, thay vì im lặng gán nhãn sai.
    """
    spans: list[dict] = []
    warnings: list[str] = []

    if not isinstance(raw_spans, list):
        return spans, ['spans không phải danh sách']

    text_length = len(text)

    for index, raw in enumerate(raw_spans):
        if not isinstance(raw, dict):
            warnings.append(f'span[{index}] không phải object')
            continue

        label = (raw.get('label') or '').strip()
        if not label or label.upper() == NO_LABEL:
            continue
        if valid_label_names and label.lower() not in valid_label_names:
            warnings.append(f'span[{index}] có nhãn lạ "{label}"')
            continue

        claimed_text = raw.get('text') or ''
        try:
            start = int(raw['start'])
            end = int(raw['end'])
        except (KeyError, TypeError, ValueError):
            start = end = -1

        # Bước 1: offset hợp lệ và khớp với text đã khai báo -> dùng luôn.
        if 0 <= start < end <= text_length and (
            not claimed_text or text[start:end] == claimed_text
        ):
            pass
        elif claimed_text:
            # Bước 2: offset sai nhưng đoạn text tồn tại -> tự tìm lại vị trí.
            found = text.find(claimed_text)
            if found == -1:
                warnings.append(
                    f'span[{index}] "{claimed_text[:40]}" không có trong văn bản'
                )
                continue
            if 0 <= start < text_length:
                # Chọn lần xuất hiện gần vị trí LLM khai báo nhất.
                best = found
                cursor = found
                while cursor != -1:
                    if abs(cursor - start) < abs(best - start):
                        best = cursor
                    cursor = text.find(claimed_text, cursor + 1)
                found = best
            start, end = found, found + len(claimed_text)
            warnings.append(f'span[{index}] offset sai, đã căn lại theo nội dung')
        else:
            warnings.append(f'span[{index}] offset không hợp lệ và không có text')
            continue

        spans.append({
            'start': start,
            'end': end,
            'text': text[start:end],
            'label': label,
            'score': _coerce_score(raw.get('score')),
        })

    # Loại bỏ span chồng lấn: giữ span dài hơn (thông tin cụ thể hơn).
    spans.sort(key=lambda item: (item['start'], -(item['end'] - item['start'])))
    deduped: list[dict] = []
    for span in spans:
        if deduped and span['start'] < deduped[-1]['end']:
            warnings.append(
                f'span "{span["text"][:30]}" chồng lấn, đã bỏ qua'
            )
            continue
        deduped.append(span)

    return deduped, warnings


def map_spans_to_tokens(text: str, spans) -> dict[int, dict]:
    """
    Ánh xạ span (theo offset ký tự) sang chỉ số token.

    Một token nhận nhãn của span nếu khoảng của nó GIAO NHAU với span. Nhờ đó
    kết quả không phụ thuộc việc LLM tách token giống hay khác server.
    """
    tokens = tokenize_text(text)
    assignment: dict[int, dict] = {}

    for span_index, span in enumerate(spans):
        for index, token in enumerate(tokens):
            # Giao nhau thực sự (không tính trường hợp chỉ chạm biên).
            if token['start'] < span['end'] and span['start'] < token['end']:
                assignment[index] = {
                    'label': span['label'],
                    'score': span['score'],
                    # Các token cùng một span chia sẻ chỉ số này -> xuất BIO đúng.
                    'span_index': span_index,
                }
    return assignment


# ---------------------------------------------------------------------------
# API công khai
# ---------------------------------------------------------------------------
def annotate_comment(text: str, labels_info=None, ollama_base_url=None,
                     ollama_api_key=None, ollama_model=None) -> dict | None:
    """Gán nhãn một bình luận. Trả về dict kết quả đã chuẩn hoá, hoặc None."""
    if not text or not text.strip():
        return {
            'comment_label': NO_LABEL,
            'confidence': 1.0,
            'spans': [],
            'is_meaningful': False,
            'source_is_vietnamese': True,
            'vietnamese_text': '',
            'warnings': [],
        }

    system_prompt = _build_annotation_prompt(labels_info)
    prompt = (
        'Phân tích bình luận sau và trả về JSON đúng schema đã yêu cầu.\n\n'
        f'Bình luận: """{text}"""'
    )

    response = _make_chat_request(
        prompt, system=system_prompt,
        base_url=ollama_base_url, api_key=ollama_api_key, model=ollama_model,
    )
    if not response:
        return None

    result = _parse_json_response(response)
    if not result:
        return None

    result.setdefault('confidence', 0.5)
    result['is_meaningful'] = _coerce_bool(result.get('is_meaningful', True), default=True)
    result['source_is_vietnamese'] = _coerce_bool(
        result.get('source_is_vietnamese', True), default=True
    )

    vietnamese_text = result.get('vietnamese_text') or text
    if not isinstance(vietnamese_text, str):
        vietnamese_text = text
    result['vietnamese_text'] = vietnamese_text

    if not result['is_meaningful']:
        result['comment_label'] = NO_LABEL
        result['confidence'] = 0.0
        result['spans'] = []
        result['warnings'] = []
        return result

    valid_names = {item['name'].lower() for item in (labels_info or [])}

    # Chấp nhận cả khoá "spans" (mới) và "token_labels" (mô hình cũ trả về).
    raw_spans = result.get('spans')
    if raw_spans is None and 'token_labels' in result:
        raw_spans, legacy_warnings = _spans_from_legacy_token_labels(
            vietnamese_text, result.get('token_labels')
        )
    else:
        legacy_warnings = []

    spans, warnings = normalize_spans(vietnamese_text, raw_spans or [], valid_names)
    result['spans'] = spans
    result['warnings'] = legacy_warnings + warnings

    comment_label = (result.get('comment_label') or NO_LABEL)
    if not isinstance(comment_label, str):
        comment_label = NO_LABEL
    comment_label = comment_label.strip()
    if valid_names and comment_label.lower() not in valid_names and comment_label != NO_LABEL:
        result['warnings'].append(f'comment_label lạ "{comment_label}", đã đổi thành O')
        comment_label = NO_LABEL
    result['comment_label'] = comment_label

    result['confidence'] = _coerce_score(result.get('confidence')) or 0.5
    return result


def _spans_from_legacy_token_labels(text: str, token_labels):
    """
    Chuyển định dạng token_labels cũ sang span, bằng cách dò tìm từng chuỗi
    trong văn bản thay vì tin vào thứ tự chỉ số.
    """
    warnings = ['mô hình trả về token_labels (định dạng cũ), đã chuyển sang span']
    spans = []
    if not isinstance(token_labels, list):
        return spans, warnings

    cursor = 0
    for item in token_labels:
        if not isinstance(item, dict):
            continue
        token_text = (item.get('text') or '').strip()
        if not token_text:
            continue

        label = item.get('label')
        if label is None:
            label = 'toxic' if _coerce_bool(item.get('is_toxic')) else NO_LABEL
        if not label or str(label).upper() == NO_LABEL:
            # Vẫn phải đẩy con trỏ để giữ đúng thứ tự dò tìm.
            position = text.find(token_text, cursor)
            if position != -1:
                cursor = position + len(token_text)
            continue

        position = text.find(token_text, cursor)
        if position == -1:
            position = text.find(token_text)
        if position == -1:
            warnings.append(f'không tìm thấy token "{token_text[:30]}" trong văn bản')
            continue

        spans.append({
            'start': position,
            'end': position + len(token_text),
            'text': token_text,
            'label': str(label),
            'score': item.get('score'),
        })
        cursor = position + len(token_text)

    return spans, warnings


def process_comment(comment_text: str, labels_info=None, ollama_base_url=None,
                    ollama_api_key=None, ollama_model=None) -> dict | None:
    """Xử lý một bình luận, trả về annotation kèm metadata."""
    if not comment_text or not comment_text.strip():
        return {
            'annotation': {
                'comment_label': NO_LABEL,
                'confidence': None,
                'source_is_vietnamese': True,
                'is_meaningful': False,
                'vietnamese_text': comment_text or '',
                'spans': [],
                'warnings': [],
            },
            'vietnamese_text': comment_text or '',
            'original_text': '',
            'was_translated': False,
            'is_meaningful': False,
        }

    annotation = annotate_comment(
        comment_text, labels_info=labels_info,
        ollama_base_url=ollama_base_url, ollama_api_key=ollama_api_key,
        ollama_model=ollama_model,
    )
    if not annotation:
        # Không bịa ra nhãn khi mô hình trả về rác: báo lỗi để task đếm là thất bại.
        raise LLMResponseError('Không phân tích được kết quả từ LLM.')

    source_is_vietnamese = annotation.get('source_is_vietnamese', True)
    return {
        'annotation': annotation,
        'vietnamese_text': annotation.get('vietnamese_text') or comment_text,
        'original_text': '' if source_is_vietnamese else comment_text,
        'was_translated': not source_is_vietnamese,
        'is_meaningful': annotation.get('is_meaningful', True),
    }


def create_token_annotations(comment_text: str, annotation_result: dict,
                             labels_info=None) -> list[dict]:
    """
    Tạo dữ liệu nhãn cấp token từ span đã chuẩn hoá.

    Token nào không nằm trong span nào thì mang nhãn None (tương đương "O").
    """
    if annotation_result.get('is_meaningful') is False:
        return []

    tokens = tokenize_text(comment_text)
    spans = annotation_result.get('spans') or []
    assignment = map_spans_to_tokens(comment_text, spans)

    return [{
        'text': token['text'],
        'position': index,
        'start_offset': token['start'],
        'end_offset': token['end'],
        'assigned_label': assignment.get(index, {}).get('label'),
        'toxicity_score': assignment.get(index, {}).get('score'),
        'span_index': assignment.get(index, {}).get('span_index'),
        'is_toxic': index in assignment,
    } for index, token in enumerate(tokens)]


def get_comment_label_name(annotation_result: dict) -> str | None:
    """
    Tên nhãn cấp câu.

    Trả về chính chuỗi 'O' (không phải None) để phân biệt rõ "AI kết luận trung
    tính" với "AI chưa xử lý".
    """
    label = annotation_result.get('comment_label')
    if label is None:
        return NO_LABEL
    return str(label)


# Giữ tên cũ cho tương thích ngược.
tokenize_vietnamese = tokenize_text
__all__ = [
    'LLMError', 'QuotaExhaustedError', 'LLMResponseError',
    'annotate_comment', 'process_comment', 'create_token_annotations',
    'get_comment_label_name', 'tokenize_text', 'normalize_spans',
    'map_spans_to_tokens',
]
