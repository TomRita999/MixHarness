import os
from pathlib import Path

from PySide6.QtWidgets import QApplication


def application():
    platform = os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if os.name == "nt" and platform.partition(":")[0] == "offscreen":
        # Windows 的 Qt 离屏插件通过 FreeType 读取这个目录中的字体。
        os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ["SystemRoot"]) / "Fonts"))
    return QApplication.instance() or QApplication(["MixHarness"])
