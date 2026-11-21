import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules, copy_metadata


root = Path(SPECPATH).parent
resources = root / "mixharness" / "resources"
build = root / ".work" / "desktop"
mac = sys.platform == "darwin"
windows = sys.platform == "win32"
icon = build / ("MixHarness.icns" if mac else "MixHarness.ico") if mac or windows else resources / "app.png"
files = [(str(resources), "mixharness/resources"), (str(root / "mixharness/static/LICENSE-lucide.txt"), "licenses")]
for dependency in ("google-genai", "openai", "PySide6-Essentials", "shiboken6", "qasync"):
    files += copy_metadata(dependency)

a = Analysis(
    [str(root / "packaging/launch.py")], pathex=[str(root)], datas=files,
    binaries=collect_dynamic_libs("winpty", search_patterns=["*.dll", "*.exe"]) if windows else [],
    hiddenimports=["compileall", "unittest.__main__", "websockets.asyncio.client", "tiktoken_ext.openai_public",
                   *collect_submodules("google.genai", filter=lambda name: not name.startswith("google.genai.tests")),
                   *collect_submodules("openai.resources.responses")],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6.QtWebEngineCore", "PIL", "uvicorn", "fastapi"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="MixHarness", console=False, icon=str(icon), upx=False)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="mixharness-cli", console=True, upx=False)
bundle = COLLECT(exe, cli, a.binaries, a.datas, name="MixHarness", upx=False)
if mac:
    app = BUNDLE(bundle, name="MixHarness.app", icon=str(icon), bundle_identifier="local.mixharness.desktop",
                 info_plist={"CFBundleDisplayName": "MixHarness", "CFBundleShortVersionString": "0.7.0",
                             "CFBundleVersion": "9", "NSHighResolutionCapable": True,
                             # 包内同时有命令行程序，PyInstaller 会因此标记为后台程序；桌面应用需要出现在程序坞。
                             "LSBackgroundOnly": False, "LSUIElement": False,
                             "LSApplicationCategoryType": "public.app-category.developer-tools",
                             "NSDesktopFolderUsageDescription": "读取你选择的桌面项目和已有会话记录。"})
