import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".work" / "desktop"
DIST = ROOT / "dist"


def main():
    scratch = WORK / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(exist_ok=True)
    env = dict(os.environ, TMP=str(scratch), TEMP=str(scratch), TMPDIR=str(scratch), PYINSTALLER_CONFIG_DIR=str(WORK / "cache"))
    mac, windows = sys.platform == "darwin", sys.platform == "win32"
    if mac or windows:
        with Image.open(ROOT / "mixharness/resources/app.png") as image:
            if mac and shutil.which("iconutil"):
                iconset = WORK / "MixHarness.iconset"
                shutil.rmtree(iconset, ignore_errors=True)
                iconset.mkdir()
                for size in (16, 32, 128, 256, 512):
                    image.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
                    image.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
                subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(WORK / "MixHarness.icns")], check=True)
            else:
                image.save(WORK / ("MixHarness.icns" if mac else "MixHarness.ico"),
                           **({} if mac else {"sizes": [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]}))
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--workpath", str(WORK / "build"),
                    "--distpath", str(WORK / "output"), str(ROOT / "packaging/MixHarness.spec")], cwd=ROOT, env=env, check=True)
    system = "macOS" if mac else "Windows" if windows else "Ubuntu"
    name = DIST / f"MixHarness-{system}-{platform.machine()}"
    output = WORK / "output"
    if mac:
        with tempfile.TemporaryDirectory(dir=DIST, prefix=".mixharness-") as temporary:
            staged = Path(temporary) / "MixHarness.app"
            subprocess.run(["ditto", str(output / "MixHarness.app"), str(staged)], check=True)
            target = DIST / "MixHarness.app"
            previous = Path(temporary) / "previous.app"
            if target.exists():
                target.rename(previous)
            try:
                staged.rename(target)
            except OSError:
                if previous.exists():
                    previous.rename(target)
                raise
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(output / "MixHarness.app"),
                        str(name) + ".zip"], check=True)
    elif windows:
        shutil.make_archive(str(name), "zip", output, "MixHarness")
    else:
        with tarfile.open(str(name) + ".tar.gz", "w:gz") as archive:
            archive.add(output / "MixHarness", arcname="MixHarness")


if __name__ == "__main__":
    main()
