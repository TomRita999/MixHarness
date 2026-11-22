import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from google.genai.interactions import CreateModelInteraction
from openai.types.responses import FunctionTool
from openai.types.responses.responses_client_event import ResponseCreate

from mixharness import workspace
from mixharness.app import markdown
from mixharness.engine import Engine
from mixharness.providers import MODELS, Plan, declarations
from mixharness.store import Store, Superseded, new_id


ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".work" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.project = self.base / "project"
        self.project.mkdir()
        shutil.copy(ROOT / "pyproject.toml", self.project)
        self.dbpath = self.base / "sessions.sqlite3"
        self.store = Store(self.dbpath)
        self.addCleanup(lambda: self.store.db.close())
        self.sid = new_id()
        self.store.create(self.sid, self.project, MODELS["gemini"][0], MODELS["openai"][0])

    def start_job(self, kind):
        job = self.store.one("SELECT * FROM jobs WHERE sid=? AND kind=? AND status='ready' ORDER BY rowid DESC LIMIT 1",
                             self.sid, kind)
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
        return job

    def requirements(self, **changes):
        return self.store.apply_plan(self.start_job("plan"), changes, "")

    def test_duplicate_messages_and_outdated_plan(self):
        request_id = new_id()
        text = "请核对当前项目的 Python 依赖。"
        first = self.store.message(self.sid, text, request_id)
        self.assertEqual(first, self.store.message(self.sid, text, request_id))
        old = self.start_job("plan")
        self.store.message(self.sid, "列出运行所需的 Python 版本。", new_id())
        with self.assertRaises(Superseded):
            self.store.apply_plan(old, {"engineering": text}, "")
        self.assertEqual(self.store.session(self.sid)["revision"], 2)
        self.assertEqual(len(self.store.snapshot(self.sid)["events"]), 2)

    def test_plan_revision_requires_explicit_implementation(self):
        store = self.store
        store.change_mode(self.sid, "plan")
        store.message(self.sid, "为发布流程制定计划。", new_id())
        first = self.start_job("proposal")
        with self.assertRaises(ValueError):
            asyncio.run(Engine(store, self.base, self.project).tool(first, {"name": "edit_file"}, None))
        with store.db:
            store.finish(first, "编译项目。", {})
        self.assertEqual([j["kind"] for j in store.snapshot(self.sid)["tasks"]], ["proposal"])
        store.message(self.sid, "加上测试步骤。", new_id())
        with self.assertRaises(ValueError):
            store.implement_plan(self.sid)
        updated = self.start_job("proposal")
        with store.db:
            store.finish(updated, "编译项目，运行单元测试。", {})
        branch = store.fork_session(self.sid, "发布方案的分支")
        self.assertEqual(store.current_proposal(branch["id"]), store.current_proposal(self.sid))
        store.implement_plan(self.sid)
        with self.assertRaises(ValueError):
            store.implement_plan(self.sid)
        state = store.snapshot(self.sid)
        self.assertEqual(state["session"]["mode"], "default")
        self.assertEqual([j["kind"] for j in state["tasks"]], ["proposal", "proposal", "plan"])
        self.assertIn("运行单元测试", state["events"][-1]["body"])

    def test_goal_persists_and_ignores_review_after_pause(self):
        store = self.store
        objective = "核对项目声明的 Python 版本并给出文件依据。"
        store.change_mode(self.sid, "goal", objective=objective)
        self.requirements()
        review = self.start_job("goal_check")
        store.goal_action(self.sid, "pause")
        with self.assertRaises(Superseded):
            store.assess_goal(review, "complete", "过期结果", "")
        store.db.close()
        self.store = store = Store(self.dbpath)
        self.assertEqual(store.session(self.sid)["goal_status"], "paused")
        store.goal_action(self.sid, "resume")
        self.requirements()
        with store.db:
            store.assess_goal(self.start_job("goal_check"), "continue", "缺少版本信息", "读取 pyproject.toml")
        next_plan = self.start_job("plan")
        self.assertEqual(next_plan["payload"]["followup"], "读取 pyproject.toml")
        store.apply_plan(next_plan, {"engineering": objective}, "")
        evidence = workspace.read_source(self.project, {"path": "pyproject.toml"})["text"]
        version = next(line for line in evidence.splitlines() if line.startswith("requires-python"))
        with store.db:
            store.finish(self.start_job("engineering"), version, {})
            store.assess_goal(self.start_job("goal_check"), "complete", version, "")
            store.wake(self.sid)
        state = store.session(self.sid)
        self.assertEqual((state["mode"], state["goal_status"], state["objective"]), ("default", "complete", objective))
        self.assertEqual(state["goal_note"], version)
        self.assertFalse(store.rows("SELECT 1 FROM jobs WHERE status='ready'"))

    def test_goal_stops_after_no_progress_or_planning_failure(self):
        store = self.store
        store.change_mode(self.sid, "goal", objective="确认发行包可用。")
        self.requirements()
        with store.db:
            store.assess_goal(self.start_job("goal_check"), "continue", "没有构建记录", "构建发行包")
        self.requirements()
        with store.db:
            store.assess_goal(self.start_job("goal_check"), "continue", "仍缺少产物", "生成新的发行包")
        self.assertEqual(store.session(self.sid)["goal_status"], "blocked")
        store.goal_action(self.sid, "resume")
        with store.db:
            store.finish(self.start_job("plan"), "客户端不可用", {}, success=False)
        self.assertEqual(store.session(self.sid)["goal_note"], "客户端不可用")
        self.assertFalse(store.rows("SELECT 1 FROM jobs WHERE status='ready'"))

    def test_scope_change_and_persistent_wait(self):
        engineering = "查看 pyproject.toml 中的依赖声明。"
        report = "将依赖整理为中文说明。"
        self.store.message(self.sid, engineering + report, new_id())
        self.requirements(engineering=engineering, report=report)
        engineer = self.start_job("engineering")
        previous_report = self.start_job("report")
        self.store.message(self.sid, "报告读者调整为产品经理。", new_id())
        self.requirements(report="为产品经理编写中文说明。")
        self.store.guard(engineer)
        with self.assertRaises(Superseded):
            self.store.guard(previous_report)
        writer = self.start_job("report")
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET status='waiting' WHERE id=?", (writer["id"],))
        self.store.db.close()
        self.store = Store(self.dbpath)
        self.assertEqual(self.store.job(writer["id"])["status"], "waiting")
        engineer = self.start_job("engineering")
        result = workspace.read_source(self.project, {"path": "pyproject.toml"})
        with self.store.db:
            self.store.finish(engineer, result["text"], {"sha256": result["sha256"]})
        self.assertEqual(self.store.job(writer["id"])["status"], "ready")

    def test_independent_model_choices(self):
        self.store.message(self.sid, "说明项目的启动命令。", new_id())
        self.store.set_models(self.sid, MODELS["gemini"][1], MODELS["openai"][0])
        self.assertEqual(self.start_job("plan")["model"], MODELS["gemini"][1])
        self.store.set_models(self.sid, MODELS["gemini"][1], MODELS["openai"][1])
        s = self.store.session(self.sid)
        self.assertEqual((s["gemini"], s["openai"]), (MODELS["gemini"][1], MODELS["openai"][1]))

    def test_source_research_waits_and_retains_its_evidence(self):
        store, sid = self.store, self.sid
        request = "查阅 pyproject.toml 后说明项目的 CLI 入口。"
        seq = store.message(sid, request, new_id())
        self.requirements(engineering="只读核对 CLI 入口。", engineering_mode="inspect",
                          report="根据 GPT 查到的入口写说明。", report_after_gpt=True)
        research = self.start_job("engineering")
        report = store.one("SELECT * FROM jobs WHERE sid=? AND kind='report'", sid)
        trace = next(e for e in store.snapshot(sid)["events"] if e["kind"] == "requirements")
        self.assertEqual(trace["meta"]["source_messages"], [seq])
        self.assertEqual([item["job"] for item in trace["meta"]["assigned"]], [research["id"], report["id"]])
        self.assertEqual(report["status"], "waiting")
        self.assertTrue(research["payload"]["read_only"])
        engine = Engine(store, self.base, self.project)
        with self.assertRaisesRegex(ValueError, "无法使用工具"):
            asyncio.run(engine.tool(research, {"name": "edit_file"}, self.project))
        self.assertNotIn("edit_file", [d["name"] for d in declarations("engineering", research["payload"])])
        with store.db:
            value = workspace.read_source(self.project, {"path": "pyproject.toml"})
            engine.record_reads(research, "MixHarness", ["pyproject.toml"])
            store.finish(research, value["text"], {"sha256": value["sha256"]})
        self.assertEqual(store.job(report["id"])["status"], "ready")
        self.assertEqual(store.report_wait_status(sid, trace["seq"]), "ready")
        events = store.snapshot(sid)["events"]
        self.assertIsNone(store.report_wait_status(sid, events[-1]["seq"]))
        self.assertIn(("本次任务实际读取", "pyproject.toml"), events[-1]["trace"])

    def test_snapshot_runs_an_actual_process(self):
        shutil.copytree(ROOT / "mixharness", self.project / "mixharness",
                        ignore=shutil.ignore_patterns("__pycache__"))
        parent = self.base / "snapshots"
        version = workspace.snapshot(self.project, parent)
        self.assertEqual(version, workspace.fingerprint(self.project))
        result = asyncio.run(workspace.run_check(parent / version, self.base / "runs",
                                                 ["@python", "-m", "compileall", "-q", "."], invocation=version))
        self.assertEqual(result["exit_code"], 0, result["output"])
        self.assertTrue(Path(result["log"]).is_file())
        self.assertEqual(version, workspace.fingerprint(self.project))
        self.assertFalse(list(self.project.rglob("*.pyc")))
        replay = asyncio.run(workspace.run_check(parent / version, self.base / "runs",
                                                 ["@python", "-m", "compileall", "-q", "."], invocation=version))
        self.assertEqual(replay, result)
        self.assertEqual(len(list((self.base / "runs").rglob("output.log"))), 1)

    def test_manual_file_edit_wins(self):
        source = workspace.read_source(self.project, {"path": "pyproject.toml"})
        actual = (self.project / "pyproject.toml").read_text(encoding="utf-8") + "\n# 已使用 Python " + sys.version.split()[0] + " 检查\n"
        workspace.edit_source(self.project, {"path": "pyproject.toml", "expected_sha256": source["sha256"],
                                            "content": actual})
        with self.assertRaisesRegex(ValueError, "重新读取"):
            workspace.edit_source(self.project, {"path": "pyproject.toml", "expected_sha256": source["sha256"],
                                                "content": source["text"]})
        self.assertEqual((self.project / "pyproject.toml").read_text(encoding="utf-8"), actual)
        for path in ("../pyproject.toml", ".ENV", ".env.local"):
            with self.assertRaises(ValueError):
                workspace.project_path(self.project, path)

    def test_markdown_renders_code_and_escapes_html(self):
        rendered = markdown((ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn("<h1>MixHarness</h1>", rendered)
        self.assertIn("<pre>", rendered)
        self.assertNotIn("<script>", markdown("<script>"))

    def test_tools_and_plan_match_official_sdk_types(self):
        requirements = "语法验证报告需要记录源文件版本和真实退出码。"
        for kind in ("report", "section"):
            request = CreateModelInteraction.model_validate({
                "model": MODELS["gemini"][0], "input": requirements, "tools": declarations(kind),
                "generation_config": {"thinking_level": "medium"},
            })
            self.assertIn("request_verification", [tool.name for tool in request.tools])
            self.assertEqual(request.generation_config.thinking_level, "medium")
        CreateModelInteraction.model_validate({
            "model": MODELS["gemini"][0], "input": requirements,
            "response_format": {"type": "text", "mime_type": "application/json", "schema": Plan.model_json_schema()},
        })
        for kind in ("engineering", "verification"):
            tools = [FunctionTool.model_validate(dict(tool, strict=False)) for tool in declarations(kind)]
            event = ResponseCreate.model_validate({"type": "response.create", "model": MODELS["openai"][0],
                                                   "input": requirements, "reasoning": {"effort": "high"}, "tools": tools})
            self.assertEqual(event.reasoning.effort, "high")
            self.assertEqual([t.name for t in event.tools], [t["name"] for t in declarations(kind)])

    def test_source_change_reopens_verification_before_publication(self):
        request = "核对 pyproject.toml 中的 Python 版本要求并写入报告。"
        self.store.message(self.sid, request, new_id())
        self.requirements(verification=request, report=request)
        engine = Engine(self.store, self.base, self.project)
        check = self.start_job("verification")
        source = asyncio.run(engine.verify_source(check))
        observed = workspace.read_source(source, {"path": "pyproject.toml"})
        with self.store.db:
            self.store.finish(check, observed["text"], {"source": source.name})
        shutil.copy(ROOT / "README.md", self.project)
        report = self.start_job("report")
        asyncio.run(engine.check_report_sources(report, self.store.session(self.sid)))
        self.assertEqual(self.store.job(check["id"])["status"], "obsolete")
        self.assertTrue(self.store.pending_work(self.sid))
        current = self.start_job("verification")
        new_source = asyncio.run(engine.verify_source(current))
        self.assertNotEqual(source.name, new_source.name)
        self.assertEqual(new_source.name, workspace.fingerprint(self.project))
