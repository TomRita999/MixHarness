import hashlib
import os
import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    runtime = root / ".venv"
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    work = root / ".work"
    scratch = work / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch),
               PIP_CACHE_DIR=str(work / "cache"), PIP_DISABLE_PIP_VERSION_CHECK="1")
    if not python.is_file():
        if sys.version_info < (3, 11):
            raise SystemExit("首次启动请使用 Python 3.11 或更高版本。")
        subprocess.run([sys.executable, "-m", "venv", str(runtime)], check=True, env=env)
    signature = hashlib.sha256((root / "pyproject.toml").read_bytes()).hexdigest()
    installed = runtime / ".mixharness-dependencies"
    if not installed.exists() or installed.read_text(encoding="ascii") != signature:
        subprocess.run([str(python), "-m", "pip", "install", "-e", str(root)], check=True, env=env)
        installed.write_text(signature, encoding="ascii")
    args = sys.argv[1:]
    if args[:1] in (["cli"], ["--cli"]):
        return subprocess.call([str(python), "-m", "mixharness.cli", "--library", str(root), *args[1:]], cwd=root, env=env)
    return subprocess.call([str(python), "-m", "mixharness", "--project", str(root), *args], cwd=root, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
