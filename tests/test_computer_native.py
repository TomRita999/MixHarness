import tempfile
import unittest
from pathlib import Path

from mixharness.window_native import NativeWindows, supported


ROOT = Path(__file__).resolve().parents[1]


class ComputerNativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_desktop_requests_require_an_explicit_target(self):
        scratch = ROOT / '.work' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch, prefix='computer-native-') as directory:
            home = Path(directory)
            native = NativeWindows(home)
            if not supported():
                with self.assertRaisesRegex(ValueError, 'macOS 14.2'):
                    await native.request('computer_list')
                self.assertEqual(list(home.iterdir()), [])
                return
            grants = await native.request('permissions')
            self.assertTrue((await native.binary()).resolve().is_relative_to(home.resolve()))
            self.assertIs(type(grants['screen']), bool)
            self.assertIs(type(grants['accessibility']), bool)
            for op in ('capture', 'inspect', 'focus', 'click', 'double_click', 'drag', 'scroll', 'type', 'key'):
                with self.subTest(operation=op), self.assertRaisesRegex(ValueError, '指定显示器或窗口编号'):
                    await native.request('computer_' + op)
            if not grants['screen']:
                with self.assertRaisesRegex(ValueError, '屏幕录制权限'):
                    await native.request('computer_list')
            self.assertFalse((home / 'service.lock').exists())
