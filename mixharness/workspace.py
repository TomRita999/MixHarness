import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import tomllib
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock

from .store import new_id, now, pack


EXCLUDED = {".git", ".venv", "venv", "__pycache__", "node_modules", ".mixharness", ".work",
            ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".ds_store", "mixharness.toml", "dist", "build"}
DEFAULT_CHECKS = {"python-tests": ["@python", "-m", "unittest", "discover", "-v"],
                  "python-compile": ["@python", "-m", "compileall", "-q", "."]}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def visible(name):
    lower = name.casefold()
    return (lower not in EXCLUDED and not lower.startswith((".env", ".mixharness-"))
            and not lower.endswith((".egg-info", ".pyc", ".pem", ".key")))


def files(root, exclude=None, *, cancelled=None):
    if not root.is_dir():
        raise FileNotFoundError(f"项目文件夹不存在：{root}")
    for folder, dirs, names in os.walk(root):
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError("已停止读取项目文件")
        base = Path(folder)
        dirs[:] = sorted(d for d in dirs if visible(d) and not (base / d).is_symlink()
                         and (base / d).resolve() != exclude and (base / d).resolve().is_relative_to(root))
        for name in sorted(names):
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("已停止读取项目文件")
            path = base / name
            if visible(name) and not path.is_symlink() and path.is_file():
                yield path


def project_path(root, relative):
    part = Path(relative)
    if not part.parts or part.is_absolute() or part.drive or ".." in part.parts or not all(map(visible, part.parts)):
        raise ValueError("请指定项目目录中的普通文件")
    target = root
    for segment in part.parts:
        target = target / segment
        if target.is_symlink():
            raise ValueError("项目文件路径包含符号链接")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("文件位于项目目录外")
    return target


def read_source(root, args):
    path = project_path(root, args["path"])
    data = path.read_bytes()
    start = args.get("start_line", 1)
    if start < 1:
        raise ValueError("行号必须从 1 开始")
    lines = data.decode("utf-8").splitlines()
    return {"path": args["path"], "sha256": digest(data), "start_line": start, "total_lines": len(lines),
            "text": "\n".join(lines[start - 1:start + 199])[:24000]}


class ReviewCommands:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.stopped = False

    def stop(self):
        with self.lock:
            self.stopped = True
            if self.process is not None and self.process.poll() is None:
                self.process.kill()

    @contextmanager
    def open(self, args):
        with self.lock:
            if self.stopped:
                raise InterruptedError("已停止读取 Git 改动")
            self.process = process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                      stderr=subprocess.DEVNULL)
        try:
            with process:
                yield process
        finally:
            with self.lock:
                self.process = None


def review_changes(sources, exclude, *, commands=None):
    """从项目的暂存区和工作区读取限长的 Git 差异。"""
    commands = commands or ReviewCommands()
    sections = []
    if not shutil.which("git"):
        return "本机未安装 Git，可按关注点审查项目文件。"
    remaining = 32000
    for name, folder in sources.folders.items():
        git = ["git", "--literal-pathspecs", "-C", str(folder)]
        with commands.open([*git, "rev-parse", "--show-toplevel"]) as process:
            top, _ = process.communicate()
        if process.returncode:
            sections.append(name + " · 普通目录，可按关注点审查项目文件。")
            continue
        root = Path(os.fsdecode(top).rstrip("\r\n"))
        with commands.open([*git, "status", "--porcelain=v1", "-z", "--no-renames", "-uall", "--", "."]) as process:
            status, _ = process.communicate()
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, process.args)
        files = []
        for entry in status.split(b"\0"):
            if not entry:
                continue
            path = root / os.fsdecode(entry[3:])
            relative = path.relative_to(folder)
            if path.is_relative_to(exclude) or not all(map(visible, relative.parts)):
                continue
            files.append((entry[:2].decode("ascii"), relative.as_posix()))
        sections.append(name + (" · Git 改动" if files else " · 工作区没有可审查的改动"))
        sections.extend(code + " " + path for code, path in files[:100])
        if len(files) > 100:
            sections.append(f"另有 {len(files) - 100} 个改动文件")
        tracked = [path for code, path in files[:100] if code != "??"]
        if not tracked:
            continue
        for label, flags in (("暂存区", ["--cached"]), ("工作区", [])):
            if remaining <= 0:
                break
            args = [*git, "diff", "--relative", "--no-ext-diff", "--no-textconv", "--no-renames", *flags, "--", *tracked]
            with commands.open(args) as process:
                data = process.stdout.read(remaining + 1)
                if len(data) > remaining:
                    process.kill()
                if data:
                    sections.extend((label, data[:remaining].decode("utf-8", errors="replace")))
                    remaining -= len(data)
        if remaining < 0:
            sections.append("差异节选已达到上限，可按路径读取快照中的文件。")
            remaining = 0
    return "\n".join(sections)


