import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from mixharness.catalog import MODELS
from mixharness.cli import parser
from mixharness.clients import ClientReply, client_prompt
from mixharness.commands import control_server, remote_command
from mixharness.computer_use import ComputerDrag, ComputerScroll, ComputerTarget, ComputerWait
from mixharness.engine import open_engine
from mixharness.providers import declarations
from mixharness.store import CHAT_JOBS, new_id
from mixharness.window_native import supported


ROOT = Path(__file__).resolve().parents[1]
READS = {'computer_list', 'computer_capture', 'computer_inspect', 'computer_wait'}
INPUTS = {'computer_focus', 'computer_click', 'computer_double_click', 'computer_drag', 'computer_type',
          'computer_scroll', 'computer_key'}


class ComputerAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scratch = ROOT / '.work' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=scratch, prefix='computer-access-')))
        self.home = self.root / 'data'
        self.engine = self.enterContext(open_engine(self.root, self.home))

    def new_job(self, permission, provider='openai'):
        models = {p: choices[0] for p, choices in MODELS.items()}
        session = self.engine.create_session(**models, standalone=True, permission=permission)
        sid = session['id']
        self.engine.submit(sid, '等待界面更新。', new_id(), recipient=provider)
        job = self.engine.store.one('SELECT j.*, s.backend FROM jobs j JOIN sessions s ON s.id=j.sid '
                                    'WHERE j.sid=? AND j.kind=?', sid, CHAT_JOBS[provider])
        self.engine.set_status(job, 'running')
        self.addCleanup(self.engine.windows.discard, sid)
        return session, job

    def invoke(self, job, name, **args):
        store = self.engine.store
        with store.db:
            store.save_turn(job, new_id(), job['payload']['message_seq'], '',
                            [{'name': name, 'args': args, 'cid': new_id()}], backend=job['backend'])
        job.update(store.job(job['id']))
        call, = store.calls(job)
        return asyncio.create_task(self.engine.tool(job, call, self.engine.sources(store.session(job['sid']))))

    async def pending(self, sid, task):
        async with asyncio.timeout(3):
            while not self.engine.store.approvals(sid):
                if task.done():
                    task.result()
                    self.fail('操作在审批前已经完成')
                await asyncio.sleep(0.01)
        approval, = self.engine.store.approvals(sid)
        return approval

    def test_tool_scope_and_argument_types(self):
        def names(kind, **options):
            return {tool['name'] for tool in declarations(kind, options) if tool['name'].startswith(('computer_', 'window_'))}

        desktop = {'window_scope': 'desktop', 'window_access': 'control'}
        self.assertEqual(names('engineering', window_scope='desktop'), set())
        self.assertEqual(names('engineering', **desktop), READS | INPUTS)
        self.assertEqual(names('engineering', read_only=True, **desktop), READS)
        self.assertEqual(names('engineering', review=True, **desktop), set())
        for kind in CHAT_JOBS.values():
            self.assertEqual(names(kind, window_scope='desktop', window_access='view'), READS)
            self.assertEqual(names(kind, read_only=True, **desktop), READS | INPUTS)
        for kind in ('verification', 'report', 'side', 'proposal', 'section', 'goal_check'):
            self.assertEqual(names(kind, **desktop), set())
        share, frame = new_id(), new_id()
        with self.assertRaises(ValidationError):
            ComputerTarget(share=share, target='desktop')
        with self.assertRaises(ValidationError):
            ComputerWait(share=share, milliseconds=True)
        with self.assertRaises(ValidationError):
            ComputerWait(share=share, milliseconds=10001)
        with self.assertRaises(ValidationError):
            ComputerDrag(share=share, target='display:1', frame=frame, x=0, y=0, to_x=-1, to_y=0)
        with self.assertRaises(ValidationError):
            ComputerScroll(share=share, target='display:1', frame=frame, x=0, y=0)

    async def test_desktop_is_off_without_a_grant_at_every_permission_level(self):
        for permission in ('ask', 'auto', 'full'):
            session, job = self.new_job(permission)
            request, _, context, _ = self.engine.turn_input(job, session, [])
            self.assertNotIn('共享桌面', json.loads(context))
            self.assertFalse(declarations(job['kind'], request['payload']))
            with self.assertRaisesRegex(ValueError, '共享已停止或更换'):
                await self.invoke(job, 'computer_list', share=session['id'])
            self.assertFalse(self.engine.store.approvals(session['id']))
        self.assertFalse(self.engine.windows.native.cache.exists())

    async def test_cli_binding_is_scoped_to_the_current_session_and_library(self):
        session, job = self.new_job('full', 'gemini')
        other, other_job = self.new_job('full')
        sid = session['id']
        options = vars(parser().parse_args(['window', 'bind', sid, 'desktop']))
        if not supported():
            with self.assertRaisesRegex(ValueError, 'macOS 14.2'):
                await self.engine.windows.bind(sid, options['target'], options['access'])
            return
        async with control_server(self.engine):
            connected, receipt = await remote_command(self.home, options)
            self.assertTrue(connected)
            share = receipt['share']['share']
            self.assertEqual((receipt['share']['scope'], receipt['share']['access']), ('desktop', 'view'))
            request, _, context, _ = self.engine.turn_input(job, session, [])
            self.assertEqual(json.loads(context)['共享桌面']['share'], share)
            prompt, schema = client_prompt(request, context)
            self.assertEqual(schema, ClientReply.model_json_schema())
            advertised = json.loads(prompt.split('\n可用工具\n', 1)[1].split('\n共享会话\n', 1)[0])
            self.assertEqual({tool['name'] for tool in advertised}, READS)
            with self.assertRaisesRegex(ValueError, '共享已停止或更换'):
                await self.invoke(other_job, 'computer_wait', share=share, milliseconds=1)
            self.assertIsNone(self.engine.windows.status(other['id']))
            with self.assertRaisesRegex(ValueError, '不允许此操作'):
                await self.invoke(job, 'window_capture', share=share)
            with self.assertRaisesRegex(ValueError, '不允许此操作'):
                await self.invoke(job, 'computer_click', share=share, target='display:1', frame=new_id(), x=0, y=0)
            _, changed = await remote_command(self.home, dict(options, access='control'))
            self.assertNotEqual(changed['share']['share'], share)
            with self.assertRaisesRegex(ValueError, '共享已停止或更换'):
                await self.invoke(job, 'computer_wait', share=share, milliseconds=1)
            _, result = await remote_command(self.home, {'command': 'window', 'window_action': 'stop', 'id': sid})
            self.assertTrue(result['stopped'])
        self.assertFalse((self.root / '.mixharness').exists())
        self.assertFalse((self.home / 'control.json').exists())
        self.assertFalse(self.engine.windows.native.cache.exists())

    @unittest.skipUnless(supported(), '桌面授权需要 macOS 14.2 或更新版本')
    async def test_real_wait_approvals_and_revocation_without_desktop_input(self):
        for permission in ('ask', 'auto', 'full'):
            session, job = self.new_job(permission)
            sid = session['id']
            share = (await self.engine.windows.bind(sid, 'desktop', 'control'))['share']
            task = self.invoke(job, 'computer_wait', share=share, milliseconds=5)
            if permission == 'ask':
                approval = await self.pending(sid, task)
                self.assertEqual(json.loads(approval['detail'])['share'], share)
                self.engine.decide(sid, approval['id'], True)
            await asyncio.wait_for(task, 3)
            self.assertEqual(self.engine.store.calls(job)[0]['result']['milliseconds'], 5)
            if permission == 'ask':
                task = self.invoke(job, 'computer_list', share=share)
                approval = await self.pending(sid, task)
                self.engine.decide(sid, approval['id'], False)
                with self.assertRaisesRegex(ValueError, '已拒绝'):
                    await task
                task = self.invoke(job, 'computer_list', share=share)
                approval = await self.pending(sid, task)
                await self.engine.windows.stop(sid)
                with self.assertRaisesRegex(ValueError, '已拒绝'):
                    await task
                self.assertEqual(self.engine.store.one('SELECT decision FROM approvals WHERE id=?', approval['id'])['decision'], 'expired')
            else:
                self.assertFalse(self.engine.store.rows('SELECT id FROM approvals WHERE sid=?', sid))
                task = self.invoke(job, 'computer_wait', share=share, milliseconds=10000)
                await asyncio.sleep(0)
                await self.engine.windows.stop(sid)
                with self.assertRaisesRegex(ValueError, '共享已停止或更换'):
                    await asyncio.wait_for(task, 2)
            self.assertIsNone(self.engine.windows.status(sid))
        self.assertFalse(self.engine.windows.native.cache.exists())
