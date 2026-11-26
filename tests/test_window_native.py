import tempfile
import unittest
from pathlib import Path

from mixharness.window_native import NativeWindows, supported


ROOT = Path(__file__).resolve().parents[1]


class NativeWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_permission_probe_in_an_isolated_directory(self):
        scratch = ROOT / '.work' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            home = Path(directory)
            native = NativeWindows(home)
            if supported():
                permissions = await native.request('permissions')
                self.assertIs(type(permissions['screen']), bool)
                self.assertIs(type(permissions['accessibility']), bool)
                self.assertTrue((await native.binary()).is_relative_to(home))
                if not permissions['screen']:
                    with self.assertRaisesRegex(ValueError, '屏幕录制权限'):
                        await native.request('list')
                with self.assertRaisesRegex(ValueError, '未知窗口请求'):
                    await native.request('invalid-operation')
            else:
                with self.assertRaisesRegex(ValueError, 'macOS 14.2'):
                    await native.request('permissions')
                self.assertEqual(list(home.iterdir()), [])
            self.assertFalse((home / 'service.lock').exists())
