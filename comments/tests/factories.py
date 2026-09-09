"""Hàm dựng dữ liệu mẫu dùng chung cho test."""
from django.contrib.auth.models import User

from comments.models import Comment, Label, Project, ProjectLabel, YouTubeLink


def make_user(username, **kwargs):
    kwargs.setdefault('email', f'{username}@example.com')
    kwargs.setdefault('password', 'test-pass-12345')
    return User.objects.create_user(username=username, **kwargs)


def make_superuser(username, **kwargs):
    kwargs.setdefault('email', f'{username}@example.com')
    kwargs.setdefault('password', 'test-pass-12345')
    return User.objects.create_superuser(username=username, **kwargs)


def make_project(owner, name='Dự án thử', participants=(), **kwargs):
    project = Project.objects.create(name=name, owner=owner, **kwargs)
    for participant in participants:
        project.participants.add(participant)
    return project


def make_label(owner, name='toxic', color='#FF0000'):
    return Label.objects.create(owner=owner, name=name, color=color,
                                description=f'Mô tả cho {name}')


def make_project_label(project, label=None, **kwargs):
    label = label or make_label(project.owner)
    return ProjectLabel.objects.create(project=project, label=label, **kwargs)


def make_link(project, video_id='abc12345678', **kwargs):
    kwargs.setdefault('url', f'https://youtube.com/watch?v={video_id}')
    kwargs.setdefault('title', 'Video thử nghiệm')
    return YouTubeLink.objects.create(project=project, video_id=video_id, **kwargs)


def make_comment(link, text='đây là một bình luận thử', comment_id='c1', **kwargs):
    kwargs.setdefault('source_text', text)
    return Comment.objects.create(
        youtube_link=link, youtube_comment_id=comment_id, text=text, **kwargs
    )
