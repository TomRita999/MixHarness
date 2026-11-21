import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def check_window(executable, cli, work, env):
    home = work / "sessions"
    database = home / "sessions.sqlite3"
    with (work / "launch.log").open("ab") as log:
        process = subprocess.Popen([executable, "--project", work, "--data-dir", home],
                                   cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError((work / "launch.log").read_text(encoding="utf-8", errors="replace"))
                if (home / "control.json").is_file() and (home / "desktop.ini").is_file() and database.is_file():
                    with closing(sqlite3.connect(database)) as db:
                        if db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0:
                            result = subprocess.run([cli, "--library", work, "--data-dir", home, "project-create", "发行包检查"],
                                                    env=env, capture_output=True, check=True, timeout=20)
                            pid = json.loads(result.stdout)["id"]
                            if not db.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
                                raise RuntimeError("桌面端与命令行未打开同一个项目库")
                            chat = [cli, "--library", work, "--data-dir", home, "chat", "--project-id", pid, "--backend", "api"]
                            lines = "/new\n/context 16000\n请检查项目的启动方式。\n/context\n/exit\n"
                            terminal = subprocess.run(chat, input=lines, text=True, encoding="utf-8", env=env,
                                                      capture_output=True, check=True, timeout=25)
                            if "共享输入约" not in terminal.stdout or terminal.stderr:
                                raise RuntimeError(terminal.stderr or terminal.stdout)
                            return
                time.sleep(0.1)
            raise RuntimeError("桌面应用未能打开独立会话库")
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)


def main():
    scratch = ROOT / ".work" / "tests"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as temp:
        work = Path(temp)
        env = dict(os.environ, TMP=str(work), TEMP=str(work), TMPDIR=str(work))
        mac, windows = sys.platform == "darwin", os.name == "nt"
        system = "macOS" if mac else "Windows" if windows else "Ubuntu"
        name = f"MixHarness-{system}-{platform.machine()}" + (".zip" if mac or windows else ".tar.gz")
        archive = ROOT / "dist" / name
        if mac:
            subprocess.run(["ditto", "-x", "-k", archive, work], check=True, env=env)
            executable = work / "MixHarness.app/Contents/MacOS/MixHarness"
            subprocess.run(["codesign", "--verify", "--deep", "--strict", executable.parents[2]], check=True, env=env)
        else:
            shutil.unpack_archive(archive, work)
            executable = work / "MixHarness" / ("MixHarness.exe" if windows else "MixHarness")
        for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "MIXHARNESS_WORKSPACE"):
            env.pop(key, None)
        cli = executable.with_name("mixharness-cli.exe" if windows else "mixharness-cli")
        check_window(executable, cli, work, env)
        run = work / "check"
        run.mkdir()
        subprocess.run([executable, "--check-worker", run, ROOT / "mixharness", "30", "", ".",
                        "@python", "-m", "compileall", "-f", "."], cwd=work, env=env, check=True, timeout=45)
        result = json.loads((run / "result.json").read_text(encoding="utf-8"))
        if result["exit_code"] != 0 or "desktop.py" not in result["output"] or not list(run.rglob("desktop.*.pyc")):
            raise RuntimeError(result)
        print("桌面应用与命令行已在同一个会话库创建项目。内置 Python 成功编译项目源文件并保存验证日志。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
