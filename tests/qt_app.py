import os
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication


ISOLATED_DESKTOP = os.environ.get("MIXHARNESS_TEST_ISOLATED_DESKTOP") == "1"
requires_isolated_desktop = unittest.skipUnless(
    ISOLATED_DESKTOP,
    "此测试会打开或激活真实窗口，需要在专用测试桌面设置 MIXHARNESS_TEST_ISOLATED_DESKTOP=1",
)


def application():
    platform = os.environ.get("QT_QPA_PLATFORM", "offscreen") if ISOLATED_DESKTOP else "offscreen"
    os.environ["QT_QPA_PLATFORM"] = platform
    if os.name == "nt" and platform.partition(":")[0] == "offscreen":
        # Windows 的 Qt 离屏插件通过 FreeType 读取这个目录中的字体。
        os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ["SystemRoot"]) / "Fonts"))
    return QApplication.instance() or QApplication(["MixHarness"])
