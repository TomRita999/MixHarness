import asyncio
import shutil
from pathlib import Path

from mixharness.engine import Engine
from mixharness.providers import MODELS
from mixharness.store import Store, new_id


async def main():
    root = Path(__file__).resolve().parents[1]
    home = root / ".work" / "live-check" / new_id()
    source = home / "project"
    source.mkdir(parents=True)
    shutil.copytree(root / "mixharness", source / "mixharness", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(root / "pyproject.toml", source)
    store = Store(home / "sessions.sqlite3")
    engine = Engine(store, home, root)
    if not all(engine.keys.values()):
        store.db.close()
        raise SystemExit("在线验证需要实际的 GEMINI_API_KEY 和 OPENAI_API_KEY。")
    sid = new_id()
    store.create(sid, source, MODELS["gemini"][0], MODELS["openai"][0], permission="auto")
    store.message(sid, "请 Gemini 写一份当前源码的 Python 语法检查报告。由写作者调用 request_verification，"
                  "请 GPT 实际运行 python-compile，取得退出码与快照版本后写出报告。", new_id())
    worker = asyncio.create_task(engine.serve())
    try:
        async with asyncio.timeout(600):
            while True:
                if worker.done():
                    worker.result()
                failed = store.rows("SELECT title,error FROM jobs WHERE sid=? AND status='failed'", sid)
                if failed:
                    raise RuntimeError(failed)
                reports = store.rows("SELECT id FROM jobs WHERE sid=? AND kind='report' AND status='done'", sid)
                if reports:
                    break
                await asyncio.sleep(0.4)
        report = reports[-1]["id"]
        calls = store.rows("SELECT * FROM calls WHERE job=? AND name='request_verification'", report)
        if not calls or not all(c["child"] and c["result"] and c["result"].get("artifact_id") for c in calls):
            raise RuntimeError("报告缺少已完成的验证调用")
        logs = store.rows("SELECT * FROM artifacts WHERE job=?", calls[-1]["child"])
        actual = [a for a in logs if a["meta"]["kind"] == "run_check" and a["meta"]["exit_code"] == 0]
        if not actual or not Path(actual[-1]["meta"]["log"]).is_file():
            raise RuntimeError("缺少实际本地命令的成功记录")
        print("模型交接和报告续写通过，完整会话与原始验证记录位于", home)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        store.db.close()


if __name__ == "__main__":
    asyncio.run(main())
