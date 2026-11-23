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
    jid = task["id"]
    bound = state["session"]["antigravity_session"]
    if window:
        QTest.mouseClick(window.continue_button, Qt.MouseButton.LeftButton)
    else:
        await dispatch(engine, {"command": "command", "id": sid, "line": "/continue"})
    async with asyncio.timeout(120):
        while engine.activity.get(jid) != phase:
            task = engine.store.job(jid)
            assert task["status"] not in ("failed", "done"), task["error"]
            await asyncio.sleep(0.02)
    if window:
        window.refresh()
        assert window.send_button.accessibleName() == "发送引导"
        message = ("将计划缩短为两句话，标题用「本机续写验收」。写清如何用 mixharness --version 核对安装版本，"
                   "以及输出什么算通过。这就是全部验收条件，可以直接给出计划。")
        window.editor.setPlainText(message)
        QTest.keyClick(window.editor, Qt.Key.Key_Return)
    else:
        message = "取消发布过程梳理。这是一个简单问候，请直接在 reply 中回复「会话可以继续」。开发、验证和报告均无须安排。"
        await dispatch(engine, {"command": "send", "id": sid, "message": message})
    final = await settled(engine, sid, 450, return_on_approval=True)
    assert not final["approvals"]
    assert engine.store.job(jid)["status"] == "done", engine.store.job(jid)["error"]
    assert len([t for t in final["tasks"] if t["kind"] == task["kind"]]) == 1
    guides = [e for e in final["events"] if e["meta"].get("guide")]
    assert len(guides) == 1 and guides[0]["body"] == message and guides[0]["meta"]["jobs"] == [jid]
    if window:
        assert "本机续写验收" in engine.store.job(jid)["draft"]
        ack = [e for e in final["events"] if e["meta"].get("guide_seq") == guides[0]["seq"]]
        print("Codex 已按执行中发出的引导生成计划，" + ("原生轮次已确认接收" if ack else "已从同一任务重新生成"), flush=True)
    else:
        assert bound and bound == final["session"]["antigravity_session"]
        assert any(e["actor"] == "gemini" and e["kind"] == "message" and "会话可以继续" in e["body"] for e in final["events"])
        print("Antigravity 已在原会话和原任务接收引导并修改回复", flush=True)


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
                    async with asyncio.TaskGroup() as controls:
                        controls.create_task(stop_native(engine, engine.create_session(**options, mode="plan"),
                                             "根据 pyproject.toml 写一份简短计划，说明如何核对命令行入口。", "Codex 已开始生成", window))
                        controls.create_task(stop_native(engine, engine.create_session(**options),
                                             "根据项目配置梳理跨平台发布过程，先分析要执行哪些任务。", "Antigravity 正在处理请求"))
                finally:
                    window.close()
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
                engine.continue_session(sid)
                pending = await settled(engine, sid, 120, return_on_approval=True)
                renewed, = pending["approvals"]
                assert renewed["id"] != approval["id"] and renewed["job"] == approval["job"]
                assert renewed["detail"] == approval["detail"]
                engine.submit(sid, "取消文件写入。保持文件内容原样，仅在答复中写明「已保留原文件」。", new_id())
                final = await settled(engine, sid, 300, return_on_approval=True)
                assert not final["approvals"] and engine.store.job(renewed["job"])["status"] == "done"
                assert engine.store.one("SELECT decision FROM approvals WHERE id=?", renewed["id"])["decision"] == "expired"
                assert target.read_text(encoding="utf-8") == name + "\n"
                assert "已保留原文件" in engine.store.job(renewed["job"])["draft"]
                print("原任务已重新请求批准，并按运行中的引导撤回写入", flush=True)
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
