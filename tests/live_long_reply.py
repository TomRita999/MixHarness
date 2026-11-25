import asyncio
import secrets
import tempfile
from pathlib import Path

from mixharness.commands import dispatch
from mixharness.engine import open_engine
from mixharness.store import new_id


async def main():
    scratch = Path(__file__).resolve().parents[1] / ".work"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch, prefix="long-reply-") as directory:
        work = Path(directory)
        project = work / "project"
        project.mkdir()
        receipt = project / "receipt.txt"
        with open_engine(project, work / "library") as engine:
            catalog = engine.client_catalogs["openai"]
            await catalog.refresh()
            model = "gpt-6.2-astra-dot"
            assert model in catalog.labels, catalog.error or catalog.labels
            sid = engine.create_session("", model, project, recipient="openai", backend="clients")["id"]
            engine.submit(sid, "请在当前项目等待 receipt.txt 出现并包含文字。测试程序将在十分钟后写入回执。"
                          "请用只读终端命令定期检查文件，在同一轮里等到它可读，然后读取并原样回复回执内容。",
                          new_id(), recipient="openai")
            worker = asyncio.create_task(engine.serve())
            waiter = asyncio.create_task(dispatch(engine, {"command": "wait", "id": sid}))
            try:
                job, = engine.store.active_jobs(sid)
                async with asyncio.timeout(90):
                    while engine.activity.get(job["id"]) != "Codex 已开始生成":
                        if waiter.done():
                            raise AssertionError(waiter.result()["tasks"])
                        await asyncio.sleep(.1)
                loop = asyncio.get_running_loop()
                start = loop.time()
                print(model, "已开始生成，正在等待项目回执", flush=True)
                # 在旧版单轮七分钟和命令行十分钟两个截止点之后才生成回执。
                done, _ = await asyncio.wait((waiter,), timeout=610)
                if done:
                    raise AssertionError(waiter.result()["tasks"])
                assert engine.store.job(job["id"])["status"] == "running"
                value = secrets.token_hex(12)
                receipt.write_text(value, encoding="utf-8")
                state = await asyncio.wait_for(waiter, 120)
                task, = state["tasks"]
                reply, = [e for e in state["events"] if e["kind"] == "message" and e["actor"] == "openai"]
                assert task["status"] == "done" and value in reply["body"], (task, reply)
                print(f"{model} 单轮 {loop.time() - start:.1f} 秒，已从聊天记录核对回执", flush=True)
            finally:
                waiter.cancel()
                worker.cancel()
                await asyncio.gather(waiter, worker, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
