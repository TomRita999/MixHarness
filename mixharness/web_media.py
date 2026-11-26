import asyncio
import os
import re
from contextlib import ExitStack
from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from .attachments import inspect_video, video_mime
from .chat_snapshot import markdown
from .video_links import MEDIA_MARKDOWN, local_video, video_link_tokens


router = APIRouter(prefix='/api/sessions/{sid}')
CHUNK_SIZE = 256 * 1024
BYTE_RANGE = re.compile(r'bytes=([0-9]{0,20})-([0-9]{0,20})', re.I)


@lru_cache(maxsize=128)
def video_document(text, workspace, prefix, filename=''):
    env = {}
    tokens, links = video_link_tokens(text, workspace, filename, env=env)
    for index, mentions in enumerate(links.values()):
        for token in mentions:
            token.attrSet('href', prefix + str(index))
    html = markdown(text) if tokens is None else MEDIA_MARKDOWN.renderer.render(tokens, MEDIA_MARKDOWN.options, env)
    return html, tuple(links)


def document_media(sid, kind, identifier, text, workspace, filename=''):
    prefix = f'/api/sessions/{sid}/{kind}/{identifier}/videos/'
    html, paths = video_document(text, workspace, prefix, filename)
    videos = [{'name': path.name, 'mime': video_mime(path), 'url': prefix + str(index)}
              for index, path in enumerate(paths)]
    return html, videos


def media_response(request, name, mime, size, etag, chunks, *, download=False):
    disposition = 'attachment' if download else 'inline'
    headers = {'Accept-Ranges': 'bytes', 'ETag': etag, 'Cache-Control': 'private, no-store',
               'X-Content-Type-Options': 'nosniff',
               'Content-Disposition': disposition + "; filename*=UTF-8''" + quote(name, safe='')}
    value = request.headers.get('range')
    start, stop, status = 0, size, 200
    if value and ',' not in value and request.headers.get('if-range', etag) == etag:
        match = BYTE_RANGE.fullmatch(value.strip())
        if not match or not any(match.groups()):
            raise HTTPException(416, headers={'Content-Range': f'bytes */{size}'})
        first, last = match.groups()
        if first:
            start = int(first)
            stop = min(size, int(last) + 1) if last else size
        else:
            start = max(0, size - int(last))
        if start >= stop:
            raise HTTPException(416, headers={'Content-Range': f'bytes */{size}'})
        headers['Content-Range'] = f'bytes {start}-{stop - 1}/{size}'
        status = 206
    headers['Content-Length'] = str(stop - start)
    if request.method == 'HEAD':
        return Response(status_code=status, media_type=mime, headers=headers)
    return StreamingResponse(chunks(start, stop), status_code=status, media_type=mime, headers=headers)


@router.api_route('/attachments/{identifier}', methods=['GET', 'HEAD'])
async def saved_attachment(request: Request, sid: str, identifier: str):
    library = request.app.state.engine.store.inputs
    item = library.media_for_session(sid, identifier)

    async def chunks(start, stop):
        while start < stop:
            # 每次读取后结束 SQLite 查询，慢速播放器不会占用数据库事务。
            data = library.data_range(identifier, start, min(CHUNK_SIZE, stop - start))
            if not data:
                raise OSError('附件读取提前结束')
            start += len(data)
            yield data

    return media_response(request, item['name'], item['mime'], item['size'], f'"{identifier}"', chunks)


def permitted_path(engine, session, candidate):
    path = candidate.resolve(strict=True)
    roots = list(engine.sources(session).folders.values())
    if path.is_relative_to(engine.home):
        roots = [root for root in roots if root.is_relative_to(engine.home)]
        roots.extend(engine.home / area / session['id'] for area in ('runs', 'snapshots'))
    if not any(path.is_relative_to(root) for root in roots) or not path.is_file():
        raise KeyError('会话中没有此视频文件')
    return path


@router.api_route('/{kind}/{identifier}/videos/{index}', methods=['GET', 'HEAD'])
async def linked_video(request: Request, sid: str, kind: Literal['messages', 'artifacts'], identifier: str, index: int,
                       download: bool = False):
    engine = request.app.state.engine
    store = engine.store
    session = store.session(sid)
    if kind == 'messages':
        text = store.one("SELECT body FROM events WHERE sid=? AND seq=? AND kind='message' AND actor IN ('openai','gemini')",
                         sid, identifier)['body']
        filename = ''
    else:
        asset = store.one('SELECT name,content FROM artifacts WHERE sid=? AND id=?', sid, identifier)
        text, filename = asset['content'], asset['name']
    prefix = f'/api/sessions/{sid}/{kind}/{identifier}/videos/'
    _, paths = video_document(text, session['workspace'], prefix, filename)
    if not 0 <= index < len(paths):
        raise KeyError('会话中没有此视频链接')
    candidate = paths[index]
    with ExitStack() as cleanup:
        try:
            path = permitted_path(engine, session, candidate)
            source = cleanup.enter_context(path.open('rb'))
            info = os.fstat(source.fileno())
            if not os.path.samestat(info, permitted_path(engine, session, candidate).stat()):
                raise KeyError('视频文件已更换')
        except (OSError, RuntimeError) as exc:
            raise KeyError('无法打开会话引用的视频文件') from exc
        try:
            mime = await asyncio.to_thread(inspect_video, candidate.name, source)
        except ValueError as exc:
            raise HTTPException(415, str(exc)) from exc

        async def chunks(start, stop):
            try:
                source.seek(start)
                remaining = stop - start
                while remaining:
                    data = await asyncio.to_thread(source.read, min(CHUNK_SIZE, remaining))
                    if not data:
                        raise OSError('视频文件在播放期间被截短')
                    remaining -= len(data)
                    yield data
            finally:
                source.close()

        etag = f'"{info.st_dev:x}-{info.st_ino:x}-{info.st_mtime_ns:x}-{info.st_size:x}"'
        response = media_response(request, candidate.name, mime, info.st_size, etag, chunks, download=download)
        if request.method != 'HEAD':
            response.background = BackgroundTask(source.close)
            cleanup.pop_all()
        return response
