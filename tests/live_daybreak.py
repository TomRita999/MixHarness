import asyncio
import os
import shutil
import tempfile
import tomllib
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

from mixharness.agent_settings import available_speeds
from mixharness.commands import settled
from mixharness.desktop import Window
from mixharness.desktop_style import load_icons
from mixharness.engine import open_engine
from mixharness.reasoning import model_available
from mixharness.slash import execute_command


ROOT = Path(__file__).resolve().parents[1]


async def exercise(window, workspace):
    engine = window.engine
    window.show()
    window.backend_box.setCurrentIndex(window.backend_box.findData("clients"))
    picker = window.pickers["openai"]
    left = Qt.MouseButton.LeftButton
    QTest.mouseClick(picker.control, left)
    await asyncio.gather(*window.catalog_tasks.values())
    catalog = engine.client_catalogs["openai"]
    assert catalog.groups, catalog.error
    blue = [g for g in catalog.groups if "daybreakBlue" in (g["access_programs"] or [])]
    menu = QApplication.activePopupWidget()
    assert menu is picker.menu and menu is not None
    assert menu.actions()[0].text().startswith("Daybreak 模式") == bool(blue), "已打开的菜单没有更新账户授权"
    assert any(a.text().partition("    ")[0] == catalog.groups[0]["name"] for a in menu.actions())
    QTest.keyClick(menu, Qt.Key.Key_Escape)
    fast = next((g for g in catalog.groups if model_available(g, False) and "fast" in available_speeds(g)), None)
    if fast:
        window.choose_model("openai", fast["id"])
        standard_icon = window.speed_box.icon().pixmap(16, 16).toImage()
        for tier in ("fast", "standard"):
            QTest.mouseClick(window.speed_box, left)
            assert QApplication.activePopupWidget() is None
            assert window.speed_box.isChecked() == (tier == "fast")
            assert window.draft_speed == tier
            assert (standard_icon != window.speed_box.icon().pixmap(16, 16).toImage()) == (tier == "fast")
        session = engine.create_session(window.pickers["gemini"].model, fast["id"], directory=workspace, backend="clients")
        window.select_session(session["id"])
        for tier in ("fast", "standard"):
            QTest.mouseClick(window.speed_box, left)
            assert QApplication.activePopupWidget() is None
            assert engine.store.session(window.sid)["speed"] == tier
            window.select_session(session["id"])
            assert window.speed_box.isChecked() == (tier == "fast")
        window.backend_box.setCurrentIndex(window.backend_box.findData("api"))
        QTest.mouseClick(window.speed_box, left)
        assert QApplication.activePopupWidget() is None
        await window.backend_task
        saved = engine.store.session(window.sid)
        assert saved["backend"] == "clients" and saved["speed"] == "fast" and window.speed_box.isChecked()
        QTest.mouseClick(window.speed_box, left)
        window.new_session()
        print("已验证草稿、已保存会话和从 API 切换到本机客户端的单击 Fast 操作。", flush=True)
    if not blue:
        assert window.daybreak_button.isHidden()
        picker.control.click()
        assert all("Daybreak 模式" not in a.text() for a in picker.menu.actions())
        QTest.keyClick(picker.menu, Qt.Key.Key_Escape)
        print("当前账户没有 Daybreak Blue，开关已隐藏。", flush=True)
        window.close()
        return
    standard = next((g for g in catalog.groups if g["access_programs"] == ["standard"]), None)
    if standard:
        window.choose_model("openai", standard["id"])
    QTest.mouseClick(picker.control, left)
    menu = picker.menu
    switch = menu.actions()[0]
    assert switch.text().startswith("Daybreak 模式") and not switch.isChecked()
    QTest.mouseClick(menu, left, pos=menu.actionGeometry(switch).center())
    await asyncio.sleep(0.05)
    assert window.daybreak_button.isChecked() and picker.model in {g["id"] for g in blue}
    menu = picker.menu
    assert menu is QApplication.activePopupWidget() and menu.actions()[0].isChecked()
    for group in catalog.groups:
        action = next(a for a in menu.actions() if a.text().partition("    ")[0] == group["name"])
        assert action.isEnabled() == (group in blue), group["id"]
    QTest.keyClick(menu, Qt.Key.Key_Escape)
    print("模型菜单", picker.model, "可选", len(blue), "灰显", len(catalog.groups) - len(blue), flush=True)

    session = engine.create_session(window.pickers["gemini"].model, picker.model, directory=workspace,
                                    backend="clients", daybreak=True)
    sid = session["id"]
    window.select_session(sid)
    assert window.daybreak_button.isChecked()
    QTest.mouseClick(window.daybreak_button, left)
    assert not engine.store.session(sid)["daybreak"]
    picker.control.click()
    menu = picker.menu
    switch = menu.actions()[0]
    assert not switch.isChecked()
    menu.setActiveAction(switch)
    QTest.keyClick(menu, Qt.Key.Key_Return)
    await asyncio.sleep(0.05)
    assert engine.store.session(sid)["daybreak"] and window.daybreak_button.isChecked()
    QTest.keyClick(picker.menu, Qt.Key.Key_Escape)
    result = await execute_command(engine, {"id": sid, "line": "/model openai"})
    if standard:
        assert standard["id"] in result["unavailable_models"]
        try:
            await execute_command(engine, {"id": sid, "line": "/model openai " + standard["id"]})
        except ValueError as exc:
            assert standard["id"] in str(exc)
        else:
            raise AssertionError("命令行接受了不兼容的模型")
    side = await execute_command(engine, {"id": sid, "line": "/btw 只读查看 pyproject.toml，说明项目名称和最低 Python 版本，附上指向这个文件的 Markdown 链接。"})
    assert engine.store.session(side["id"])["daybreak"]
    worker = asyncio.create_task(engine.serve())
    try:
        state = await settled(engine, side["id"], 180, return_on_approval=True)
        jobs = state["tasks"]
        assert jobs and all(j["status"] == "done" for j in jobs), [(j["status"], j["error"]) for j in jobs]
        reply = next(e["body"] for e in state["events"] if e["actor"] == "openai" and e["kind"] == "message")
        project = tomllib.loads((workspace / "pyproject.toml").read_text())["project"]
        assert project["name"].lower() in reply.lower() and project["requires-python"].removeprefix(">=") in reply, reply
        assert "pyproject.toml" in reply, reply
        print("Daybreak Blue 只读回复", reply, flush=True)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    only_blue = next((g for g in blue if "standard" not in g["access_programs"]), None)
    if only_blue:
        await execute_command(engine, {"id": sid, "line": "/model openai " + only_blue["id"]})
    picker.control.click()
    await execute_command(engine, {"id": sid, "line": "/daybreak off"})
    window.refresh(force=True)
    assert not window.daybreak_button.isChecked()
    assert picker.menu is QApplication.activePopupWidget() and not picker.menu.actions()[0].isChecked()
    QTest.keyClick(picker.menu, Qt.Key.Key_Escape)
    if only_blue:
        assert window.pickers["openai"].model != only_blue["id"]
    window.backend_box.setCurrentIndex(window.backend_box.findData("api"))
    assert window.daybreak_button.isHidden()
    window.close()
    await asyncio.sleep(0)


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication([])
    app.setStyle("Fusion")
    app.setProperty("iconFamily", load_icons())
    app.setQuitOnLastWindowClosed(False)
    scratch = ROOT / ".work/tests"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as folder, QEventLoop(app) as loop:
        asyncio.set_event_loop(loop)
        root = Path(folder)
        workspace = root / "project"
        workspace.mkdir()
        shutil.copy2(ROOT / "pyproject.toml", workspace)
        with open_engine(root, root / "data") as engine:
            settings = QSettings(str(root / "ui.ini"), QSettings.Format.IniFormat)
            loop.run_until_complete(exercise(Window(engine, settings), workspace))
