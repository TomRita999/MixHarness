import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

from websockets.sync.client import connect

from mixharness.catalog import MODELS
from mixharness.engine import open_engine
from mixharness.store import new_id
from mixharness.workspace import artifact
from tests.test_http import request, service
from tests.test_video_media import ROOT, recording


def chrome_binary():
    for name in ('google-chrome', 'chromium', 'chromium-browser', 'msedge'):
        executable = shutil.which(name)
        if executable:
            return executable
    paths = [Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')]
    for key in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        if key in os.environ:
            paths.extend(Path(os.environ[key]) / suffix for suffix in (
                'Google/Chrome/Application/chrome.exe', 'Microsoft/Edge/Application/msedge.exe'))
    return next((str(path) for path in paths if path.is_file()), None)


class BrowserPage:
    def __init__(self, channel):
        self.channel = channel
        self.serial = 0

    def call(self, method, **params):
        self.serial += 1
        self.channel.send(json.dumps({'id': self.serial, 'method': method, 'params': params}))
        deadline = time.monotonic() + 12
        while True:
            reply = json.loads(self.channel.recv(timeout=max(0, deadline - time.monotonic())))
            if reply.get('id') == self.serial:
                if 'error' in reply:
                    raise AssertionError(reply['error'])
                return reply['result']

    def evaluate(self, expression):
        result = self.call('Runtime.evaluate', expression=expression, returnByValue=True, awaitPromise=True, userGesture=True)
        if 'exceptionDetails' in result:
            raise AssertionError(result['exceptionDetails'])
        return result['result'].get('value')

    def wait_for(self, expression):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self.evaluate(expression):
                return
            time.sleep(0.05)
        raise AssertionError('等待页面状态超时 ' + expression)


@contextmanager
def browser(executable, root, url):
    # Linux 上 Chrome 的 SingletonSocket 受 108 字节 Unix Socket 路径上限约束，使用短系统临时目录避免路径超长。
    with tempfile.TemporaryDirectory(prefix='mh-chr-', ignore_cleanup_errors=True) as short_tmp:
        short_root = Path(short_tmp)
        profile = short_root / 'p'
        profile.mkdir()
        log = root / 'browser.log'
        env = dict(os.environ, TMPDIR=str(short_root), TEMP=str(short_root), TMP=str(short_root))
        args = [executable, '--headless', '--no-first-run', '--no-default-browser-check',
                '--disable-background-networking', '--disable-extensions', '--disable-sync',
                '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                '--user-data-dir=' + str(profile)]
        if sys.platform.startswith('linux'):
            args.append('--no-sandbox')
        args.append(url)
        with log.open('wb') as output:
            process = subprocess.Popen(args, env=env, cwd=root,
                                       stdout=output, stderr=subprocess.STDOUT)
            try:
                active = profile / 'DevToolsActivePort'
                deadline = time.monotonic() + 15
                while not active.is_file() or not active.stat().st_size:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise AssertionError(log.read_text(errors='replace')[-2000:])
                    time.sleep(0.05)
                port = int(active.read_text().splitlines()[0])
                with urlopen(f'http://127.0.0.1:{port}/json/list', timeout=5) as response:
                    page = next(target for target in json.load(response) if target['type'] == 'page')
                with connect(page['webSocketDebuggerUrl'], open_timeout=8, close_timeout=3) as channel:
                    yield BrowserPage(channel)
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


class BrowserVideoTests(unittest.TestCase):
    def test_upload_play_and_open_message_and_artifact_videos(self):
        executable = chrome_binary()
        if not executable:
            self.skipTest('浏览器播放测试需要已安装的 Chromium、Chrome 或 Edge')
        scratch = ROOT / '.work' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=scratch, prefix='web-video-')))
        library = root / 'library'
        project = root / 'project'
        library.mkdir()
        project.mkdir()
        path = project / '界面演示.webm'
        path.write_bytes(recording('.webm'))
        with open_engine(library, library / '.mixharness') as engine:
            models = {provider: values[0] for provider, values in MODELS.items()}
            sid = engine.create_session(**models, directory=project)['id']
            engine.submit(sid, '请查看本地视频。', new_id())
            job = engine.store.one('SELECT * FROM jobs WHERE sid=?', sid)
            with engine.store.db:
                engine.store.event(sid, 'message', 'openai', f'[播放演示](<{path.name}>)')
                artifact(engine.store, job, '录像说明.md', f'[播放演示]({path.as_uri()})', {})
        with service(library) as api, browser(executable, root, api.removesuffix('/api') + '/') as page:
            page.wait_for("document.querySelector('article.openai video source')?.type === 'video/webm'")
            document = page.call('DOM.getDocument')['root']['nodeId']
            picker = page.call('DOM.querySelector', nodeId=document, selector='#video-files')['nodeId']
            page.call('DOM.setFileInputFiles', nodeId=picker, files=[str(path)])
            page.wait_for("!document.querySelector('#send').disabled")
            page.evaluate("document.querySelector('#composer').requestSubmit()")
            page.wait_for("document.querySelector('article.user video source')?.type === 'video/webm'")
            page.evaluate("window.checkedVideo = document.querySelector('article.user video'); checkedVideo.muted = true; checkedVideo.play()")
            page.wait_for('checkedVideo.currentTime > 0 && checkedVideo.videoWidth === 64')
            position = page.evaluate('checkedVideo.pause(); checkedVideo.currentTime')
            request(api + '/sessions/' + sid + '/messages', 'POST', {'client_id': new_id(), 'text': '继续检查播放位置'})
            page.wait_for("document.querySelector('#content').textContent.includes('继续检查播放位置')")
            self.assertTrue(page.evaluate("checkedVideo === document.querySelector('article.user video')"))
            self.assertAlmostEqual(page.evaluate('checkedVideo.currentTime'), position, places=2)
            page.evaluate("document.querySelector('#open-inspector').click(); document.querySelector('#tab-files').click(); document.querySelector('#panel-content .row-link').click()")
            page.wait_for("document.querySelector('#dialog[open] video source')?.type === 'video/webm'")
            page.evaluate("const video = document.querySelector('#dialog video'); video.muted = true; video.play()")
            page.wait_for("document.querySelector('#dialog video').videoWidth === 64")
            page.evaluate("document.querySelector('#close-dialog').click()")
            page.wait_for("!document.querySelector('#dialog-body video')")
