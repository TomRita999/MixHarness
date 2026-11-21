import asyncio
import shutil
import tempfile
from pathlib import Path

from mixharness import clients
from mixharness.catalog import MODELS
from mixharness.commands import settled
from mixharness.engine import open_engine
from mixharness.store import new_id


ROOT = Path(__file__).resolve().parents[1]


async def client_ids():
    pages = await asyncio.gather(*(clients.chats(c) for c in ("codex", "antigravity")))
    return {page["source"]: {r["id"] for r in page["data"]} for page in pages}


async def main():
    scratch = ROOT / ".work"
    scratch.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=scratch, prefix="client-verification-"))
    before = await client_ids()
    with open_engine(work, work / ".mixharness") as engine:
        await asyncio.gather(*(catalog.refresh() for catalog in engine.client_catalogs.values()))
        for catalog in engine.client_catalogs.values():
            assert catalog.labels, catalog.error
        writers = engine.client_catalogs["gemini"].labels
        writing = "gemini-3.8-flash-low" if "gemini-3.8-flash-low" in writers else next(iter(writers))
        coding = next(iter(engine.client_catalogs["openai"].labels))
        group = next(g for g in engine.client_catalogs["openai"].groups if g["id"] == coding)
        coding_effort = "medium" if "medium" in group["choices"] else group["default"]
        root = work / "source"
        root.mkdir()
        shutil.copy2(ROOT / "mixharness/check_runner.py", root)
        api_models = {p: values[0] for p, values in MODELS.items()}
        sid = engine.create_session(**api_models, directory=root)["id"]
        engine.save_settings({"context_window": 32000}, sid)
        worker = asyncio.create_task(engine.serve())
        try:
            request = ("请撰写一段项目语法核验说明。写报告时使用 request_verification 请 GPT 实际执行 python-compile，"
                       "拿到退出码和日志再完成报告。无需开发任务。")
            engine.submit(sid, request, new_id())
            if not engine.keys["gemini"]:
                pending = await settled(engine, sid, 10)
                assert pending["tasks"][-1]["status"] == "needs_key"
            engine.switch_backend(sid, "clients", {"gemini": writing, "openai": coding}, {"openai": coding_effort})
            state = await settled(engine, sid, 360, return_on_approval=True)
            while state["approvals"]:
                for request in state["approvals"]:
                    assert request["source"] == "MixHarness" and request["title"] == "运行 python-compile", request["title"]
                    assert str(root) in request["detail"]
                    engine.decide(sid, request["id"], True)
                    print("已核对并批准实际语法验证", flush=True)
                state = await settled(engine, sid, 360, return_on_approval=True)
            assert all(j["status"] in ("done", "obsolete") for j in state["tasks"]), [
                j["error"] for j in state["tasks"] if j["status"] == "failed"]
            assert any(a["meta"].get("exit_code") == 0 and Path(a["meta"]["log"]).is_file() for a in state["artifacts"])
            assert engine.store.rows("SELECT id FROM approvals WHERE sid=? AND decision='allow'", sid)
            assert any(e["kind"] == "verification" for e in state["events"])
            assert any(t["kind"] == "verification" and t["effort"] == coding_effort for t in state["tasks"])
            assert all(t["effort"] == state["session"]["efforts"]["gemini"] for t in state["tasks"]
                       if t["kind"] in ("plan", "report") and t["status"] == "done")
            assert any(a["meta"].get("kind") == "report" for a in state["artifacts"])
            bound = state["session"]["antigravity_session"]
            engine.submit(sid, "保留已有核验结果，把报告改成一句给开发者看的说明。", new_id())
            updated = await settled(engine, sid, 300)
            assert bound and updated["session"]["antigravity_session"] == bound
            assert updated["session"]["revision"] == 2 and all(j["status"] in ("done","obsolete") for j in updated["tasks"])
            assert all(updated["session"]["backend_models"]["api"][p] == m for p, m in api_models.items())
            engine.switch_backend(sid, "api", api_models)
            remembered = engine.store.session(sid)["backend_models"]["clients"]
            assert (remembered["gemini"], remembered["openai"]) == (writing, coding)
            assert remembered["efforts"] == updated["session"]["efforts"]
            after = await client_ids()
            for source in before:
                print(clients.NAMES[source], "可见历史新增数", len(after[source] - before[source]))
            assert bound not in after["antigravity"]
            print("两家本机客户端完成验证和续写，记录位于", work)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
