import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import frontmatter

from mixharness.agent_settings import (AUTO_CLIENT_CONTEXT_WINDOW, AgentSettings, compact_context,
                                       encode_context, shared_input, skill_catalog, tokens)
from mixharness.catalog import MODELS
from mixharness.commands import dispatch
from mixharness.engine import open_engine
from mixharness.store import new_id


ROOT = Path(__file__).resolve().parents[1]


class AgentTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".work/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / ".mixharness"

    def test_stopped_goal_survives_restart_and_guidance_is_idempotent(self):
        models = {p: names[0] for p, names in MODELS.items()}
        with open_engine(self.root, self.home) as engine:
            sid = engine.create_session(**models, mode="goal", directory=self.root)["id"]
            other = engine.create_session(**models, directory=self.root)["id"]
            engine.submit(sid, "核对 Python 项目标注的最低版本。", new_id())
            engine.submit(other, "整理依赖项。", new_id())
            jid = engine.store.active_jobs(sid)[0]["id"]
            self.assertEqual(asyncio.run(engine.stop_session(sid)), 1)
            self.assertEqual(asyncio.run(engine.stop_session(sid)), 0)
            self.assertEqual(engine.store.job(jid)["status"], "stopped")
        with open_engine(self.root, self.home) as engine:
            self.assertTrue(engine.store.session(sid)["paused_work"])
            self.assertIsNone(engine.store.session(other)["paused_work"])
            self.assertEqual(engine.continue_session(sid), 1)
            self.assertEqual(engine.continue_session(sid), 0)
            session = engine.store.session(sid)
            self.assertEqual((session["mode"], session["goal_status"], session["planned"]), ("goal", "active", 0))
            self.assertEqual(engine.store.active_jobs(sid)[0]["id"], jid)
            text, request = "同时核对 Windows 安装说明。", new_id()
            seq, changed = engine.submit(sid, text, request)
            self.assertTrue(changed)
            self.assertEqual(engine.submit(sid, text, request), (seq, False))
            second, _ = engine.submit(sid, "在最后列出相关文件名。", new_id())
            self.assertEqual(engine.store.job(jid)["guide_seq"], second)
            self.assertEqual(engine.store.active_jobs(other)[0]["guide_seq"], 0)
            self.assertEqual(engine.store.session(sid)["revision"], 1)
            async def stop_and_send():
                stop = asyncio.create_task(engine.stop_session(sid))
                await asyncio.sleep(0)
                engine.submit(sid, "继续", new_id())
                await stop
            asyncio.run(stop_and_send())
            self.assertEqual(engine.store.active_jobs(sid)[0]["id"], jid)
            engine.submit(sid, "改为只分析依赖。", new_id(), intent="revise")
            self.assertEqual(engine.store.job(jid)["status"], "obsolete")
            self.assertEqual(engine.store.session(sid)["revision"], 2)

    def test_terminal_commands_resume_fork_and_persistence(self):
        content = "请检查项目的构建命令。"
        lines = ["/", "/new", "/permissions auto", "/effort openai high", "/context 8192", content,
                 "/compact", "/context", "/rename 项目检查", "/fork 构建说明", "/exit"]
        env = dict(os.environ)
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "MIXHARNESS_WORKSPACE"):
            env.pop(name, None)
        command = [sys.executable, "-m", "mixharness.cli", "--library", str(self.root)]
        terminal = subprocess.run([*command, "chat", "--backend", "api", "--standalone"], input="\n".join(lines) + "\n",
                                  cwd=ROOT, env=env, text=True, encoding="utf-8", capture_output=True, check=True, timeout=30)
        self.assertFalse(terminal.stderr, terminal.stderr)
        self.assertIn("/review", terminal.stdout)
        with open_engine(self.root, self.home) as engine:
            sessions = {s["title"]: engine.store.session(s["id"]) for s in engine.store.sessions()}
            original, branch = sessions["项目检查"], sessions["构建说明"]
            self.assertEqual(original["permission"], "auto")
            self.assertEqual(branch["permission"], "ask")
            self.assertEqual(original["efforts"], branch["efforts"])
            self.assertEqual(original["efforts"]["openai"], "high")
            self.assertEqual(engine.settings.context_window, 8192)
            self.assertIn(content, original["context_summary"])
            self.assertEqual(engine.store.snapshot(branch["id"])["events"][0]["body"], content)
            self.assertFalse(engine.store.snapshot(branch["id"])["tasks"])
        output = self.root / "导出会话.md"
        exported = "command", "/export " + str(output), "--session", branch["id"]
        subprocess.run([*command, *exported], env=env, cwd=ROOT, check=True, capture_output=True)
        self.assertIn(content, output.read_text(encoding="utf-8"))
        self.assertNotEqual(subprocess.run([*command, *exported], env=env, cwd=ROOT, capture_output=True).returncode, 0)
        restored = subprocess.run([*command, "chat", original["id"]], input="/status\n/exit\n", env=env,
                                  cwd=ROOT, text=True, encoding="utf-8", check=True, capture_output=True)
        self.assertIn("帮我批准", restored.stdout)

    def test_context_budget_preserves_requirements_and_recent_sources(self):
        paths = sorted((ROOT / "mixharness").glob("*.py"))
        history = [{"path": p.name, "text": p.read_text(encoding="utf-8")[:3200]} for p in paths]
        requirement = "根据当前源码，检查桌面程序启动时的配置读取过程。"
        context = {"当前需求": requirement, "共享记录": history}
        limited = compact_context(context, 4096)
        self.assertLessEqual(tokens(limited), 4096)
        remaining = json.loads(limited)
        self.assertEqual(remaining["当前需求"], requirement)
        self.assertLess(len(remaining["共享记录"]), len(history))
        self.assertEqual(remaining["共享记录"][-1], history[-1])
        self.assertEqual(json.loads(compact_context(context, 0)), context)

    def test_project_skill_is_read_from_disk_and_scoped_to_its_folder(self):
        installed = skill_catalog(AgentSettings(), [])
        if not installed:
            self.skipTest("本机未安装可用于测试的真实技能")
        example = min(installed.values(), key=lambda s: Path(s["path"]).stat().st_size)
        source = self.root / "source"
        skill = source / ".agents/skills" / Path(example["path"]).parent.name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        shutil.copy2(example["path"], skill)
        with open_engine(self.root, self.home) as engine:
            pid = engine.store.open_project(source)["id"]
            session = asyncio.run(dispatch(engine, dict(command="new", backend="api", project_id=pid)))
            sid = session["id"]
            saved = asyncio.run(dispatch(engine, dict(command="settings", id=sid, enable_skill=[example["name"]],
                                                     context_window="64000")))
            self.assertIn(example["name"], saved["skills"])
            self.assertNotIn(example["name"], engine.settings_for([self.root]).skills)
            engine.submit(sid, "检查当前项目的说明文件。", new_id())
            job = engine.store.one("SELECT j.*,s.backend FROM jobs j JOIN sessions s ON s.id=j.sid WHERE j.sid=?", sid)
            request, _, payload, _ = engine.turn_input(job, engine.store.session(sid), [])
            self.assertEqual(request["context_window"], 64000)
            selected = json.loads(payload)["用户启用的技能"]
            self.assertEqual(selected[0]["使用说明"], frontmatter.load(str(skill)).content)
            self.assertEqual(selected[0]["目录"], str(skill.parent))
        with open_engine(self.root, self.home) as engine:
            self.assertEqual(engine.settings_for([source]).skills, [example["name"]])
            with self.assertRaises(ValueError):
                engine.save_settings({"context_window": 1})
            self.assertEqual(engine.settings.context_window, 64000)

    def test_tool_result_budget_preserves_the_call_and_actual_file_hash(self):
        from mixharness.workspace import read_source
        evidence = read_source(ROOT, {"path": "mixharness/engine.py"})
        cid = new_id()
        results, text = shared_input({"共享记录": [], "当前需求": "核对引擎源文件"}, [
            {"cid": cid, "name": "read_file", "result": evidence}], 4096)
        self.assertEqual(results[0]["cid"], cid)
        self.assertEqual(results[0]["result"]["sha256"], evidence["sha256"])
        self.assertLessEqual(tokens(encode_context(results[0]["result"])) + tokens(text), 4096)

    def test_write_requires_scoped_one_time_approval(self):
        docs = self.root / "source"
        docs.mkdir()
        path = docs / "README.md"
        shutil.copy2(ROOT / "README.md", path)
        original = path.read_text(encoding="utf-8")
        from mixharness.workspace import digest
        with open_engine(self.root, self.home) as engine:
            store = engine.store
            pid = store.open_project(docs)["id"]
            session = asyncio.run(dispatch(engine, dict(command="new", backend="api", project_id=pid)))
            other = asyncio.run(dispatch(engine, dict(command="new", backend="api", project_id=pid)))
            sid = session["id"]
            sources = engine.sources(session)
            with store.db:
                jid = store.enqueue(session, "engineering", {"task": "更新本地项目的说明标题"})
            job = store.job(jid)
            engine.set_status(job, "running")
            replacement = original.replace("# MixHarness", "# MixHarness CLI", 1)
            args = {"path": "README.md", "expected_sha256": digest(path.read_bytes()), "content": replacement}

            async def exercise(allow):
                with store.db:
                    store.save_turn(job, new_id(), 0, "", [{"cid": new_id(), "name": "edit_file", "args": args}], backend="api")
                current = store.job(jid)
                call = store.calls(current)[0]
                operation = asyncio.create_task(engine.tool(current, call, sources))
                try:
                    async with asyncio.timeout(4):
                        while not store.approvals(sid):
                            await asyncio.sleep(0.01)
                    request = store.approvals(sid)[0]
                    self.assertEqual(path.read_text(encoding="utf-8"), original)
                    self.assertIn(str(path), request["detail"])
                    with self.assertRaises(ValueError):
                        engine.decide(other["id"], request["id"], True)
                    directive = "/approve " if allow else "/deny "
                    await dispatch(engine, dict(command="command", id=sid, line=directive + request["id"]))
                    if allow:
                        await operation
                    else:
                        with self.assertRaises(ValueError):
                            await operation
                    with self.assertRaises(ValueError):
                        engine.decide(sid, request["id"], True)
                finally:
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
            asyncio.run(exercise(False))
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            asyncio.run(exercise(True))
            self.assertEqual(path.read_text(encoding="utf-8"), replacement)

    def test_clients_auto_context_places_latest_first_and_compacts_history(self):
        with open_engine(self.root, self.home) as engine:
            store = engine.store
            sid = new_id()
            with store.db:
                store.create(sid, str(self.root), "gemini", "gpt", backend="clients")
            chunk = "这是一段用于模拟超长历史记录的中文对话内容，用来验证自动上下文压缩不会把最新消息挤掉。" * 60
            with store.db:
                for i in range(40):
                    store.event(sid, "message", "user" if i % 2 == 0 else "gemini", f"第{i}轮：{chunk}")
            latest = "请只回答：我收到了这条最新消息。"
            engine.submit(sid, latest, new_id(), recipient="gemini")
            chat_job = store.one("SELECT j.*,s.backend FROM jobs j JOIN sessions s ON s.id=j.sid "
                                 "WHERE j.sid=? AND j.kind='gemini_chat' ORDER BY j.rowid DESC LIMIT 1", sid)
            _, _, chat_text, _ = engine.chat_input(chat_job, store.session(sid))
            self.assertLess(chat_text.index('"本次提问"'), chat_text.index('"共享记录"'))
            self.assertLessEqual(tokens(chat_text), AUTO_CLIENT_CONTEXT_WINDOW)
            parsed_chat = json.loads(chat_text)
            self.assertEqual(parsed_chat["本次提问"], latest)
            self.assertLess(len(parsed_chat["共享记录"]), 40)

            plan_msg = "请检查当前仓库里的测试文件并列出清单。"
            engine.submit(sid, plan_msg, new_id())
            plan_job = store.one("SELECT j.*,s.backend FROM jobs j JOIN sessions s ON s.id=j.sid "
                                 "WHERE j.sid=? AND j.kind='plan' ORDER BY j.rowid DESC LIMIT 1", sid)
            _, _, plan_text, _ = engine.turn_input(plan_job, store.session(sid), [])
            self.assertLess(plan_text.index('"待处理用户消息"'), plan_text.index('"共享记录"'))
            self.assertLessEqual(tokens(plan_text), AUTO_CLIENT_CONTEXT_WINDOW)
            parsed_plan = json.loads(plan_text)
            self.assertEqual(parsed_plan["待处理用户消息"], [plan_msg])
            self.assertLess(len(parsed_plan["共享记录"]), 40)

            oversized = {"本次提问": "超长固定内容" * 1500, "共享记录": [{"seq": 1, "text": "旧消息"}]}
            relaxed = json.loads(compact_context(oversized, 128, strict=False))
            self.assertEqual(relaxed["本次提问"], oversized["本次提问"])
            self.assertEqual(relaxed["共享记录"], [])

    def test_antigravity_incremental_seen_and_supervision_loop(self):
        from mixharness.providers import instructions
        with open_engine(self.root, self.home) as engine:
            store = engine.store
            sid = new_id()
            with store.db:
                store.create(sid, str(self.root), "gemini", "gpt", backend="clients")
                store.event(sid, "message", "user", "第一轮旧消息")
                first_seq = store.event(sid, "message", "gemini", "第一轮旧回复")
                store.db.execute("UPDATE sessions SET antigravity_session='ag-1', antigravity_seen=? WHERE id=?",
                                 (first_seq, sid))
            engine.submit(sid, "请给项目加个新功能", new_id())
            plan_job = store.one("SELECT j.*,s.backend FROM jobs j JOIN sessions s ON s.id=j.sid "
                                 "WHERE j.sid=? AND j.kind='plan' ORDER BY j.rowid DESC LIMIT 1", sid)
            _, _, plan_text, _ = engine.turn_input(plan_job, store.session(sid), [])
            parsed = json.loads(plan_text)
            self.assertEqual([e["text"] for e in parsed["共享记录"]], ["请给项目加个新功能"])
            self.assertEqual(parsed["待处理用户消息"], ["请给项目加个新功能"])

            # 监工打回 -> 触发带反馈的返工 -> 第二轮通过 -> 唤醒等待中的报告
            store.db.execute("UPDATE jobs SET status='running' WHERE id=?", (plan_job["id"],))
            store.db.commit()
            store.apply_plan(plan_job, {
                "engineering": "实现功能", "engineering_mode": "develop",
                "report": "写变更总结", "report_after_gpt": True,
                "constraints": ["禁止私自加硬编码超时"], "supervise": True,
            }, "")
            eng = store.one("SELECT * FROM jobs WHERE sid=? AND kind='engineering'", sid)
            rep = store.one("SELECT * FROM jobs WHERE sid=? AND kind='report'", sid)
            self.assertTrue(eng["payload"]["supervise"])
            self.assertEqual(rep["status"], "waiting")
            self.assertIn("禁止私自加硬编码超时", instructions(eng))

            with store.db:
                sup_id = store.enqueue(store.session(sid), "supervision", {
                    "git_changes": {"diff": "+ timeout=420"}, "constraints": ["禁止私自加硬编码超时"],
                    "review_round": 0, "engineering_output": "已加7分钟超时",
                }, "监工审查（第 1 轮）")
                store.db.execute("UPDATE jobs SET status='done' WHERE id=?", (eng["id"],))
                store.db.execute("UPDATE jobs SET status='running' WHERE id=?", (sup_id,))
            sup = store.one("SELECT j.*,s.backend FROM jobs j JOIN sessions s ON s.id=j.sid WHERE j.id=?", sup_id)
            _, _, sup_text, _ = engine.turn_input(sup, store.session(sid), [])
            self.assertIn("timeout=420", sup_text)
            self.assertEqual(json.loads(sup_text)["共享记录"], [])

            with store.db:
                store.apply_supervision(sup, "reject", "发现私自添加7分钟超时", ["engine.py: 禁止硬编码 timeout=420"])
            rework = store.one("SELECT * FROM jobs WHERE sid=? AND kind='engineering' ORDER BY rowid DESC LIMIT 1", sid)
            self.assertEqual(rework["payload"]["review_round"], 1)
            self.assertIn("禁止硬编码 timeout=420", instructions(rework))
            self.assertEqual(store.job(rep["id"])["status"], "waiting")

            with store.db:
                sup2_id = store.enqueue(store.session(sid), "supervision", {
                    "git_changes": {"diff": "- timeout=420"}, "constraints": ["禁止私自加硬编码超时"],
                    "review_round": 1, "engineering_output": "已移除超时",
                }, "监工审查（第 2 轮）")
                store.db.execute("UPDATE jobs SET status='done' WHERE id=?", (rework["id"],))
                store.db.execute("UPDATE jobs SET status='running' WHERE id=?", (sup2_id,))
                store.apply_supervision(store.job(sup2_id), "pass", "已移除超时限制，无其他问题", [])
            self.assertEqual(store.job(rep["id"])["status"], "ready")

    def test_engineering_question_codex_narration_and_gemini_guidance(self):
        with open_engine(self.root, self.home) as engine:
            store = engine.store
            sid = new_id()
            with store.db:
                store.create(sid, str(self.root), "gemini", "gpt", backend="clients")
            engine.submit(sid, "在目标程序里抓包并整理报告", new_id())
            plan_job = store.one("SELECT * FROM jobs WHERE sid=? AND kind='plan'", sid)
            with store.db:
                store.db.execute("UPDATE jobs SET status='running' WHERE id=?", (plan_job["id"],))
            store.apply_plan(plan_job, {
                "engineering": "抓包并排查请求", "engineering_mode": "develop",
                "report": "写排查总结", "report_after_gpt": True,
            }, "")
            eng = store.one("SELECT * FROM jobs WHERE sid=? AND kind='engineering'", sid)
            rep = store.one("SELECT * FROM jobs WHERE sid=? AND kind='report'", sid)
            with store.db:
                store.db.execute("UPDATE jobs SET status='running' WHERE id=?", (eng["id"],))
                store.save_turn(eng, "c1", 0, "", [{"cid": "q1", "name": "ask_clarification",
                                                    "args": {"question": "请在目标程序里发起一次登录请求并回复确认。"}}],
                                backend="clients")
            # 1) 工程任务（含目标模式）调用 ask_clarification：直接展示提问、暂停目标模式并取消所有其他任务，严格等待用户回复
            with store.db:
                store.db.execute("UPDATE sessions SET mode='goal', objective='完成联调', goal_status='active' WHERE id=?", (sid,))
            call = store.calls(store.job(eng["id"]))[0]
            with self.assertRaises(Exception):
                asyncio.run(engine.tool(store.job(eng["id"]), call, engine.sources(store.session(sid))))
            snap = store.snapshot(sid)
            q_events = [e for e in snap["events"] if e["kind"] == "message" and e["meta"].get("needs_input")]
            self.assertEqual(len(q_events), 1)
            self.assertIn("目标程序里发起一次登录请求", q_events[0]["body"])
            self.assertFalse(q_events[0].get("intermediate"))
            self.assertEqual(store.job(rep["id"])["status"], "obsolete")
            self.assertEqual(store.session(sid)["goal_status"], "blocked")
            self.assertEqual(store.active_jobs(sid), [])

            # 2) Codex 中间日志 + Gemini 节流解读 + 3) 引导时 Gemini 协同参与
            with store.db:
                eng2_id = store.enqueue(store.session(sid), "engineering", {"task": "继续排查"})
                store.db.execute("UPDATE jobs SET status='running' WHERE id=?", (eng2_id,))
            eng2 = store.one("SELECT j.*,s.backend FROM jobs j JOIN sessions s ON s.id=j.sid WHERE j.id=?", eng2_id)
            delivered = []

            async def fake_quick(target_sid, system_prompt, user_prompt, *, tag="helper", timeout=15):
                if tag == "narrate":
                    return "Codex 正在排查登录代理与网络请求路径，下一步将定位超时原因。"
                if tag == "guide":
                    return json.dumps({
                        "ack": "收到，已让 Codex 跳过 ruffle 直接检查打包脚本，报告里也会补充说明。",
                        "codex_instruction": "停止排查 ruffle，直接修改 macos/package.sh 并验证。",
                        "report_note": "报告里补充打包脚本改动说明。",
                    }, ensure_ascii=False)
                return None

            engine.quick_gemini = fake_quick

            async def exercise_live():
                engine.narration[eng2_id] = {"buffer": [], "last_at": 0.0, "count": 0, "task": None}
                engine.record_codex_progress(eng2, "读取文件：a.py", milestone=False, visible=False)
                engine.record_codex_progress(eng2, "读取文件：b.py", milestone=False, visible=False)
                engine.record_codex_progress(eng2, "Codex 已运行命令：pytest（退出码 0）", milestone=True, visible=True)
                narrator = engine.narration[eng2_id]["task"]
                self.assertIsNotNone(narrator)
                await narrator

                done = asyncio.Event()
                async def fake_send(msg, images):
                    delivered.append(msg)
                    done.set()

                engine.register_steering(eng2, fake_send)
                engine.submit(sid, "别看 ruffle 了，直接改打包脚本，报告里顺便说明一下", new_id())
                await asyncio.wait_for(done.wait(), timeout=3)
                await asyncio.sleep(0.05)
                worker = engine.steer_workers.pop(eng2_id, None)
                if worker:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)

            asyncio.run(exercise_live())
            events = store.snapshot(sid)["events"]
            narrations = [e for e in events if e["kind"] == "message" and e["meta"].get("narration")]
            self.assertEqual(len(narrations), 1)
            self.assertIn("正在排查登录代理", narrations[0]["body"])
            # 进度解读消息不进入后续模型的上下文历史，避免额外 token 消耗
            _, _, next_ctx, _ = engine.turn_input(eng2, store.session(sid), [])
            self.assertNotIn("正在排查登录代理", next_ctx)
            self.assertTrue(any("Gemini 协同引导 ·" in e["body"] for e in events))
            self.assertEqual(len(delivered), 1)
            self.assertIn("停止排查 ruffle", delivered[0])
            self.assertIn("报告里补充打包脚本改动说明", store.session(sid)["brief"]["report"])
