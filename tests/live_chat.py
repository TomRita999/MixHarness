import asyncio
import secrets
import tempfile
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from qasync import QEventLoop

from mixharness.clients import is_dot_model
from mixharness.desktop import Window
from mixharness.desktop_style import load_icons
from mixharness.engine import open_engine
from tests.qt_app import application


ROOT = Path(__file__).resolve().parents[1]


async def exercise(app, engine, work):
    settings = QSettings(str(work / "desktop.ini"), QSettings.Format.IniFormat)
    settings.setValue("backend", "clients")
    writers = engine.client_catalogs["gemini"]
    await writers.refresh()
    assert writers.labels, writers.error
    model = next((m for m in writers.labels if m.startswith("gemini-3.8-flash") and m.endswith("low")), next(iter(writers.labels)))
    settings.setValue("client-models/gemini", model)
    window = Window(engine, settings)
    window.show()
    worker = asyncio.create_task(engine.serve())
    left = Qt.MouseButton.LeftButton

    def choose(title):
        QTest.mouseClick(window.recipient_button, left)
        menu = app.activePopupWidget()
        action = next(a for a in menu.actions() if a.text() == title)
        QTest.mouseClick(menu, left, pos=menu.actionGeometry(action).center())

    def ask(text):
        window.editor.setPlainText(text)
        assert window.send_button.isEnabled(), window.work_status.text()
        QTest.keyClick(window.editor, Qt.Key.Key_Return)

    async def replies(count, *, key):
        streamed = False
        async with asyncio.timeout(240):
            while True:
                await asyncio.sleep(.12)
                window.refresh()
                if worker.done():
                    worker.result()
                tasks = window.state["tasks"]
                failures = [t["error"] for t in tasks if t["status"] == "failed"]
                assert not failures, failures
                streamed |= any(row.body.isVisible() and row.text for row in window.timeline.draft_rows.values())
                messages = [e for e in window.state["events"] if e["kind"] == "message" and e["actor"] != "user"]
                if len(messages) == count:
                    assert not window.timeline.draft_rows
                    assert key in messages[-1]["body"], messages[-1]["body"]
                    return messages, streamed
    try:
        choose("Gemini")
        token = secrets.token_hex(4)
        ask("请记下这次对话的编号 " + token + "，在答复里重复编号。")
        sid = window.sid
        assert not engine.store.session(sid)["openai"]
        messages, streamed = await replies(1, key=token)
        assert messages[-1]["actor"] == "gemini"
        window.apply_backend(sid, "api")
        window.apply_backend(sid, "clients")
        assert engine.store.session(sid)["backend"] == "clients"
        await asyncio.gather(*window.catalog_tasks.values())
        assert engine.store.session(sid)["openai"] in engine.client_catalogs["openai"].labels
        choose("GPT")
        ask("只回复刚才 Gemini 说的对话编号。")
        messages, _ = await replies(2, key=token)
        assert messages[-1]["actor"] == "openai"
        choose("Gemini")
        ask("上一位回答者是哪个模型？附上它回复的编号。")
        messages, more_stream = await replies(3, key=token)
        assert any(name in messages[-1]["body"].upper() for name in ("GPT", "OPENAI")), messages[-1]["body"]
        assert window.sid == sid
        assert [j["kind"] for j in window.state["tasks"]] == ["gemini_chat", "gpt_chat", "gemini_chat"]
        assert window.state["session"]["revision"] == 0
        for theme in ("light", "dark"):
            window.set_theme(theme)
            await asyncio.sleep(.1)
            assert window.grab().save(str(work / (theme + ".png")))
        choose("双模型协作")
        assert not window.recipient and window.sid == sid
        print("同一会话 Gemini → GPT → Gemini 三轮真实应答通过。Gemini 流式显示", bool(streamed or more_stream),
              "；窗口截图目录", work, flush=True)

        if any(is_dot_model(m) for m in engine.client_catalogs["openai"].labels):
            window.apply_backend(sid, "api")
            follow_up = "请只回复刚才对话中的编号。"
            window.editor.setPlainText(follow_up)
            QTest.mouseClick(window.dot_chat, left)
            await window.backend_task
            session = engine.store.session(sid)
            dot = session["openai"]
            assert is_dot_model(dot) and session["backend"] == "clients" and window.sid == sid
            assert window.recipient == "openai" and window.editor.toPlainText() == follow_up
            assert "GPT-dot" in window.editor.placeholderText() and window.send_button.isEnabled()
            QTest.keyClick(window.editor, Qt.Key.Key_Return)
            messages, _ = await replies(4, key=token)
            assert messages[-1]["actor"] == "openai" and window.state["tasks"][-1]["model"] == dot
            choose("Gemini")
            QTest.mouseClick(window.dot_chat, left)
            await window.backend_task
            assert window.sid == sid and window.pickers["openai"].model == dot
            assert len(window.state["tasks"]) == 4
            window.new_standalone()
            window.backend_box.setCurrentIndex(window.backend_box.findData("api"))
            window.editor.setPlainText(follow_up)
            QTest.mouseClick(window.dot_chat, left)
            await window.backend_task
            assert window.sid is None and window.recipient == "openai" and window.send_button.isEnabled()
            assert is_dot_model(window.pickers["openai"].model) and window.editor.toPlainText() == follow_up
            variant = next((g for g in engine.client_catalogs["openai"].groups
                            if is_dot_model(g["id"]) and "high" in g["choices"] and window.pickers["openai"].accepts(g)), None)
            if variant:
                window.choose_model("openai", variant["id"], "high")
                QTest.mouseClick(window.dot_chat, left)
                await window.backend_task
                assert (window.pickers["openai"].model, window.pickers["openai"].effort) == (variant["id"], "high")
            print("GPT-dot 侧栏入口在 API 会话和新草稿中均可使用，实际回复模型", dot, flush=True)
    finally:
        window.close()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


def main():
    scratch = ROOT / ".work"
    scratch.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=scratch, prefix="chat-ui-"))
    app = application()
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    app.setProperty("iconFamily", load_icons())
    with open_engine(work, work / "data") as engine, QEventLoop(app) as loop:
        loop.run_until_complete(exercise(app, engine, work))


if __name__ == "__main__":
    main()
