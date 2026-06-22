"""
Ollama Toxicity Annotation Service
Uses Ollama API to annotate comments for toxicity.
Supports Vietnamese comments and translates non-Vietnamese comments to Vietnamese.
"""
import json
import logging
import re
import time
from typing import List, Dict, Optional
import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

# Ollama configuration (global defaults)
DEFAULT_OLLAMA_BASE_URL = settings.OLLAMA_BASE_URL
DEFAULT_OLLAMA_API_KEY = settings.OLLAMA_API_KEY
DEFAULT_OLLAMA_MODEL = settings.OLLAMA_MODEL


def _resolve_ollama_config(base_url=None, api_key=None, model=None):
    """
    Resolve Ollama configuration, prioritizing explicitly provided values,
    then falling back to global defaults.
    Returns (base_url, api_key, model) tuple.
    """
    resolved_base_url = (base_url or '').strip() or DEFAULT_OLLAMA_BASE_URL
    resolved_api_key = (api_key or '').strip() or DEFAULT_OLLAMA_API_KEY
    resolved_model = (model or '').strip() or DEFAULT_OLLAMA_MODEL
    return resolved_base_url, resolved_api_key, resolved_model

# System prompt for toxicity annotation (single-call, Vietnamese-normalized)
# ANNOTATION_SYSTEM_PROMPT = """Bạn là một chuyên gia phân tích độc hại (toxicity analysis) cho comment YouTube.
# Nhiệm vụ của bạn là làm TẤT CẢ trong MỘT LẦN gọi:
# 1. Xác định comment có độc hại (toxic) hay không độc hại (non_toxic)
# 2. Gán nhãn từng từ/token trong câu
# 3. Nếu comment không phải tiếng Việt, hãy dịch sang tiếng Việt
# 4. Xác định comment có ý nghĩa hay không
# 5. Trả về CHỈ JSON, không có bất kỳ văn bản nào khác

# Định nghĩa "toxic" bao gồm:
# - Chửi thề, xúc phạm, miệt thị
# - Quấy rối, đe dọa, bạo lực
# - Phân biệt chủng tộc, giới tính, tôn giáo
# - Ngôn từ khiêu dâm, đồi trụy
# - Spam, quảng cáo lừa đảo

# QUAN TRỌNG:
# - Nếu comment là tiếng Việt, đặt "source_is_vietnamese": true và "vietnamese_text" có thể giữ nguyên nội dung gốc.
# - Nếu comment không phải tiếng Việt, đặt "source_is_vietnamese": false và "vietnamese_text" phải là bản dịch tiếng Việt của comment.
# - Nếu comment không có ý nghĩa, đặt "is_meaningful": false, "comment_label": null, "token_labels": [].
# - "token_labels" phải bao phủ toàn bộ câu "vietnamese_text" theo thứ tự token.

