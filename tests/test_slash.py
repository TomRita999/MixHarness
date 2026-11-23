import asyncio
import base64
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton
from qasync import QEventLoop

from mixharness import workspace
from mixharness.catalog import MODELS
from mixharness.desktop import Window
from mixharness.desktop_chat_actions import ShareDialog, SideChat
from mixharness.desktop_style import load_icons
from mixharness.engine import open_engine
from mixharness.slash import execute_command
from mixharness.store import new_id
from tests.qt_app import application


ROOT = Path(__file__).resolve().parents[1]


class SlashTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".work/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_composer_search_keyboard_mouse_and_side_chat(self):
        app = application()
        app.setQuitOnLastWindowClosed(False)
        app.setProperty("iconFamily", load_icons())
        with open_engine(self.root, self.root / "data") as engine, QEventLoop(app) as loop:
            window = Window(engine, QSettings(str(self.root / "ui.ini"), QSettings.Format.IniFormat))
            editor, menu = window.editor, window.slash_menu
            async def exercise():
                window.show()
                window.activateWindow()
                await asyncio.sleep(0.04)
                editor.setFocus()
                QTest.keyClicks(editor, "/")
                self.assertTrue(menu.isVisible())
                self.assertTrue(editor.hasFocus())
                self.assertIn("compact", menu.names)
                QTest.keyClick(editor, Qt.Key.Key_Down)
                self.assertEqual(menu.currentItem().data(Qt.ItemDataRole.UserRole), "review")
                QTest.keyClick(editor, Qt.Key.Key_Tab)
                self.assertEqual(editor.toPlainText(), "/review ")
                self.assertIsNone(window.sid)
                self.assertFalse(menu.isVisible())
                editor.clear()
                QTest.keyClicks(editor, "/")
                ime = QInputMethodEvent()
                ime.setCommitString("计划")
                app.sendEvent(editor, ime)
                self.assertEqual(menu.names[0], "plan")
                QTest.mouseClick(menu.viewport(), Qt.MouseButton.LeftButton, pos=menu.visualItemRect(menu.item(0)).center())
                self.assertEqual(window.draft_mode, "plan")
                self.assertEqual(editor.toPlainText(), "")
                QTest.keyClicks(editor, "/billing")
                self.assertEqual(menu.names[0], "usage")
                QTest.keyClick(editor, Qt.Key.Key_Escape)
                self.assertFalse(menu.isVisible())
                self.assertEqual(editor.toPlainText(), "/billing")
                editor.clear()
                QTest.keyClicks(editor, "src/a")
                self.assertFalse(menu.isVisible())
                editor.clear()
                QTest.keyClicks(editor, "/fast")
                QTest.keyClick(editor, Qt.Key.Key_Return)
                popup = app.activePopupWidget()
                self.assertEqual(popup.accessibleName(), "Codex 速度")
                QTest.keyClick(popup, Qt.Key.Key_Escape)
                await asyncio.sleep(0)
                window.select_mode("default")
                editor.setPlainText("查看当前项目的构建说明")
                window.send()
                sid = window.sid
                parents = engine.store.snapshot(sid)
                editor.setFocus()
                QTest.keyClicks(editor, "/side")
                QTest.keyClick(editor, Qt.Key.Key_Return)
                dock = window.findChild(SideChat)
                self.assertTrue(dock.isVisible())
                dock.editor.setPlainText("第一条消息问了什么？")
                QTest.keyClick(dock.editor, Qt.Key.Key_Return)
                self.assertEqual(window.sid, sid)
                self.assertEqual(engine.store.snapshot(sid), parents)
                side = engine.store.snapshot(dock.sid)
                self.assertEqual(side["session"]["mode"], "side")
                self.assertEqual([t["kind"] for t in side["tasks"]], ["side"])
                self.assertEqual(side["events"][0]["body"], parents["events"][0]["body"])
                self.assertIn("第一条消息", dock.timeline.toPlainText())
                dock.close()
                editor.setFocus()
                QTest.keyClicks(editor, "/share")
                QTest.keyClick(editor, Qt.Key.Key_Return)
                share = window.findChild(ShareDialog)
                self.assertTrue(share.isVisible())
                next(b for b in share.findChildren(QPushButton) if b.text() == "复制快照链接").click()
                url = app.clipboard().text()
                page = base64.b64decode(url.split(",", 1)[1]).decode()
                self.assertIn("查看当前项目的构建说明", page)
                self.assertNotIn("第一条消息问了什么", page)
                share.accept()
                window.close()
                await asyncio.sleep(0)
            loop.run_until_complete(exercise())

    def test_git_changes_and_worktree_fork_keep_the_original_workspace(self):
        source = self.root / "repo"
        project = source / "子项目"
        project.mkdir(parents=True)
        def git(*args):
            return subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
        git("init")
        readme = project / "README.md"
        shutil.copyfile(ROOT / "README.md", readme)
        git("add", ".")
        git("-c", "user.name=MixHarness", "-c", "user.email=test@localhost", "-c", "commit.gpgsign=false",
            "commit", "-m", "项目说明")
        readme.rename(project / "之前的说明.md")
        shutil.copyfile(ROOT / "pyproject.toml", project / "pyproject.toml")
        git("add", "子项目/pyproject.toml")
        (project / ".env").write_text("EXCLUDED=1", encoding="utf-8")
        with open_engine(self.root, self.root / "data") as engine:
            sid = engine.create_session(**{p: m[0] for p, m in MODELS.items()}, directory=project)["id"]
            engine.submit(sid, "请审查这次的项目调整", new_id())
            result = workspace.review_changes(engine.sources(engine.store.session(sid)), engine.home)
            self.assertIn("之前的说明.md", result)
            self.assertIn("diff --git a/README.md", result)
            self.assertIn("diff --git a/pyproject.toml", result)
            self.assertNotIn(".env", "\n".join(s for s in result.splitlines() if s.startswith("??")))
            tree = self.root / "另一份工作树"
            branch = asyncio.run(execute_command(engine, {"id": sid, "line": "/fork --worktree " + str(tree)}))
            other = engine.store.snapshot(branch["id"])
            self.assertEqual(Path(other["session"]["workspace"]), tree / project.name)
            self.assertTrue((tree / project.name / "README.md").is_file())
            self.assertEqual(other["events"][0]["body"], "请审查这次的项目调整")
            with self.assertRaises(ValueError):
                asyncio.run(execute_command(engine, {"id": sid, "line": "/fork --worktree " + str(tree)}))
            asyncio.run(execute_command(engine, {"id": branch["id"], "line": "/archive"}))
            self.assertTrue(engine.store.session(branch["id"])["archived"])
            self.assertFalse(engine.store.session(sid)["archived"])
            self.assertFalse(readme.exists())
