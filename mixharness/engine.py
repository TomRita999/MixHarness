import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values
from filelock import FileLock

from . import workspace
from .attachments import MAX_IMAGE_TOTAL, MAX_ITEMS, TYPES, image_parts
from .agent_settings import AgentSettings, PERMISSIONS, SPEEDS, available_speeds, selected_skills, shared_input, skill_catalog
from .catalog import ModelCatalog
from .clients import CLIENTS, ClientCatalog, CodexTurnError, private_directory, workflow_turn
from .providers import FUNCTIONS, tool_names, google_turn, openai_turn
from .reasoning import api_levels, group_for, resolve_choice
from .store import PROVIDERS, Store, Superseded, new_id, now, pack


@contextmanager
def open_engine(project, home):
    home.mkdir(parents=True, exist_ok=True)
    with FileLock(home / "service.lock", timeout=0):
        store = Store(home / "sessions.sqlite3")
        try:
            yield Engine(store, home, project)
        finally:
            store.db.close()


class Engine:
    def __init__(self, store, home, project):
        self.store = store
        self.home = home
        self.project = project
        self.active = {}
        self.steering = {}
        self.previews = {}
        self.changes = {}
        self.file_operations = {}
        self.closing_jobs = {}
        self.cleanup_signal = asyncio.Event()
        self.catalogs = {}
        self.config_revision = 0
        self.config_error = ""
        self.client_catalogs = {p: ClientCatalog(c) for p, c in CLIENTS.items()}
        self.client_locks = {}
        self.approval_waiters = {}
        self.deadlines = {}
        record = self.store.rows("SELECT value FROM preferences WHERE key='agent'")
        self.settings = AgentSettings.model_validate_json(record[0]["value"]) if record else AgentSettings()
        self.signal = asyncio.Event()
        self.reload_config()

    def reload_config(self):
        path = self.project / ".env"
        values = dotenv_values(path)
        config = {**values, **os.environ}
        keys, sources = {}, {}
        for provider, names in (("gemini", ("GEMINI_API_KEY", "GOOGLE_API_KEY")), ("openai", ("OPENAI_API_KEY",))):
            keys[provider], sources[provider] = "", ""
            for source, mapping in (("环境变量", os.environ), (str(path), values)):
                found = next((k for k in names if (mapping.get(k) or "").strip()), None)
                if found:
                    keys[provider] = mapping[found].strip()
                    sources[provider] = source + " · " + found
                    break
        configured = config.get("MIXHARNESS_WORKSPACE")
        root = (self.project / Path(configured).expanduser()).resolve() if configured else None
        if root and not root.is_dir():
            raise ValueError("MIXHARNESS_WORKSPACE 必须指向已存在的项目目录")
        commands = workspace.check_commands(self.project / "mixharness.toml")
        self.keys, self.key_sources = keys, sources
        self.workspace, self.commands = root, commands
        for provider, key in self.keys.items():
            if provider not in self.catalogs or self.catalogs[provider].key != key:
                self.catalogs[provider] = ModelCatalog(provider, key)
        self.check_python = config.get("MIXHARNESS_PYTHON")
        self.config_revision += 1
        self.config_error = ""
        self.config_stamp = self.configuration_stamp()
        self.signal.set()

    def configuration_stamp(self):
        result = []
        for path in (self.project / ".env", self.project / "mixharness.toml"):
            try:
                info = path.stat()
                result.append((info.st_mtime_ns, info.st_size, info.st_ino))
            except FileNotFoundError:
                result.append(None)
        return result

    async def monitor_config(self):
        while True:
            await asyncio.sleep(1)
            stamp = self.configuration_stamp()
            if stamp != self.config_stamp:
                try:
                    self.reload_config()
                except (OSError, ValueError) as exc:
                    self.config_stamp = stamp
                    self.config_revision += 1
                    self.config_error = str(exc)

    def model_choice(self, backend, provider, model, effort):
        if backend == "clients":
            catalog = self.client_catalogs[provider]
            group = group_for(catalog.groups, model)
            if group:
                return resolve_choice(group, model, effort)
            if catalog.error:
                raise ValueError(catalog.error)
            if catalog.groups or effort:
                raise ValueError(catalog.error or catalog.view()["client"] + " 没有提供所选模型或档位")
        elif effort and effort not in api_levels(provider, model):
            raise ValueError(model + " 不支持思考强度 " + effort)
        return model, effort or ""

    def resolve_models(self, backend, models, efforts):
        choices = {p: self.model_choice(backend, p, models[p], (efforts or {}).get(p)) for p in CLIENTS}
        return {p: m for p, (m, _) in choices.items()}, {p: e for p, (_, e) in choices.items()}

    def create_session(self, gemini, openai, directory=None, *, project_id=None, standalone=False, backend="api",
                       efforts=None, permission="ask", speed="standard", mode="default"):
        models, efforts = self.resolve_models(backend, {"gemini": gemini, "openai": openai}, efforts)
        self.validate_access(backend, models["openai"], permission, speed)
        sid = new_id()
        selected = directory or (None if standalone else self.workspace)
        if project_id:
            project = self.store.project(project_id)
            if project["archived"]:
                raise ValueError("请先重新打开项目")
            root = Path(project["folders"][0]["path"])
        elif selected:
            project = self.store.open_project(selected)
            project_id = project["id"]
            root = Path(project["folders"][0]["path"])
        else:
            root = self.home / "projects" / sid
            root.mkdir(parents=True)
        return self.store.create(sid, root, models["gemini"], models["openai"], project_id, backend, efforts, permission, speed, mode)

    def validate_access(self, backend, model, permission, speed):
        if permission not in PERMISSIONS:
            raise ValueError("未知的权限模式")
        if speed != "standard":
            group = group_for(self.client_catalogs["openai"].groups, model)
            if speed not in SPEEDS or backend != "clients" or speed not in available_speeds(group):
                raise ValueError("Ultrafast 需要 Pro 200 及以上套餐及模型支持" if speed == "ultrafast"
                                 else "当前 Codex 客户端或模型不支持 Fast")

    def set_access(self, sid, permission=None, speed=None):
        session = self.store.session(sid)
        mode = session["permission"] if permission is None else permission
        tier = session["speed"] if speed is None else speed
        self.validate_access(session["backend"], session["openai"], mode, tier if speed is not None else "standard")
        with self.store.db:
            self.store.db.execute("UPDATE sessions SET permission=?,speed=? WHERE id=?", (mode, tier, sid))
            if mode != session["permission"]:
                self.store.db.execute("DELETE FROM approvals WHERE sid=? AND decision='pending'", (sid,))
                for job, task in self.active.items():
                    if self.store.db.execute("UPDATE jobs SET status='ready' WHERE id=? AND sid=? AND status='running'",
                                             (job, sid)).rowcount:
                        task.cancel()
        self.notify(sid)

    def skill_folders(self, sid=None, pid=None, directory=None):
        if sid:
            return list(self.sources(self.store.session(sid)).folders.values())
        if pid:
            return [Path(f["path"]) for f in self.store.project(pid)["folders"]]
        return [Path(directory).resolve() if directory else self.workspace or self.project]

    def settings_for(self, folders):
        local = [name for f in folders for name in self.settings.project_skills.get(str(f), [])]
        return self.settings.model_copy(update={"skills": list(dict.fromkeys([*self.settings.skills, *local]))})

    def save_settings(self, updates, sid=None, pid=None, directory=None):
        settings = AgentSettings.model_validate({**self.settings.model_dump(), **updates})
        settings.skill_dirs = list(dict.fromkeys(str(Path(d).expanduser().resolve()) for d in settings.skill_dirs))
        for directory in settings.skill_dirs:
            if not Path(directory).expanduser().is_dir():
                raise ValueError("技能目录不存在：" + directory)
        folders = self.skill_folders(sid, pid, directory)
        if "skills" in updates:
            catalog = skill_catalog(settings, folders)
            selected_skills(settings, folders)
            project_skills = dict(settings.project_skills)
            for folder in folders:
                project_skills.pop(str(folder), None)
            shared = []
            for name in settings.skills:
                path = Path(catalog[name]["path"])
                project = next((f for f in folders if path.is_relative_to((f / ".agents/skills").resolve())), None)
                if project:
                    project_skills.setdefault(str(project), []).append(name)
                else:
                    shared.append(name)
            settings.skills, settings.project_skills = shared, project_skills
        else:
            selected_skills(settings, folders)
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO preferences VALUES('agent',?)", (settings.model_dump_json(),))
        self.settings = settings
        self.config_revision += 1
        self.notify(sid or "")

    async def wait_approval(self, job, source, title, detail, identifier):
        sid = job["sid"]
        with self.store.db:
            self.store.db.execute("INSERT OR IGNORE INTO approvals(id,sid,job,source,title,detail,at) VALUES(?,?,?,?,?,?,?)",
                                  (identifier, sid, job["id"], source, title, detail, now()))
        decision = self.store.one("SELECT decision FROM approvals WHERE id=?", identifier)["decision"]
        if decision != "pending":
            return decision == "allow"
        future = asyncio.get_running_loop().create_future()
        self.approval_waiters[identifier] = future
        self.notify(sid)
        try:
            return await future
        finally:
            self.approval_waiters.pop(identifier, None)
            if source != "MixHarness":
                with self.store.db:
                    self.store.db.execute("UPDATE approvals SET decision='expired' WHERE id=? AND decision='pending'", (identifier,))
            self.notify(sid)

    def decide(self, sid, identifier, allow):
        with self.store.db:
            changed = self.store.db.execute("UPDATE approvals SET decision=? WHERE sid=? AND id=? AND decision='pending' "
                                            "AND job IN (SELECT id FROM jobs WHERE status NOT IN ('obsolete','done','failed'))",
                                            ("allow" if allow else "deny", sid, identifier)).rowcount
            if not changed:
                raise ValueError("这项审批请求已经结束")
            action = self.store.one("SELECT source,title FROM approvals WHERE id=?", identifier)
            self.store.event(sid, "progress", "system", ("已批准" if allow else "已拒绝") + " · " + action["source"]
                             + " · " + action["title"], {"approval": identifier, **action})
        future = self.approval_waiters.get(identifier)
        if future and not future.done():
            future.set_result(allow)
        self.notify(sid)

    async def native_approval(self, job, source, title, details):
        if job["kind"] in ("proposal", "goal_check", "side") or job["payload"].get("review") or job["payload"].get("read_only"):
            return False
        if self.store.session(job["sid"])["permission"] != job["permission"]:
            return False
        if job["permission"] == "full":
            return True
        deadline = self.deadlines.get(job["id"])
        if deadline:
            loop = asyncio.get_running_loop()
            remaining = deadline.when() - loop.time()
            deadline.reschedule(None)
        try:
            return await self.wait_approval(job, source, title, json.dumps(details, ensure_ascii=False, indent=2), new_id())
        finally:
            if deadline and not deadline.expired():
                deadline.reschedule(loop.time() + remaining)

    async def authorize_tool(self, job, call, title, detail, routine=False):
        session = self.store.session(job["sid"])
        if session["permission"] == "full" or (routine and session["permission"] == "auto"):
            return
        key = workspace.digest(pack([job["id"], call["cursor"], call["cid"], detail]).encode())
        if not await self.wait_approval(job, "MixHarness", title, detail, key):
            raise ValueError("用户已拒绝这次操作：" + title)
        await self.gate(job)

    def sources(self, session):
        if session["project_id"]:
            folders = self.store.project(session["project_id"])["folders"]
            return workspace.Sources({f["alias"]: Path(f["path"]) for f in folders})
        return workspace.Sources({"本地": Path(session["workspace"])})

    def checks(self, sources):
        return {alias: workspace.check_commands(root / "mixharness.toml", self.commands)
                for alias, root in sources.folders.items()}

    def submit(self, sid, text, client_id, *, draft=None, attachments=()):
        previous = self.store.session(sid)["revision"]
        seq = self.store.message(sid, text, client_id, draft=draft, attachments=attachments)
        self.notify(sid)
        return seq, self.store.session(sid)["revision"] != previous

    def set_choices(self, sid, updates):
        session = self.store.session(sid)
        models = {p: session[p] for p in CLIENTS}
        efforts = dict(session["efforts"])
        for p, (model, effort) in updates.items():
            if effort is None and model == session[p]:
                effort = efforts.get(p, "")
            models[p], efforts[p] = self.model_choice(session["backend"], p, model, effort)
        self.store.set_models(sid, **models, efforts=efforts)
        if session["openai"] != models["openai"]:
            with self.store.db:
                self.store.db.execute("UPDATE sessions SET speed='standard' WHERE id=?", (sid,))
        self.notify(sid)

    def switch_backend(self, sid, backend, models, efforts=None):
        models, efforts = self.resolve_models(backend, models, efforts)
        if backend == "clients":
            for provider, catalog in self.client_catalogs.items():
                if models.get(provider) not in catalog.labels:
                    raise ValueError(catalog.error or catalog.view()["client"] + " 没有提供所选模型")
        self.store.switch_backend(sid, backend, models, efforts)
        self.notify(sid)

    def retry(self, sid, jid, *, read_only=False):
        job = self.store.job(jid)
        if job["sid"] != sid:
            raise KeyError("任务不存在")
        if job["status"] not in ("failed", "needs_key"):
            raise ValueError("该任务当前无需重试")
        if read_only and job["kind"] not in ("engineering", "verification"):
            raise ValueError("只读重试适用于调研和验证任务")
        s = self.store.session(sid)
        if (s["mode"] == "plan") != (job["kind"] == "proposal"):
            raise ValueError("当前模式无法重试这项任务")
        if (s["mode"] == "side") != (job["kind"] == "side"):
            raise ValueError("当前模式无法重试这项任务")
        if job["kind"] == "goal_check" and (s["mode"] != "goal" or s["goal_status"] != "active"):
            raise ValueError("请先继续目标，再核对进度")
        with self.store.db:
            if read_only and not job["payload"].get("read_only"):
                self.store.db.execute("UPDATE jobs SET payload=?,cursor=NULL,seen=0 WHERE id=?",
                                      (pack(dict(job["payload"], read_only=True)), jid))
            self.store.db.execute("UPDATE jobs SET status='ready',error='' WHERE id=?", (jid,))
            if job["kind"] in ("verification", "engineering") and s["brief"]["report"] and not self.store.rows(
                "SELECT id FROM jobs WHERE sid=? AND kind='report' AND status IN ('ready','waiting','running','needs_key')", sid
            ):
                report = self.store.enqueue(s, "report")
                self.store.db.execute("UPDATE jobs SET status='waiting' WHERE id=?", (report,))
            self.store.event(sid, "progress", "system", "重新执行任务", {"job": jid})
        self.notify(sid)

    def notify(self, sid):
        self.changes[sid] = self.changes.get(sid, 0) + 1
        self.signal.set()

    def delete_session(self, sid):
        jobs = self.store.delete_session(sid)
        def closed(task):
            self.closing_jobs[sid].discard(task)
            if not self.closing_jobs[sid]:
                del self.closing_jobs[sid]
            self.cleanup_signal.set()
        for jid in jobs:
            self.steering.pop(jid, None)
            self.previews.pop(jid, None)
            if jid in self.active:
                task = self.active[jid]
                self.closing_jobs.setdefault(sid, set()).add(task)
                task.add_done_callback(closed)
                task.cancel()
        self.notify(sid)
        self.client_locks.pop(sid, None)
        self.cleanup_signal.set()

    async def file_operation(self, sid, operation):
        # 记录还在写入的快照和验证副本，待文件操作结束后再清理磁盘。
        task = asyncio.create_task(operation)
        self.file_operations.setdefault(sid, set()).add(task)
        def finished(done):
            self.file_operations[sid].discard(done)
            if not self.file_operations[sid]:
                del self.file_operations[sid]
            if not done.cancelled():
                done.exception()
            self.cleanup_signal.set()
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def clean_deleted(self):
        while True:
            self.cleanup_signal.clear()
            retry = False
            for row in self.store.rows("SELECT sid FROM session_cleanup"):
                sid = row["sid"]
                if sid in self.file_operations or sid in self.closing_jobs:
                    continue
                try:
                    profile = Path.home() / private_directory(self.home, sid)
                    await asyncio.to_thread(workspace.remove_session_evidence, self.home, sid, profile)
                except OSError:
                    # Windows 文件占用或上一次启动留下的验证进程可暂时持有日志。
                    retry = True
                    continue
                with self.store.db:
                    self.store.db.execute("DELETE FROM session_cleanup WHERE sid=?", (sid,))
            if retry:
                try:
                    await asyncio.wait_for(self.cleanup_signal.wait(), timeout=3)
                except TimeoutError:
                    pass
            else:
                await self.cleanup_signal.wait()

    def describe_inputs(self, items):
        descriptions = []
        for item in items:
            desc = {"类型": TYPES[item["kind"]], "名称": item["name"]}
            if item["kind"] != "image":
                desc.update(来源=item["source"], 标识=item["target"], 内容=self.store.inputs.data(item["id"]).decode())
            descriptions.append(desc)
        return descriptions

    def image_inputs(self, groups):
        # 优先保留最近消息的图片，同一张图在同一次请求里只传一份。
        images = {}
        size = 0
        for items in reversed(groups):
            for item in reversed(items):
                if item["kind"] != "image" or item["id"] in images:
                    continue
                if len(images) >= MAX_ITEMS or size + item["size"] > MAX_IMAGE_TOTAL:
                    continue
                images[item["id"]] = dict(item, data=self.store.inputs.data(item["id"]))
                size += item["size"]
        return list(reversed(images.values()))

    async def steer(self, sid, text, seq=None):
        if not self.steering:
            return
        items = self.store.inputs.for_session(sid, seq - 1).get(seq, []) if seq else []
        message = "共享会话收到用户补充。只处理与当前任务有关的要求。\n" + text
        if items:
            message += "\n随消息附加的材料：" + pack(self.describe_inputs(items))
            message = [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": message}, *image_parts(self.image_inputs([items]), "openai")]}]
        for jid, send in list(self.steering.items()):
            if self.steering.get(jid) is not send or self.store.job(jid)["sid"] != sid:
                continue
            try:
                await send(message)
            except Exception:
                if self.steering.get(jid) is not send:
                    continue
                with self.store.db:
                    self.store.event(sid, "progress", "system", "补充内容已写入会话，将在下一轮生效", {"job": jid})
                self.notify(sid)

    async def serve(self):
        cleanup = asyncio.create_task(self.clean_deleted())
        config = asyncio.create_task(self.monitor_config())
        try:
            while True:
                self.signal.clear()
                for jid, task in list(self.active.items()):
                    if task.done():
                        del self.active[jid]
                        if not task.cancelled():
                            task.result()
                    elif not self.store.rows("SELECT 1 FROM jobs WHERE id=? AND status!='obsolete'", jid):
                        task.cancel()
                ready = self.store.rows(
                    "SELECT j.*,s.backend FROM jobs j JOIN sessions s ON s.id=j.sid "
                    "WHERE j.status IN ('ready','needs_key') AND (j.kind IN ('plan','goal_check') OR s.planned=s.revision) "
                    "ORDER BY (j.kind='plan') DESC,j.rowid"
                )
                for job in ready:
                    if job["id"] in self.active:
                        continue
                    provider = PROVIDERS[job["kind"]]
                    if job["backend"] == "api" and not self.keys[provider]:
                        if job["status"] != "needs_key":
                            self.set_status(job, "needs_key")
                        continue
                    if job["kind"] == "verification" and self.store.rows(
                        "SELECT id FROM jobs WHERE sid=? AND kind='engineering' AND status IN ('ready','running')",
                        job["sid"],
                    ):
                        continue
                    if len(self.active) >= 6 and job["kind"] != "plan":
                        break
                    self.set_status(job, "running")
                    task = asyncio.create_task(self.execute(job, self.keys[provider]))
                    task.add_done_callback(lambda _: self.signal.set())
                    self.active[job["id"]] = task
                await self.signal.wait()
        finally:
            cleanup.cancel()
            config.cancel()
            for task in self.active.values():
                task.cancel()
            await asyncio.gather(cleanup, config, *self.active.values(), return_exceptions=True)

    def set_status(self, job, status):
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET status=? WHERE id=?", (status, job["id"]))
        self.notify(job["sid"])

    def record_reads(self, job, source, paths):
        files = list(dict.fromkeys(paths))
        with self.store.db:
            self.store.guard(job)
            self.store.event(job["sid"], "progress", PROVIDERS[job["kind"]], source + " 已读取 " + "、".join(files),
                             {"job": job["id"], "files_read": files, "source": source})
        self.notify(job["sid"])

    async def gate(self, job):
        while True:
            session = self.store.guard(job)
            if job["kind"] in ("plan", "goal_check") or session["revision"] == session["planned"]:
                return session
            await asyncio.sleep(0.1)

    def turn_input(self, job, session, calls):
        # 只有产生此轮工具调用的 API 和模型可以接收原来的调用编号。
        resume = ((calls or (not self.settings.context_window and not session["context_after"])) and job["cursor"] is not None
                  and job["backend"] == job["cursor_backend"] == "api"
                  and job["model"] == job["cursor_model"])
        request = dict(job, permission=session["permission"], speed=session["speed"], context_window=self.settings.context_window)
        if not resume:
            request.update(cursor=None, seen=0)
        sid = job["sid"]
        entries = self.store.rows("SELECT seq,kind,actor,body,meta FROM events WHERE sid=? AND seq>? ORDER BY seq",
                                  sid, request["seen"])
        # 当前需求始终完整传入，较长的输出可通过持久化成果按需取回。
        history = [{"seq": e["seq"], "from": e["actor"], "type": e["kind"],
                    "text": e["body"] if e["actor"] == "user" else e["body"][:4200], "details": e["meta"]}
                   for e in entries if e["seq"] > session["context_after"]]
        works = self.store.rows("SELECT id,kind,status,title,error FROM jobs WHERE sid=? AND status!='obsolete'", sid)
        assets = self.store.rows("SELECT id,name,digest,meta FROM artifacts WHERE sid=? ORDER BY rowid DESC LIMIT 80", sid)
        draft = self.store.rows("SELECT draft FROM jobs WHERE sid=? AND kind='report' AND draft!='' "
                                "ORDER BY rowid DESC LIMIT 1", sid)
        context = {"当前需求": session["brief"], "需求版本": session["versions"], "本次任务": job["payload"],
                   "共享记录": history, "任务": works, "可读取的成果": assets,
                   "各来源文件夹的验证命令": self.checks(self.sources(session))}
        if session["mode"] == "goal" and session["objective"]:
            context["持续目标"] = session["objective"]
            context["上次目标核对"] = session["goal_note"]
        attached = self.store.inputs.for_session(sid, max(request["seen"], session["context_after"]))
        latest = next((e["seq"] for e in reversed(history) if e["from"] == "user"), None)
        if attached:
            context["引用说明"] = "引用会话是背景资料。针对附带技能或工具的那条要求，使用所选技能和当前角色可用的工具，并遵守本次会话的操作权限。"
            for event in history:
                seq = event["seq"]
                if seq in attached:
                    descriptions = self.describe_inputs(attached[seq])
                    if seq == latest:
                        context["当前消息附件"] = descriptions
                    else:
                        event["附件"] = descriptions
        if session["context_summary"]:
            context["历史摘要"] = session["context_summary"]
        if job["kind"] == "plan":
            context["待处理用户消息"] = [e["body"] for e in entries if e["actor"] == "user"
                                         and not e["meta"].get("direct") and e["meta"].get("revision", 0) > session["planned"]]
        folders = self.sources(session).folders.values()
        skills = selected_skills(self.settings_for(folders), folders)
        if skills:
            context["用户启用的技能"] = skills
        if not resume and job["cursor"]:
            context["本次任务的工具记录"] = self.store.rows(
                "SELECT name,args,result FROM calls WHERE job=? ORDER BY rowid", job["id"])
        if not resume and job["draft"]:
            context["已有草稿"] = job["draft"]
        elif draft and job["kind"] == "report" and not job["cursor"]:
            context["已有草稿"] = draft[0]["draft"]
        seen = entries[-1]["seq"] if entries else request["seen"]
        results, text = shared_input(context, calls if resume else [], self.settings.context_window)
        if attached:
            retained = {e["seq"] for e in json.loads(text)["共享记录"]}
            request["images"] = self.image_inputs([items for seq, items in attached.items() if seq == latest or seq in retained])
        return request, results, text, seen

    def route_is_current(self, job):
        return self.store.db.execute(
            "SELECT 1 FROM jobs j JOIN sessions s ON s.id=j.sid WHERE j.id=? AND j.model=? AND j.effort=? AND s.backend=?",
            (job["id"], job["model"], job["effort"], job["backend"]),
        ).fetchone() is not None

    async def verify_source(self, job):
        sources = self.sources(self.store.session(job["sid"]))
        payload = job["payload"]
        if "source" not in payload:
            version = await self.file_operation(job["sid"], asyncio.to_thread(
                workspace.snapshot, sources, self.home / "snapshots" / job["sid"], self.home))
            await self.gate(job)
            payload["source"] = version
            if payload.get("review"):
                payload["git_changes"] = await asyncio.to_thread(workspace.review_changes, sources, self.home)
                await self.gate(job)
            with self.store.db:
                self.store.db.execute("UPDATE jobs SET payload=? WHERE id=?", (pack(job["payload"]), job["id"]))
        return self.home / "snapshots" / job["sid"] / job["payload"]["source"]

    async def check_report_sources(self, job, session):
        checks = self.store.rows("SELECT * FROM jobs WHERE sid=? AND kind='verification' AND status='done'", job["sid"])
        relevant = [c for c in checks if "source" in c["payload"] and
                    all(session["versions"][k] == v for k, v in c["basis"].items())]
        if not relevant:
            return
        current = await asyncio.to_thread(workspace.fingerprint, self.sources(session), exclude=self.home)
        await self.gate(job)
        with self.store.db:
            for check in relevant:
                if check["payload"]["source"] == current or self.store.job(check["id"])["status"] != "done":
                    continue
                self.store.db.execute("UPDATE jobs SET status='obsolete' WHERE id=?", (check["id"],))
                del check["payload"]["source"]
                child = self.store.enqueue(session, "verification", check["payload"], check["title"])
                self.store.event(job["sid"], "progress", "system", "文件已变动，正在重新验证", {"job": child})

    async def execute(self, job, key):
        jid, sid = job["id"], job["sid"]
        try:
            session = await self.gate(job)
            sources = self.sources(session)
            if job["kind"] == "verification":
                sources = sources.at_snapshot(await self.verify_source(job))
            calls = self.store.calls(job)
            for call in calls:
                if call["result"] is None and not call["child"]:
                    await self.gate(job)
                    try:
                        await self.tool(job, call, sources)
                    except (ValueError, OSError, KeyError) as exc:
                        await self.gate(job)
                        error = {"tool": call["name"], "error": str(exc)}
                        with self.store.db:
                            self.store.tool_result(call, error)
                            self.store.event(sid, "progress", "system", str(exc), {"job": jid, "tool": call["name"]})
            calls = self.store.calls(job)
            if any(c["result"] is None for c in calls):
                self.set_status(job, "waiting")
                return
            session = await self.gate(job)
            if not self.route_is_current(job):
                self.set_status(job, "ready")
                return
            request, results, context, seen = self.turn_input(job, session, calls)
            self.previews[jid] = ""

            def preview(text):
                self.previews[jid] += text

            async with asyncio.timeout(420) as deadline:
                self.deadlines[jid] = deadline
                if job["backend"] == "clients":
                    reply = await workflow_turn(self, request, context, next(iter(sources.folders.values())))
                elif PROVIDERS[job["kind"]] == "gemini":
                    reply = await google_turn(key, request, context, results, preview)
                else:
                    reply = await openai_turn(key, request, context, results, preview,
                                              lambda sender: self.steering.update({jid: sender}))
                    self.steering.pop(jid, None)
            await self.gate(job)
            if job["kind"] == "plan":
                self.store.apply_plan(job, reply.model_dump(), reply.reply)
                return
            if job["kind"] == "goal_check" and not reply.calls:
                raise ValueError("目标核对没有返回完成依据或下一步，请恢复目标以重新核对")
            if not reply.calls and not reply.text.strip():
                raise ValueError("模型没有返回正文或工具调用")
            if job["kind"] == "report" and not reply.calls:
                await self.check_report_sources(job, session)
            if job["kind"] == "verification" and not reply.calls:
                current = await asyncio.to_thread(workspace.fingerprint, self.sources(session), exclude=self.home)
                await self.gate(job)
                if current != job["payload"]["source"]:
                    del job["payload"]["source"]
                    with self.store.db:
                        self.store.db.execute("UPDATE jobs SET cursor=NULL,seen=0,status='ready',draft='',payload=? WHERE id=?",
                                              (pack(job["payload"]), jid))
                        self.store.event(sid, "progress", "system", "文件已变动，正在重新验证", {"job": jid})
                    return
            with self.store.db:
                self.store.guard(job)
                self.store.save_turn(job, reply.cursor, seen, reply.text, reply.calls, backend=job["backend"])
                if reply.calls:
                    self.store.db.execute("UPDATE jobs SET status='ready' WHERE id=?", (jid,))
                elif job["kind"] == "report" and (status := self.store.report_wait_status(sid, seen)):
                    self.store.db.execute("UPDATE jobs SET status=? WHERE id=?", (status, jid))
                    self.store.event(sid, "progress", "gemini", "等待工程或验证结果" if status == "waiting"
                                     else "已收到 GPT 的任务结果，正在更新报告", {"job": jid})
                else:
                    meta = {"kind": job["kind"], "basis": job["basis"], "sources": reply.sources}
                    if job["kind"] == "verification":
                        meta["source"] = job["payload"]["source"]
                    aid = workspace.artifact(self.store, job, job["title"] + ".md", reply.text, meta)
                    evidence = self.store.rows("SELECT id,name,meta FROM artifacts WHERE job=? AND id!=?", jid, aid)
                    if job["kind"] == "verification":
                        evidence = [a for a in evidence if a["meta"].get("source") == job["payload"]["source"]]
                        meta["evidence_status"] = "已记录依据" if evidence or reply.sources else "证据不足"
                    self.store.finish(job, reply.text, {"artifact_id": aid, "text": reply.text,
                                      "evidence": evidence, **meta})
        except Superseded:
            return
        except asyncio.CancelledError:
            if self.store.rows("SELECT 1 FROM jobs WHERE id=? AND status='running'", jid):
                self.set_status(job, "ready")
            raise
        except Exception as exc:
            # 在任务边界记录错误，让其他会话继续执行，并把失败交回等待中的报告。
            message = str(exc) or type(exc).__name__
            for secret in self.keys.values():
                if secret:
                    message = message.replace(secret, "[已隐藏]")
            if self.store.rows("SELECT 1 FROM jobs WHERE id=? AND status='running'", jid):
                if not self.route_is_current(job):
                    self.set_status(job, "ready")
                    return
                with self.store.db:
                    result = {"error": message[:2200]}
                    if isinstance(exc, CodexTurnError):
                        result["diagnostic"] = exc.details
                    self.store.finish(job, message[:2200], result, success=False)
        finally:
            self.deadlines.pop(jid, None)
            self.steering.pop(jid, None)
            self.previews.pop(jid, None)
            self.notify(sid)

    async def tool(self, job, call, sources):
        name = call["name"]
        if name not in tool_names(job["kind"], job["payload"]):
            raise ValueError(f"当前任务无法使用工具 {name}")
        args = FUNCTIONS[name][0].model_validate(call["args"]).model_dump()
        sid, jid = job["sid"], job["id"]
        st = self.store
        originals = self.sources(st.session(sid))
        if name == "assess_goal":
            with st.db:
                st.tool_result(call, args)
                st.assess_goal(job, args["status"], args["evidence"], args["next_step"])
            raise Superseded()
        if name == "ask_clarification":
            with st.db:
                st.guard(job)
                st.tool_result(call, args)
                st.db.execute("UPDATE jobs SET payload=? WHERE id=?", (pack(dict(job["payload"], needs_input=True)), jid))
                st.finish(job, args["question"], {"needs_input": True})
            raise Superseded()
        if name == "request_verification":
            with st.db:
                st.request_verification(job, call)
            self.notify(sid)
            return
        if name == "draft_section":
            with st.db:
                child = st.enqueue(st.session(sid), "section", args, args["title"])
                st.tool_result(call, {"section_task": child, "title": args["title"]})
                st.event(sid, "progress", "gemini", "开始撰写章节「" + args["title"] + "」", {"job": child})
            self.notify(sid)
            return
        if name == "list_files":
            paths = await asyncio.to_thread(lambda: [relative.as_posix() for relative, _ in sources.paths(self.home)])
            result = {"paths": paths[:2000], "total": len(paths), "checks_by_folder": self.checks(originals)}
        elif name in ("read_file", "edit_file"):
            folder, relative = sources.locate(args["path"])
            if name == "edit_file":
                target = workspace.project_path(folder, relative)
                detail = str(target) + "\n\n" + args["content"]
                await self.authorize_tool(job, call, "写入 " + args["path"], detail, routine=True)
            operation = workspace.read_source if name == "read_file" else workspace.edit_source
            result = operation(folder, dict(args, path=relative))
            result["path"] = args["path"]
        elif name == "read_artifact":
            record = st.one("SELECT * FROM artifacts WHERE sid=? AND id=?", sid, args["id"])
            offset = args["offset"]
            result = {"name": record["name"], "text": record["content"][offset:offset + 24000], "meta": record["meta"],
                      "total_chars": len(record["content"])}
        elif name == "save_section":
            result = {"section": args["title"]}
        elif name == "run_check":
            alias = args["folder"] or next(iter(originals.folders))
            if alias not in originals.folders:
                raise ValueError("请选择当前项目的来源文件夹")
            commands = self.checks(originals)[alias]
            if args["name"] not in commands:
                raise ValueError("请使用项目已注册的验证命令")
            command = commands[args["name"]]
            isolated_compile = (st.session(sid)["permission"] == "auto" and command == workspace.DEFAULT_CHECKS["python-compile"]
                                and not self.check_python and not (originals.folders[alias] / ".venv").exists())
            detail = str(originals.folders[alias]) + "\n" + json.dumps(command, ensure_ascii=False)
            await self.authorize_tool(job, call, "运行 " + args["name"], detail, routine=isolated_compile)
            if job["kind"] == "engineering":
                parent = self.home / "snapshots" / sid
                version = await self.file_operation(sid, asyncio.to_thread(workspace.snapshot, originals, parent, self.home))
                sources = originals.at_snapshot(parent / version)
            else:
                version = job["payload"]["source"]
            invocation = workspace.digest(pack([jid, call["cursor"], call["cid"], version]).encode())
            source = self.home / "snapshots" / sid / version
            subdirectory = alias if len(originals.folders) > 1 else "."
            result = await self.file_operation(sid, workspace.run_check(
                source, self.home / "runs" / sid, commands[args["name"]], invocation=invocation,
                project=originals.folders[alias], python=self.check_python, subdirectory=subdirectory, isolated_compile=isolated_compile))
            result.update(source=version, folder=alias)
        await self.gate(job)
        with st.db:
            if name == "read_file":
                st.event(sid, "progress", PROVIDERS[job["kind"]], "已读取 " + args["path"],
                         {"job": jid, "files_read": [args["path"]], "source": "MixHarness"})
            if name in ("run_check", "save_section", "edit_file") or (name == "read_file" and job["kind"] == "verification"):
                if name == "save_section":
                    title, content = args["title"] + ".md", args["markdown"]
                elif name == "run_check":
                    title, content = args["name"] + ".log", result["output"]
                else:
                    title = args["path"]
                    content = args["content"] if name == "edit_file" else result["text"]
                meta = {"kind": name, "basis": job["basis"]}
                for field in ("source", "folder", "sha256", "exit_code", "error", "argv", "log", "bytes"):
                    if field in result:
                        meta[field] = result[field]
                if "source" in job["payload"]:
                    meta["source"] = job["payload"]["source"]
                aid = workspace.artifact(st, job, title, content, meta)
                result["artifact_id"] = aid
                st.event(sid, "artifact", "gemini" if name == "save_section" else "openai", title,
                         {"job": jid, "artifact_id": aid, **meta})
            st.tool_result(call, result)
        self.notify(sid)