# Định dạng JSON bắt buộc:
# {
#   "comment_label": "toxic hoặc non_toxic hoặc null",
#   "confidence": 0.0-1.0,
#   "source_is_vietnamese": true,
#   "is_meaningful": true,
#   "vietnamese_text": "chuỗi tiếng Việt cuối cùng dùng để lưu trữ",
#   "token_labels": [
#     {"text": "từ", "is_toxic": false, "score": 0.0}
#   ]
# }
# """
ANNOTATION_SYSTEM_PROMPT1 = """Bạn là hệ thống phân tích độ độc hại (toxicity) cho comment YouTube.

Nhiệm vụ: Phân tích DUY NHẤT một comment đầu vào và trả về CHỈ một chuỗi JSON hợp lệ. Tuyệt đối không sử dụng ký hiệu markdown (như ```json), không giải thích, không thêm bất kỳ văn bản nào ngoài cấu trúc JSON.

========================
MỤC TIÊU & QUY TẮC NGÔN NGỮ
========================
1. Ngôn ngữ: 
- Nếu comment là tiếng Việt: source_is_vietnamese = true, vietnamese_text = nội dung gốc.
- Nếu không phải tiếng Việt: source_is_vietnamese = false, vietnamese_text = bản dịch tiếng Việt tự nhiên của comment gốc (phải giữ nguyên mức độ toxicity và ý nghĩa). TẤT CẢ phân tích dưới đây phải dựa trên vietnamese_text.

2. Tính có ý nghĩa (is_meaningful): 
- False nếu: Chỉ có emoji, ký tự ngẫu nhiên (asdfgh), spam vô nghĩa (kkkkkk), hoặc quá ngắn không rõ nội dung.
- Nếu False: comment_label = null, confidence = 0.0, token_labels = [].

3. Phân loại (comment_label): 
- "toxic": Bao gồm chửi thề, xúc phạm, miệt thị, quấy rối, đe dọa, kích động bạo lực, phân biệt đối xử, tình dục thô tục, spam lừa đảo.
- "non_toxic": Cảm xúc tiêu cực nhẹ, phê bình lịch sự, joke không công kích, từ nhạy cảm trong ngữ cảnh kỹ thuật/trung tính (vd: "sex education", "kill process").
- null: Nếu is_meaningful = false.

========================
QUY TẮC TOKEN VÀ GÁN NHÃN (DỰA TRÊN VIETNAMESE_TEXT)
========================
- Tách token dựa trên khoảng trắng.
- QUAN TRỌNG: Tự động tách các dấu câu dính liền ra khỏi chữ (Ví dụ: "ngu!" -> "ngu", "!").
- Không tự sửa chính tả, giữ nguyên nội dung gốc.
- is_toxic: true nếu token đó mang tính độc hại.
- score: Thể hiện độ chắc chắn của mô hình đối với nhãn của token này (0.0 đến 1.0).
- Hệ thống tự động đảm bảo chuỗi token_labels bao phủ toàn bộ vietnamese_text theo đúng thứ tự.

========================
VÍ DỤ MẪU (FEW-SHOT)
========================
Input: "cái video này rác rưởi vcl 😂"
Output:
{
  "comment_label": "toxic",
  "confidence": 0.95,
  "source_is_vietnamese": true,
  "is_meaningful": true,
  "vietnamese_text": "cái video này rác rưởi vcl 😂",
  "token_labels": [
    {"text": "cái", "is_toxic": false, "score": 1.0},
    {"text": "video", "is_toxic": false, "score": 1.0},
    {"text": "này", "is_toxic": false, "score": 1.0},
    {"text": "rác", "is_toxic": true, "score": 0.9},
    {"text": "rưởi", "is_toxic": true, "score": 0.9},
    {"text": "vcl", "is_toxic": true, "score": 0.98},
    {"text": "😂", "is_toxic": false, "score": 1.0}
  ]
}

========================
RÀNG BUỘC OUTPUT (STRICT SCHEMA)
========================
Trả về JSON định dạng chuẩn xác như sau (thay thế các giá trị mặc định bằng kết quả phân tích):

{
  "comment_label": "toxic",
  "confidence": 0.0,
  "source_is_vietnamese": true,
  "is_meaningful": true,
  "vietnamese_text": "",
  "token_labels": [
    {
      "text": "",
      "is_toxic": false,
      "score": 0.0
    }
  ]
}"""

