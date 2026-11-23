import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]


def request(url, method="GET", data=None):
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode()
    req = Request(url, data=body, method=method, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=4) as response:
        if response.status == 204:
            return None
        return json.load(response)


@contextmanager
def service(project):
    with socket.socket() as socket_probe:
        socket_probe.bind(("127.0.0.1", 0))
        port = socket_probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}/api"
    env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "OPENAI_API_KEY", "MIXHARNESS_WORKSPACE")}
    with (project / "service.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, "-m", "mixharness", "--project", str(project), "--port", str(port)],
                                   cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None:
                    raise RuntimeError((project / "service.log").read_text(encoding="utf-8", errors="replace"))
                try:
                    request(url + "/config")
                    break
                except URLError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            yield url
        finally:
            process.terminate()
            process.wait(timeout=10)


class HttpTests(unittest.TestCase):
    def test_manage_project_and_remove_one_conversation(self):
        scratch = ROOT / ".work" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            root = Path(tmp)
            with service(root) as api:
                p = request(api + "/projects", "POST", {"name": "编译测试", "folders": [str(ROOT / "mixharness")]})
                ids = [request(api + "/sessions", "POST", {"project_id": p["id"]})["id"] for _ in range(2)]
                path = api + "/sessions/" + ids[0]
                request(path + "/messages", "POST", {"text": "核对验证命令。", "client_id": uuid4().hex})
                request(path, "PATCH", {"title": "验证流程", "pinned": True, "archived": True})
                self.assertEqual([r["id"] for r in request(api + "/sessions")], ids[1:])
                self.assertEqual(request(api + "/sessions?archived=true")[0]["title"], "验证流程")
                request(path, "PATCH", {"archived": False})
                request(path, "DELETE")
                with self.assertRaises(HTTPError) as missing:
                    request(path)
                self.assertEqual(missing.exception.code, 404)
            with service(root) as api:
                self.assertEqual([r["id"] for r in request(api + "/sessions")], ids[1:])
                self.assertEqual(request(api + "/projects")[0]["id"], p["id"])

    def test_session_survives_service_restart(self):
        scratch = ROOT / ".work" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            project = Path(tmp)
            with service(project) as api:
                config = request(api + "/config")
                self.assertFalse(any(config["keys"].values()))
                for provider, model in (("gemini", "gemini-4-carbon"), ("openai", "gpt-6-luna")):
                    catalog = request(api + "/models/" + provider + "/refresh", "POST")
                    self.assertEqual(catalog["source"], "reference")
                    self.assertIn(model, catalog["models"])
                root = request(api + "/projects", "POST", {"directory": str(ROOT)})
                self.assertEqual(root["id"], request(api + "/projects", "POST", {"directory": str(ROOT)})["id"])
                linked = request(api + "/sessions", "POST", {"project_id": root["id"]})
                self.assertEqual(linked["workspace"], str(ROOT))
                sid = request(api + "/sessions", "POST", {})["id"]
                path = "/sessions/" + sid
                model = config["models"]["openai"][1]
                request(api + path + "/models", "PATCH", {"provider": "openai", "model": model, "effort": "high"})
                request(api + path + "/models", "PATCH", {"provider": "gemini", "model": config["models"]["gemini"][0],
                                                          "effort": "medium"})
                with self.assertRaises(HTTPError) as invalid:
                    request(api + path + "/models", "PATCH", {"provider": "openai", "model": model, "effort": "none"})
                self.assertEqual(invalid.exception.code, 422)
                message = {"text": "请为这个项目写一份验证报告。", "client_id": uuid4().hex}
                original = request(api + path + "/messages", "POST", message)
            with service(project) as api:
                repeated = request(api + path + "/messages", "POST", message)
                self.assertEqual(repeated, original)
                request(api + path + "/messages", "POST",
                        {"text": "报告读者调整为产品经理。", "client_id": uuid4().hex})
                state = request(api + path)
                self.assertEqual(state["session"]["revision"], 2)
                self.assertEqual(state["session"]["openai"], model)
                self.assertEqual(state["session"]["efforts"], {"gemini": "medium", "openai": "high"})
                self.assertEqual(state["session"]["gemini"], config["models"]["gemini"][0])
                self.assertEqual(len([e for e in state["events"] if e["actor"] == "user"]), 2)
                jid = state["tasks"][-1]["id"]
                self.assertEqual(request(api + path + "/stop", "POST"), {"stopped": 1})
            with service(project) as api:
                self.assertEqual(request(api + path + "/continue", "POST"), {"continued": 1})
                guide = {"intent": "steer", "text": "请用中文列出验证步骤。", "client_id": uuid4().hex}
                receipt = request(api + path + "/messages", "POST", guide)
                self.assertEqual(request(api + path + "/messages", "POST", guide), receipt)
                state = request(api + path)
                self.assertEqual(state["session"]["revision"], 2)
                self.assertEqual(state["tasks"][-1]["id"], jid)
                self.assertEqual(state["tasks"][-1]["guide_seq"], receipt["seq"])
