import asyncio
import shutil
import tempfile
import tomllib
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from qasync import QEventLoop

from mixharness.clients import is_dot_model
from mixharness.commands import dispatch, settled
from mixharness.desktop import Window
from mixharness.desktop_style import load_icons
from mixharness.engine import open_engine
from mixharness.store import new_id
from tests.qt_app import application


ROOT = Path(__file__).resolve().parents[1]


async def stop_native(engine, session, question, phase, window=None):
    sid = session["id"]
    if window:
        window.select_session(sid)
        window.editor.setPlainText(question)
        QTest.keyClick(window.editor, Qt.Key.Key_Return)
    else:
        engine.submit(sid, question, new_id())
    async with asyncio.timeout(120):
        while True:
            task = engine.store.snapshot(sid)["tasks"][-1]
            if engine.activity.get(task["id"]) == phase:
                break
            assert task["status"] not in ("done", "failed"), (task["status"], task["error"])
            await asyncio.sleep(0.02)
    if window:
        window.refresh()
        assert window.stop_button.isVisible() and window.work_progress.isVisible()
        assert "GPT" in window.work_status.text() and phase in window.work_status.toolTip()
        assert "进行中" in window.history_rows[("chat", sid)].toolTip(0)
        window.editor.setPlainText("把测试步骤写得更具体")
        QTest.mouseClick(window.stop_button, Qt.MouseButton.LeftButton)
        async with asyncio.timeout(12):
            while window.work_status.text() != "任务已停止":
                await asyncio.sleep(0.03)
        assert not window.stop_button.isVisible() and window.editor.toPlainText() == "把测试步骤写得更具体"
    else:
        update = await dispatch(engine, {"command": "updates", "id": sid})
        assert update["active_count"] and update["running"]
        result = await dispatch(engine, {"command": "command", "id": sid, "line": "/stop"})
        assert "停止" in result["text"]
    state = engine.store.snapshot(sid)
    assert state["tasks"][-1]["status"] == "stopped" and not state["approvals"]
    assert state["events"][-1]["meta"]["stopped"] == 1
    receipts = [e["meta"]["stop_status"] for e in state["events"] if "stop_status" in e["meta"]]
    assert len(receipts) == 1 and receipts[0] in ("interrupted", "cancelled"), receipts
    assert task["id"] not in engine.activity
    print(phase + "，客户端已确认中断", flush=True)


async def main():
    scratch = ROOT / ".work"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        work = Path(directory)
        project = work / "project"
        project.mkdir()
        metadata = project / "pyproject.toml"
        shutil.copy2(ROOT / metadata.name, metadata)
        name = tomllib.loads(metadata.read_text(encoding="utf-8"))["project"]["name"]
        target = project / "package-name.txt"
        with open_engine(work, work / ".mixharness") as engine:
            await asyncio.gather(*(c.refresh() for c in engine.client_catalogs.values()))
            writers = engine.client_catalogs["gemini"].labels
            coders = engine.client_catalogs["openai"].labels
            writer = "gemini-3.8-flash-low" if "gemini-3.8-flash-low" in writers else next(iter(writers))
            coder = "gpt-6.2-astra-dot" if "gpt-6.2-astra-dot" in coders else next(m for m in coders if is_dot_model(m))
            options = dict(gemini=writer, openai=coder, directory=project, backend="clients", permission="ask")
            session = engine.create_session(**options)
            sid = session["id"]
            worker = asyncio.create_task(engine.serve())
            try:
                print(f"实际客户端模型 {writer} / {coder}", flush=True)
                engine.submit(sid, "请 GPT 从 pyproject.toml 读取项目名称，在项目根目录新建 package-name.txt，"
                                   "只写项目名称并以换行结束。完成文件写入即可。", new_id())
                state = await settled(engine, sid, 300, return_on_approval=True)
                while state["approvals"]:
                    for request in state["approvals"]:
                        assert request["source"] == "MixHarness", request["source"]
                        if request["title"].startswith("写入 "):
                            assert request["detail"] == str(target) + "\n\n" + name + "\n"
                            assert not target.exists()
                        else:
                            assert request["title"] == "运行 python-compile" and str(project) in request["detail"]
                        engine.decide(sid, request["id"], True)
                        print("已核对并批准 " + request["title"], flush=True)
                    state = await settled(engine, sid, 300, return_on_approval=True)
                assert all(t["status"] == "done" for t in state["tasks"]), [(t["kind"], t["status"], t["error"]) for t in state["tasks"]]
                assert any(t["kind"] == "engineering" and t["model"] == coder for t in state["tasks"])
                assert target.read_text(encoding="utf-8") == name + "\n"
                assert engine.store.rows("SELECT 1 FROM approvals WHERE sid=? AND source='MixHarness' AND decision='allow' AND title LIKE '写入 %'", sid)
                print("Dot 工程任务已完成，磁盘内容与批准的内容一致", flush=True)
                window = Window(engine, QSettings(str(work / "ui.ini"), QSettings.Format.IniFormat))
                window.show()
                try:
                    await stop_native(engine, engine.create_session(**options, mode="plan"),
                                      "逐项核对 pyproject.toml 中的依赖和命令入口，制定跨平台测试计划。", "Codex 已开始生成", window)
                finally:
                    window.close()
                await stop_native(engine, engine.create_session(**options),
                                  "根据项目配置梳理跨平台发布过程，先分析要执行哪些任务。", "Antigravity 正在处理请求")
                engine.submit(sid, "请 GPT 更新刚才生成的 package-name.txt，在包名后面加一个空格和 pyproject.toml "
                                   "中的版本号，再换行。只改这个文件。", new_id())
                pending = await settled(engine, sid, 300, return_on_approval=True)
                assert len(pending["approvals"]) == 1
                approval = pending["approvals"][0]
                assert approval["source"] == "MixHarness" and approval["detail"].splitlines()[0] == str(target)
                await engine.stop_session(sid)
                stored = engine.store.one("SELECT decision FROM approvals WHERE id=?", approval["id"])
                assert stored["decision"] == "expired" and not engine.store.approvals(sid)
                assert target.read_text(encoding="utf-8") == name + "\n"
                print("等待批准时停止任务，审批已过期，文件保留上次批准的内容", flush=True)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)


if __name__ == "__main__":
    app = application()
    app.setStyle("Fusion")
    app.setProperty("iconFamily", load_icons())
    app.setQuitOnLastWindowClosed(False)
    with QEventLoop(app) as loop:
        loop.run_until_complete(main())
