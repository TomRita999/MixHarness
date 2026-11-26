import asyncio
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path


RESOURCES = Path(__file__).resolve().with_name('resources')
HELPER_NAME = 'mixharness-window'


def supported():
    if sys.platform != 'darwin':
        return False
    version = platform.mac_ver()[0].split('.')
    return tuple(int(part) for part in version[:2]) >= (14, 2)


def build_window_helper(directory):
    source = (RESOURCES / 'shared_window.m').read_bytes()
    desktop = (RESOURCES / 'computer_use.h').read_bytes()
    flags = ['-x', 'objective-c', '-fobjc-arc', '-O2', '-mmacosx-version-min=14.2',
             '-framework', 'AppKit', '-framework', 'ApplicationServices', '-framework', 'ScreenCaptureKit']
    checksum = hashlib.sha256(source + b'\0' + desktop + b'\0' + repr((flags, platform.machine())).encode())
    release = Path(directory) / checksum.hexdigest()
    release.mkdir(mode=0o700, parents=True, exist_ok=True)
    executable = release / HELPER_NAME
    if executable.is_file():
        return executable
    with tempfile.TemporaryDirectory(dir=release) as scratch:
        staged = Path(scratch) / HELPER_NAME
        # 编译时使用参与摘要计算的同一份头文件。
        (staged.parent / 'computer_use.h').write_bytes(desktop)
        env = dict(os.environ, TMPDIR=scratch, TEMP=scratch, TMP=scratch, CLANG_MODULE_CACHE_PATH=scratch)
        try:
            built = subprocess.run(['/usr/bin/clang', *flags, '-iquote', scratch, '-', '-o', str(staged)], input=source,
                                   capture_output=True, env=env, cwd=scratch, timeout=90)
        except subprocess.TimeoutExpired as exc:
            raise ValueError('编译窗口辅助程序超时。') from exc
        if built.returncode:
            raise ValueError('无法编译窗口辅助程序。请检查 Xcode Command Line Tools 和 macOS SDK。\n'
                             + built.stderr.decode(errors='replace')[-2400:])
        staged.chmod(0o700)
        os.replace(staged, executable)
    return executable


class NativeWindows:
    def __init__(self, home):
        self.cache = Path(home) / 'window-helper'
        self.executable = None
        self.build_lock = asyncio.Lock()

    async def binary(self):
        if not supported():
            raise ValueError('共享窗口与桌面操作需要 macOS 14.2 或更新版本。')
        async with self.build_lock:
            if self.executable is None:
                if getattr(sys, 'frozen', False):
                    path = RESOURCES / HELPER_NAME
                    if not path.is_file():
                        raise FileNotFoundError('安装包缺少窗口辅助程序。')
                else:
                    path = await asyncio.to_thread(build_window_helper, self.cache)
                self.executable = path
        return self.executable

    async def request(self, op, **arguments):
        body = json.dumps(dict(arguments, op=op), ensure_ascii=False).encode()
        executable = await self.binary()
        process = await asyncio.create_subprocess_exec(str(executable), stdin=asyncio.subprocess.PIPE,
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            async with asyncio.timeout(20):
                output, error = await process.communicate(body)
        except TimeoutError as exc:
            raise ValueError('窗口未及时响应，请重新读取窗口。') from exc
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if process.returncode:
            raise ValueError(error.decode(errors='replace').strip() or '窗口辅助程序已退出。')
        return json.loads(output)
