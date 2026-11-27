import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QSettings

from mixharness.attachments import Attachment
from mixharness.catalog import MODELS
from mixharness.cli import parser
from mixharness.commands import control_server, remote_command
from mixharness.desktop import Window
from mixharness.desktop_windows import WindowPicker, session_for_share
from mixharness.engine import open_engine
from tests.qt_app import application


ROOT = Path(__file__).resolve().parents[1]


class WindowEntryTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.work' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=scratch, prefix='window-entry-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_module_selects_window_or_desktop_with_leading_data_dir(self):
        data = self.root / 'data'
        library = self.root / 'library'
        cases = [
            (['--data-dir', str(data), '--library', str(library), 'window', 'bind'], '--control'),
            ([f'--data-dir={data}', f'--library={library}', 'window', 'bind'], '--control'),
            (['--data-dir', str(data), 'window', 'bind'], '--control'),
            (['--data-dir', str(data), '--project', str(library)], '启动 MixHarness 桌面应用'),
        ]
        utf8_env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        for args, expected in cases:
            with self.subTest(args=args):
                help_output = subprocess.run([sys.executable, '-m', 'mixharness', *args, '--help'], cwd=ROOT,
                                             env=utf8_env, capture_output=True, text=True, encoding='utf-8',
                                             timeout=30, check=True)
                self.assertIn(expected, help_output.stdout)
        self.assertFalse(data.exists())
        self.assertFalse(library.exists())

    def test_cli_window_commands_stay_in_selected_library(self):
        async def exercise():
            first, second = self.root / 'first', self.root / 'second'
            data_one, data_two = first / 'data', second / 'data'
            with open_engine(first, data_one) as one, open_engine(second, data_two) as two:
                models = {provider: names[0] for provider, names in MODELS.items()}
                sid = one.create_session(**models, standalone=True)['id']
                other = two.create_session(**models, standalone=True)['id']
                cli = parser()
                self.assertEqual(cli.parse_args(['window', 'bind', sid, 'target']).access, 'view')
                self.assertEqual(cli.parse_args(['window', 'bind', sid, 'target', '--control']).access, 'control')
                async with control_server(one), control_server(two):
                    port_one = json.loads((data_one / 'control.json').read_text())['port']
                    port_two = json.loads((data_two / 'control.json').read_text())['port']
                    self.assertNotEqual(port_one, port_two)
                    utf8_env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
                    prefix = [sys.executable, '-m', 'mixharness', '--library', str(first), '--data-dir', str(data_one), 'window']
                    for action, receipt in [('status', {'id': sid, 'share': None}), ('stop', {'id': sid, 'stopped': False})]:
                        completed = await asyncio.to_thread(subprocess.run, [*prefix, action, sid], cwd=ROOT,
                                                            env=utf8_env, capture_output=True, text=True, encoding='utf-8',
                                                            check=True, timeout=30)
                        self.assertEqual(json.loads(completed.stdout), receipt)
                    rejected = await asyncio.to_thread(subprocess.run, [*prefix, 'status', other], cwd=ROOT,
                                                        env=utf8_env, capture_output=True, text=True, encoding='utf-8', timeout=30)
                    self.assertNotEqual(rejected.returncode, 0)
                    connected, result = await remote_command(data_two, {'command': 'window', 'window_action': 'status', 'id': other})
                    self.assertTrue(connected)
                    self.assertEqual(result, {'id': other, 'share': None})
                    self.assertTrue((data_one / 'service.lock').samefile(one.home / 'service.lock'))
                    self.assertFalse((data_two / 'service.lock').samefile(data_one / 'service.lock'))
            self.assertFalse((first / '.mixharness').exists())
            self.assertFalse((second / '.mixharness').exists())
            unused = self.root / 'offline-data'
            status = subprocess.run([sys.executable, '-m', 'mixharness.cli', '--library', str(first), '--data-dir',
                                     str(unused), 'window', 'status', sid], cwd=ROOT, env=utf8_env,
                                    capture_output=True, text=True, encoding='utf-8', check=True, timeout=30)
            self.assertIsNone(json.loads(status.stdout)['share'])
            self.assertFalse(unused.exists())
        asyncio.run(exercise())

    def test_desktop_picker_defaults_and_first_message_draft(self):
        app = application()
        settings = QSettings(str(self.root / 'desktop.ini'), QSettings.Format.IniFormat)
        settings.setValue('backend', 'api')
        with open_engine(self.root, self.root / 'desktop-data') as engine:
            host = Window(engine, settings)
            try:
                host.inputs.menu()
                menu = app.activePopupWidget()
                self.assertIn('与 AI 共享窗口…', [action.text() for action in menu.actions()])
                menu.close()
                dialog = WindowPicker(host)
                self.assertEqual(dialog.access.currentData(), 'view')
                self.assertFalse(dialog.confirm.isEnabled())
                dialog.reject()
                self.assertIsNone(host.sid)
                original = host.draft_key()
                picture = Attachment.from_file(ROOT / 'mixharness' / 'resources' / 'app.png')
                host.editor.setPlainText('请说明共享窗口的内容。')
                host.inputs.stage([picture])
                sid = session_for_share(host)
                self.assertEqual(host.sid, sid)
                self.assertEqual(host.editor.toPlainText(), '请说明共享窗口的内容。')
                self.assertEqual([item['id'] for item in host.inputs.items], [picture.id])
                self.assertEqual(engine.store.inputs.metadata(original), [])
                self.assertIsNone(engine.windows.status(sid))
                self.assertEqual(session_for_share(host), sid)
            finally:
                host.close()
        self.assertFalse((self.root / '.mixharness').exists())