def edit_source(root, args):
    target = project_path(root, args["path"])
    data = args["content"].encode("utf-8")
    expected = args["expected_sha256"]
    current = target.read_bytes() if target.exists() else None
    if current == data:
        return {"path": args["path"], "sha256": digest(data)}
    if (digest(current) if current is not None else "") != expected:
        raise ValueError("文件已变动，请重新读取后再编辑")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".mixharness-", delete=False) as stream:
        staging = Path(stream.name)
        stream.write(data)
    try:
        if target.exists():
            shutil.copymode(target, staging)
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    return {"path": args["path"], "sha256": digest(data)}


@dataclass
class Sources:
    folders: dict[str, Path]

    def paths(self, exclude=None, *, cancelled=None):
        multiple = len(self.folders) > 1
        for name, root in self.folders.items():
            for path in files(root, exclude, cancelled=cancelled):
                relative = path.relative_to(root)
                yield (Path(name) / relative if multiple else relative), path

    def list_files(self, exclude=None, *, cancelled=None):
        return [relative.as_posix() for relative, _ in self.paths(exclude, cancelled=cancelled)]

    def locate(self, relative):
        parts = Path(relative)
        root = next(iter(self.folders.values()))
        if len(self.folders) > 1 and parts.parts and parts.parts[0] in self.folders:
            root = self.folders[parts.parts[0]]
            parts = Path(*parts.parts[1:])
        return root, parts.as_posix()

    def at_snapshot(self, path):
        return Sources({k: path / k if len(self.folders) > 1 else path for k in self.folders})


def fingerprint(root, copy_to=None, exclude=None, *, cancelled=None):
    sources = root if isinstance(root, Sources) else Sources({"": root})
    manifest = hashlib.sha256()
    if len(sources.folders) > 1:
        manifest.update(b"project-folders\0" + pack(list(sources.folders)).encode() + b"\0")
        if copy_to is not None:
            for alias in sources.folders:
                (copy_to / alias).mkdir(exist_ok=True)
    for name, path in sources.paths(exclude, cancelled=cancelled):
        checksum = hashlib.sha256()
        output = nullcontext()
        if copy_to is not None:
            destination = copy_to / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            output = destination.open("wb")
        with output as target, path.open("rb") as stream:
            for block in iter(lambda: stream.read(262144), b""):
                if cancelled is not None and cancelled.is_set():
                    raise InterruptedError("已停止读取项目文件")
                checksum.update(block)
                if target is not None:
                    target.write(block)
        if copy_to is not None:
            shutil.copystat(path, destination)
        manifest.update(name.as_posix().encode() + b"\0" + checksum.digest())
    return manifest.hexdigest()


def snapshot(root, parent, exclude=None, *, cancelled=None):
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=parent, prefix=".mixharness-"))
    try:
        version = fingerprint(root, staging, exclude, cancelled=cancelled)
        target = parent / version
        if not target.exists():
            staging.rename(target)
        return version
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def check_commands(config, inherited=None):
    commands = dict(DEFAULT_CHECKS if inherited is None else inherited)
    if config.exists():
        with config.open("rb") as stream:
            commands.update(tomllib.load(stream)["checks"])
    for name, argv in commands.items():
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise ValueError(f"验证命令 {name} 必须是非空字符串列表")
    return commands


