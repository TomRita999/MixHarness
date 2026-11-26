from copy import copy
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from .attachments import VIDEO_TYPES, video_mime
from .chat_snapshot import MARKDOWN, display_markdown
from .markdown_links import link_target


def local_video(href, workspace=None):
    target = link_target(href, workspace)
    if target.startswith('file:'):
        url = urlsplit(target)
        if not url.netloc:
            path = Path(url2pathname(url.path))
            if video_mime(path):
                return path
    return None


MEDIA_MARKDOWN = copy(MARKDOWN)
MEDIA_MARKDOWN.validateLink = lambda href: MARKDOWN.validateLink(href) or local_video(href) is not None


def video_link_tokens(text, workspace, filename='', *, env=None):
    tokens = None
    links = {}
    lowered = text.lower()
    if '%' in text or any(suffix in lowered for suffix in VIDEO_TYPES):
        tokens = MEDIA_MARKDOWN.parse(display_markdown(text), env)
        for token in tokens:
            for child in token.children or ():
                if child.type == 'link_open':
                    path = local_video(child.attrGet('href'), workspace)
                    if path is not None:
                        links.setdefault(path, []).append(child)
    if filename:
        path = local_video(filename, workspace)
        if path is not None:
            links.setdefault(path, [])
    return tokens, links
