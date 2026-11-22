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

from mixharness.commands import settled
from mixharness.desktop import Window
from mixharness.engine import open_engine
from mixharness.slash import execute_command


ROOT = Path(__file__).resolve().parents[1]


async def exercise(window, workspace):
    engine = window.engine
    window.show()
    window.backend_box.setCurrentIndex(window.backend_box.findData("clients"))
    await asyncio.gather(*window.catalog_tasks.values())
    catalog = engine.client_catalogs["openai"]
    assert catalog.groups, catalog.error
    blue = [g for g in catalog.groups if "daybreakBlue" in (g["access_programs"] or [])]
    if not blue:
        assert window.daybreak_button.isHidden()
        print("当前账户没有 Daybreak Blue，开关已隐藏。", flush=True)
        window.close()
        return
    standard = next((g for g in catalog.groups if g["access_programs"] == ["standard"]), None)
    if standard:
        window.choose_model("openai", standard["id"])
    window.daybreak_button.click()
    picker = window.pickers["openai"]
    assert window.daybreak_button.isChecked() and picker.model in {g["id"] for g in blue}
    picker.control.click()
    menu = picker.menu
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
    await execute_command(engine, {"id": sid, "line": "/daybreak off"})
    window.refresh(force=True)
    assert not window.daybreak_button.isChecked()
    if only_blue:
        assert window.pickers["openai"].model != only_blue["id"]
    window.backend_box.setCurrentIndex(window.backend_box.findData("api"))
    assert window.daybreak_button.isHidden()
    window.close()
    await asyncio.sleep(0)


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication([])
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
