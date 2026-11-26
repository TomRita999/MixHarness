import base64
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PySide6.QtGui import QImage

from mixharness.attachments import Attachment, VIDEO_TYPES
from mixharness.catalog import MODELS
from mixharness.engine import open_engine
from mixharness.store import new_id
from mixharness.workspace import artifact
from tests.test_http import request, service


ROOT = Path(__file__).resolve().parents[1]
ICON = ROOT / 'mixharness/resources/app.png'


def recording(suffix):
    import av

    image = QImage(str(ICON))
    if image.isNull():
        raise ValueError('无法读取项目图标')
    fmt, codec = {'.mp4': ('mp4', 'mpeg4'), '.mov': ('mov', 'mpeg4'), '.webm': ('webm', 'libvpx')}[suffix]
    output = BytesIO()
    with av.open(output, 'w', format=fmt) as container:
        track = container.add_stream(codec, rate=8)
        track.width = track.height = 64
        track.pix_fmt = 'yuv420p'
        for index in range(8):
            edge = index * 12
            picture = image.copy(edge, edge, image.width() - 2 * edge, image.height() - 2 * edge)
            picture = picture.scaled(64, 64).convertToFormat(QImage.Format.Format_RGBA8888)
            frame = av.VideoFrame(64, 64, 'rgba')
            frame.planes[0].update(bytes(picture.constBits()))
            for packet in track.encode(frame):
                container.mux(packet)
        for packet in track.encode():
            container.mux(packet)
    return output.getvalue()


class VideoMediaTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.work' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=scratch, prefix='video-')))
        self.project = self.root / 'project'
        self.project.mkdir()
        self.home = self.root / '.mixharness'
        self.models = {key: choices[0] for key, choices in MODELS.items()}

    def test_formats_saved_after_source_deletion_and_fork(self):
        originals = {}
        with open_engine(self.root, self.home) as engine:
            sid = engine.create_session(**self.models, directory=self.project)['id']
            draft = 'drafts/' + sid
            for suffix, mime in VIDEO_TYPES.items():
                with self.subTest(suffix=suffix):
                    path = self.project / ('项目演示' + suffix.upper())
                    data = recording(suffix)
                    path.write_bytes(data)
                    item = Attachment.from_file(path)
                    self.assertEqual((item.kind, item.mime), ('video', mime))
                    engine.store.inputs.stage(draft, [item])
                    originals[item.id] = data
                    path.unlink()
            engine.submit(sid, '', new_id(), draft=draft)
        with open_engine(self.root, self.home) as engine:
            branch = engine.store.fork_session(sid, '')['id']
            engine.store.delete_session(sid)
            library = engine.store.inputs
            items = next(iter(library.for_session(branch).values()))
            self.assertEqual({item['id'] for item in items}, set(originals))
            for item, description in zip(items, library.describe(items), strict=True):
                self.assertEqual(description['字节数'], len(originals[item['id']]))
                self.assertNotIn('内容', description)
                self.assertEqual(library.media_for_session(branch, item['id'])['mime'], item['mime'])
                self.assertEqual(library.data(item['id']), originals[item['id']])
                with self.assertRaises(KeyError):
                    library.media_for_session(sid, item['id'])
            engine.store.delete_session(branch)
            self.assertEqual(engine.store.rows('SELECT id FROM input_items'), [])

    def test_rejects_wrong_container_and_non_video(self):
        mp4 = recording('.mp4')
        for name, content in [('图像.mp4', ICON.read_bytes()), ('录制.mov', mp4), ('录制.webm', mp4),
                              ('录制.mp4', mp4[:8]), ('录制.txt', mp4)]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                Attachment.video(name, content)

    def test_http_upload_message_artifact_ranges_and_session_scope(self):
        data = recording('.mp4')
        path = self.project / '项目 演示.mp4'
        path.write_bytes(data)
        outside = self.root / '其他项目.mp4'
        outside.write_bytes(data)
        with open_engine(self.root, self.home) as engine:
            sid = engine.create_session(**self.models, directory=self.project)['id']
            other = engine.create_session(**self.models, standalone=True)['id']
            engine.submit(sid, '检查项目演示。', new_id())
            job = engine.store.one('SELECT * FROM jobs WHERE sid=?', sid)
            with engine.store.db:
                seq = engine.store.event(sid, 'message', 'openai', f'[查看演示](<{path.name}>)')
                aid = artifact(engine.store, job, '演示说明.md', f'[播放]({path.as_uri()})', {})
                blocked = engine.store.event(sid, 'message', 'openai', f'[其他项目]({outside.as_uri()})')
        with service(self.root) as api:
            uri = api + '/sessions/' + sid
            origin = api.removesuffix('/api')
            message = {'client_id': new_id(), 'videos': [{'name': path.name, 'data': base64.b64encode(data).decode()}]}
            receipt = request(uri + '/messages', 'POST', message)
            self.assertEqual(request(uri + '/messages', 'POST', message), receipt)
            events = {event['seq']: event for event in request(uri)['events']}
            item, = events[receipt['seq']]['attachments']
            self.assertEqual(item['kind'], 'video')
            attachment = uri + '/attachments/' + item['id']
            linked = origin + events[seq]['videos'][0]['url']
            report = request(uri + '/artifacts/' + aid)
            self.assertIn(report['videos'][0]['url'], report['html'])
            for url in (attachment, linked, origin + report['videos'][0]['url']):
                with self.subTest(url=url):
                    with urlopen(Request(url, method='HEAD'), timeout=5) as response:
                        self.assertEqual(response.headers.get_content_type(), 'video/mp4')
                        self.assertEqual(int(response.headers['Content-Length']), len(data))
                        self.assertEqual(response.read(), b'')
                    for header, first, stop in [('bytes=0-1', 0, 2), ('bytes=-24', len(data)-24, len(data)),
                                                ('bytes=16-', 16, len(data))]:
                        with urlopen(Request(url, headers={'Range': header}), timeout=5) as response:
                            self.assertEqual(response.status, 206)
                            self.assertEqual(response.headers['Content-Range'], f'bytes {first}-{stop-1}/{len(data)}')
                            self.assertEqual(response.read(), data[first:stop])
                    with self.assertRaises(HTTPError) as error:
                        urlopen(Request(url, headers={'Range': f'bytes={len(data)}-'}), timeout=5)
                    self.assertEqual(error.exception.code, 416)
                    error.exception.close()
            for url in (attachment.replace(sid, other), linked.replace(sid, other), origin + events[blocked]['videos'][0]['url']):
                with self.assertRaises(HTTPError) as error:
                    urlopen(url, timeout=5)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
            path.unlink()
            with urlopen(attachment, timeout=5) as response:
                self.assertEqual(response.read(), data)
