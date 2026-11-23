import asyncio
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtGui import QPalette, QTextTable
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QInputDialog, QMenu, QMessageBox, QPushButton
from qasync import QEventLoop

from mixharness.chat_snapshot import markdown
from mixharness.desktop import Window
from mixharness.desktop_agent import AgentDialog
from mixharness.desktop_projects import ArchiveDialog, ProjectDialog
from mixharness.desktop_style import load_icons
from mixharness.desktop_usage import UsageDialog
from mixharness.desktop_widgets import Document, SettingsDialog
from mixharness.engine import open_engine
from mixharness.providers import MODELS
from mixharness.store import new_id
from tests.qt_app import application


ROOT = Path(__file__).resolve().parents[1]


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = application()
        cls.app.setQuitOnLastWindowClosed(False)
        cls.app.setStyle("Fusion")
        cls.app.setProperty("iconFamily", load_icons())

    def test_nested_controls_return_to_their_parent(self):
        scratch = ROOT / ".work/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        left = Qt.MouseButton.LeftButton
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            with open_engine(root, root / ".mixharness") as engine, QEventLoop(self.app) as loop:
                window = Window(engine, QSettings(str(root / "desktop.ini"), QSettings.Format.IniFormat))
                async def exercise():
                    window.show()
                    def click_label(parent, title):
                        widget = next(b for b in parent.findChildren(QPushButton) if b.text() == title and b.isVisible())
                        QTest.mouseClick(widget, left)
                    picker = window.pickers["gemini"]
                    QTest.mouseClick(picker.control, left)
                    menu = picker.menu
                    family = next(a for a in menu.actions() if a.menu())
                    menu.setActiveAction(family)
                    QTest.keyClick(menu, Qt.Key.Key_Right)
                    child = family.menu()
                    self.assertTrue(child.isVisible())
                    QTest.keyClick(child, Qt.Key.Key_Escape)
                    self.assertFalse(child.isVisible())
                    self.assertTrue(menu.isVisible())
                    QTest.keyClick(menu, Qt.Key.Key_Escape)
                    picker.open_dialog()
                    click_label(picker.dialog, "查看套餐用量")
                    usage = picker.dialog.findChild(UsageDialog)
                    self.assertTrue(usage.isVisible())
                    click_label(usage, "关闭")
                    self.assertTrue(picker.dialog.isVisible())
                    QTest.keyClick(picker.dialog, Qt.Key.Key_Escape)
                    self.assertIsNone(picker.dialog)
                    window.connections()
                    settings = window.findChild(SettingsDialog)
                    click_label(settings, "技能与上下文…")
                    advanced = settings.findChild(AgentDialog)
                    advanced.window_size.setValue(32000)
                    click_label(advanced, "保存")
                    self.assertEqual(engine.settings.context_window, 32000)
                    self.assertTrue(settings.isVisible())
                    click_label(settings, "取消")
                    self.assertFalse(settings.isVisible())
                    self.assertTrue(window.speed_box.isVisible())
                    speed_end = window.speed_box.mapTo(window, QPoint(window.speed_box.width(), 0)).x()
                    model_start = window.pickers["openai"].control.mapTo(window, QPoint()).x()
                    self.assertLessEqual(speed_end, model_start)
                    self.assertFalse(window.speed_box.icon().isNull())
                    self.assertIn("Fast 已关闭", window.speed_box.accessibleName())
                    self.assertTrue(window.daybreak_button.isHidden())
                    QTest.mouseClick(window.speed_box, left)
                    self.assertIsNone(self.app.activePopupWidget())
                    self.assertIsNotNone(window.backend_task)
                    self.assertFalse(window.speed_box.isEnabled())
                    window.backend_box.setCurrentIndex(window.backend_box.findData("api"))
                    await asyncio.sleep(0)
                    self.assertFalse(window.speed_box.isChecked())
                    window.send_command("/fast")
                    speed_menu = self.app.activePopupWidget()
                    self.assertTrue(any("开启 Fast" in a.text() and a.isEnabled() for a in speed_menu.actions()))
                    QTest.keyClick(speed_menu, Qt.Key.Key_Escape)
                    QTest.mouseClick(window.mode_button, left)
                    menu = self.app.activePopupWidget()
                    plan = next(a for a in menu.actions() if a.text() == "计划模式")
                    QTest.mouseClick(menu, left, pos=menu.actionGeometry(plan).center())
                    window.permission_box.setCurrentIndex(window.permission_box.findData("full"))
                    window.editor.setPlainText("请检查当前项目的构建方式。")
                    QTest.keyClick(window.editor, Qt.Key.Key_Return)
                    self.assertEqual(engine.store.session(window.sid)["permission"], "full")
                    self.assertEqual(engine.store.session(window.sid)["mode"], "plan")
                    self.assertEqual(engine.store.snapshot(window.sid)["tasks"][0]["kind"], "proposal")
                    window.permission_box.setCurrentIndex(window.permission_box.findData("ask"))
                    self.assertEqual(engine.store.session(window.sid)["permission"], "ask")
                    window.close()
                    await asyncio.sleep(0)
                loop.run_until_complete(exercise())

    def test_report_input_expands_from_draft_to_finished_message(self):
        scratch = ROOT / ".work/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            with open_engine(root, root / "data") as engine:
                store = engine.store
                session = engine.create_session(*(MODELS[p][0] for p in ("gemini", "openai")), directory=root)
                sid = session["id"]
                shutil.copy2(ROOT / "pyproject.toml", root)
                request = "核对项目的 Python 版本，并为安装说明写一句话。"
                seq, _ = engine.submit(sid, request, new_id())
                plan = store.one("SELECT * FROM jobs WHERE sid=? AND kind='plan'", sid)
                engine.set_status(plan, "running")
                store.apply_plan(plan, {"engineering": "读取 pyproject.toml", "report": request, "report_after_gpt": True}, "")
                job = store.one("SELECT * FROM jobs WHERE sid=? AND kind='engineering'", sid)
                engine.set_status(job, "running")
                project = (root / "pyproject.toml").read_text(encoding="utf-8")
                version = tomllib.loads(project)["project"]["requires-python"]
                version_line = next(line for line in project.splitlines() if line.startswith("requires-python"))
                engine.record_reads(job, "MixHarness", ["pyproject.toml"])
                with store.db:
                    store.save_turn(job, new_id(), seq, version_line, [], backend="api")
                window = Window(engine, QSettings(str(root / "desktop.ini"), QSettings.Format.IniFormat))
                window.show()
                try:
                    window.select_session(sid)
                    self.app.processEvents()
                    timeline = window.timeline
                    draft = timeline.draft_rows[job["id"]]
                    self.assertTrue(draft.result_toggle.isVisible())
                    self.assertTrue(draft.body.isHidden())
                    self.assertTrue(window.preview.isHidden())
                    QTest.mouseClick(draft.result_toggle, Qt.MouseButton.LeftButton)
                    self.assertIn(version, draft.body.toPlainText())
                    with store.db:
                        store.finish(job, project, {})
                    window.refresh(force=True)
                    self.app.processEvents()
                    result = timeline.rows[-1]
                    self.assertFalse(timeline.draft_rows)
                    self.assertTrue(result.result_toggle.isChecked())
                    self.assertTrue(result.body.isVisible() and result.trace.isVisible())
                    self.assertIn(version, result.body.toPlainText())
                    QTest.keyClick(result.result_toggle, Qt.Key.Key_Space)
                    self.assertTrue(result.body.isHidden() and result.trace.isHidden())
                    writer = store.one("SELECT * FROM jobs WHERE sid=? AND kind='report'", sid)
                    with store.db:
                        store.finish(writer, "Python " + version, {})
                    window.refresh(force=True)
                    self.app.processEvents()
                    self.assertTrue(timeline.rows[-1].body.isVisible())
                    self.assertIsNone(timeline.rows[-1].result_toggle)
                    self.assertTrue(result.body.isHidden())
                    window.set_theme("dark")
                    self.assertTrue(timeline.rows[-2].body.isHidden())
                    branch = store.fork_session(sid, "")
                    window.select_session(branch["id"])
                    self.app.processEvents()
                    copied = next(r for r in timeline.rows if r.result_toggle is not None)
                    self.assertTrue(copied.body.isHidden() and copied.result_toggle.isVisible())
                    QTest.mouseClick(copied.result_toggle, Qt.MouseButton.LeftButton)
                    self.assertIn(version, copied.body.toPlainText())
                finally:
                    window.close()

    def test_markdown_table_and_code_examples_keep_their_structure(self):
        settings = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        table = "| 命令 | 入口 |\n|---|---|\n" + "\n".join(
            f"| {name} | {target} |" for name, target in settings["project"]["scripts"].items())
        wrapped = "~~~markdown\n" + table + "\n~~~"
        view = Document()
        self.addCleanup(view.close)
        view.show_markdown(wrapped)
        frame, = view.document().rootFrame().childFrames()
        self.assertIsInstance(frame, QTextTable)
        self.assertEqual(frame.rows(), 1 + len(settings["project"]["scripts"]))
        self.assertIn("<table>", markdown(wrapped))
        example = "Markdown 写法\n\n" + wrapped
        view.show_markdown(example)
        self.assertFalse(view.document().rootFrame().childFrames())
        self.assertIn("|---|---|", view.toPlainText())
        self.assertNotIn("<table>", markdown(example))

    def test_conversation_and_model_choices_survive_reopening(self):
        scratch = ROOT / ".work" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            project = Path(temp)
            home = project / ".mixharness"
            settings = QSettings(str(project / "desktop.ini"), QSettings.Format.IniFormat)
            with open_engine(project, home) as engine:
                window = Window(engine, settings)
                window.show()
                self.app.processEvents()
                self.assertIsNone(window.sid)
                self.assertTrue(window.heading.isVisible())
                choices = list(window.pickers.values())
                self.assertEqual(choices[0].mapTo(window, QPoint()).y(), choices[1].mapTo(window, QPoint()).y())
                for picker in choices:
                    QTest.mouseClick(picker.control, Qt.MouseButton.LeftButton)
                    if picker.provider == "gemini":
                        menu = self.app.activePopupWidget()
                        family = next(a for a in menu.actions() if a.text().startswith("Gemini 4 Neon"))
                        self.assertIn("XHigh", [a.text() for a in family.menu().actions()])
                        search = next(a for a in menu.actions() if a.text() == "搜索全部模型…")
                        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(search).center())
                    model = MODELS[picker.provider][1]
                    picker.search.setText(model)
                    self.assertEqual(picker.options.count(), 1)
                    self.assertEqual(picker.options.item(0).data(Qt.ItemDataRole.UserRole), model)
                    QTest.mouseClick(picker.apply, Qt.MouseButton.LeftButton)
                for picker, level in zip(choices, ("Medium", "High")):
                    QTest.mouseClick(picker.effort_control, Qt.MouseButton.LeftButton)
                    menu = self.app.activePopupWidget()
                    option = next(a for a in menu.actions() if a.text() == level)
                    QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(option).center())
                first = "请核对项目的 Python 版本并撰写说明。"
                second = "把报告读者改为应用开发者。"
                window.editor.setPlainText(first)
                QTest.keyClick(window.editor, Qt.Key.Key_Return)
                sid = window.sid
                window.editor.setPlainText(second)
                QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
                self.app.processEvents()
                self.assertFalse(window.heading.isVisible())
                self.assertIn(second, window.timeline.toPlainText())
                self.assertEqual(engine.store.session(sid)["revision"], 2)
                self.assertEqual([t["status"] for t in engine.store.snapshot(sid)["tasks"]], ["obsolete", "ready"])
                QTest.mouseClick(window.inspect_button, Qt.MouseButton.LeftButton)
                self.assertTrue(window.inspector.isVisible())
                self.assertEqual(window.tasks.count(), 2)
                window.connections()
                dialog = window.findChild(SettingsDialog)
                self.assertTrue(dialog.isVisible())
                self.assertEqual(set(dialog.fields), {"GEMINI_API_KEY", "OPENAI_API_KEY"})
                dialog.reject()
                window.toggle_theme()
                rgb = self.app.palette().color(QPalette.ColorRole.Window)
                self.assertEqual((rgb.red(), rgb.green(), rgb.blue()), (22, 22, 22))
                window.editor.setPlainText("补充说明版本来源。")
                window.new_session(directory=ROOT)
                self.assertIsNone(window.sid)
                self.assertEqual(Path(engine.store.project(window.pid)["folders"][0]["path"]), ROOT)
                self.assertEqual(window.editor.toPlainText(), "")
                window.select_session(sid)
                self.assertEqual(window.editor.toPlainText(), "补充说明版本来源。")
                window.close()
            with open_engine(project, home) as engine:
                window = Window(engine, settings)
                self.assertEqual(window.sid, sid)
                self.assertEqual(window.theme, "dark")
                self.assertEqual(window.editor.toPlainText(), "补充说明版本来源。")
                self.assertEqual(window.pickers["gemini"].model, MODELS["gemini"][1])
                self.assertEqual(window.pickers["openai"].model, MODELS["openai"][1])
                self.assertEqual(window.pickers["gemini"].effort, "medium")
                self.assertEqual(window.pickers["openai"].effort, "high")
                self.assertIn(first, window.timeline.toPlainText())
                self.assertIn(second, window.timeline.toPlainText())
                window.close()

    def test_worker_accepts_updated_requirements_from_native_window(self):
        scratch = ROOT / ".work" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            project = Path(temp)
            with open_engine(project, project / ".mixharness") as engine, QEventLoop(self.app) as loop:
                if engine.keys["gemini"]:
                    self.skipTest("等待密钥的测试需要无 Gemini 密钥的启动环境")
                window = Window(engine, QSettings(str(project / "desktop.ini"), QSettings.Format.IniFormat))
                async def exercise():
                    window.show()
                    worker = asyncio.create_task(engine.serve())
                    try:
                        for message in ("检查桌面程序的启动方式。", "说明中加上 Windows 和 Ubuntu。"):
                            window.editor.setPlainText(message)
                            QTest.keyClick(window.editor, Qt.Key.Key_Return)
                            async with asyncio.timeout(5):
                                while window.state["tasks"][-1]["status"] != "needs_key":
                                    await asyncio.sleep(0.03)
                        self.assertEqual(window.state["session"]["revision"], 2)
                        self.assertEqual(window.state["tasks"][0]["status"], "obsolete")
                        self.assertIn("等待密钥", window.tasks.item(0).text())
                        self.assertIn("等待 Gemini API Key", window.work_status.text())
                        self.assertIn("Gemini 未配置", window.connection_state.text())
                        self.assertTrue(window.timeline.rows[-1].user)
                        self.assertGreater(window.timeline.rows[-1].block.x(), 0)
                        QTest.mouseClick(window.backend_box, Qt.MouseButton.LeftButton)
                        self.assertTrue(window.backend_box.view().isVisible())
                        self.assertEqual([window.backend_box.itemText(i) for i in range(window.backend_box.count())],
                                         ["API", "本机客户端"])
                        view = window.backend_box.view()
                        self.assertEqual(view.textElideMode(), Qt.TextElideMode.ElideNone)
                        self.assertGreaterEqual(view.viewport().width(), view.sizeHintForColumn(0))
                        QTest.keyClick(window.backend_box.view(), Qt.Key.Key_End)
                        QTest.keyClick(window.backend_box.view(), Qt.Key.Key_Return)
                        self.assertIn("正在读取", window.work_status.text())
                        QTest.mouseClick(window.backend_box, Qt.MouseButton.LeftButton)
                        QTest.keyClick(window.backend_box.view(), Qt.Key.Key_Home)
                        QTest.keyClick(window.backend_box.view(), Qt.Key.Key_Return)
                        self.assertEqual(window.backend_box.currentData(), "api")
                        self.assertIn("等待 Gemini API Key", window.work_status.text())
                        self.assertEqual(window.state["session"]["revision"], 2)
                    finally:
                        worker.cancel()
                        await asyncio.gather(worker, return_exceptions=True)
                        window.close()
                loop.run_until_complete(exercise())

    def test_project_creation_and_chat_menus(self):
        scratch = ROOT / ".work" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            root = Path(temp)
            sources = [root / "应用", root / "说明"]
            for path in sources:
                path.mkdir()
            settings = QSettings(str(root / "desktop.ini"), QSettings.Format.IniFormat)
            with open_engine(root, root / ".mixharness") as engine:
                window = Window(engine, settings)
                window.show()
                def click(widget):
                    self.assertTrue(widget.isVisible())
                    QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
                    self.app.processEvents()
                def row_for(sid):
                    for i in range(window.history.topLevelItemCount()):
                        group = window.history.topLevelItem(i)
                        for j in range(group.childCount()):
                            child = group.child(j)
                            if child.data(0, Qt.ItemDataRole.UserRole) == sid:
                                return child
                    self.fail("侧栏中找不到会话")
                def action(sid, title):
                    row = window.history.itemWidget(row_for(sid), 1)
                    click(row.findChild(QPushButton))
                    menu = self.app.activePopupWidget()
                    self.assertIsInstance(menu, QMenu)
                    option = next(a for a in menu.actions() if a.text() == title)
                    QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(option).center())
                    self.app.processEvents()
                def confirmation(text):
                    dialog = next(x for x in window.findChildren(QMessageBox) if x.isVisible())
                    click(next(b for b in dialog.buttons() if b.text() == text))
                click(window.create_project_button)
                dialog = window.findChild(ProjectDialog)
                self.assertFalse(dialog.save.isEnabled())
                dialog.name.setText("桌面发布")
                for path in sources:
                    dialog.add_folder(str(path))
                click(dialog.primary)
                click(dialog.save)
                pid = window.pid
                project = engine.store.project(pid)
                self.assertEqual(project["name"], "桌面发布")
                self.assertEqual([f["path"] for f in project["folders"]], list(map(str, reversed(sources))))
                self.assertEqual(engine.store.sessions(), [])
                window.editor.setPlainText("整理桌面应用的发布说明。")
                click(window.send_button)
                first = window.sid
                action(first, "重命名会话…")
                edit = window.findChild(QInputDialog)
                edit.setTextValue("发布说明")
                edit.accept()
                self.app.processEvents()
                action(first, "置顶会话")
                self.assertTrue(engine.store.session(first)["pinned"])
                self.assertEqual(window.title.text(), "发布说明")
                window.new_session()
                window.editor.setPlainText("记录工程验证步骤。")
                click(window.send_button)
                second = window.sid
                window.editor.setPlainText("保留当前会话的未发送要求。")
                action(first, "归档会话")
                self.assertEqual(window.sid, second)
                self.assertEqual([s["id"] for s in engine.store.sessions(archived=True)], [first])
                window.show_archive()
                archive = window.findChild(ArchiveDialog)
                click(archive.restore)
                archive.reject()
                self.app.processEvents()
                window.filter.setText("发布说明。")
                self.assertEqual(row_for(first).text(0), "发布说明")
                window.filter.clear()
                action(first, "删除会话…")
                confirmation("取消")
                self.assertEqual(len(engine.store.sessions()), 2)
                action(first, "删除会话…")
                confirmation("删除会话")
                self.assertEqual(window.sid, second)
                self.assertEqual(window.editor.toPlainText(), "保留当前会话的未发送要求。")
                self.assertEqual([s["id"] for s in engine.store.sessions()], [second])
                action(second, "删除会话…")
                confirmation("删除会话")
                self.assertIsNone(window.sid)
                self.assertEqual(window.pid, pid)
                self.assertEqual(window.editor.toPlainText(), "")
                self.assertEqual(engine.store.sessions(), [])
                self.assertFalse(settings.contains("drafts/" + second))
                window.close()
            with open_engine(root, root / ".mixharness") as engine:
                window = Window(engine, settings)
                self.assertEqual(window.pid, pid)
                self.assertIsNone(window.sid)
                self.assertEqual(engine.store.sessions(), [])
                self.assertTrue(all(p.is_dir() for p in sources))
                window.close()

    def test_switching_projects_restores_their_own_chats(self):
        scratch = ROOT / ".work" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            home = Path(temp)
            first_folder, second_folder = home / "first", home / "second"
            first_folder.mkdir()
            second_folder.mkdir()
            settings = QSettings(str(home / "desktop.ini"), QSettings.Format.IniFormat)
            with open_engine(home, home / ".mixharness") as engine:
                window = Window(engine, settings)
                window.show()
                def submit(text):
                    window.editor.setPlainText(text)
                    QTest.keyClick(window.editor, Qt.Key.Key_Return)
                    return window.sid
                local = submit("整理今天的工作。")
                window.open_project(first_folder)
                first, pid = submit("说明项目的目录结构。"), window.pid
                window.new_session()
                second = submit("检查项目的测试命令。")
                self.assertNotEqual(first, second)
                self.assertEqual(engine.store.session(second)["project_id"], pid)
                self.assertEqual(engine.store.session(second)["workspace"], str(first_folder))
                window.select_session(first)
                window.editor.setPlainText("说明项目中的验证流程。")
                window.open_project(second_folder)
                other = submit("整理另一个项目的需求。")
                window.open_project(first_folder / ".." / "first")
                self.assertEqual(window.sid, first)
                self.assertEqual(window.editor.toPlainText(), "说明项目中的验证流程。")
                self.assertEqual(len(engine.store.sessions()), 4)
                tree = window.history
                group = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                             if tree.topLevelItem(i).text(0) == "second")
                tree.setCurrentItem(group)
                self.assertEqual(window.sid, other)
                self.assertIs(group, tree.currentItem().parent())
                self.assertEqual([(index.column(), index.data()) for index in tree.selectedIndexes()],
                                 [(0, engine.store.session(other)["title"])])
                group.setExpanded(False)
                self.assertEqual(window.sid, other)
                self.assertIs(tree.currentItem(), group)
                group.setExpanded(True)
                self.assertEqual(tree.currentItem().data(0, Qt.ItemDataRole.UserRole), other)
                window.hide_project(pid)
                self.assertNotIn(first, [s["id"] for s in engine.store.sessions()])
                window.open_project(first_folder)
                self.assertEqual(window.sid, first)
                window.select_session(local)
                self.assertIsNone(window.pid)
                window.close()
            with open_engine(home, home / ".mixharness") as engine:
                window = Window(engine, settings)
                self.assertEqual(window.sid, local)
                window.open_project(first_folder)
                self.assertEqual(window.sid, first)
                window.close()
