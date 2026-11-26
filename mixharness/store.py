import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .attachments import InputLibrary
from .conversation_trace import describe_events, report_input_kind, report_inputs
from .reasoning import LEVELS


SCOPES = ("engineering", "verification", "report")
CHAT_JOBS = {"gemini": "gemini_chat", "openai": "gpt_chat"}
DEPENDS = {
    "plan": (),
    "proposal": (),
    "goal_check": (),
    "side": (),
    **dict.fromkeys(CHAT_JOBS.values(), ()),
    "engineering": ("engineering",),
    "verification": ("engineering", "verification"),
    "supervision": ("engineering",),
    "report": SCOPES,
    "section": SCOPES,
}
PROVIDERS = {**{kind: provider for provider, kind in CHAT_JOBS.items()},
             "plan": "gemini", "report": "gemini", "section": "gemini", "supervision": "gemini",
             "engineering": "openai", "verification": "openai", "proposal": "openai",
             "goal_check": "openai", "side": "openai"}
LABELS = {**dict.fromkeys(CHAT_JOBS.values(), "直接对话"),
          "plan": "解析需求", "engineering": "开发与研究", "verification": "验证",
          "supervision": "监工审查", "report": "撰写报告",
          "section": "撰写章节", "proposal": "制定计划", "goal_check": "核对目标", "side": "侧边问答"}
MODES = {"default": "默认模式", "plan": "计划模式", "goal": "目标模式"}
MODE_TITLES = {**MODES, "side": "侧边问答"}
GOAL_STATES = {"": "待设置", "active": "进行中", "paused": "已暂停", "blocked": "需要补充", "complete": "已完成"}
STATUSES = {"ready": "待执行", "running": "进行中", "needs_key": "等待密钥", "waiting": "等待中",
            "done": "已完成", "failed": "失败", "obsolete": "已跳过", "stopped": "已停止"}
ACTIVE_STATUSES = {"ready", "running", "waiting", "needs_key"}
JSON_FIELDS = {"brief", "versions", "basis", "payload", "args", "result", "meta", "backend_models", "efforts", "paused_work"}


