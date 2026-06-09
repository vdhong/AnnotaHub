from unittest.mock import MagicMock, patch
import json

from django.http import StreamingHttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase

from comments.models import Comment, Project, YouTubeLink, Token
from comments.tasks import cancel_tasks_for_link_now
from comments.tasks import clear_link_data_for_refetch
from comments.views import _serialize_task_progress, health_check, progress_event_stream
from comments.api_views import ContinueAnnotationView, RetryFetchView, ClearAndRefetchView


class ProgressHelpersTests(SimpleTestCase):
    def test_serialize_task_progress_falls_back_to_pending(self):
        data = _serialize_task_progress(None, 'fetch')
        self.assertEqual(data['status'], 'pending')
        self.assertEqual(data['progress'], 0)

    @patch('comments.views.connection')
    @patch('comments.views.redis.Redis.from_url')
    def test_health_check_reports_ok_when_dependencies_respond(self, mock_redis_from_url, mock_connection):
        cursor = mock_connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (1,)
        mock_redis_from_url.return_value.ping.return_value = True

        response = health_check(RequestFactory().get('/health/'))

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(payload['components']['database']['status'], 'ok')
        self.assertEqual(payload['components']['redis']['status'], 'ok')

    @patch('annotahub.celery.app.control.revoke')
    @patch('comments.tasks.YouTubeLink.objects.get')
    @patch('comments.tasks.TaskProgress.objects.filter')
    def test_cancel_tasks_for_link_now_marks_tasks_cancelled(self, mock_filter, mock_get, mock_revoke):
        task = MagicMock()
        task.task_id = 'abc123'
        task.current_step = 'Working'
        task.status = 'running'
        task.save = MagicMock()
        mock_filter.return_value = [task]

        link = MagicMock()
        link.comments.exists.return_value = True
        link.comments.filter.return_value.exclude.return_value.exists.return_value = False
        link.status = 'fetching'
        link.save = MagicMock()
        mock_get.return_value = link

        result = cancel_tasks_for_link_now('link-id')

        mock_revoke.assert_called_once_with('abc123', terminate=True)
        task.save.assert_called_once()
        link.save.assert_called_once()
        self.assertEqual(result['cancelled'], 1)
        self.assertEqual(result['status'], 'annotated')

    @patch('comments.views.close_old_connections')
    @patch('comments.views.time.sleep')
    @patch('comments.views.get_object_or_404')
    @patch('comments.views.TaskProgress.objects.filter')
    def test_progress_stream_returns_streaming_response(self, mock_filter, mock_get, mock_sleep, mock_close):
        link = MagicMock()
        link.status = 'annotated'
        mock_get.return_value = link

        progress = MagicMock()
        progress.status = 'completed'
        progress.progress_percent = 100
        progress.current_step = 'Done'
        progress.total_items = 10
        progress.processed_items = 10

        def filter_side_effect(*args, **kwargs):
            queryset = MagicMock()
            if kwargs.get('status') == 'running':
                queryset.exists.return_value = False
            else:
                queryset.order_by.return_value.first.return_value = progress
            return queryset

        mock_filter.side_effect = filter_side_effect

        response = progress_event_stream(RequestFactory().get('/sse/progress/link-id/'), 'link-id')

        self.assertIsInstance(response, StreamingHttpResponse)

    @patch('comments.api_views.get_effective_task_progress')
    @patch('comments.api_views.get_object_or_404')
    def test_continue_annotation_returns_running_snapshot_instead_of_error(self, mock_get_object, mock_get_progress):
        link = MagicMock()
        link.comments.filter.return_value.count.return_value = 5
        mock_get_object.return_value = link

        running_task = MagicMock()
        running_task.task_type = 'annotating'
        running_task.status = 'running'
        running_task.get_status_display.return_value = 'Running'
        running_task.progress_percent = 42
        running_task.current_step = 'Annotating comments'
        running_task.total_items = 10
        running_task.processed_items = 4
        mock_get_progress.return_value = running_task

        response = ContinueAnnotationView.as_view()(RequestFactory().post('/api/links/link-id/continue-annotate/'), link_id='link-id')

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertTrue(payload['success'])
        self.assertTrue(payload['already_running'])
        self.assertEqual(payload['task']['status'], 'running')
        self.assertEqual(payload['task']['progress'], 42)

    def test_was_translated_prefers_model_response_source_flag(self):
        translated = Comment(
            text='Xin chào',
            original_text='Hello',
            model_response={'source_is_vietnamese': False},
        )
        self.assertTrue(translated.was_translated)

        native = Comment(
            text='Xin chào',
            original_text='Hello',
            model_response={'source_is_vietnamese': True},
        )
        self.assertFalse(native.was_translated)


class CommentTokenFallbackTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='Project A')
        self.link = YouTubeLink.objects.create(
            project=self.project,
            video_id='abc123',
            url='https://www.youtube.com/watch?v=abc123',
            title='Video A',
        )

    def test_display_tokens_falls_back_to_text_when_no_tokens_exist(self):
        comment = Comment.objects.create(
            youtube_link=self.link,
            youtube_comment_id='c1',
            text='hello toxic world',
        )

        tokens = comment.display_tokens

        self.assertEqual([t['text'] for t in tokens], ['hello', 'toxic', 'world'])
        self.assertTrue(all(t['is_toxic'] is False for t in tokens))

    def test_get_or_create_token_for_position_creates_missing_token(self):
        comment = Comment.objects.create(
            youtube_link=self.link,
            youtube_comment_id='c2',
            text='hello toxic world',
        )

        token = comment.get_or_create_token_for_position(1)

        self.assertIsNotNone(token)
        self.assertEqual(token.text, 'toxic')
        self.assertEqual(comment.tokens.count(), 3)

    def test_display_tokens_merges_partial_db_tokens_with_text(self):
        comment = Comment.objects.create(
            youtube_link=self.link,
            youtube_comment_id='c3',
            text='hello, toxic world!',
        )
        Token.objects.create(
            comment=comment,
            text='toxic',
            position=2,
            start_offset=7,
            end_offset=12,
            is_toxic=True,
            annotation_source='manual',
        )

        tokens = comment.display_tokens

        self.assertEqual([t['text'] for t in tokens], ['hello', ',', 'toxic', 'world', '!'])
        self.assertTrue(tokens[2]['is_toxic'])
        self.assertEqual(len(tokens), 5)

    @patch('comments.tasks.TaskProgress.objects.filter')
    @patch('comments.tasks.cancel_tasks_for_link_now')
    @patch('comments.tasks.YouTubeLink.objects.get')
    def test_clear_link_data_for_refetch_resets_link_state(self, mock_get_link, mock_cancel, mock_task_filter):
        link = MagicMock()
        link.comments.count.return_value = 7
        link.comments.all.return_value.delete.return_value = (7, {})
        link.save = MagicMock()
        link.status = 'annotated'
        mock_get_link.return_value = link
        mock_cancel.return_value = {'cancelled': 1, 'status': 'annotated'}

        task_qs = MagicMock()
        mock_task_filter.return_value = task_qs

        result = clear_link_data_for_refetch('link-id')

        self.assertEqual(result['status'], 'reset')
        self.assertEqual(result['deleted_comments'], 7)
        mock_cancel.assert_called_once_with('link-id')
        link.comments.all.return_value.delete.assert_called_once()
        task_qs.delete.assert_called_once()
        link.save.assert_called_once()

    @patch('comments.api_views.enqueue_fetch_comments_task')
    @patch('comments.api_views.cancel_tasks_for_link_now')
    @patch('comments.api_views.get_object_or_404')
    def test_retry_fetch_view_keeps_existing_comments(self, mock_get_object, mock_cancel, mock_enqueue):
        link = MagicMock()
        link.id = 'link-id'
        link.title = 'Test video'
        link.video_id = 'abc123'
        link.save = MagicMock()
        mock_get_object.return_value = link

        response = RetryFetchView.as_view()(RequestFactory().post('/api/links/link-id/retry-fetch/'), link_id='link-id')

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertTrue(payload['success'])
        mock_cancel.assert_called_once_with('link-id')
        mock_enqueue.assert_called_once()
        self.assertEqual(link.save.call_count, 1)

    @patch('comments.api_views.enqueue_fetch_comments_task')
    @patch('comments.api_views.clear_link_data_for_refetch')
    @patch('comments.api_views.get_object_or_404')
    def test_clear_and_refetch_view_clears_existing_comments(self, mock_get_object, mock_clear, mock_enqueue):
        link = MagicMock()
        link.id = 'link-id'
        link.title = 'Test video'
        link.video_id = 'abc123'
        mock_get_object.return_value = link
        mock_clear.return_value = {'status': 'reset', 'deleted_comments': 9, 'cancelled_tasks': 1}

        response = ClearAndRefetchView.as_view()(RequestFactory().post('/api/links/link-id/clear-refetch/'), link_id='link-id')

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertTrue(payload['success'])
        self.assertEqual(payload['cleared_comments'], 9)
        mock_clear.assert_called_once_with('link-id')
        mock_enqueue.assert_called_once()
