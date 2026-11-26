import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from mixharness.catalog import MODELS
from mixharness.clients import ClientReply, client_prompt
from mixharness.engine import open_engine
from mixharness.providers import declarations
from mixharness.shared_window import ClickWindow, KeyWindow, ReadWindow
from mixharness.store import CHAT_JOBS, new_id


ROOT = Path(__file__).resolve().parents[1]
WINDOW_NAMES = {'window_capture', 'window_click', 'window_type', 'window_scroll', 'window_key'}


class WindowAccessTests(unittest.IsolatedAsyncioTestCase):
    def test_engineering_and_verification_tool_declarations(self):
        def declared(kind, **payload):
            return {tool['name'] for tool in declarations(kind, payload)
                    if tool['name'].startswith(('window_', 'computer_'))}

        self.assertEqual(declared('engineering'), set())
        self.assertEqual(declared('engineering', window_access='view'), {'window_capture'})
        self.assertEqual(declared('engineering', window_access='control'), WINDOW_NAMES)
        self.assertEqual(declared('engineering', window_access='control', review=True), set())
        self.assertEqual(declared('engineering', window_access='control', read_only=True), {'window_capture'})
        for kind in ('verification', 'report', 'section', 'side', 'goal_check', 'proposal'):
            with self.subTest(kind=kind):
                self.assertEqual(declared(kind, window_access='control'), set())

    def test_direct_chat_client_schema_follows_only_the_window_grant(self):
        scratch = ROOT / '.work' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            project = Path(directory)
            with open_engine(project, project / 'data') as engine:
                models = {provider: choices[0] for provider, choices in MODELS.items()}
                for provider, kind in CHAT_JOBS.items():
                    with self.subTest(provider=provider):
                        session = engine.create_session(**models, standalone=True)
                        sid = session['id']
                        engine.submit(sid, '请说明窗口里的内容。', new_id(), recipient=provider)
                        job = engine.store.one('SELECT j.*, s.backend FROM jobs j JOIN sessions s ON s.id=j.sid '
                                               'WHERE j.sid=? AND j.kind=?', sid, kind)
                        request, _, context, _ = engine.turn_input(job, session, [])
                        self.assertTrue(request['payload']['read_only'])
                        self.assertIsNone(client_prompt(request, context)[1])
                        self.assertEqual(declarations(kind, request['payload']), [])
                        for access, expected in [('view', {'window_capture'}), ('control', WINDOW_NAMES)]:
                            payload = dict(request['payload'], window_access=access)
                            prompt, schema = client_prompt(dict(request, payload=payload), context)
                            self.assertEqual(schema, ClientReply.model_json_schema())
                            advertised = json.loads(prompt.split('\n可用工具\n', 1)[1].split('\n共享会话\n', 1)[0])
                            self.assertEqual(advertised, declarations(kind, payload))
                            self.assertEqual({tool['name'] for tool in advertised}, expected)
                            self.assertTrue(payload['read_only'])

    def test_window_arguments_reject_external_targets_and_system_shortcuts(self):
        share, frame = new_id(), new_id()
        with self.assertRaises(ValidationError):
            ReadWindow.model_validate({'share': share, 'pid': os.getpid()})
        with self.assertRaises(ValidationError):
            ClickWindow.model_validate({'share': share, 'frame': frame, 'x': -1, 'y': 0})
        with self.assertRaises(ValidationError):
            ClickWindow.model_validate({'share': share, 'frame': frame, 'x': True, 'y': 0})
        with self.assertRaises(ValidationError):
            KeyWindow.model_validate({'share': share, 'frame': frame, 'key': 'Cmd+Tab'})

    async def test_session_permission_alone_cannot_capture_a_window(self):
        scratch = ROOT / '.work' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            project = Path(directory)
            home = project / 'data'
            with open_engine(project, home) as engine:
                models = {provider: choices[0] for provider, choices in MODELS.items()}
                for permission in ('ask', 'auto', 'full'):
                    with self.subTest(permission=permission):
                        session = engine.create_session(**models, standalone=True, permission=permission)
                        sid = session['id']
                        engine.submit(sid, '读取当前会话共享的窗口', new_id(), recipient='openai')
                        job = engine.store.one('SELECT j.*, s.backend FROM jobs j JOIN sessions s ON s.id=j.sid '
                                               'WHERE j.sid=? AND j.kind=?', sid, 'gpt_chat')
                        engine.set_status(job, 'running')
                        request, _, context, _ = engine.turn_input(job, session, [])
                        self.assertNotIn('共享窗口', json.loads(context))
                        self.assertEqual(request.get('images'), [])
                        call = {'name': 'window_capture', 'cid': new_id(), 'cursor': new_id(), 'args': {'share': sid}}
                        with self.assertRaisesRegex(ValueError, '窗口共享已停止或更换'):
                            async with asyncio.timeout(2):
                                await engine.tool(job, call, engine.sources(session))
                        self.assertEqual(engine.store.approvals(sid), [])
                self.assertFalse(engine.windows.native.cache.exists())
            self.assertFalse((project / '.mixharness').exists())
