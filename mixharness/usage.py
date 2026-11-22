import asyncio
import json
import os
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .client_rpc import ClientError, executable
from .clients import connection


API_DASHBOARDS = {"gemini": "https://aistudio.google.com/usage", "openai": "https://platform.openai.com/usage"}
CODEX_BILLING = "https://chatgpt.com/#settings/Billing"


@asynccontextmanager
async def usage_terminal(args, cwd, env):
    if os.name == "nt":
        from winpty import PtyProcess
        process = PtyProcess.spawn(args, cwd=str(cwd), env=env, dimensions=(32, 120))
        try:
            yield process.isalive
        finally:
            process.close(force=True)
        return
    import fcntl
    import pty
    import termios
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
        process = await asyncio.create_subprocess_exec(*args, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                                                       start_new_session=True)
        try:
            os.set_blocking(master, False)
            def alive():
                try:
                    while os.read(master, 65536):
                        pass
                except BlockingIOError:
                    pass
                return process.returncode is None
            yield alive
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
    finally:
        os.close(master)
        os.close(slave)


async def antigravity_usage(home):
    if getattr(sys, "frozen", False):
        cli = Path(sys.executable).with_name("mixharness-cli.exe" if os.name == "nt" else "mixharness-cli")
        callback = [str(cli), "--quota-statusline"]
    else:
        callback = [sys.executable, str(Path(__file__).with_name("quota_statusline.py").resolve())]
    command = subprocess.list2cmdline(callback) if os.name == "nt" else shlex.join(callback)
    settings = {"statusLine": {"type": "command", "command": command, "enabled": True}}
    args = [executable("antigravity"), "--settings", json.dumps(settings)]
    with tempfile.TemporaryDirectory(prefix="quota-", dir=Path(home).resolve()) as temp:
        target = Path(temp) / "status.json"
        env = dict(os.environ, MIXHARNESS_USAGE_FILE=str(target), TERM="xterm-256color")
        async with usage_terminal(args, temp, env) as alive:
            async with asyncio.timeout(30):
                while alive():
                    if target.is_file():
                        data = json.loads(target.read_text(encoding="utf-8"))
                        if data.get("quota"):
                            break
                        if data["agent_state"] == "error":
                            raise ClientError("Antigravity 无法读取账户额度，请检查客户端登录状态")
                    await asyncio.sleep(0.15)
                else:
                    raise ClientError("Antigravity 已退出，请检查客户端登录状态")
    names = {"gemini-5h": "Gemini · 5 小时", "gemini-weekly": "Gemini · 每周",
             "3p-5h": "第三方模型 · 5 小时", "3p-weekly": "第三方模型 · 每周"}
    buckets = []
    for key, quota in data["quota"].items():
        reset = quota.get("reset_time")
        if not reset and quota.get("reset_in_seconds") is not None:
            reset = (datetime.now(timezone.utc) + timedelta(seconds=quota["reset_in_seconds"])).isoformat()
        bucket = {"name": names.get(key, key), "reset": reset, "disabled": quota.get("disabled", False)}
        if quota.get("remaining_fraction") is not None:
            bucket["remaining_percent"] = round(100 * quota["remaining_fraction"], 2)
        if quota.get("remaining_amount") is not None:
            bucket["remaining_amount"] = quota["remaining_amount"]
        buckets.append(bucket)
    return {"plan": data.get("plan_tier"), "buckets": buckets}


async def codex_usage():
    async with asyncio.timeout(30), connection("codex") as rpc:
        await rpc.initialize("codex")
        snapshot = await rpc.request("account/rateLimits/read", {})
    primary = snapshot["rateLimits"]
    limits = snapshot.get("rateLimitsByLimitId") or {primary.get("limitId") or "Codex": primary}
    buckets = []
    for key, limit in limits.items():
        for field in ("primary", "secondary"):
            window = limit.get(field)
            if window is None:
                continue
            mins = window.get("windowDurationMins")
            interval = {300: "5 小时", 10080: "每周"}.get(mins)
            if not interval:
                interval = f"{mins} 分钟" if mins else {"primary": "主要额度", "secondary": "次要额度"}[field]
            reset = window.get("resetsAt")
            buckets.append({"name": (limit.get("limitName") or key) + " · " + interval,
                            "remaining_percent": 100 - window["usedPercent"],
                            "reset": datetime.fromtimestamp(reset, timezone.utc).isoformat() if reset is not None else None})
    return {"plan": primary.get("planType"), "buckets": buckets, "credits": primary.get("credits")}


async def plan_usage(source, home):
    if source in API_DASHBOARDS:
        return {"source": source, "dashboard": API_DASHBOARDS[source]}
    if source not in ("codex", "antigravity"):
        raise ValueError("未知的用量来源")
    try:
        result = await codex_usage() if source == "codex" else await antigravity_usage(home)
    except TimeoutError as exc:
        raise ClientError("读取套餐用量超时，请确认客户端已登录后刷新") from exc
    return dict(result, source=source, updated=datetime.now(timezone.utc).isoformat())