ANNOTATION_SYSTEM_PROMPT2 = """Bạn là hệ thống phân tích ngữ nghĩa và gán nhãn nội dung cho comment YouTube.

Nhiệm vụ: Phân tích DUY NHẤT một comment đầu vào và trả về CHỈ một chuỗi JSON hợp lệ. Tuyệt đối không sử dụng ký hiệu markdown (như ```json), không giải thích, không thêm bất kỳ văn bản nào ngoài cấu trúc JSON.

========================
DANH SÁCH NHÃN PHÂN LOẠI
========================
Chỉ được phép sử dụng các nhãn được định nghĩa dưới đây:
{LABEL_LIST}

(Nếu không có nhãn nào phù hợp, hoặc comment mang tính trung tính/tích cực, sử dụng nhãn mặc định là "O")

========================
MỤC TIÊU & QUY TẮC NGÔN NGỮ
========================
1. Ngôn ngữ: 
- Nếu comment là tiếng Việt: source_is_vietnamese = true, vietnamese_text = nội dung gốc.
- Nếu không phải tiếng Việt: source_is_vietnamese = false, vietnamese_text = bản dịch tiếng Việt tự nhiên của comment gốc (phải giữ nguyên sắc thái và ý nghĩa). TẤT CẢ phân tích dưới đây phải dựa trên vietnamese_text.

2. Tính có ý nghĩa (is_meaningful): 
- False nếu: Chỉ có emoji, ký tự ngẫu nhiên (asdfgh), spam vô nghĩa (kkkkkk), hoặc quá ngắn không rõ nội dung.
- Nếu False: comment_label = null, confidence = 0.0, token_labels = [].

3. Phân loại (comment_label): 
- Đánh giá tổng thể vietnamese_text và chọn MỘT nhãn chính xác nhất từ DANH SÁCH NHÃN PHÂN LOẠI.
- Nếu nội dung không vi phạm hoặc không khớp với bất kỳ nhãn nào trong danh sách, gán comment_label = "O".
- null: Chỉ khi is_meaningful = false.

========================
QUY TẮC TOKEN VÀ GÁN NHÃN (DỰA TRÊN VIETNAMESE_TEXT)
========================
- Tách token dựa trên khoảng trắng.
- QUAN TRỌNG: Tự động tách các dấu câu dính liền ra khỏi chữ (Ví dụ: "ngu!" -> "ngu", "!").
- Không tự sửa chính tả, giữ nguyên nội dung gốc.
- label (của token): Gán tên nhãn từ DANH SÁCH NHÃN PHÂN LOẠI nếu token đó trực tiếp mang ý nghĩa của nhãn. Gán "O" (chữ O in hoa) nếu token là từ ngữ bình thường, dấu câu, hoặc không mang tính chất của các nhãn trên.
- score: Thể hiện độ chắc chắn của mô hình đối với nhãn của token này (0.0 đến 1.0). Đối với token nhãn "O", score thường là 1.0.
- Hệ thống tự động đảm bảo chuỗi token_labels bao phủ toàn bộ vietnamese_text theo đúng thứ tự.

========================
VÍ DỤ MẪU (FEW-SHOT ĐỊNH DẠNG)
========================
Input: "cái video này rác rưởi vcl 😂"
(Giả định danh sách nhãn có nhãn "offensive")
Output:
{
  "comment_label": "offensive",
  "confidence": 0.95,
  "source_is_vietnamese": true,
  "is_meaningful": true,
  "vietnamese_text": "cái video này rác rưởi vcl 😂",
  "token_labels": [
    {"text": "cái", "label": "O", "score": 1.0},
    {"text": "video", "label": "O", "score": 1.0},
    {"text": "này", "label": "O", "score": 1.0},
    {"text": "rác", "label": "offensive", "score": 0.9},
    {"text": "rưởi", "label": "offensive", "score": 0.9},
    {"text": "vcl", "label": "offensive", "score": 0.98},
    {"text": "😂", "label": "O", "score": 1.0}
  ]
}

========================
RÀNG BUỘC OUTPUT (STRICT SCHEMA)
========================
Trả về JSON định dạng chuẩn xác như sau (thay thế các giá trị mặc định bằng kết quả phân tích):

{
  "comment_label": "tên_nhãn",
  "confidence": 0.0,
  "source_is_vietnamese": true,
  "is_meaningful": true,
  "vietnamese_text": "",
  "token_labels": [
    {
      "text": "",
      "label": "tên_nhãn hoặc O",
      "score": 0.0
    }
  ]
}
"""

def _build_annotation_prompt(labels_info=None):
    """
    Build the annotation system prompt, optionally including custom label definitions.
    
    Args:
        labels_info: List of dicts with 'name', 'description', 'color' for each label.
                     If None, uses default toxic/non-toxic mode.
    """
    if labels_info and len(labels_info) > 0:
        # Multi-label mode
        label_definitions = '\n'.join(
            f'  - "{lb["name"]}": {lb["description"]}'
            for lb in labels_info
        )
        return ANNOTATION_SYSTEM_PROMPT2.replace("{LABEL_LIST}", label_definitions)
    else:
        # Legacy toxic/non-toxic mode
        return ANNOTATION_SYSTEM_PROMPT1

