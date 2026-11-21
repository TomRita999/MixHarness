import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
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


def files(root, exclude=None):
    if not root.is_dir():
        raise FileNotFoundError(f"项目文件夹不存在：{root}")
    for folder, dirs, names in os.walk(root):
        base = Path(folder)
        dirs[:] = sorted(d for d in dirs if visible(d) and not (base / d).is_symlink()
                         and (base / d).resolve() != exclude and (base / d).resolve().is_relative_to(root))
        for name in sorted(names):
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

    def paths(self, exclude=None):
        multiple = len(self.folders) > 1
        for name, root in self.folders.items():
            for path in files(root, exclude):
                relative = path.relative_to(root)
                yield (Path(name) / relative if multiple else relative), path

    def locate(self, relative):
        parts = Path(relative)
        root = next(iter(self.folders.values()))
        if len(self.folders) > 1 and parts.parts and parts.parts[0] in self.folders:
            root = self.folders[parts.parts[0]]
            parts = Path(*parts.parts[1:])
        return root, parts.as_posix()

    def at_snapshot(self, path):
        return Sources({k: path / k if len(self.folders) > 1 else path for k in self.folders})


def fingerprint(root, copy_to=None, exclude=None):
    sources = root if isinstance(root, Sources) else Sources({"": root})
    manifest = hashlib.sha256()
    if len(sources.folders) > 1:
        manifest.update(b"project-folders\0" + pack(list(sources.folders)).encode() + b"\0")
        if copy_to is not None:
            for alias in sources.folders:
                (copy_to / alias).mkdir(exist_ok=True)
    for name, path in sources.paths(exclude):
        checksum = hashlib.sha256()
        if copy_to is not None:
            destination = copy_to / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            path = destination
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(262144), b""):
                checksum.update(block)
        manifest.update(name.as_posix().encode() + b"\0" + checksum.digest())
    return manifest.hexdigest()


def snapshot(root, parent, exclude=None):
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=parent, prefix=".mixharness-"))
    try:
        version = fingerprint(root, staging, exclude)
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


async def run_check(source, runs, argv, timeout=180, invocation=None, project=None, python=None, subdirectory=".", isolated_compile=False):
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
    command = [sys.executable, runner, str(run_dir), str(source), str(timeout), python, subdirectory, *argv]
    env = {k: v for k, v in os.environ.items() if not any(word in k.upper() for word in ("KEY", "TOKEN", "PASSWORD", "SECRET"))}
    await asyncio.to_thread(subprocess.run, command, check=True, env=env, stdin=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return json.loads(receipt.read_text(encoding="utf-8"))


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