def pack(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id():
    return uuid4().hex


def accepts_guidance(job):
    if job["kind"] in CHAT_JOBS.values():
        return False
    return job["status"] in ("running", "waiting") or (
        job["status"] in ("ready", "needs_key") and (job["guide_seq"] > 0 or job["approval_epoch"] > 0 or job["kind"] != "plan"))


def unpack(row):
    data = dict(row)
    if "daybreak" in data:
        data["daybreak"] = bool(data["daybreak"])
    for key in JSON_FIELDS & data.keys():
        if data[key] is not None:
            data[key] = json.loads(data[key])
    return data


class Superseded(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.home = path.parent.resolve()
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS projects(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS project_folders(
                project_id TEXT NOT NULL REFERENCES projects(id), path_key TEXT NOT NULL,
                path TEXT NOT NULL, alias TEXT NOT NULL COLLATE NOCASE,
                PRIMARY KEY(project_id,path_key), UNIQUE(project_id,alias)
            );
            CREATE INDEX IF NOT EXISTS project_paths ON project_folders(path_key);
            CREATE TABLE IF NOT EXISTS sessions(
                id TEXT PRIMARY KEY, title TEXT NOT NULL, gemini TEXT NOT NULL, openai TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0, planned INTEGER NOT NULL DEFAULT 0,
                brief TEXT NOT NULL, versions TEXT NOT NULL, workspace TEXT NOT NULL, updated TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT NOT NULL REFERENCES sessions(id),
                kind TEXT NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL, meta TEXT NOT NULL, at TEXT NOT NULL,
                client_id TEXT, UNIQUE(sid, client_id)
            );
            CREATE INDEX IF NOT EXISTS session_events ON events(sid,seq);
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, sid TEXT NOT NULL REFERENCES sessions(id), kind TEXT NOT NULL,
                status TEXT NOT NULL, title TEXT NOT NULL, model TEXT NOT NULL, basis TEXT NOT NULL,
                payload TEXT NOT NULL, cursor TEXT, seen INTEGER NOT NULL DEFAULT 0,
                draft TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS session_jobs ON jobs(sid,status);
            CREATE TABLE IF NOT EXISTS calls(
                job TEXT NOT NULL REFERENCES jobs(id), cursor TEXT NOT NULL, cid TEXT NOT NULL,
                name TEXT NOT NULL, args TEXT NOT NULL, result TEXT, child TEXT REFERENCES jobs(id),
                PRIMARY KEY(job,cursor,cid)
            );
            CREATE TABLE IF NOT EXISTS artifacts(
                id TEXT PRIMARY KEY, sid TEXT NOT NULL REFERENCES sessions(id), job TEXT NOT NULL REFERENCES jobs(id),
                name TEXT NOT NULL, content TEXT NOT NULL, digest TEXT NOT NULL, meta TEXT NOT NULL, at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS session_artifacts ON artifacts(sid);
            CREATE TABLE IF NOT EXISTS session_cleanup(sid TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS approvals(
                id TEXT PRIMARY KEY, sid TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                job TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, source TEXT NOT NULL,
                title TEXT NOT NULL, detail TEXT NOT NULL, decision TEXT NOT NULL DEFAULT 'pending',
                at TEXT NOT NULL
            );
        """)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if "project_id" not in {r["name"] for r in self.db.execute("PRAGMA table_info(sessions)")}:
                self.db.execute("ALTER TABLE sessions ADD COLUMN project_id TEXT REFERENCES projects(id)")
                for session in self.rows("SELECT id,workspace FROM sessions ORDER BY rowid"):
                    root = Path(session["workspace"])
                    if not (root.name == session["id"] and root.parent.name == "projects"):
                        pid = self._register_folder(root)
                        self.db.execute("UPDATE sessions SET project_id=? WHERE id=?", (pid, session["id"]))
            for table, fields in (("projects", ("pinned",)), ("sessions", ("pinned", "archived"))):
                columns = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
                for field in fields:
                    if field not in columns:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {field} INTEGER NOT NULL DEFAULT 0")
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(sessions)")}
            migrate_stops = "paused_work" not in columns
            if migrate_stops:
                self.db.execute("ALTER TABLE sessions ADD COLUMN paused_work TEXT")
                self.db.execute("ALTER TABLE jobs ADD COLUMN guide_seq INTEGER NOT NULL DEFAULT 0")
                self.db.execute("ALTER TABLE jobs ADD COLUMN approval_epoch INTEGER NOT NULL DEFAULT 0")
            if "daybreak" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN daybreak INTEGER NOT NULL DEFAULT 0")
            if "mode" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'default'")
            for field in ("objective", "goal_status", "goal_note", "goal_next"):
                if field not in columns:
                    self.db.execute(f"ALTER TABLE sessions ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
            if "backend" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN backend TEXT NOT NULL DEFAULT 'api'")
            if "antigravity_session" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN antigravity_session TEXT")
            if "antigravity_seen" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN antigravity_seen INTEGER NOT NULL DEFAULT 0")
            if "backend_models" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN backend_models TEXT NOT NULL DEFAULT '{}'")
            if "permission" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN permission TEXT NOT NULL DEFAULT 'ask'")
                self.db.execute("ALTER TABLE sessions ADD COLUMN speed TEXT NOT NULL DEFAULT 'standard'")
                self.db.execute("ALTER TABLE sessions ADD COLUMN context_after INTEGER NOT NULL DEFAULT 0")
            if "context_summary" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN context_summary TEXT NOT NULL DEFAULT ''")
            if "cursor_backend" not in {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}:
                self.db.execute("ALTER TABLE jobs ADD COLUMN cursor_backend TEXT")
                self.db.execute("ALTER TABLE jobs ADD COLUMN cursor_model TEXT")
                self.db.execute("UPDATE jobs SET cursor_backend=(SELECT backend FROM sessions WHERE id=jobs.sid), "
                                "cursor_model=model WHERE cursor IS NOT NULL")
            if "efforts" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN efforts TEXT NOT NULL DEFAULT '{}'")
                self.db.execute("ALTER TABLE jobs ADD COLUMN effort TEXT NOT NULL DEFAULT ''")
                # 旧版把 Antigravity 的档位保存在具体模型 ID 的末尾。
                for level in LEVELS:
                    suffix = "%-" + level
                    self.db.execute("UPDATE sessions SET efforts=? WHERE backend='clients' AND gemini LIKE ?",
                                    (pack({"gemini": level}), suffix))
                    self.db.execute("UPDATE jobs SET effort=? WHERE kind IN ('plan','section','report') AND model LIKE ? "
                                    "AND COALESCE(cursor_backend,(SELECT backend FROM sessions WHERE id=jobs.sid))='clients'",
                                    (level, suffix))
            self.db.execute("CREATE INDEX IF NOT EXISTS project_sessions ON sessions(project_id,updated)")
            self.db.execute("UPDATE jobs SET status='ready' WHERE status='running'")
            self.db.execute("UPDATE approvals SET decision='expired' WHERE decision='pending' AND source!='MixHarness'")
            if migrate_stops:
                self.migrate_stopped_jobs()
        self.inputs = InputLibrary(self.db)

    def migrate_stopped_jobs(self):
        # 旧版只记录停止数量。仅恢复能从最后一条停止记录确定的那批任务。
        stops = self.rows("SELECT e.sid,json_extract(e.meta,'$.stopped') AS count FROM events e "
                          "WHERE e.seq=(SELECT MAX(seq) FROM events WHERE sid=e.sid) "
                          "AND json_extract(e.meta,'$.stopped')>0")
        for stop in stops:
            sid = stop["sid"]
            session = self.session(sid)
            jobs = self.rows("SELECT id,kind,basis,payload FROM jobs WHERE sid=? AND status='stopped'", sid)
            if len(jobs) != stop["count"] or self.active_jobs(sid):
                continue
            if any(j["payload"].get("revision", session["revision"]) != session["revision"] or
                   any(session["versions"][key] != v for key, v in j["basis"].items()) for j in jobs):
                continue
            planned = session["planned"]
            if any(j["kind"] == "plan" for j in jobs):
                planned = self.one("SELECT COALESCE(MAX(json_extract(meta,'$.revision')),0) AS n FROM events "
                                   "WHERE sid=? AND kind='requirements'", sid)["n"]
            prior = [{"id": j["id"], "status": "ready"} for j in jobs]
            mode, goal = session["mode"], session["goal_status"]
            if goal == "paused" and any(j["kind"] == "goal_check" for j in jobs):
                mode, goal = "goal", "active"
            self.db.execute("UPDATE sessions SET paused_work=? WHERE id=?",
                            (pack(dict(jobs=prior, mode=mode, goal_status=goal, planned=planned)), sid))
            self.db.executemany("UPDATE jobs SET approval_epoch=1 WHERE id=?", [(j["id"],) for j in jobs])

    def rows(self, sql, *params):
        return [unpack(row) for row in self.db.execute(sql, params)]

    def one(self, sql, *params):
        row = self.db.execute(sql, params).fetchone()
        if row is None:
            raise KeyError("记录不存在")
        return unpack(row)

    def session(self, sid):
        return self.one("SELECT * FROM sessions WHERE id=?", sid)

    def approvals(self, sid):
        return self.rows("SELECT a.* FROM approvals a JOIN jobs j ON j.id=a.job WHERE a.sid=? AND a.decision='pending' "
                         "AND j.status IN ('ready','running','waiting','needs_key') ORDER BY a.rowid", sid)

    def project(self, pid):
        project = self.one("SELECT * FROM projects WHERE id=?", pid)
        project["folders"] = self.rows("SELECT alias,path FROM project_folders WHERE project_id=? ORDER BY rowid", pid)
        return project

    def projects(self, archived=False):
        return [self.project(r["id"]) for r in self.rows(
            "SELECT id FROM projects WHERE archived=? ORDER BY pinned DESC,rowid", archived)]

    def _register_folder(self, directory):
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        path = directory.resolve()
        key = os.path.normcase(str(path))
        rows = self.rows("SELECT project_id,path FROM project_folders WHERE path_key=? ORDER BY rowid", key)
        if not rows and path.exists():
            rows = [r for r in self.rows("SELECT project_id,path FROM project_folders ORDER BY rowid")
                    if Path(r["path"]).exists() and path.samefile(r["path"])]
        if rows:
            pid = rows[0]["project_id"]
            self.db.execute("UPDATE projects SET archived=0 WHERE id=?", (pid,))
        else:
            pid = new_id()
            self.db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, path.name or str(path)))
            self._attach(pid, path, key)
        return pid

    def _attach(self, pid, path, key):
        existing = self.rows("SELECT path_key,path,alias FROM project_folders WHERE project_id=?", pid)
        for row in existing:
            if row["path_key"] == key or (path.exists() and Path(row["path"]).exists() and path.samefile(row["path"])):
                return
        names = {row["alias"].casefold() for row in existing}
        base = path.name or "root"
        alias = base
        suffix = 2
        while alias.casefold() in names:
            alias = f"{base}-{suffix}"
            suffix += 1
        self.db.execute("INSERT INTO project_folders VALUES(?,?,?,?)", (pid, key, str(path), alias))

    def open_project(self, directory):
        path = Path(directory).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError("请选择项目文件夹")
        with self.db:
            pid = self._register_folder(path)
        return self.project(pid)

    def save_project(self, name, directories, pid=None):
        name = name.strip()
        if not 1 <= len(name) <= 100:
            raise ValueError("项目名称需要包含 1 至 100 个字符")
        paths = []
        for directory in directories:
            path = Path(directory).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError(f"请选择来源文件夹：{path}")
            if not any(path.samefile(other) for other in paths):
                paths.append(path)
        with self.db:
            if pid is None:
                pid = new_id()
                self.db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, name))
            project = self.project(pid)
            self.db.execute("UPDATE projects SET name=? WHERE id=?", (name, pid))
            if not paths:
                directory = self.home / "project-files" / pid
                paths.append(directory)
            old_paths = {Path(f["path"]): f["alias"] for f in project["folders"]}
            if list(old_paths) != paths:
                self.require_idle_project(pid)
                if not directories:
                    paths[0].mkdir(parents=True, exist_ok=True)
                self.db.execute("DELETE FROM project_folders WHERE project_id=?", (pid,))
                # 先保留旧别名，再为同名的新文件夹分配别名，顺序以用户选择为准。
                reserved = {old_paths[p].casefold() for p in paths if p in old_paths}
                for path in paths:
                    alias = old_paths.get(path)
                    if alias is None:
                        base = "项目文件" if path == self.home / "project-files" / pid else path.name or "root"
                        alias, suffix = base, 2
                        while alias.casefold() in reserved:
                            alias = f"{base}-{suffix}"
                            suffix += 1
                    reserved.add(alias.casefold())
                    self.db.execute("INSERT INTO project_folders VALUES(?,?,?,?)",
                                    (pid, os.path.normcase(str(path)), str(path), alias))
                self.db.execute("UPDATE sessions SET workspace=? WHERE project_id=?", (str(paths[0]), pid))
        return self.project(pid)

    def require_idle_project(self, pid):
        if self.rows("SELECT j.id FROM jobs j JOIN sessions s ON s.id=j.sid WHERE s.project_id=? "
                     "AND j.status IN ('ready','running','waiting','needs_key') LIMIT 1", pid):
            raise ValueError("请等当前任务跑完后再调整项目文件夹")

    def attach_folder(self, pid, directory):
        path = Path(directory).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError("请选择来源文件夹")
        with self.db:
            project = self.project(pid)
            if any(Path(f["path"]).exists() and path.samefile(f["path"]) for f in project["folders"]):
                return project
            self.require_idle_project(pid)
            self._attach(pid, path, os.path.normcase(str(path)))
        return self.project(pid)

    def rename_project(self, pid, name):
        name = name.strip()
        if not 1 <= len(name) <= 100:
            raise ValueError("项目名称需要包含 1 至 100 个字符")
        with self.db:
            self.project(pid)
            self.db.execute("UPDATE projects SET name=? WHERE id=?", (name, pid))

    def archive_project(self, pid, archived=True):
        with self.db:
            self.project(pid)
            self.db.execute("UPDATE projects SET archived=? WHERE id=?", (archived, pid))

    def pin_project(self, pid, pinned):
        with self.db:
            self.project(pid)
            self.db.execute("UPDATE projects SET pinned=? WHERE id=?", (pinned, pid))

    def sessions(self, archived=False, query=""):
        search = ""
        params = [archived]
        if query:
            search = (" AND (instr(lower(s.title),lower(?)) OR instr(lower(p.name),lower(?)) OR EXISTS "
                      "(SELECT 1 FROM events e WHERE e.sid=s.id AND e.kind='message' AND instr(lower(e.body),lower(?))))")
            params.extend([query] * 3)
        return self.rows("SELECT s.id,s.title,s.updated,s.project_id,s.workspace,s.pinned,s.backend,"
                         "'mixharness' AS source,p.name AS project_name,EXISTS(SELECT 1 FROM jobs j WHERE j.sid=s.id "
                         "AND j.status IN ('ready','running','waiting')) AS working "
                         "FROM sessions s LEFT JOIN projects p ON p.id=s.project_id "
                         "WHERE s.archived=? AND (s.archived=1 OR COALESCE(p.archived,0)=0)" + search +
                         " ORDER BY s.pinned DESC,s.updated DESC,s.rowid DESC", *params)

    def rename_session(self, sid, title):
        title = title.strip()
        if not 1 <= len(title) <= 200:
            raise ValueError("会话名称需要包含 1 至 200 个字符")
        with self.db:
            self.session(sid)
            self.db.execute("UPDATE sessions SET title=? WHERE id=?", (title, sid))

    def pin_session(self, sid, pinned):
        with self.db:
            self.session(sid)
            self.db.execute("UPDATE sessions SET pinned=? WHERE id=?", (pinned, sid))

    def archive_session(self, sid, archived=True):
        with self.db:
            session = self.session(sid)
            self.db.execute("UPDATE sessions SET archived=? WHERE id=?", (archived, sid))
            if not archived and session["project_id"]:
                self.db.execute("UPDATE projects SET archived=0 WHERE id=?", (session["project_id"],))

    def delete_session(self, sid):
        with self.db:
            self.session(sid)
            jobs = self.rows("SELECT id FROM jobs WHERE sid=?", sid)
            self.db.execute("DELETE FROM calls WHERE job IN (SELECT id FROM jobs WHERE sid=?)", (sid,))
            for table in ("artifacts", "events", "jobs"):
                self.db.execute(f"DELETE FROM {table} WHERE sid=?", (sid,))
            self.db.execute("DELETE FROM draft_inputs WHERE draft=?", ("drafts/" + sid,))
            self.inputs.prune()
            self.db.execute("DELETE FROM sessions WHERE id=?", (sid,))
            self.db.execute("INSERT INTO session_cleanup VALUES(?)", (sid,))
        return [j["id"] for j in jobs]

    def job(self, jid):
        return self.one("SELECT * FROM jobs WHERE id=?", jid)

    def event(self, sid, kind, actor, body, meta=None, client_id=None):
        stamp = now()
        self.db.execute("UPDATE sessions SET updated=? WHERE id=?", (stamp, sid))
        return self.db.execute(
            "INSERT INTO events(sid,kind,actor,body,meta,at,client_id) VALUES(?,?,?,?,?,?,?)",
            (sid, kind, actor, body, pack(meta or {}), stamp, client_id),
        ).lastrowid

    def create(self, sid, workspace, gemini, openai, project_id=None, backend="api", efforts=None,
               permission="ask", speed="standard", mode="default", daybreak=False):
        if backend not in ("api", "clients"):
            raise ValueError("未知的执行方式")
        if mode not in MODES:
            raise ValueError("请选择默认、计划或目标模式")
        with self.db:
            if project_id is None and workspace != self.home / "projects" / sid:
                project_id = self._register_folder(Path(workspace))
            self.db.execute(
                "INSERT INTO sessions(id,title,workspace,gemini,openai,brief,versions,updated,project_id,backend,efforts,permission,speed,mode,daybreak) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, "新会话", str(workspace), gemini, openai, pack(dict.fromkeys(SCOPES, "")),
                 pack(dict.fromkeys(SCOPES, 0)), now(), project_id, backend, pack(efforts or {}), permission, speed, mode, daybreak),
            )
        return self.session(sid)

    def enqueue(self, session, kind, payload=None, title=None):
        chat = kind in CHAT_JOBS.values()
        if session["mode"] == "plan" and kind != "proposal" and not chat:
            raise ValueError("请先退出计划模式或选择执行计划")
        if session["mode"] == "side" and kind != "side" and not chat:
            raise ValueError("侧边聊天只支持问答，切换到默认模式即可执行任务")
        jid = new_id()
        basis = {key: session["versions"][key] for key in DEPENDS[kind]}
        provider = PROVIDERS[kind]
        self.db.execute(
            "INSERT INTO jobs(id,sid,kind,status,title,model,effort,basis,payload,at) VALUES(?,?,?,'ready',?,?,?,?,?,?)",
            (jid, session["id"], kind, title or LABELS[kind], session[provider], session["efforts"].get(provider, ""),
             pack(basis), pack(payload or {}), now()),
        )
        if session.get("paused_work") and not chat:
            self.db.execute("UPDATE sessions SET paused_work=NULL WHERE id=?", (session["id"],))
        return jid

    def fork_session(self, sid, title, *, workspace=None, side=False):
        original = self.session(sid)
        if original["archived"]:
            raise ValueError("请先恢复要分支的会话")
        suffix = "（侧边）" if side else "（分支）"
        name = title.strip() or original["title"][:200 - len(suffix)] + suffix
        if len(name) > 200:
            raise ValueError("会话名称最多 200 个字符")
        branch = new_id()
        with self.db:
            project = self._register_folder(workspace) if workspace else original["project_id"]
            mode = "plan" if original["mode"] == "plan" else "default"
            goal_status = "paused" if original["goal_status"] == "active" else original["goal_status"]
            objective, note, speed = original["objective"], original["goal_note"], "standard"
            if side:
                mode, objective, goal_status, note, speed = "side", "", "", "", original["speed"]
            self.db.execute("INSERT INTO sessions(id,title,workspace,gemini,openai,brief,versions,updated,project_id,"
                            "backend,efforts,revision,planned,mode,objective,goal_status,goal_note,speed,daybreak) "
                            "SELECT ?,?,?,gemini,openai,brief,versions,?,?,backend,efforts,revision,revision,?,?,?,?,?,daybreak "
                            "FROM sessions WHERE id=?", (branch, name, str(workspace or original["workspace"]), now(), project, mode,
                            objective, goal_status, note, speed, sid))
            inputs = report_inputs(self.task_summaries(sid, with_output=False))
            copied_prompts = {}
            for event in self.db.execute("SELECT seq,kind,actor,body,meta,at FROM events WHERE sid=? AND kind='message' ORDER BY seq", (sid,)):
                original_meta = json.loads(event["meta"])
                meta = {}
                if event["actor"] == "user":
                    meta = original_meta
                elif original_meta.get("recipient"):
                    meta = {k: original_meta[k] for k in ("recipient", "kind", "model", "effort", "backend") if k in original_meta}
                    if original_meta.get("reply_to") in copied_prompts:
                        meta["reply_to"] = copied_prompts[original_meta["reply_to"]]
                elif event["actor"] == "openai":
                    scope = report_input_kind(original_meta, inputs)
                    if scope:
                        meta = {"report_input": scope, "model": original_meta.get("model")}
                seq = self.db.execute("INSERT INTO events(sid,kind,actor,body,meta,at) VALUES(?,?,?,?,?,?)",
                                      (branch, event["kind"], event["actor"], event["body"], pack(meta), event["at"])).lastrowid
                if event["actor"] == "user" and meta.get("recipient"):
                    copied_prompts[event["seq"]] = seq
                self.db.execute("INSERT INTO message_inputs SELECT ?,item FROM message_inputs WHERE seq=? ORDER BY rowid",
                                (seq, event["seq"]))
            if not side and self.current_proposal(sid):
                self.db.execute("INSERT INTO jobs(id,sid,kind,title,model,effort,basis,payload,status,draft,at) "
                                "SELECT ?,?,kind,title,model,effort,basis,payload,status,draft,at FROM jobs "
                                "WHERE sid=? AND kind='proposal' ORDER BY rowid DESC LIMIT 1", (new_id(), branch, sid))
            self.event(branch, "progress", "system", "从原会话创建分支", {"source_session": sid})
        return self.session(branch)

    def message(self, sid, body, client_id, *, draft=None, attachments=()):
        with self.db:
            return self._message(sid, body, client_id, draft, attachments)

    def record_prompt(self, session, body, meta, client_id, draft, attachments):
        staged = self.inputs.metadata(draft) if draft else []
        if not body.strip() and not attachments and not staged:
            raise ValueError("请输入消息或添加附件")
        sid = session["id"]
        seq = self.event(sid, "message", "user", body, meta, client_id)
        self.inputs.commit_message(seq, draft, attachments)
        if session["title"] == "新会话" and not self.db.execute(
                "SELECT 1 FROM events WHERE sid=? AND actor='user' AND seq<? LIMIT 1", (sid, seq)).fetchone():
            title = body.strip().splitlines()[0] if body.strip() else (staged[0]["name"] if staged else attachments[0].name)
            self.db.execute("UPDATE sessions SET title=? WHERE id=?", (title[:36], sid))
        return seq

    def chat_message(self, sid, provider, body, client_id, draft, attachments):
        if provider not in CHAT_JOBS:
            raise ValueError("请选择 Gemini 或 GPT")
        with self.db:
            session = self.session(sid)
            sent = self.rows("SELECT seq FROM events WHERE sid=? AND client_id=?", sid, client_id)
            if sent:
                return sent[0]["seq"], False
            if not session[provider]:
                raise ValueError("请先选择对话模型")
            seq = self.record_prompt(session, body, {"direct": True, "recipient": provider}, client_id, draft, attachments)
            self.enqueue(session, CHAT_JOBS[provider], {"message_seq": seq, "read_only": True})
        return seq, True

    def _message(self, sid, body, client_id, draft=None, attachments=()):
        session = self.session(sid)
        sent = self.rows("SELECT seq FROM events WHERE sid=? AND client_id=?", sid, client_id)
        if sent:
            return sent[0]["seq"]
        self.db.execute("UPDATE sessions SET paused_work=NULL WHERE id=? AND paused_work IS NOT NULL", (sid,))
        rev = session["revision"] + 1
        seq = self.record_prompt(session, body, {"revision": rev}, client_id, draft, attachments)
        self.db.execute("UPDATE sessions SET revision=? WHERE id=?", (rev, sid))
        self.db.execute("UPDATE jobs SET status='obsolete' WHERE sid=? AND kind IN ('plan','proposal','goal_check','side') "
                        "AND status NOT IN ('done','obsolete','stopped')", (sid,))
        if session["mode"] in ("plan", "side"):
            self.db.execute("UPDATE sessions SET planned=revision WHERE id=?", (sid,))
            kind = "proposal" if session["mode"] == "plan" else "side"
            self.enqueue(session, kind, {"revision": rev})
        else:
            if session["mode"] == "goal":
                if not session["objective"] and not body.strip():
                    raise ValueError("请用文字说明要达成的目标")
                self.db.execute("UPDATE sessions SET objective=CASE WHEN objective='' THEN ? ELSE objective END,"
                                "goal_status='active',goal_next='' WHERE id=?", (body.strip(), sid))
            self.enqueue(session, "plan", {"revision": rev})
        return seq

    def active_jobs(self, sid):
        return self.rows("SELECT id,kind,status,guide_seq,approval_epoch FROM jobs WHERE sid=? AND status IN "
                         "('ready','running','waiting','needs_key') ORDER BY rowid", sid)

    def guide_message(self, sid, body, client_id, jobs, draft, attachments):
        staged = self.inputs.metadata(draft) if draft else []
        if not body.strip() and not attachments and not staged:
            raise ValueError("请输入引导内容或添加附件")
        session = self.session(sid)
        seq = self.event(sid, "message", "user", body, {"guide": True, "revision": session["revision"],
                                                      "jobs": [j["id"] for j in jobs]}, client_id)
        self.inputs.commit_message(seq, draft, attachments)
        withdrawn = pack({"cancelled": True, "reason": "用户补充了要求，请根据最新消息重新判断是否需要这项操作。"})
        for job in jobs:
            jid = job["id"]
            self.db.execute("UPDATE jobs SET guide_seq=? WHERE id=?", (seq, jid))
            self.db.execute("UPDATE calls SET result=? WHERE job=? AND result IS NULL AND child IS NULL "
                            "AND cursor=(SELECT cursor FROM jobs WHERE id=?)", (withdrawn, jid, jid))
            self.db.execute("UPDATE approvals SET decision='expired' WHERE job=? AND decision='pending'", (jid,))
        return seq

    def stop_jobs(self, sid, *, user_requested=False):
        if user_requested:
            jobs = self.active_jobs(sid)
            if not jobs:
                return 0
            session = self.session(sid)
            checkpoint = session["paused_work"] or {field: session[field] for field in ("mode", "goal_status", "planned")}
            checkpoint.setdefault("jobs", []).extend({"id": j["id"], "status": j["status"]} for j in jobs)
            self.db.execute("UPDATE sessions SET paused_work=? WHERE id=?", (pack(checkpoint), sid))
            self.db.executemany("UPDATE jobs SET approval_epoch=approval_epoch+1 WHERE id=?", [(j["id"],) for j in jobs])
        else:
            self.db.execute("UPDATE sessions SET paused_work=NULL WHERE id=? AND paused_work IS NOT NULL", (sid,))
        status = "stopped" if user_requested else "obsolete"
        count = self.db.execute("UPDATE jobs SET status=? WHERE sid=? AND status IN "
                                "('ready','running','waiting','needs_key')", (status, sid)).rowcount
        self.db.execute("UPDATE approvals SET decision='expired' WHERE sid=? AND decision='pending'", (sid,))
        self.db.execute("UPDATE sessions SET planned=revision WHERE id=?", (sid,))
        return count

    def continue_jobs(self, sid):
        session = self.session(sid)
        checkpoint = session["paused_work"]
        if not checkpoint:
            return 0
        for job in checkpoint["jobs"]:
            status = "ready" if job["status"] == "running" else job["status"]
            provider = PROVIDERS[self.job(job["id"])["kind"]]
            self.db.execute("UPDATE jobs SET status=?,model=?,effort=? WHERE sid=? AND id=? AND status='stopped'",
                            (status, session[provider], session["efforts"].get(provider, ""), sid, job["id"]))
        self.db.execute("UPDATE sessions SET planned=?,mode=?,goal_status=?,paused_work=NULL WHERE id=?",
                        (checkpoint["planned"], checkpoint["mode"], checkpoint["goal_status"], sid))
        self.wake(sid)
        self.event(sid, "progress", "system", "已继续上次停止的任务", {"continued": len(checkpoint["jobs"])})
        return len(checkpoint["jobs"])

    def change_mode(self, sid, mode, *, objective=None):
        if mode not in MODES:
            raise ValueError("请选择默认、计划或目标模式")
        if objective is not None and (mode != "goal" or not objective.strip() or len(objective) > 120000):
            raise ValueError("请在目标模式中填写 1 至 120,000 字的目标")
        with self.db:
            session = self.session(sid)
            if mode == session["mode"] and objective is None:
                return
            self.stop_jobs(sid)
            self.db.execute("UPDATE sessions SET mode=?,goal_status=CASE WHEN goal_status='active' THEN 'paused' "
                            "ELSE goal_status END WHERE id=?", (mode, sid))
            self.event(sid, "progress", "system", "已切换至" + MODES[mode])
            if mode == "goal":
                goal = objective if objective is not None else session["objective"]
                if objective is not None or session["goal_status"] == "complete":
                    goal = (objective or "").strip()
                    self.db.execute("UPDATE sessions SET objective=?,goal_status='',goal_note='',goal_next='' WHERE id=?", (goal, sid))
                if goal:
                    self._message(sid, goal if objective is not None else "继续当前目标", new_id())

    def goal_action(self, sid, action):
        with self.db:
            session = self.session(sid)
            if not session["objective"]:
                raise ValueError("请先设置目标")
            if action == "resume":
                if session["goal_status"] not in ("paused", "blocked"):
                    raise ValueError("只有暂停或需要补充的目标可以继续")
                if session["paused_work"] and session["paused_work"]["mode"] == "goal":
                    self.continue_jobs(sid)
                    return
                self.stop_jobs(sid)
                self.db.execute("UPDATE sessions SET mode='goal' WHERE id=?", (sid,))
                self._message(sid, "继续当前目标", new_id())
            elif action in ("pause", "clear"):
                if action == "pause" and session["goal_status"] != "active":
                    raise ValueError("当前目标没有运行")
                if session["mode"] == "goal":
                    self.stop_jobs(sid)
                    self.db.execute("UPDATE sessions SET mode='default' WHERE id=?", (sid,))
                if action == "clear":
                    self.db.execute("UPDATE sessions SET objective='',goal_status='',goal_note='',goal_next='',paused_work=NULL "
                                    "WHERE id=?", (sid,))
                else:
                    self.db.execute("UPDATE sessions SET goal_status='paused' WHERE id=?", (sid,))
                self.event(sid, "progress", "system", "目标已暂停" if action == "pause" else "目标已清除")
            else:
                raise ValueError("未知的目标操作")

    def current_proposal(self, sid):
        session = self.session(sid)
        rows = self.rows("SELECT draft,payload,status FROM jobs WHERE sid=? AND kind='proposal' ORDER BY rowid DESC LIMIT 1", sid)
        if session["mode"] == "plan" and rows:
            plan = rows[0]
            if plan["status"] == "done" and plan["payload"]["revision"] == session["revision"] and not plan["payload"].get("needs_input"):
                return plan["draft"]
        return None

    def implement_plan(self, sid):
        with self.db:
            plan = self.current_proposal(sid)
            if not plan:
                raise ValueError("请先在计划模式下完成当前计划")
            self.stop_jobs(sid)
            self.db.execute("UPDATE sessions SET mode='default' WHERE id=?", (sid,))
            self._message(sid, "请执行已确认的计划。\n" + plan, new_id())

    def assess_goal(self, job, verdict, note, step):
        session = self.guard(job)
        if session["mode"] != "goal" or session["goal_status"] != "active":
            raise Superseded()
        if verdict == "continue" and not step.strip():
            raise ValueError("请说明下一步要做什么")
        if verdict == "continue" and step.strip() == session["goal_next"]:
            verdict, note = "blocked", note + "\n上轮已尝试同一步骤，请补充要求或输入。"
        if verdict == "continue" and session["goal_next"]:
            last = self.db.execute("SELECT rowid FROM jobs WHERE sid=? AND kind='goal_check' AND status='done' "
                                   "ORDER BY rowid DESC LIMIT 1", (session["id"],)).fetchone()
            if last and not self.db.execute("SELECT 1 FROM jobs WHERE sid=? AND rowid>? AND kind IN "
                                            "('engineering','report','section','verification') AND status='done' LIMIT 1",
                                            (session["id"], last[0])).fetchone():
                verdict, note = "blocked", note + "\n上轮没有完成新的工作，请补充信息后继续。"
        status = "active" if verdict == "continue" else verdict
        self.db.execute("UPDATE sessions SET goal_status=?,goal_note=?,goal_next=? WHERE id=?",
                        (status, note, step.strip() if status == "active" else "", session["id"]))
        self.db.execute("UPDATE jobs SET status='done',draft=? WHERE id=?", (note, job["id"]))
        self.event(session["id"], "message", "openai", GOAL_STATES[status] + " · " + note + ("\n下一步：" + step if verdict == "continue" else ""),
                   {"job": job["id"], "kind": "goal_check"})
        if verdict == "continue":
            self.enqueue(session, "plan", {"revision": session["revision"], "followup": step})
        elif verdict == "complete":
            self.db.execute("UPDATE sessions SET mode='default' WHERE id=?", (session["id"],))

    def set_models(self, sid, gemini, openai, efforts=None, *, daybreak=None):
        with self.db:
            old = self.session(sid)
            models = {"gemini": gemini, "openai": openai}
            selected = {p: (efforts or {}).get(p, old["efforts"].get(p, "") if old[p] == m else "") for p, m in models.items()}
            self.db.execute("UPDATE sessions SET gemini=?,openai=?,efforts=?,daybreak=COALESCE(?,daybreak),speed=? WHERE id=?",
                            (gemini, openai, pack(selected), daybreak, old["speed"] if openai == old["openai"] else "standard", sid))
            for p, model in models.items():
                if old[p] == model and old["efforts"].get(p, "") == selected[p]:
                    continue
                # API 工具链保留已使用的模型，档位可在同一模型的下一轮单独更新。
                self.db.executemany(
                    "UPDATE jobs SET model=?,effort=? WHERE sid=? AND kind=? AND status NOT IN ('done','obsolete','stopped') "
                    "AND (model=? OR ?='clients' OR (status!='running' AND (cursor IS NULL OR cursor_backend!=? OR cursor_model!=model)))",
                    [(model, selected[p], sid, kind, model, old["backend"], old["backend"])
                     for kind, provider in PROVIDERS.items() if provider == p],
                )
            self.event(sid, "models", "system", "已更新模型设置", {**models, "efforts": selected,
                                                            "daybreak": old["daybreak"] if daybreak is None else daybreak})

    def switch_backend(self, sid, backend, models, efforts=None, *, recipient=None):
        required = (recipient,) if recipient else CHAT_JOBS
        if backend not in ("api", "clients") or not all(models.get(p) for p in required):
            raise ValueError("请选择执行方式和对应的模型")
        with self.db:
            session = self.session(sid)
            if session["backend"] == backend:
                return
            remembered = session["backend_models"]
            remembered[session["backend"]] = {p: session[p] for p in ("gemini", "openai")}
            remembered[session["backend"]]["efforts"] = session["efforts"]
            selected = efforts or {}
            self.db.execute("UPDATE sessions SET backend=?,gemini=?,openai=?,backend_models=?,efforts=?,speed='standard',daybreak=0 WHERE id=?",
                            (backend, models["gemini"], models["openai"], pack(remembered), pack(selected), sid))
            self.db.executemany(
                "UPDATE jobs SET model=?,effort=?,status=CASE WHEN status='needs_key' THEN 'ready' ELSE status END "
                "WHERE sid=? AND kind=? AND status NOT IN ('done','obsolete','stopped')",
                [(models[provider], selected.get(provider, ""), sid, kind) for kind, provider in PROVIDERS.items()],
            )
            name = "本机客户端" if backend == "clients" else "API"
            self.event(sid, "progress", "system", "已切换至" + name, {"backend": backend, **models})

    def guard(self, job):
        if not self.rows("SELECT 1 FROM jobs WHERE id=? AND status='running'", job["id"]):
            raise Superseded()
        session = self.session(job["sid"])
        if any(session["versions"][k] != v for k, v in job["basis"].items()):
            raise Superseded()
        return session

    def apply_plan(self, job, changes, answer):
        with self.db:
            s = self.guard(job)
            if s["revision"] != job["payload"]["revision"]:
                raise Superseded()
            changed = set()
            for key in SCOPES:
                value = changes.get(key)
                if (value is not None and value != s["brief"][key]) or key in changes.get("rerun", ()):
                    if value is not None:
                        s["brief"][key] = value
                    s["versions"][key] += 1
                    changed.add(key)
            self.db.execute("UPDATE sessions SET planned=revision,brief=?,versions=? WHERE id=?",
                            (pack(s["brief"]), pack(s["versions"]), s["id"]))
            for old in self.rows("SELECT * FROM jobs WHERE sid=? AND status NOT IN ('done','obsolete','stopped')", s["id"]):
                if old["payload"].get("direct") and old["status"] in ("needs_key", "ready") and not old["cursor"]:
                    self.db.execute("UPDATE jobs SET basis=? WHERE id=?", (pack({k: s["versions"][k] for k in old["basis"]}), old["id"]))
                elif changed.intersection(old["basis"]):
                    self.db.execute("UPDATE jobs SET status='obsolete' WHERE id=?", (old["id"],))
                elif old["kind"] == "verification" and "question" in old["payload"] and "report" in changed and not s["brief"]["report"]:
                    self.db.execute("UPDATE jobs SET status='obsolete' WHERE id=?", (old["id"],))
            assigned = []
            for kind in SCOPES:
                if s["brief"][kind] and changed.intersection(DEPENDS[kind]):
                    payload = {}
                    if kind in ("engineering", "verification"):
                        mode = changes.get(kind + "_mode")
                        if mode == "inspect":
                            payload["read_only"] = True
                        elif mode is None and changes.get(kind) is None:
                            previous = self.rows("SELECT payload FROM jobs WHERE sid=? AND kind=? "
                                                 "AND json_extract(payload,'$.question') IS NULL ORDER BY rowid DESC LIMIT 1",
                                                 s["id"], kind)
                            if previous and previous[0]["payload"].get("read_only"):
                                payload["read_only"] = True
                    if kind == "engineering":
                        if changes.get("constraints"):
                            payload["constraints"] = list(changes["constraints"])
                        if changes.get("supervise") and not payload.get("read_only"):
                            payload["supervise"] = True
                            payload["review_round"] = 0
                    jid = self.enqueue(s, kind, payload)
                    if kind == "report" and changes.get("report_after_gpt") and self.pending_work(s["id"]):
                        self.db.execute("UPDATE jobs SET status='waiting' WHERE id=?", (jid,))
                        payload["after_gpt"] = True
                    assigned.append({"job": jid, "kind": kind, "model": s[PROVIDERS[kind]], **payload})
            messages = self.rows("SELECT seq FROM events WHERE sid=? AND actor='user' "
                                 "AND json_extract(meta,'$.revision')>? AND json_extract(meta,'$.revision')<=? "
                                 "AND NOT COALESCE(json_extract(meta,'$.direct'),0) ORDER BY seq",
                                 s["id"], s["planned"], s["revision"])
            self.event(s["id"], "requirements", "gemini", "需求已更新" if changed else "已核对当前需求",
                       {"job": job["id"], "model": job["model"], "revision": s["revision"],
                        "source_messages": [m["seq"] for m in messages], "followup": job["payload"].get("followup"),
                        "interpretation": changes.get("interpretation", ""), "assigned": assigned,
                        "changed": [k for k in SCOPES if k in changed], "brief": s["brief"], "versions": s["versions"]})
            if answer:
                self.event(s["id"], "message", "gemini", answer)
            self.db.execute("UPDATE jobs SET status='done',model=?,effort=? WHERE id=?", (job["model"], job["effort"], job["id"]))
            self.wake(s["id"])

    def calls(self, job):
        return self.rows("SELECT * FROM calls WHERE job=? AND cursor=? ORDER BY rowid", job["id"], job["cursor"])

    def save_turn(self, job, cursor, seen, text, calls, *, backend):
        self.db.execute(
            "UPDATE jobs SET cursor=?,cursor_backend=?,cursor_model=?,seen=?,draft=CASE WHEN ?='' THEN draft ELSE ? END WHERE id=?",
            (cursor, backend, job["model"], seen, text, text, job["id"]),
        )
        for call in calls:
            self.db.execute("INSERT INTO calls(job,cursor,cid,name,args) VALUES(?,?,?,?,?)",
                            (job["id"], cursor, call["cid"], call["name"], pack(call["args"])))

    def tool_result(self, call, result):
        self.db.execute("UPDATE calls SET result=? WHERE job=? AND cursor=? AND cid=?",
                        (pack(result), call["job"], call["cursor"], call["cid"]))

    def request_verification(self, job, call):
        existing = self.one("SELECT child FROM calls WHERE job=? AND cursor=? AND cid=?",
                            call["job"], call["cursor"], call["cid"])
        if existing["child"]:
            return
        args = call["args"]
        session = self.session(job["sid"])
        child = None
        for check in self.rows("SELECT * FROM jobs WHERE sid=? AND kind='verification' AND status IN ('ready','running','needs_key')", job["sid"]):
            if (check["payload"].get("question") == args["question"] and
                    check["payload"].get("evidence_needed") == args["evidence_needed"] and
                    bool(check["payload"].get("read_only")) == (args.get("method") == "inspect") and
                    all(session["versions"][key] == version for key, version in check["basis"].items())):
                child = check["id"]
                break
        if child is None:
            payload = dict(args, read_only=True) if args.get("method") == "inspect" else args
            child = self.enqueue(session, "verification", payload, args["question"][:90])
        self.db.execute("UPDATE calls SET child=? WHERE job=? AND cursor=? AND cid=?",
                        (child, job["id"], call["cursor"], call["cid"]))
        self.event(job["sid"], "verification", "gemini", args["question"], {"job": child, "parent": job["id"]})

    def finish(self, job, text, result, success=True):
        self.db.execute("UPDATE jobs SET status=?,draft=?,error=?,model=?,effort=? WHERE id=?",
                        ("done" if success else "failed", text if success else job["draft"],
                         "" if success else text, job["model"], job["effort"], job["id"]))
        for call in self.rows("SELECT * FROM calls WHERE child=? AND result IS NULL", job["id"]):
            self.tool_result(call, result)
        kind = "artifact" if job["kind"] == "section" else "message"
        if job["kind"] in CHAT_JOBS.values():
            result = dict(result, reply_to=job["payload"]["message_seq"], recipient=PROVIDERS[job["kind"]])
        self.event(job["sid"], kind if success else "failure", PROVIDERS[job["kind"]], text,
                   {"job": job["id"], "model": job["model"], "effort": job["effort"],
                    "backend": job.get("backend"), "kind": job["kind"], "basis": job["basis"], **result})
        if not success and job["kind"] in ("plan", "goal_check"):
            blocked = self.db.execute("UPDATE sessions SET goal_status='blocked',goal_note=?,goal_next='' "
                                      "WHERE id=? AND mode='goal' AND goal_status='active'", (text, job["sid"]))
            if blocked.rowcount:
                self.stop_jobs(job["sid"])
        if job["kind"] not in CHAT_JOBS.values():
            self.wake(job["sid"])

    def apply_supervision(self, job, verdict, summary, issues):
        sid = job["sid"]
        self.guard(job)
        self.db.execute("UPDATE jobs SET status='done',draft=?,error='',model=?,effort=? WHERE id=?",
                        (summary, job["model"], job["effort"], job["id"]))
        review_round = job["payload"].get("review_round", 0)
        if verdict == "pass":
            self.event(sid, "progress", "gemini", "监工审查通过 · " + summary,
                       {"job": job["id"], "kind": "supervision", "verdict": "pass"})
        elif verdict == "reject" and review_round < 2:
            self.event(sid, "progress", "gemini", f"监工打回（第 {review_round + 1} 轮）· {summary}",
                       {"job": job["id"], "kind": "supervision", "verdict": "reject", "issues": issues})
            session = self.session(sid)
            rework_payload = {
                "supervise": True,
                "review_round": review_round + 1,
                "constraints": job["payload"].get("constraints", []),
                "supervision_feedback": {"summary": summary, "issues": issues},
            }
            self.enqueue(session, "engineering", rework_payload, f"返工修复（第 {review_round + 1} 轮）")
        else:
            prefix = "监工提醒（已达最大返工轮次，请确认）" if verdict == "reject" else "监工提醒（需要你拍板）"
            lines = [f"{prefix}：{summary}", *(f"- {item}" for item in issues)]
            self.event(sid, "message", "gemini", "\n".join(lines),
                       {"job": job["id"], "kind": "supervision", "verdict": verdict, "issues": issues})
        self.wake(sid)

    def pending_work(self, sid):
        return self.db.execute(
            "SELECT 1 FROM jobs WHERE sid=? AND kind IN ('engineering','verification','supervision','section') "
            "AND status IN ('ready','running','needs_key','waiting') LIMIT 1", (sid,),
        ).fetchone() is not None

    def report_wait_status(self, sid, seen):
        if self.pending_work(sid):
            return "waiting"
        # GPT 可能在 Gemini 写作期间结束。这份草稿需要再读一次实际返回的正文或错误。
        if self.db.execute("SELECT 1 FROM events WHERE sid=? AND seq>? AND actor='openai' "
                           "AND kind IN ('message','failure') AND json_extract(meta,'$.kind') "
                           "IN ('engineering','verification') LIMIT 1", (sid, seen)).fetchone():
            return "ready"
        return None

    def wake(self, sid):
        for job in self.rows("SELECT * FROM jobs WHERE sid=? AND status='waiting'", sid):
            calls = self.calls(job)
            if any(call["child"] and call["result"] is None for call in calls):
                continue
            if calls or not self.pending_work(sid):
                self.db.execute("UPDATE jobs SET status='ready' WHERE id=?", (job["id"],))
        session = self.session(sid)
        if session["mode"] == "goal" and session["goal_status"] == "active" and not self.db.execute(
                "SELECT 1 FROM jobs WHERE sid=? AND kind NOT IN ('gemini_chat','gpt_chat') "
                "AND status IN ('ready','running','needs_key','waiting') LIMIT 1", (sid,)).fetchone():
            self.enqueue(session, "goal_check", {"revision": session["revision"]})

    def task_summaries(self, sid, *, with_output=True):
        columns = "id,kind,status,guide_seq,approval_epoch,basis,json_extract(payload,'$.direct') AS direct"
        if with_output:
            columns += ",title,model,effort,draft,error,at"
        return self.rows(f"SELECT {columns} FROM jobs WHERE sid=? ORDER BY rowid", sid)

    def snapshot(self, sid):
        events = self.rows("SELECT * FROM events WHERE sid=? ORDER BY seq", sid)
        inputs = self.inputs.for_session(sid)
        for event in events:
            if event["seq"] in inputs:
                event["attachments"] = inputs[event["seq"]]
        approvals = self.rows("SELECT id,source,title,decision FROM approvals WHERE sid=?", sid)
        tasks = self.task_summaries(sid)
        describe_events(events, tasks, approvals)
        return {
            "session": self.session(sid),
            "approvals": self.approvals(sid),
            "events": events,
            "tasks": tasks,
            "artifacts": self.rows("SELECT id,job,name,digest,meta,at FROM artifacts WHERE sid=? ORDER BY rowid", sid),
        }