def clear_git_context(environment):
    # Git 的命令行配置按数量、键和值成组传递，值中也可能包含凭据。
    # 仓库、索引和对象目录必须由验证副本中的 Git 命令自行确定。
    local = {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE",
             "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"}
    for key in tuple(environment):
        name = key.upper()
        if name in local or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            del environment[key]


async def run_check(source, runs, argv, timeout=None, invocation=None, project=None, python=None, subdirectory=".", isolated_compile=False):
    run_dir = runs / (invocation or new_id())
    run_dir.mkdir(parents=True, exist_ok=True)
    receipt = run_dir / "result.json"
    if receipt.exists():
        return json.loads(receipt.read_text(encoding="utf-8"))
    frozen = getattr(sys, "frozen", False)
    runner = "--check-worker" if frozen else str(Path(__file__).with_name("check_runner.py"))
    venv = (project or source) / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if isolated_compile:
        if argv != DEFAULT_CHECKS["python-compile"]:
            raise ValueError("独立语法检查需要使用内置编译命令")
        python = "@harness-compile"
    elif not python:
        python = str(venv) if venv.is_file() else ("" if frozen else sys.executable)
    command = [sys.executable, runner, str(run_dir), str(source), str(timeout or 0), python, subdirectory, *argv]
    env = {k: v for k, v in os.environ.items() if not any(word in k.upper() for word in ("KEY", "TOKEN", "PASSWORD", "SECRET"))}
    clear_git_context(env)
    launching = asyncio.create_task(asyncio.create_subprocess_exec(
        *command, env=env, stdin=subprocess.DEVNULL, start_new_session=os.name != "nt",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    try:
        process = await asyncio.shield(launching)
        code = await process.wait()
    except asyncio.CancelledError:
        process = await launching
        if process.returncode is None:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec("taskkill", "/T", "/F", "/PID", str(process.pid),
                                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                             creationflags=subprocess.CREATE_NO_WINDOW)
                if await killer.wait() and process.returncode is None:
                    raise RuntimeError("Windows 无法停止当前验证进程")
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        await process.wait()
        raise
    if code:
        raise subprocess.CalledProcessError(code, command)
    return json.loads(receipt.read_text(encoding="utf-8"))


async def run_shell_command(cwd, command):
    """在真实项目目录中运行构建、打包或测试命令，支持随时取消。"""
    env = {k: v for k, v in os.environ.items() if not any(word in k.upper() for word in ("KEY", "TOKEN", "PASSWORD", "SECRET"))}
    clear_git_context(env)
    launching = asyncio.create_task(asyncio.create_subprocess_shell(
        command, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=os.name != "nt",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    try:
        process = await asyncio.shield(launching)
        raw, _ = await process.communicate()
    except asyncio.CancelledError:
        process = await launching
        if process.returncode is None:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec("taskkill", "/T", "/F", "/PID", str(process.pid),
                                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                             creationflags=subprocess.CREATE_NO_WINDOW)
                await killer.wait()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        await process.wait()
        raise
    output = (raw or b"").decode("utf-8", errors="replace")
    return {"command": command, "cwd": str(cwd), "exit_code": process.returncode, "output": output}


def remove_session_evidence(home, sid, private_profile):
    if not sid or Path(sid).name != sid or sid in (".", "..") or private_profile.name != sid:
        raise ValueError("会话编号无效")
    runs = home / "runs" / sid
    for lock in runs.glob("*/execution.lock"):
        with FileLock(lock, timeout=0):
            pass
    for directory in (runs, home / "snapshots" / sid, private_profile):
        if directory.parent.resolve() != directory.parent:
            continue
        if directory.is_symlink():
            directory.unlink()
        elif directory.exists():
            shutil.rmtree(directory)


def artifact(store, job, name, content, meta):
    aid = new_id()
    store.db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
                     (aid, job["sid"], job["id"], name, content, digest(content.encode()), pack(meta), now()))
    return aid
