import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from uuid import uuid4

from filelock import FileLock


def standard_check(args):
    log_path, module, *options = args
    if module not in ("compileall", "unittest"):
        raise ValueError("该模块需要项目的 Python 环境")
    sys.argv = [module, *options]
    if module == "unittest":
        sys.path.insert(0, os.getcwd())
    with open(log_path, "w", encoding="utf-8") as log, redirect_stdout(log), redirect_stderr(log):
        runpy.run_module(module, run_name="__main__", alter_sys=True)


def check_command(argv, python, log):
    if argv[0] != "@python":
        return argv
    if python == "@harness-compile":
        if argv[1:] != ["-m", "compileall", "-q", "."]:
            raise ValueError("独立语法检查的参数无效")
        if getattr(sys, "frozen", False):
            return [sys.executable, "--standard-check", str(log), *argv[2:]]
        return [sys.executable, "-I", "-S", *argv[1:]]
    if python:
        return [python, *argv[1:]]
    if argv[1:3] in (["-m", "unittest"], ["-m", "compileall"]):
        return [sys.executable, "--standard-check", str(log), *argv[2:]]
    installed = shutil.which("python3") or shutil.which("python")
    if not installed:
        raise FileNotFoundError("此命令需要 Python，请在项目内创建 .venv 或在设置文件中填写 MIXHARNESS_PYTHON")
    return [installed, *argv[1:]]


def main(args=None):
    run, snapshot, seconds, python, subdirectory, *argv = sys.argv[1:] if args is None else args
    run_dir, source, timeout = Path(run), Path(snapshot), int(seconds)
    receipt = run_dir / "result.json"
    with FileLock(run_dir / "execution.lock"):
        if receipt.exists():
            return
        attempt = run_dir / uuid4().hex
        attempt.mkdir()
        copy = attempt / "project"
        shutil.copytree(source, copy)
        cwd = (copy / subdirectory).resolve(strict=True)
        if not cwd.is_relative_to(copy.resolve()):
            raise ValueError("验证工作目录必须属于项目快照")
        scratch = attempt / "scratch"
        scratch.mkdir()
        env = dict(os.environ, TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch), PYTHONIOENCODING="utf-8")
        log = attempt / "output.log"
        result = {"argv": argv, "log": str(log)}
        with log.open("wb") as output:
            try:
                command = check_command(argv, python, log)
                if getattr(sys, "frozen", False) and command[0] != sys.executable:
                    # PyInstaller 在启动时将这两个变量指向应用包。外部命令需要加载自己的 Qt。
                    env.pop("QT_PLUGIN_PATH", None)
                    env.pop("QML2_IMPORT_PATH", None)
                    if os.name == "nt":
                        import ctypes
                        ctypes.windll.kernel32.SetDllDirectoryW(None)
                    if "LD_LIBRARY_PATH_ORIG" in env:
                        env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
                    else:
                        env.pop("LD_LIBRARY_PATH", None)
                completed = subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                           stderr=subprocess.STDOUT, timeout=timeout, check=False,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                result["exit_code"] = completed.returncode
            except (subprocess.TimeoutExpired, OSError) as exc:
                result.update(exit_code=None, error=str(exc))
        result["bytes"] = log.stat().st_size
        with log.open("rb") as stream:
            result["output"] = stream.read(96000).decode("utf-8", errors="replace")
        # 先写完整回执，再原子发布。服务重启后按同一调用编号读取。
        staging = attempt / "result.json"
        staging.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        os.replace(staging, receipt)


if __name__ == "__main__":
    main()
