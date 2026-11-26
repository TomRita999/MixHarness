import ctypes
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.qt_app import requires_isolated_desktop


ROOT = Path(__file__).resolve().parents[1]


def read_window(work, library):
    from PySide6.QtCore import QPoint, QSettings, Qt
    from PySide6.QtGui import QAccessible
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from mixharness.desktop import Window
    from mixharness.engine import open_engine

    app = QApplication([])
    QAccessible.setActive(True)
    native = ctypes.CDLL(str(library))
    contains = native.windowContainsText
    contains.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    contains.restype = ctypes.c_int
    with open_engine(work, work / "data") as engine:
        window = Window(engine, QSettings(str(work / "ui.ini"), QSettings.Format.IniFormat))
        window.show()
        def read(text):
            app.processEvents()
            assert contains(int(window.winId()), text.encode()), text
        def send(title, body):
            window.editor.setPlainText(title + "\n" + body)
            window.send()
            read(body)
            return window.sid
        assert QTest.qWaitForWindowExposed(window)
        if window.unified_titlebar:
            native.windowHasSingleTitle.argtypes = [ctypes.c_void_p]
            assert native.windowHasSingleTitle(int(window.winId()))
            area = window.screen().availableGeometry()
            window.resize(800, 520)
            window.move(area.left() + 12, area.top() + 12)
            window.raise_()
            window.activateWindow()
            QTest.qWait(100)
            before = window.pos()
            point = window.title.mapTo(window, window.title.rect().center())
            delta = QPoint(46, 32)
            native.postWindowMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
            native.postWindowMouseEvent.restype = None
            # AppKit 先分发这组窗口坐标，再由 Qt 处理移动；松开的位置和拖动终点一致。
            for phase, location in enumerate((point, point + delta, point + delta)):
                native.postWindowMouseEvent(int(window.winId()), location.x(), location.y(), phase)
            QTest.qWait(160)
            assert window.pos() == before + delta, (before, window.pos())
        first = send("会话 A", "第一条消息的正文。")
        window.new_standalone()
        send("会话 B", "第二条消息的正文。")
        window.select_session(first)
        window.toggle_inspector()
        window.tasks.setCurrentRow(0)
        window.history.collapseAll()
        read("第一条消息的正文。")
        window.history.expandAll()
        window.refresh(force=True)
        read("第一条消息的正文。")
        window.activateWindow()
        # 从后台启动测试时也要让 Qt 把键盘事件交给当前窗口。
        app.setActiveWindow(window)
        window.editor.setFocus()
        assert window.editor.hasFocus()
        QTest.keyClicks(window.editor, "/")
        assert window.slash_menu.isVisible(), (window.editor.toPlainText(), window.editor.hasFocus())
        read("快捷指令")
        QTest.keyClick(window.editor, Qt.Key.Key_Down)
        QTest.keyClick(window.editor, Qt.Key.Key_Down)
        QTest.keyClicks(window.editor, "rev")
        read("代码审查")
        QTest.keyClick(window.editor, Qt.Key.Key_Escape)
        read("第一条消息的正文。")
        window.close()


@unittest.skipUnless(sys.platform == "darwin", "需要 macOS AppKit")
@requires_isolated_desktop
class MacWindowTests(unittest.TestCase):
    def test_window_attachment_can_read_chats_and_selections(self):
        scratch = ROOT / ".work/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            work = Path(folder)
            library = work / "window.dylib"
            env = dict(os.environ, TMPDIR=str(work), TEMP=str(work), TMP=str(work),
                       CLANG_MODULE_CACHE_PATH=str(work / "module-cache"))
            subprocess.run(["clang", "-fobjc-arc", "-dynamiclib", Path(__file__).with_name("macos_window.m"),
                            "-framework", "AppKit", "-o", library], cwd=work, env=env, check=True, timeout=60)
            subprocess.run([sys.executable, "-m", "tests.test_macos_window", work, library], cwd=ROOT,
                           env=dict(env, QT_QPA_PLATFORM="cocoa"), check=True, timeout=30)


if __name__ == "__main__":
    read_window(Path(sys.argv[1]), Path(sys.argv[2]))
