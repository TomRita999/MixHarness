import asyncio
import json
import os
import shutil
import sys
from pathlib import Path


class ClientError(RuntimeError):
    pass


def executable(client):
    command = "codex" if client == "codex" else "agy"
    suffix = ".exe" if os.name == "nt" else ""
    home = Path.home()
    override = os.environ.get("MIXHARNESS_" + client.upper())
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise ClientError(f"指定的 {command} 程序不存在")
        return str(path)
    candidates = [shutil.which(command)]
    if client == "antigravity":
        candidates.append(home / ".gemini" / "bin" / (command + suffix))
    elif sys.platform == "darwin":
        for base in (Path("/Applications"), home / "Applications"):
            for app in ("ChatGPT", "Codex"):
                root = base / (app + ".app") / "Contents/Resources"
                candidates.extend([root / "codex-cli/CodexCLI.app/Contents/MacOS/codex", root / "codex"])
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        for name in ("Codex", "ChatGPT"):
            candidates.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / name / "resources" / "codex.exe")
    candidates.append(home / ".local" / "bin" / (command + suffix))
    for path in candidates:
        if path and Path(path).is_file() and os.access(path, os.X_OK):
            return str(path)
    raise ClientError(f"找不到 {command}。请安装对应的客户端命令行工具。")


class Rpc:
    def __init__(self, argv, cwd=None):
        self.argv, self.cwd = argv, cwd
        self.counter = 0
        self.handle_request = None

    async def __aenter__(self):
        self.process = await asyncio.create_subprocess_exec(
            *self.argv, cwd=self.cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=64 * 1024 * 1024,
            creationflags=0x08000000 if os.name == "nt" else 0)
        return self

    async def __aexit__(self, *exc):
        self.process.stdin.close()
        if self.process.returncode is None:
            try:
                await asyncio.wait_for(self.process.wait(), 1.5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
        await self.process.stdin.drain()

    async def read(self):
        line = await self.process.stdout.readline()
        if not line:
            raise ClientError(f"{Path(self.argv[0]).name} 已关闭连接")
        return json.loads(line)

    async def dispatch(self, message, notify=None):
        method = message.get("method")
        if "id" in message and method:
            try:
                reply = await self.handle_request(method, message.get("params", {})) if self.handle_request else None
            except asyncio.CancelledError:
                if method == "session/request_permission":
                    answer = {"result": {"outcome": {"outcome": "cancelled"}}}
                else:
                    answer = {"error": {"code": -32800, "message": "任务已取消"}}
                await self.send({"jsonrpc": "2.0", "id": message["id"], **answer})
                raise
            if reply is not None:
                answer = {"result": reply}
            elif method == "session/request_permission":
                answer = {"result": {"outcome": {"outcome": "cancelled"}}}
            else:
                answer = {"error": {"code": -32601, "message": "MixHarness 未提供此操作"}}
            await self.send({"jsonrpc": "2.0", "id": message["id"], **answer})
        elif method and notify:
            notify(method, message.get("params", {}))

    async def request(self, method, params, notify=None):
        self.counter += 1
        number = self.counter
        await self.send({"jsonrpc": "2.0", "id": number, "method": method, "params": params})
        return await self.response(number, notify)

    async def response(self, number, notify=None):
        while True:
            message = await self.read()
            if "method" not in message and message.get("id") == number:
                if "error" in message:
                    raise ClientError(message["error"].get("message") or str(message["error"].get("code")))
                return message["result"]
            await self.dispatch(message, notify)

    async def initialize(self, client, *, experimental=False):
        if client == "codex":
            result = await self.request("initialize", {"clientInfo": {"name": "mixharness", "version": "0.7.0"},
                                                       "capabilities": {"experimentalApi": experimental}})
            await self.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            return result
        return await self.request("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                                                 "clientInfo": {"name": "MixHarness", "version": "0.7.0"}})
