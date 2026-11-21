import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mixharness.commands import control_server
from mixharness.engine import open_engine


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_cli_and_running_engine_share_the_same_session(self):
        scratch = ROOT / ".work" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            home = root / ".mixharness"
            async def invoke(*args):
                command = [sys.executable, "-m", "mixharness.cli", "--library", str(root), *args]
                result = await asyncio.to_thread(subprocess.run, command, cwd=ROOT, capture_output=True, check=True)
                return json.loads(result.stdout)
            async def exercise():
                with open_engine(root, home) as engine:
                    async with control_server(engine):
                        project = await invoke("project-create", "命令行项目", "--folder", str(ROOT / "packaging"))
                        session = await invoke("new", "--backend", "api", "--project-id", project["id"],
                                               "--gemini-effort", "medium", "--openai-effort", "high")
                        self.assertEqual(session["efforts"], {"gemini": "medium", "openai": "high"})
                        sid = session["id"]
                        first = await invoke("send", sid, "检查桌面程序的发行方式。", "--no-wait", "--request-id", sid)
                        again = await invoke("send", sid, "检查桌面程序的发行方式。", "--no-wait", "--request-id", sid)
                        self.assertEqual(first, again)
                        self.assertEqual(engine.store.session(sid)["revision"], 1)
                        self.assertEqual((await invoke("history", sid))["events"][0]["body"], "检查桌面程序的发行方式。")
                        self.assertEqual((await invoke("chats"))["data"][0]["source"], "mixharness")
                        await invoke("send", sid, "降低工程任务的思考强度。", "--no-wait", "--openai-effort", "low")
                        self.assertEqual(engine.store.session(sid)["efforts"], {"gemini": "medium", "openai": "low"})
                        flags = (await invoke("connections"))["providers"]
                        self.assertEqual({p: s["configured"] for p, s in flags.items()}, {p: bool(k) for p, k in engine.keys.items()})
                self.assertFalse((home / "control.json").exists())
                self.assertEqual((await invoke("status", sid))["session"]["id"], sid)
            asyncio.run(exercise())
