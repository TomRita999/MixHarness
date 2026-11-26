import asyncio
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from PySide6.QtGui import QImage

from mixharness.catalog import MODELS
from mixharness.engine import open_engine
from mixharness.store import CHAT_JOBS, new_id
from mixharness.window_native import supported


ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / 'README.md'


@unittest.skipUnless(supported(), '真实窗口交互需要 macOS 14.2 或更新版本')
class WindowActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        scratch = ROOT / '.work' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=scratch, prefix='window-actions-')))
        library = self.root / 'library'
        library.mkdir()
        self.engine = self.enterContext(open_engine(library, self.root / 'data'))
        bundle = self.root / 'WindowTarget.app'
        executable = bundle / 'Contents' / 'MacOS' / 'WindowTarget'
        executable.parent.mkdir(parents=True)
        with (bundle / 'Contents' / 'Info.plist').open('wb') as stream:
            plistlib.dump({'CFBundlePackageType': 'APPL', 'CFBundleExecutable': executable.name,
                          'CFBundleName': 'WindowTarget', 'CFBundleVersion': '1',
                          'CFBundleIdentifier': 'local.mixharness.tests.window-' + new_id()}, stream)
        env = dict(os.environ, TMPDIR=str(self.root), TEMP=str(self.root), TMP=str(self.root),
                   CLANG_MODULE_CACHE_PATH=str(self.root / 'module-cache'))
        await asyncio.to_thread(subprocess.run, ['/usr/bin/clang', '-fobjc-arc', '-mmacosx-version-min=14.2',
                                Path(__file__).with_name('shared_window_target.m'), '-framework', 'AppKit',
                                '-o', executable], cwd=self.root, env=env, check=True, timeout=60)
        self.permissions = await self.engine.windows.native.request('permissions')
        if not self.permissions['screen']:
            self.skipTest('系统未授予屏幕录制权限，未启动或截取测试窗口')
        # LaunchServices 提供窗口标识所需的进程启动时间。每次只启动本次目录里的测试应用。
        self.sequence = 0
        self.launcher = await asyncio.create_subprocess_exec('/usr/bin/open', '-n', '-g', '-W', str(bundle),
                                                            '--args', str(self.root), str(DOCUMENT), cwd=self.root)
        self.addAsyncCleanup(self.close_target)
        self.initial = await self.state()

    def send_command(self, action):
        self.sequence += 1
        staging = self.root / 'command.new'
        staging.write_text(json.dumps({'seq': self.sequence, 'action': action}), encoding='utf-8')
        staging.replace(self.root / 'command.json')

    async def state(self, action='inspect'):
        self.send_command(action)
        reply = self.root / 'reply.json'
        async with asyncio.timeout(12):
            while True:
                if self.launcher.returncode is not None:
                    self.fail(f'测试窗口在返回状态前退出，open 退出码为 {self.launcher.returncode}')
                if reply.is_file():
                    state = json.loads(reply.read_bytes())
                    if state['seq'] == self.sequence:
                        return state
                await asyncio.sleep(0.04)

    async def wait_state(self, predicate):
        async with asyncio.timeout(5):
            while True:
                state = await self.state()
                if predicate(state):
                    return state

    async def close_target(self):
        if self.launcher.returncode is None:
            self.send_command('quit')
            await asyncio.wait_for(self.launcher.wait(), 12)
        self.assertEqual(self.launcher.returncode, 0)

    def new_job(self, permission, provider='openai'):
        models = {name: choices[0] for name, choices in MODELS.items()}
        session = self.engine.create_session(**models, standalone=True, permission=permission)
        sid = session['id']
        self.addCleanup(self.engine.windows.discard, sid)
        self.engine.submit(sid, '读取并检查我共享的窗口。', new_id(), recipient=provider)
        job = self.engine.store.one('SELECT j.*, s.backend FROM jobs j JOIN sessions s ON s.id=j.sid '
                                    'WHERE j.sid=? AND j.kind=?', sid, CHAT_JOBS[provider])
        self.engine.set_status(job, 'running')
        return job

    async def invoke(self, job, name, args, decision=None):
        store = self.engine.store
        sid = job['sid']
        with store.db:
            store.save_turn(job, new_id(), job['payload']['message_seq'], '',
                            [{'name': name, 'args': args, 'cid': new_id()}], backend=job['backend'])
        job.update(store.job(job['id']))
        call, = store.calls(job)
        task = asyncio.create_task(self.engine.tool(job, call, self.engine.sources(store.session(sid))))
        try:
            await asyncio.sleep(0)
            pending = store.approvals(sid)
            if decision is None:
                self.assertFalse(pending)
            else:
                self.assertEqual(len(pending), 1)
                approval = pending[0]
                self.assertEqual(approval['source'], 'MixHarness')
                if decision == 'stop':
                    self.assertTrue(await self.engine.windows.stop(sid))
                    self.assertEqual(store.one('SELECT decision FROM approvals WHERE id=?', approval['id'])['decision'],
                                     'expired')
                else:
                    self.engine.decide(sid, approval['id'], decision == 'allow')
            await asyncio.wait_for(task, 25)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return store.calls(job)[0]['result']

    def coordinates(self, frame, control):
        x, y = self.initial[control]
        return {'x': int(frame['width'] * x), 'y': int(frame['height'] * y)}

    async def test_capture_approval_pixels_session_scope_and_revocation(self):
        job = self.new_job('ask')
        windows = self.engine.windows
        target = self.initial['target']
        grant = await windows.bind(job['sid'], target, 'view')
        self.assertEqual(grant['title'], self.initial['title'])
        args = {'share': grant['share']}
        with self.assertRaisesRegex(ValueError, '已拒绝'):
            await self.invoke(job, 'window_capture', args, 'deny')
        # 第二个窗口遮住第一个窗口上的绿色方块。单窗口截图应仍能读到它。
        await self.state('sibling')
        frame = await self.invoke(job, 'window_capture', args, 'allow')
        self.assertEqual(set(frame), {'share', 'frame', 'width', 'height'})
        self.assertEqual((frame['width'], frame['height']), (self.initial['width'], self.initial['height']))
        store = self.engine.store
        request, _, _, _ = self.engine.turn_input(job, store.session(job['sid']), store.calls(job))
        picture, = request['images']
        self.assertEqual(picture['mime'], 'image/jpeg')
        decoded = QImage.fromData(picture['data'])
        self.assertEqual((decoded.width(), decoded.height()), (frame['width'], frame['height']))
        point = self.coordinates(frame, 'swatch')
        color = decoded.pixelColor(point['x'], point['y'])
        self.assertGreater(color.green(), max(color.red(), color.blue()) + 45)
        self.assertEqual(store.rows('SELECT id FROM input_items'), [])
        click = dict(args, frame=frame['frame'], **self.coordinates(frame, 'button'))
        with self.assertRaisesRegex(ValueError, '不允许此操作'):
            await self.invoke(job, 'window_click', click)
        other = self.new_job('full')
        with self.assertRaisesRegex(ValueError, '共享已停止或更换'):
            await self.invoke(other, 'window_capture', args)
        with self.assertRaisesRegex(ValueError, '已拒绝'):
            await self.invoke(job, 'window_capture', args, 'stop')
        self.assertIsNone(windows.status(job['sid']))
        request, _, _, _ = self.engine.turn_input(job, store.session(job['sid']), [])
        self.assertEqual(request['images'], [])

    async def test_control_approval_click_scroll_text_shortcut_and_sibling_focus(self):
        job = self.new_job('auto')
        windows = self.engine.windows
        target = self.initial['target']
        share = (await windows.bind(job['sid'], target, 'control'))['share']
        frame = await self.invoke(job, 'window_capture', {'share': share})
        click = dict(share=share, frame=frame['frame'], **self.coordinates(frame, 'button'))
        with self.assertRaisesRegex(ValueError, '已拒绝'):
            await self.invoke(job, 'window_click', click, 'deny')
        self.assertEqual((await self.state())['clicks'], 0)
        if not self.permissions['accessibility']:
            with self.assertRaisesRegex(ValueError, '辅助功能'):
                await self.invoke(job, 'window_click', click, 'allow')
            self.assertEqual((await self.state())['clicks'], 0)
            self.skipTest('已验证系统拒绝输入，实际控件操作需要辅助功能授权')
        await self.invoke(job, 'window_click', click, 'allow')
        await self.wait_state(lambda state: state['clicks'] == 1)
        with self.assertRaisesRegex(ValueError, '画面已经失效'):
            await self.invoke(job, 'window_click', click)
        self.assertTrue(await windows.stop(job['sid']))

        job = self.new_job('full', 'gemini')
        share = (await windows.bind(job['sid'], target, 'control'))['share']
        capture_args = {'share': share}
        before = await self.state('editor')
        frame = await self.invoke(job, 'window_capture', capture_args)
        await self.invoke(job, 'window_scroll', dict(capture_args, frame=frame['frame'], direction='down',
                                                    **self.coordinates(frame, 'scrollArea')))
        await self.wait_state(lambda state: state['scrollY'] > before['scrollY'])
        frame = await self.invoke(job, 'window_capture', capture_args)
        await self.invoke(job, 'window_key', dict(capture_args, frame=frame['frame'], key='Cmd+A'))
        await self.wait_state(lambda state: state['selected'] == state['length'] and state['length'] > 0)
        frame = await self.invoke(job, 'window_capture', capture_args)
        await self.invoke(job, 'window_type', dict(capture_args, frame=frame['frame'], text=DOCUMENT.name))
        await self.wait_state(lambda state: state['text'] == DOCUMENT.name)
        frame = await self.invoke(job, 'window_capture', capture_args)
        before = await self.state('sibling')
        with self.assertRaisesRegex(ValueError, '焦点不属于共享窗口'):
            await self.invoke(job, 'window_type', dict(capture_args, frame=frame['frame'], text=Path(__file__).name))
        after = await self.state()
        self.assertEqual((after['text'], after['otherText']), (before['text'], before['otherText']))
        self.assertEqual(after['clicks'], 1)
