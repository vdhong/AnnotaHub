"""
YouTube Comment Fetching Service
Uses YouTube Data API v3 to fetch comments from videos.
"""
import logging
import re
import time

from django.conf import settings
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .. import task_messages

logger = logging.getLogger(__name__)


def _get_youtube_api_key(api_key=None):
    """
    Resolve YouTube API key, prioritizing the explicitly provided key,
    then falling back to global settings.
    """
    if api_key and api_key.strip():
        return api_key.strip()
    return settings.YOUTUBE_API_KEY


def extract_video_id(url: str) -> str | None:
    """Extract video ID from various YouTube URL formats."""
    patterns = [
        r'(?:youtube\.com/watch\?v=)([a-zA-Z0-9_-]{11})',
        r'(?:youtube\.com/shorts/)([a-zA-Z0-9_-]{11})',
        r'(?:youtube\.com/embed/)([a-zA-Z0-9_-]{11})',
        r'(?:youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'(?:youtube\.com/v/)([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    if re.match(r'^[a-zA-Z0-9_-]{11}$', url):
        return url
    return None


def get_video_info(video_id: str, api_key=None) -> dict | None:
    """Get video metadata and statistics from YouTube API."""
    resolved_key = _get_youtube_api_key(api_key)
    if not resolved_key:
        raise Exception("YOUTUBE_API_KEY not configured")

    try:
        youtube = build('youtube', 'v3', developerKey=resolved_key)
        request = youtube.videos().list(
            part='snippet,statistics',
            id=video_id
        )
        response = request.execute()

        if response.get('items'):
            item = response['items'][0]

            # Khởi tạo an toàn đề phòng trường hợp object bị rỗng
            snippet = item.get('snippet', {})
            statistics = item.get('statistics', {})

            return {
                # Thông tin Metadata
                'title': snippet.get('title', ''),
                'channel': snippet.get('channelTitle', ''),
                'thumbnail': snippet.get('thumbnails', {}).get('high', {}).get('url', ''),
                'published_at': snippet.get('publishedAt'),

                # Thông tin Statistics (Đã ép kiểu về Integer và xử lý an toàn)
                # Dùng default=0 nếu key không tồn tại, sau đó ép kiểu int()
                'view_count': int(statistics.get('viewCount', 0)),
                'like_count': int(statistics.get('likeCount', 0)),
                'comment_count': int(statistics.get('commentCount', 0)),
                'favorite_count': int(statistics.get('favoriteCount', 0))
            }

    except HttpError as e:
        logger.error(f"YouTube API error getting video info for {video_id}: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error getting video info for {video_id}: {e}")
        raise
    return None


def _get_total_comment_count(video_id: str, api_key=None) -> int:
    """
    Get the total number of comments for a YouTube video.

    Makes a single API call with maxResults=1 to retrieve pageInfo.totalResults.

    Returns:
        Total comment count, or -1 if unable to determine.
    """
    resolved_key = _get_youtube_api_key(api_key)
    if not resolved_key:
        return -1

    try:
        youtube = build('youtube', 'v3', developerKey=resolved_key)
        request = youtube.commentThreads().list(
            part='snippet',
            videoId=video_id,
            maxResults=1,
            fields='pageInfo,nextPageToken'
        )
        response = request.execute()
        total = response.get('pageInfo', {}).get('totalResults', -1)
        logger.info(f"Total comment count for {video_id}: {total}")
        return total
    except HttpError as e:
        logger.error(f"YouTube API error getting comment count: {e}")
        return -1
    except Exception as e:
        logger.error(f"Unexpected error getting comment count: {e}")
        return -1


def fetch_comments(
    video_id: str,
    max_results: int = None,
    on_progress: callable = None,
    api_key: str = None,
    should_stop: callable = None,
    include_replies: bool = True,
) -> list[dict]:
    """
    Fetch comments from a YouTube video.

    Args:
        video_id: YouTube video ID
        max_results: Optional hard limit for comments. If None, fetch all pages until exhausted.
        on_progress: Callback function(progress_percent, current_step, total, processed)
        api_key: Optional YouTube API key (defaults to global settings)

    Returns:
        List of comment dictionaries
    """
    resolved_key = _get_youtube_api_key(api_key)
    if not resolved_key:
        raise Exception("YOUTUBE_API_KEY not configured")

    all_comments = []
    try:
        youtube = build('youtube', 'v3', developerKey=resolved_key)
        # videoinfo = get_video_info(video_id)
        # If no explicit limit is provided, keep paging until the API is exhausted.
        expected_total = None
        if max_results is None:
            max_results = float('inf')
            # if max_results != float('inf'):
            #     expected_total = max_results
        elif max_results > 0:
            expected_total = max_results

        total_fetched = 0
        page_token = None

        while True:
            per_page = 100

            request = youtube.commentThreads().list(
                part='snippet,replies',
                videoId=video_id,
                maxResults=per_page,
                textFormat="plainText",
                pageToken=page_token
            )
            response = request.execute()

            items = response.get('items', [])
            if not items:
                break

            for item in items:
                for comment_data in _extract_thread_data(item, include_replies):
                    all_comments.append(comment_data)
                    total_fetched += 1

            if on_progress:
                # expected_total từ statistics.commentCount đã bao gồm cả reply,
                # nên chỉ dùng làm mốc khi ta cũng lấy reply; ngược lại tiến độ
                # sẽ không bao giờ chạm 100%.
                progress_total = (
                    expected_total
                    if include_replies and expected_total and expected_total > 0
                    else total_fetched
                )
                progress = min(100, int((total_fetched / max(progress_total, 1)) * 100))
                on_progress(
                    progress,
                    task_messages.FETCHING,
                    max(progress_total, total_fetched),
                    total_fetched,
                )

            # Dừng sớm khi người dùng yêu cầu huỷ.
            if should_stop is not None and should_stop():
                logger.info("Dừng tải bình luận cho %s theo yêu cầu huỷ.", video_id)
                break

            page_token = response.get('nextPageToken')
            if not page_token:
                break

            if max_results != float('inf') and total_fetched >= max_results:
                break

            # Nghỉ giữa các trang, không phải giữa từng bình luận.
            time.sleep(0.5)

    except HttpError as e:
        logger.error(f"YouTube API error fetching comments: {e}")
        if on_progress:
            on_progress(0, f"API Error: {str(e)}", 0, 0)
        raise
    except Exception as e:
        logger.error(f"Unexpected error fetching comments: {e}")
        if on_progress:
            on_progress(0, f"Error: {str(e)}", 0, 0)
        raise

    logger.info(f"Fetched {len(all_comments)} comments for video {video_id}")
    return all_comments


def _extract_thread_data(item: dict, include_replies: bool = True) -> list[dict]:
    """
    Trích một luồng bình luận: bình luận gốc + (tuỳ chọn) các trả lời.

    Phải đọc cả phần replies. Câu trả lời thường chiếm 20-40% dữ liệu và là nơi
    tập trung nhiều nội dung độc hại nhất, bỏ qua là mất mảng quan trọng nhất.
    """
    rows: list[dict] = []

    snippet = item.get('snippet', {})
    top_level = snippet.get('topLevelComment', {})
    top_snippet = top_level.get('snippet', {})

    parent = _row_from_comment(
        top_level.get('id', ''), top_snippet,
        is_public=snippet.get('isPublic', True), parent_id=''
    )
    if parent:
        rows.append(parent)

    if include_replies:
        for reply in (item.get('replies', {}) or {}).get('comments', []) or []:
            row = _row_from_comment(
                reply.get('id', ''), reply.get('snippet', {}) or {},
                is_public=True, parent_id=top_level.get('id', '')
            )
            if row:
                rows.append(row)

    return rows


def _row_from_comment(comment_id: str, snippet: dict, *, is_public=True,
                      parent_id='') -> dict | None:
    """Chuẩn hoá một bình luận đơn lẻ thành dict để lưu DB."""
    if not comment_id:
        return None
    try:
        # textOriginal là văn bản thô do người dùng gõ; textDisplay có HTML
        # entity và thẻ <br>, không phù hợp làm dữ liệu huấn luyện.
        original = snippet.get('textOriginal') or ''
        display = snippet.get('textDisplay') or ''
        return {
            'youtube_comment_id': comment_id,
            'author': snippet.get('authorDisplayName', ''),
            'author_channel_url': snippet.get('authorChannelUrl', ''),
            'avatar_url': snippet.get('authorProfileImageUrl', ''),
            'text': original or display,
            'text_original': original or display,
            'like_count': snippet.get('likeCount', 0),
            'published_at': snippet.get('publishedAt'),
            'updated_at': snippet.get('updatedAt'),
            'is_public': is_public,
            'parent_id': parent_id,
        }
    except Exception as exc:
        logger.error('Lỗi trích xuất bình luận %s: %s', comment_id, exc)
        return None


def _extract_comment_data(item: dict) -> dict | None:
    """Giữ cho tương thích ngược: chỉ trả bình luận gốc."""
    rows = _extract_thread_data(item, include_replies=False)
    return rows[0] if rows else None
