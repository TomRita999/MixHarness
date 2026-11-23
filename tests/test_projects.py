import asyncio
import hashlib
import shutil
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path

from filelock import FileLock

from mixharness import workspace
from mixharness.catalog import MODELS
from mixharness.engine import open_engine
from mixharness.store import new_id


ROOT = Path(__file__).resolve().parents[1]


class ProjectTests(unittest.TestCase):
    def test_stop_running_check_terminates_the_project_command(self):
        scratch = ROOT / ".work" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            shutil.copy2(ROOT / "pyproject.toml", source)
            shutil.copy2(ROOT / "tests/check_project_digest.py", source)
            expected = hashlib.sha256((source / "pyproject.toml").read_bytes()).digest()
            runs = root / "runs"

            async def exercise():
                connected = asyncio.get_running_loop().create_future()
                def accept(reader, writer):
                    connected.set_result((reader, writer))

                async with await asyncio.start_server(accept, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    command = ["@python", "check_project_digest.py", str(port)]
                    task = asyncio.create_task(workspace.run_check(source, runs, command, timeout=120))
                    try:
                        async with asyncio.timeout(30):
                            done, _ = await asyncio.wait((task, connected), return_when=asyncio.FIRST_COMPLETED)
                            if task in done:
                                self.fail(str(await task))
                            reader, writer = connected.result()
                            self.assertEqual(await reader.readexactly(len(expected)), expected)
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                            with suppress(ConnectionResetError):
                                self.assertEqual(await reader.read(1), b"")
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        if connected.done():
                            _, writer = connected.result()
                            writer.close()
                            with suppress(ConnectionResetError):
                                await writer.wait_closed()
                self.assertFalse(list(runs.rglob("result.json")))
            asyncio.run(exercise())

    def test_named_projects_without_source_folders(self):
        scratch = ROOT / ".work" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            library = Path(temp)
            with open_engine(library, library / ".mixharness") as engine:
                st = engine.store
                project = st.save_project("研究笔记", [])
                pid = project["id"]
                root = Path(project["folders"][0]["path"])
                self.assertTrue(root.is_dir())
                self.assertEqual(st.sessions(), [])
                models = (MODELS["gemini"][0], MODELS["openai"][0])
                a = engine.create_session(*models, project_id=pid)
                b = engine.create_session(*models, project_id=pid)
                self.assertEqual((a["workspace"], b["workspace"]), (str(root), str(root)))
                shutil.copy(ROOT / "README.md", root)
                source = library / "实现"
                source.mkdir()
                st.save_project("研究与实现", [source, root], pid)
                self.assertEqual(st.session(a["id"])["workspace"], str(source))
                self.assertEqual(engine.sources(st.session(b["id"])).locate("项目文件/README.md"), (root, "README.md"))
                st.archive_project(pid)
                self.assertEqual(st.projects(), [])
                self.assertEqual(st.projects(archived=True)[0]["id"], pid)
                st.archive_project(pid, False)
                self.assertEqual({s["id"] for s in st.sessions()}, {a["id"], b["id"]})

    def test_delete_during_a_local_verification(self):
        scratch = ROOT / ".work" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            library = Path(temp)
            with open_engine(library, library / ".mixharness") as engine:
                st = engine.store
                project = st.save_project("验证命令", [])
                source = Path(project["folders"][0]["path"])
                code = source / "workspace.py"
                shutil.copy(ROOT / "mixharness/workspace.py", code)
                models = (MODELS["gemini"][0], MODELS["openai"][0])
                sid = engine.create_session(*models, project_id=project["id"])["id"]
                other = engine.create_session(*models, project_id=project["id"])["id"]
                version = workspace.snapshot(source, engine.home / "snapshots" / sid)
                snapshot = engine.home / "snapshots" / sid / version
                runs = engine.home / "runs" / sid
                invocation = new_id()
                (runs / invocation).mkdir(parents=True)
                command = engine.checks(engine.sources(st.session(sid)))["项目文件"]["python-compile"]
                with st.db:
                    job = st.enqueue(st.session(sid), "verification")
                    st.db.execute("UPDATE jobs SET status='running' WHERE id=?", (job,))
                async def exercise():
                    with FileLock(runs / invocation / "execution.lock"):
                        task = asyncio.create_task(engine.file_operation(sid, workspace.run_check(
                            snapshot, runs, command, invocation=invocation)))
                        engine.active[job] = task
                        worker = asyncio.create_task(engine.serve())
                        await asyncio.sleep(0)
                        engine.delete_session(sid)
                        await asyncio.gather(task, return_exceptions=True)
                        self.assertTrue(snapshot.is_dir())
                        self.assertEqual([s["id"] for s in st.sessions()], [other])
                    try:
                        async with asyncio.timeout(10):
                            while st.rows("SELECT sid FROM session_cleanup"):
                                await asyncio.sleep(0.03)
                        self.assertFalse(worker.done())
                        self.assertFalse(snapshot.exists())
                        self.assertFalse(runs.exists())
                        self.assertEqual(code.read_bytes(), (ROOT / "mixharness/workspace.py").read_bytes())
                        self.assertEqual(st.rows("PRAGMA foreign_key_check"), [])
                    finally:
                        worker.cancel()
                        await asyncio.gather(worker, return_exceptions=True)
                asyncio.run(exercise())

    def test_both_roots_participate_in_tools_snapshots_and_checks(self):
        scratch = ROOT / ".work" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            project = Path(temp)
            frontend, backend = project / "desktop", project / "engine"
            frontend.mkdir()
            backend.mkdir()
            shutil.copy(ROOT / "mixharness/desktop_style.py", frontend)
            shutil.copy(ROOT / "mixharness/store.py", backend)
            with open_engine(project, project / ".mixharness") as engine:
                pid = engine.store.open_project(frontend)["id"]
                self.assertEqual(engine.store.attach_folder(pid, backend), engine.store.attach_folder(pid, backend))
                session = engine.create_session(MODELS["gemini"][0], MODELS["openai"][0], project_id=pid, permission="auto")
                sid = session["id"]
                sources = engine.sources(session)
                engine.submit(sid, "验证两个来源目录中的 Python 文件。", new_id())
                plan = engine.store.one("SELECT * FROM jobs WHERE sid=? AND kind='plan'", sid)
                engine.set_status(plan, "running")
                engine.store.apply_plan(plan, {"verification": "编译两个目录中的实际源文件。"}, "")
                job = engine.store.one("SELECT * FROM jobs WHERE sid=? AND kind='verification'", sid)
                engine.set_status(job, "running")
                snapshot = asyncio.run(engine.verify_source(job))
                self.assertEqual(snapshot.name, workspace.fingerprint(sources))
                self.assertEqual((snapshot / "desktop/desktop_style.py").read_bytes(), (frontend / "desktop_style.py").read_bytes())
                self.assertEqual((snapshot / "engine/store.py").read_bytes(), (backend / "store.py").read_bytes())
                self.assertEqual(sources.locate("engine/store.py"), (backend, "store.py"))
                with self.assertRaises(ValueError):
                    folder, path = sources.locate("desktop/../engine/store.py")
                    workspace.project_path(folder, path)
                calls = [{"cid": new_id(), "name": "read_file", "args": {"path": "engine/store.py"}},
                         {"cid": new_id(), "name": "run_check", "args": {"name": "python-compile", "folder": "engine"}}]
                with engine.store.db:
                    engine.store.save_turn(job, new_id(), 0, "", calls, backend=session["backend"])
                job = engine.store.job(job["id"])
                for call in engine.store.calls(job):
                    asyncio.run(engine.tool(job, call, sources.at_snapshot(snapshot)))
                read, run = [c["result"] for c in engine.store.calls(job)]
                self.assertEqual(read["sha256"], workspace.digest((backend / "store.py").read_bytes()))
                self.assertEqual(run["exit_code"], 0)
                self.assertEqual(run["folder"], "engine")
                run_copy = Path(run["log"]).parent / "project"
                self.assertTrue((run_copy / "desktop/desktop_style.py").is_file())
                self.assertTrue(list((run_copy / "engine").rglob("store.*.pyc")))
                shutil.copy(ROOT / "mixharness/providers.py", backend)
                self.assertNotEqual(snapshot.name, workspace.fingerprint(sources))
