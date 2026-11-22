import ctypes
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_window(work, library):
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtGui import QAccessible
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from mixharness.desktop import Window
    from mixharness.engine import open_engine

    app = QApplication([])
    QAccessible.setActive(True)
    contains = ctypes.CDLL(str(library)).windowContainsText
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
        window.editor.setFocus()
        QTest.qWait(80)
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
class MacWindowTests(unittest.TestCase):
    def test_window_attachment_can_read_chats_and_selections(self):
        scratch = ROOT / ".work/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            work = Path(folder)
            library = work / "window.dylib"
            subprocess.run(["clang", "-fobjc-arc", "-dynamiclib", Path(__file__).with_name("macos_window.m"),
                            "-framework", "AppKit", "-o", library], check=True, timeout=60)
            subprocess.run([sys.executable, "-m", "tests.test_macos_window", work, library], cwd=ROOT,
                           env=dict(os.environ, QT_QPA_PLATFORM="cocoa"), check=True, timeout=30)


if __name__ == "__main__":
    read_window(Path(sys.argv[1]), Path(sys.argv[2]))