def _make_chat_request(prompt: str, system: str, max_retries: int = 3, timeout: int = 120,
                       base_url: str = None, api_key: str = None, model: str = None) -> Optional[str]:
    """
    Thực thi API gọi LLM.
    - Bổ sung: Nếu 429 là do hết quota (insufficient_quota) -> Thoát ngay.
    - Nếu 429 do rate limit tốc độ -> Chờ và lặp vô hạn.
    - Lỗi 5xx/Timeout -> Thử lại theo max_retries.
    """
    resolved_url, resolved_key, resolved_model = _resolve_ollama_config(base_url, api_key, model)
    
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {resolved_key}' if resolved_key else 'Bearer ollama',
    }

    payload = {
        'model': resolved_model,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': prompt}, 
            {"role": "assistant", "content": "<think>\n\n</think>\n\n"}
        ],
        'stream': False,
        'temperature': 0.7,
        'max_tokens': 2048, 
    }
    
    base = resolved_url.rstrip('/')
    url = base if base.endswith('/v1/chat/completions') else (
        f"{base}/chat/completions" if base.endswith('/v1') else f"{base}/v1/chat/completions"
    )

    attempt_for_network_errors = 0

    while True:
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, json=payload, headers=headers)
                
                # 1. XỬ LÝ LỖI 429 (Rate Limit hoặc Quota)
                if response.status_code == 429:
                    error_content = response.text
                    
                    # Kiểm tra chuỗi insufficient_quota để chặn vòng lặp vô hạn
                    if "insufficient_quota" in error_content:
                        raise Exception(f"Tài khoản hết quota/credits. Dừng tiến trình! Chi tiết: {error_content}")
                        
                    # Nếu không phải hết quota, xử lý như Rate Limit bình thường
                    retry_after = response.headers.get('Retry-After')
                    wait_time = int(retry_after) + 1 if retry_after and retry_after.isdigit() else 5
                    
                    logger.warning(f"Bị Rate Limit tốc độ. Tạm dừng {wait_time}s... Chi tiết: {error_content}")
                    time.sleep(wait_time)
                    continue 

                # 2. XỬ LÝ CÁC LUỒNG CÒN LẠI
                response.raise_for_status()
                result = response.json()
                
                choices = result.get('choices', [])
                if choices and len(choices) > 0:
                    return choices[0].get('message', {}).get('content', '')
                else:
                    logger.error(f"Cấu trúc response từ API bị sai: {result}")
                    raise Exception("Cấu trúc response từ API bị sai")
                    
        except httpx.TimeoutException:
            attempt_for_network_errors += 1
            if attempt_for_network_errors > max_retries:
                raise Exception(f"Hết thời gian chờ mạng sau {max_retries} lần thử.")
                
            logger.warning(f"Mạng bị Timeout. Đang thử lại ({attempt_for_network_errors}/{max_retries})...")
            time.sleep(2 ** attempt_for_network_errors)
            
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            error_content = e.response.text
            
            # Một số nhà cung cấp API dùng mã 400, 402 hoặc 403 thay vì 429 cho việc hết quota
            if "insufficient_quota" in error_content:
                 raise Exception(f"Tài khoản hết quota (Mã HTTP {status}). Dừng tiến trình! Chi tiết: {error_content}")
            
            logger.error(f"Lỗi HTTP {status}: {error_content}")
            
            if status >= 500: 
                attempt_for_network_errors += 1
                if attempt_for_network_errors > max_retries:
                    raise Exception(error_content)
                time.sleep(2 ** attempt_for_network_errors)
            else:
                raise Exception(error_content)
                
        except Exception as e:
            attempt_for_network_errors += 1
            if attempt_for_network_errors > max_retries:
                raise e
            logger.error(f"Lỗi hệ thống bất ngờ: {e}")
            time.sleep(2 ** attempt_for_network_errors)

