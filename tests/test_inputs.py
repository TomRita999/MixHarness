import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QMimeData, QPoint, QPointF, QSettings, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QListWidget, QPushButton
from qasync import QEventLoop

from mixharness.attachments import Attachment, image_parts
from mixharness.catalog import MODELS
from mixharness.desktop import Window
from mixharness.desktop_clients import ClientHistory
from mixharness.engine import open_engine
from mixharness.store import new_id


ROOT = Path(__file__).resolve().parents[1]
ICON = ROOT / "mixharness/resources/app.png"


class InputTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".work/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / ".mixharness"

    def test_image_survives_restart_retry_fork_and_original_deletion(self):
        picture = Attachment.from_file(ICON)
        with self.assertRaises(ValueError):
            Attachment.from_file(ROOT / "README.md")
        with open_engine(self.root, self.home) as engine:
            store = engine.store
            sid = engine.create_session(**{p: m[0] for p, m in MODELS.items()})["id"]
            draft = "drafts/" + sid
            store.inputs.stage(draft, [picture])
        with open_engine(self.root, self.home) as engine:
            store = engine.store
            request_id = new_id()
            seq, _ = engine.submit(sid, "", request_id, draft=draft)
            self.assertEqual(engine.submit(sid, "", request_id, draft=draft), (seq, False))
            self.assertFalse(store.inputs.metadata(draft))
            job = store.one("SELECT j.*,s.backend FROM jobs j JOIN sessions s ON s.id=j.sid WHERE s.id=?", sid)
            request, _, context, _ = engine.turn_input(job, store.session(sid), [])
            self.assertEqual(json.loads(context)["当前消息附件"][0]["名称"], ICON.name)
            for provider in ("codex", "antigravity", "openai", "gemini"):
                caption, part = image_parts(request["images"], provider)
                self.assertIn(ICON.name, caption["text"])
                encoded = part.get("data") or (part.get("url") or part["image_url"]).split(",", 1)[1]
                self.assertEqual(base64.b64decode(encoded), ICON.read_bytes())
                self.assertNotIn(encoded, context)
            branch = store.fork_session(sid, "")["id"]
            store.delete_session(sid)
            saved = store.snapshot(branch)["events"][0]["attachments"][0]
            self.assertEqual(store.inputs.data(saved["id"]), picture.data)
            store.delete_session(branch)
            self.assertEqual(store.rows("SELECT id FROM input_items"), [])

    def test_composer_paste_remove_restore_and_reference(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        settings = QSettings(str(self.root / "desktop.ini"), QSettings.Format.IniFormat)
        with open_engine(self.root, self.home) as engine, QEventLoop(app) as loop:
            window = Window(engine, settings)
            async def exercise():
                window.show()
                window.add_input.click()
                menu = app.activePopupWidget()
                self.assertEqual({a.text() for a in menu.actions() if not a.isSeparator()},
                                 {"添加图片…", "引用会话…", "引用工具…", "使用技能…"})
                QTest.keyClick(menu, Qt.Key.Key_Escape)
                mime = QMimeData()
                mime.setImageData(QImage(str(ICON)))
                window.editor.insertFromMimeData(mime)
                self.assertTrue(window.send_button.isEnabled())
                window.inputs.remove(window.inputs.items[0]["id"])
                self.assertFalse(window.send_button.isEnabled())
                files = QMimeData()
                files.setUrls([QUrl.fromLocalFile(str(ICON))])
                drag = (Qt.DropAction.CopyAction, files, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                enter = QDragEnterEvent(QPoint(5, 5), *drag)
                app.sendEvent(window.editor.viewport(), enter)
                self.assertTrue(enter.isAccepted())
                drop = QDropEvent(QPointF(5, 5), *drag)
                app.sendEvent(window.editor.viewport(), drop)
                self.assertTrue(drop.isAccepted())
                window.new_session(directory=ROOT)
                self.assertFalse(window.inputs.items)
                window.new_standalone()
                self.assertEqual(window.inputs.items[0]["name"], ICON.name)
                window.send_button.click()
                image_sid = window.sid
                self.assertIn(ICON.name, window.timeline.toPlainText())
                self.assertFalse(window.inputs.items)
                window.editor.setPlainText((ROOT / "README.md").read_text(encoding="utf-8").splitlines()[2])
                window.send_button.click()
                window.new_standalone()
                window.inputs.choose_chat()
                dialog = window.inputs.dialog
                browser = dialog.findChild(ClientHistory)
                browser.select_source("mixharness")
                await browser.loading
                item = browser.list.item(0)
                self.assertEqual(item.data(Qt.ItemDataRole.UserRole)["id"], image_sid)
                browser.list.setCurrentItem(item)
                next(b for b in dialog.findChildren(QPushButton) if b.text() == "引用").click()
                await dialog.attach_task
                window.inputs.choose_reference("tool")
                dialog = window.inputs.dialog
                choices = dialog.findChild(QListWidget)
                choices.setCurrentItem(choices.findItems("MixHarness · read_file", Qt.MatchFlag.MatchExactly)[0])
                next(b for b in dialog.findChildren(QPushButton) if b.text() == "引用").click()
                window.send_button.click()
                event = engine.store.snapshot(window.sid)["events"][0]
                self.assertEqual([i["kind"] for i in event["attachments"]], ["chat", "tool"])
                job = engine.store.one("SELECT j.*,s.backend FROM jobs j JOIN sessions s ON s.id=j.sid WHERE s.id=?", window.sid)
                _, _, context, _ = engine.turn_input(job, engine.store.session(window.sid), [])
                materials = json.loads(context)["当前消息附件"]
                self.assertIn("跨平台桌面工作台", materials[0]["内容"])
                self.assertEqual(materials[1]["标识"], "read_file")
                window.close()
                await asyncio.sleep(0)
            loop.run_until_complete(exercise())
