"""
Export service for generating datasets in various formats.
Uses effective labels (manual_label takes priority over ai_label).
"""
import csv
import io
import json
import logging
import xml.etree.ElementTree as ET
from xml.dom import minidom
from django.http import HttpResponse
from django.utils import timezone
from .models import Project, YouTubeLink, Comment, Token, ExportRecord
from django.db.models import Count, Q
logger = logging.getLogger(__name__)


def _get_comments(project, youtube_link, filter_toxicity):
    """Get comments based on filters."""
    if youtube_link:
        comments = youtube_link.comments
    else:
        link_ids = project.youtubelinks.all().values_list('id', flat=True)
        comments = Comment.objects.filter(youtube_link_id__in=link_ids)
    if filter_toxicity and filter_toxicity!='all':
        comments = comments.filter(Q(ai_label__label__name=filter_toxicity)|Q(manual_label__label__name=filter_toxicity))
    comments = comments.prefetch_related(
        'tokens',
        'tokens__ai_label',
        'tokens__ai_label__label',
        'tokens__manual_label',
        'tokens__manual_label__label',
        'ai_label',
        'ai_label__label',
        'manual_label',
        'manual_label__label',
    )
    return comments


def _get_comment_label(comment):
    """Get effective comment label name (manual > ai)."""
    eff = comment.effective_label
    if eff:
        return eff.display_name
    return comment.toxicity_label or 'unknown'


def _get_token_label(token_dict):
    """Get effective token label name from token display dict."""
    eff = token_dict.get('effective_label')
    if eff:
        return eff.get('name', 'O')
    return 'B_TOXIC' if token_dict.get('is_toxic') else 'O'


def export_json_sentence(project, youtube_link, filter_toxicity):
    """Export as JSON - sentence level annotation (uses effective label: manual > ai)."""
    comments = _get_comments(project, youtube_link, filter_toxicity)
    data = []

    for comment in comments:
        label_info = _get_comment_label(comment)
        entry = {
            'id': str(comment.youtube_comment_id),
            'text': comment.text,
            'label': label_info,
            'ai_label': comment.ai_label_data['name'] if comment.ai_label_data else None,
            'manual_label': comment.manual_label_data['name'] if comment.manual_label_data else None,
            'is_meaningful': comment.is_meaningful,
            'author': comment.author,
            'like_count': comment.like_count,
        }
        data.append(entry)

    return json.dumps(data, ensure_ascii=False, indent=2)


def export_json_token(project, youtube_link, filter_toxicity):
    """Export as JSON - token level annotation (uses effective label: manual > ai)."""
    comments = _get_comments(project, youtube_link, filter_toxicity)
    data = []

    for comment in comments:
        tokens = comment.display_tokens
        token_list = []
        for token in tokens:
            token_list.append({
                'text': token['text'],
                'label': _get_token_label(token),
                'ai_label': token.get('ai_label', {}).get('name') if token.get('ai_label') else None,
                'manual_label': token.get('manual_label', {}).get('name') if token.get('manual_label') else None,
                'position': token['position'],
                'start_offset': token['start_offset'],
                'end_offset': token['end_offset'],
            })

        entry = {
            'id': str(comment.youtube_comment_id),
            'text': comment.text,
            'label': _get_comment_label(comment),
            'ai_label': comment.ai_label_data['name'] if comment.ai_label_data else None,
            'manual_label': comment.manual_label_data['name'] if comment.manual_label_data else None,
            'is_meaningful': comment.is_meaningful,
            'tokens': token_list,
        }
        data.append(entry)

    return json.dumps(data, ensure_ascii=False, indent=2)


def export_json_llm(project, youtube_link, filter_toxicity):
    """Export as JSON for LLM training (instruction format)."""
    comments = _get_comments(project, youtube_link, filter_toxicity)
    data = []

    for comment in comments:
        tokens = comment.display_tokens
        labeled_words = [t['text'] for t in tokens if _get_token_label(t) != 'O']

        entry = {
            'instruction': 'Xác định các từ được gán nhãn trong comment tiếng Việt sau. Trả về danh sách các từ được tìm thấy.',
            'input': comment.text,
            'output': json.dumps({
                'is_meaningful': comment.is_meaningful,
                'label': _get_comment_label(comment),
                'labeled_words': labeled_words,
                'confidence': comment.toxicity_confidence,
            }, ensure_ascii=False),
        }
        data.append(entry)

    return json.dumps(data, ensure_ascii=False, indent=2)