def _parse_json_response(response_text: str) -> Optional[Dict]:
    """Parse the JSON response from Ollama."""
    if not response_text:
        return None

    # Try to extract JSON from the response
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON in markdown code blocks
    json_pattern = r'```(?:json)?\s*\n?(.*?)\n?\s*```'
    match = re.search(json_pattern, response_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find any JSON object in the text
    brace_pattern = r'\{.*\}'
    match = re.search(brace_pattern, response_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    logger.error(f"Failed to parse response: {response_text[:200]}")
    return None

def _coerce_bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ('true', '1', 'yes', 'y', 'on'):
            return True
        if normalized in ('false', '0', 'no', 'n', 'off'):
            return False
    return default

def annotate_comment(text: str, labels_info=None, ollama_base_url=None, ollama_api_key=None, ollama_model=None) -> Optional[Dict]:
    """
    Annotate a single Vietnamese comment for toxicity or custom labels.

    Args:
        text: The Vietnamese comment text to annotate
        labels_info: Optional list of dicts with 'name', 'description', 'color'.
                     If provided, uses multi-label mode with custom labels.
        ollama_base_url: Optional Ollama base URL (defaults to global settings)
        ollama_api_key: Optional Ollama API key (defaults to global settings)
        ollama_model: Optional Ollama model name (defaults to global settings)

    Returns:
        Dictionary with annotation result. Schema depends on mode:
        - Legacy: comment_label, token_labels with is_toxic
        - Multi-label: comment_label, token_labels with 'label' field
    """
    if not text or not text.strip():
        return {
            'comment_label': 'non_toxic',
            'confidence': 1.0,
            'token_labels': [],
            'is_meaningful': False,
            'source_is_vietnamese': True,
            'vietnamese_text': '',
        }

    # Build system prompt with or without custom labels
    system_prompt = _build_annotation_prompt(labels_info)
    use_multi_label = labels_info is not None and len(labels_info) > 0

    prompt = (
        "Phân tích comment sau và trả về JSON theo đúng schema đã yêu cầu.\n\n"
        f"Comment: \"{text}\""
    )
    response = _make_chat_request(prompt, system=system_prompt,
                                  base_url=ollama_base_url, api_key=ollama_api_key, model=ollama_model)
    if not response:
        return None
    logger.debug(f"Ollama response: {response[:500]}")
    result = _parse_json_response(response)
    if not result:
        return None

    # Ensure required fields
    result.setdefault('is_meaningful', True)
    result.setdefault('confidence', 0.5)
    result.setdefault('source_is_vietnamese', True)
    result.setdefault('vietnamese_text', text)
    result.setdefault('token_labels', [])

    result['is_meaningful'] = _coerce_bool(result.get('is_meaningful', True), default=True)
    result['source_is_vietnamese'] = _coerce_bool(result.get('source_is_vietnamese', True), default=True)
    if not result.get('vietnamese_text'):
        result['vietnamese_text'] = text

    if not result['is_meaningful']:
        result['comment_label'] = None
        result['confidence'] = None
        result['token_labels'] = []
        return result

    # Normalize comment_label
    if use_multi_label:
        # In multi-label mode, comment_label is one of the label names or 'O'
        valid_names = [lb['name'].lower() for lb in labels_info]
        cl = result.get('comment_label', 'O') or 'O'
        if cl.lower() not in valid_names and cl != 'O':
            # If AI returned an unknown label, default to 'O'
            result['comment_label'] = 'O'
    else:
        # Legacy mode: normalize to toxic/non_toxic
        if result.get('comment_label') not in ('toxic', 'non_toxic'):
            result['comment_label'] = (
                'toxic' if 'toxic' in str(result['comment_label']).lower() else 'non_toxic'
            )

    return result


def process_comment(comment_text: str, labels_info=None,
                    ollama_base_url=None, ollama_api_key=None, ollama_model=None) -> Optional[Dict]:
    """
    Process a comment using one Ollama call.

    Args:
        comment_text: The original comment text
        labels_info: Optional list of label dicts for custom label mode
        ollama_base_url: Optional Ollama base URL (defaults to global settings)
        ollama_api_key: Optional Ollama API key (defaults to global settings)
        ollama_model: Optional Ollama model name (defaults to global settings)

    Returns:
        Dictionary with annotation result and metadata
    """
    if not comment_text or not comment_text.strip():
        return {
            'annotation': {
                'comment_label': None,
                'confidence': None,
                'source_is_vietnamese': True,
                'is_meaningful': False,
                'vietnamese_text': comment_text or '',
                'token_labels': []
            },
            'vietnamese_text': comment_text or '',
            'original_text': '',
            'was_translated': False,
        }

    annotation = annotate_comment(comment_text, labels_info=labels_info,
                                  ollama_base_url=ollama_base_url,
                                  ollama_api_key=ollama_api_key,
                                  ollama_model=ollama_model)
    if not annotation:
        return {
            'annotation': {
                'comment_label': 'non_toxic',
                'confidence': 0.0,
                'source_is_vietnamese': True,
                'is_meaningful': True,
                'vietnamese_text': comment_text,
                'token_labels': []
            },
            'vietnamese_text': comment_text,
            'original_text': '',
            'was_translated': False,
        }

    source_is_vietnamese = annotation.get('source_is_vietnamese', True)
    is_meaningful = annotation.get('is_meaningful', True)
    vietnamese_text = annotation.get('vietnamese_text', comment_text) or comment_text
    original_text = '' if source_is_vietnamese else comment_text

    return {
        'annotation': annotation,
        'vietnamese_text': vietnamese_text,
        'original_text': original_text,
        'was_translated': not source_is_vietnamese,
        'is_meaningful': is_meaningful,
    }

def tokenize_text(text: str) -> List[Dict]:
    """
    Simple text tokenizer that preserves character positions.
    Returns list of {text, start, end} for each token.
    """
    raw_tokens = []
    # Match URLs, words, numbers, punctuation and emoji/symbols as separate tokens.
    pattern = (
        r'https?://\S+|www\.\S+'
        r'|[\wÀ-ỹ]+(?:[\'’_-][\wÀ-ỹ]+)*'
        r'|\d+(?:[.,:/-]\d+)*'
        r'|[^\w\s]'
    )
    for match in re.finditer(pattern, text):
        raw_tokens.append({
            'text': match.group(0),
            'start': match.start(),
            'end': match.end()
        })

    tokens = []
    for token in raw_tokens:
        if tokens:
            previous = tokens[-1]
            consecutive = previous['end'] == token['start']
            previous_is_symbol = bool(re.fullmatch(r'[^\w\s]+', previous['text'], flags=re.UNICODE))
            current_is_symbol = bool(re.fullmatch(r'[^\w\s]+', token['text'], flags=re.UNICODE))
            if consecutive and previous_is_symbol and current_is_symbol:
                previous['text'] += token['text']
                previous['end'] = token['end']
                continue

        tokens.append(token)

    return tokens


def create_token_annotations(comment_text: str, annotation_result: Dict, labels_info=None) -> List[Dict]:
    """
    Create token-level annotations from comment-level annotation.
    Supports both legacy (is_toxic) and multi-label (label field) modes.

    Args:
        comment_text: The vietnamese_text to tokenize
        annotation_result: Parsed AI response dict
        labels_info: Optional list of label dicts to detect mode
    """
    if annotation_result.get('is_meaningful') is False:
        return []

    tokens = tokenize_text(comment_text)
    token_labels = annotation_result.get('token_labels', []) or []

    # Detect mode: multi-label if 'label' key in first token entry or labels_info provided
    use_multi_label = labels_info is not None and len(labels_info) > 0
    if token_labels and 'label' in token_labels[0]:
        use_multi_label = True

    # Build name lookup for project labels
    valid_label_names = set()
    if use_multi_label and labels_info:
        valid_label_names = {lb['name'].lower() for lb in labels_info}

    token_annotations = []
    for idx, token in enumerate(tokens):
        is_toxic = False
        score = None
        assigned_label = None  # For multi-label mode: stores label name or None

        if idx < len(token_labels):
            label = token_labels[idx] or {}
            if use_multi_label:
                # Multi-label: token has 'label' field with label name or 'O'
                assigned_label = label.get('label', 'O') or 'O'
                score = label.get('score', None)
                # Check if this label means toxic for backward compat
                if assigned_label.lower() != 'o':
                    is_toxic = assigned_label.lower() in valid_label_names
            else:
                # Legacy: token has 'is_toxic' boolean
                label_is_toxic = _coerce_bool(label.get('is_toxic', False))
                is_toxic = label_is_toxic
                score = label.get('score', None)
                assigned_label = 'toxic' if is_toxic else None

        token_annotations.append({
            'text': token['text'],
            'position': idx,
            'start_offset': token['start'],
            'end_offset': token['end'],
            'is_toxic': is_toxic,
            'toxicity_score': score,
            'assigned_label': assigned_label,
        })

    return token_annotations


def get_comment_label_name(annotation_result: Dict) -> Optional[str]:
    """
    Extract comment-level label name from annotation result.
    Returns the label name string, or None if not meaningful.
    """
    cl = annotation_result.get('comment_label')
    if cl is None:
        return None
    if cl == 'O':
        return None
    return cl


# Keep backward compatibility
annotate_comment = annotate_comment
tokenize_vietnamese = tokenize_text