def export_xml_conll(project, youtube_link, filter_toxicity):
    """Export as XML in CoNLL-like format (uses effective label: manual > ai)."""
    comments = _get_comments(project, youtube_link, filter_toxicity)

    root = ET.Element('corpus')
    root.set('name', str(project.name))
    root.set('exported', timezone.now().isoformat())

    for comment in comments:
        sentence_elem = ET.SubElement(root, 'sentence')
        sentence_elem.set('id', str(comment.youtube_comment_id))
        sentence_elem.set('label', _get_comment_label(comment))
        sentence_elem.set('is_meaningful', 'true' if comment.is_meaningful else 'false')

        tokens_elem = ET.SubElement(sentence_elem, 'tokens')
        for token in comment.display_tokens:
            token_elem = ET.SubElement(tokens_elem, 'token')
            token_elem.set('text', token['text'])
            token_elem.set('label', _get_token_label(token))
            token_elem.set('position', str(token['position']))

    # Pretty print XML
    xml_str = minidom.parseString(ET.tostring(root, encoding='unicode')).toprettyxml(indent="  ")
    # Remove XML declaration
    lines = xml_str.split('\n')
    return '\n'.join(lines[1:])


def export_csv_sentence(project, youtube_link, filter_toxicity):
    """Export as CSV - sentence level (uses effective label: manual > ai)."""
    comments = _get_comments(project, youtube_link, filter_toxicity)
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(['id', 'text', 'label', 'ai_label', 'manual_label', 'is_meaningful', 'author', 'like_count'])
    for comment in comments:
        writer.writerow([
            comment.youtube_comment_id,
            comment.text,
            _get_comment_label(comment),
            comment.ai_label_data['name'] if comment.ai_label_data else '',
            comment.manual_label_data['name'] if comment.manual_label_data else '',
            'true' if comment.is_meaningful else 'false',
            comment.author,
            comment.like_count,
        ])

    return output.getvalue()


def export_csv_token(project, youtube_link, filter_toxicity):
    """Export as CSV - token level (uses effective label: manual > ai)."""
    comments = _get_comments(project, youtube_link, filter_toxicity)
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        'comment_id', 'text', 'comment_label', 'comment_ai_label', 'comment_manual_label',
        'is_meaningful', 'token_text', 'token_label', 'token_ai_label', 'token_manual_label',
        'position', 'start_offset', 'end_offset'
    ])
    for comment in comments:
        for token in comment.display_tokens:
            writer.writerow([
                comment.youtube_comment_id,
                comment.text,
                _get_comment_label(comment),
                comment.ai_label_data['name'] if comment.ai_label_data else '',
                comment.manual_label_data['name'] if comment.manual_label_data else '',
                'true' if comment.is_meaningful else 'false',
                token['text'],
                _get_token_label(token),
                token.get('ai_label', {}).get('name') if token.get('ai_label') else '',
                token.get('manual_label', {}).get('name') if token.get('manual_label') else '',
                token['position'],
                token['start_offset'],
                token['end_offset'],
            ])

    return output.getvalue()


def generate_export(project, youtube_link, export_format, filter_toxicity='all'):
    """Generate export file and return as HTTP response."""
    exporters = {
        'json_sentence': (export_json_sentence, 'application/json', '.json'),
        'json_token': (export_json_token, 'application/json', '.json'),
        'json_llm': (export_json_llm, 'application/json', '.json'),
        'xml_conll': (export_xml_conll, 'application/xml', '.xml'),
        'csv_sentence': (export_csv_sentence, 'text/csv', '.csv'),
        'csv_token': (export_csv_token, 'text/csv', '.csv'),
    }

    if export_format not in exporters:
        return HttpResponse('Invalid export format', status=400)

    export_func, content_type, extension = exporters[export_format]
    content = export_func(project, youtube_link, filter_toxicity)

    # Record export
    ExportRecord.objects.create(
        project=project,
        youtube_link=youtube_link,
        export_format=export_format,
        filter_toxicity=filter_toxicity,
        comment_count=len(content.split('\n')) if extension == '.csv' else 0,
        file_size=f"{len(content)} bytes",
    )

    filename = f"{project.name}_{export_format}_{timezone.now().strftime('%Y%m%d_%H%M%S')}{extension}"
    response = HttpResponse(content, content_type=content_type)
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response