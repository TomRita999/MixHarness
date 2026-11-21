import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .reasoning import LEVELS


SCOPES = ("engineering", "verification", "report")
DEPENDS = {
    "plan": (),
    "engineering": ("engineering",),
    "verification": ("engineering", "verification"),
    "report": SCOPES,
    "section": SCOPES,
}
PROVIDERS = {"plan": "gemini", "report": "gemini", "section": "gemini", "engineering": "openai", "verification": "openai"}
LABELS = {"plan": "解析需求", "engineering": "开发与研究", "verification": "验证", "report": "撰写报告", "section": "撰写章节"}
STATUSES = {"ready": "待执行", "running": "进行中", "needs_key": "等待密钥", "waiting": "等待中",
            "done": "已完成", "failed": "失败", "obsolete": "已跳过"}
JSON_FIELDS = {"brief", "versions", "basis", "payload", "args", "result", "meta", "backend_models", "efforts"}


def pack(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id():
    return uuid4().hex


def unpack(row):
    data = dict(row)
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
            if "backend" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN backend TEXT NOT NULL DEFAULT 'api'")
            if "antigravity_session" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN antigravity_session TEXT")
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
                         "AND j.status NOT IN ('obsolete','failed','done') ORDER BY a.rowid", sid)

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
                     "AND j.status NOT IN ('done','obsolete','failed') LIMIT 1", pid):
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
                         "'mixharness' AS source,p.name AS project_name "
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

    def create(self, sid, workspace, gemini, openai, project_id=None, backend="api", efforts=None, permission="ask", speed="standard"):
        if backend not in ("api", "clients"):
            raise ValueError("未知的执行方式")
        with self.db:
            if project_id is None and workspace != self.home / "projects" / sid:
                project_id = self._register_folder(Path(workspace))
            self.db.execute(
                "INSERT INTO sessions(id,title,workspace,gemini,openai,brief,versions,updated,project_id,backend,efforts,permission,speed) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, "新会话", str(workspace), gemini, openai, pack(dict.fromkeys(SCOPES, "")),
                 pack(dict.fromkeys(SCOPES, 0)), now(), project_id, backend, pack(efforts or {}), permission, speed),
            )
        return self.session(sid)

    def enqueue(self, session, kind, payload=None, title=None):
        jid = new_id()
        basis = {key: session["versions"][key] for key in DEPENDS[kind]}
        provider = PROVIDERS[kind]
        self.db.execute(
            "INSERT INTO jobs(id,sid,kind,status,title,model,effort,basis,payload,at) VALUES(?,?,?,'ready',?,?,?,?,?,?)",
            (jid, session["id"], kind, title or LABELS[kind], session[provider], session["efforts"].get(provider, ""),
             pack(basis), pack(payload or {}), now()),
        )
        return jid

    def fork_session(self, sid, title):
        original = self.session(sid)
        if original["archived"]:
            raise ValueError("请先恢复要分支的会话")
        name = title.strip() or original["title"] + "（分支）"
        if len(name) > 200:
            raise ValueError("会话名称最多 200 个字符")
        branch = new_id()
        with self.db:
            self.db.execute("INSERT INTO sessions(id,title,workspace,gemini,openai,brief,versions,updated,project_id,"
                            "backend,efforts,revision,planned) SELECT ?,?,workspace,gemini,openai,brief,versions,?,"
                            "project_id,backend,efforts,revision,planned FROM sessions WHERE id=?", (branch, name, now(), sid))
            self.db.execute("INSERT INTO events(sid,kind,actor,body,meta,at) SELECT ?,kind,actor,body,"
                            "CASE WHEN actor='user' THEN meta ELSE '{}' END,at FROM events "
                            "WHERE sid=? AND kind='message' ORDER BY seq", (branch, sid))
            self.event(branch, "progress", "system", "从原会话创建分支", {"source_session": sid})
        return self.session(branch)

    def message(self, sid, body, client_id):
        with self.db:
            session = self.session(sid)
            sent = self.rows("SELECT seq FROM events WHERE sid=? AND client_id=?", sid, client_id)
            if sent:
                return sent[0]["seq"]
            rev = session["revision"] + 1
            seq = self.event(sid, "message", "user", body, {"revision": rev}, client_id)
            title = body.splitlines()[0][:36] if rev == 1 and session["title"] == "新会话" else session["title"]
            self.db.execute("UPDATE sessions SET revision=?,title=? WHERE id=?", (rev, title, sid))
            self.db.execute("UPDATE jobs SET status='obsolete' WHERE sid=? AND kind='plan' AND status!='done'", (sid,))
            self.enqueue(session, "plan", {"revision": rev})
            return seq

    def set_models(self, sid, gemini, openai, efforts=None):
        with self.db:
            old = self.session(sid)
            models = {"gemini": gemini, "openai": openai}
            selected = {p: (efforts or {}).get(p, old["efforts"].get(p, "") if old[p] == m else "") for p, m in models.items()}
            self.db.execute("UPDATE sessions SET gemini=?,openai=?,efforts=? WHERE id=?", (gemini, openai, pack(selected), sid))
            for p, model in models.items():
                if old[p] == model and old["efforts"].get(p, "") == selected[p]:
                    continue
                # API 工具链保留已使用的模型，档位可在同一模型的下一轮单独更新。
                self.db.executemany(
                    "UPDATE jobs SET model=?,effort=? WHERE sid=? AND kind=? AND status NOT IN ('done','obsolete') "
                    "AND (model=? OR ?='clients' OR (status!='running' AND (cursor IS NULL OR cursor_backend!=? OR cursor_model!=model)))",
                    [(model, selected[p], sid, kind, model, old["backend"], old["backend"])
                     for kind, provider in PROVIDERS.items() if provider == p],
                )
            self.event(sid, "models", "system", "已切换模型", {**models, "efforts": selected})

    def switch_backend(self, sid, backend, models, efforts=None):
        if backend not in ("api", "clients") or not all(models.get(p) for p in ("gemini", "openai")):
            raise ValueError("请选择执行方式和对应的两个模型")
        with self.db:
            session = self.session(sid)
            if session["backend"] == backend:
                return
            remembered = session["backend_models"]
            remembered[session["backend"]] = {p: session[p] for p in ("gemini", "openai")}
            remembered[session["backend"]]["efforts"] = session["efforts"]
            selected = efforts or {}
            self.db.execute("UPDATE sessions SET backend=?,gemini=?,openai=?,backend_models=?,efforts=?,speed='standard' WHERE id=?",
                            (backend, models["gemini"], models["openai"], pack(remembered), pack(selected), sid))
            self.db.executemany(
                "UPDATE jobs SET model=?,effort=?,status=CASE WHEN status='needs_key' THEN 'ready' ELSE status END "
                "WHERE sid=? AND kind=? AND status NOT IN ('done','obsolete')",
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
            for old in self.rows("SELECT * FROM jobs WHERE sid=? AND status NOT IN ('done','obsolete')", s["id"]):
                if old["payload"].get("direct") and old["status"] in ("needs_key", "ready") and not old["cursor"]:
                    self.db.execute("UPDATE jobs SET basis=? WHERE id=?", (pack({k: s["versions"][k] for k in old["basis"]}), old["id"]))
                elif changed.intersection(old["basis"]):
                    self.db.execute("UPDATE jobs SET status='obsolete' WHERE id=?", (old["id"],))
                elif old["kind"] == "verification" and "question" in old["payload"] and "report" in changed and not s["brief"]["report"]:
                    self.db.execute("UPDATE jobs SET status='obsolete' WHERE id=?", (old["id"],))
            for kind in ("engineering", "verification", "report"):
                if s["brief"][kind] and changed.intersection(DEPENDS[kind]):
                    self.enqueue(s, kind)
            if changed:
                self.event(s["id"], "requirements", "system", "需求已更新", {"brief": s["brief"], "versions": s["versions"]})
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
                    all(session["versions"][key] == version for key, version in check["basis"].items())):
                child = check["id"]
                break
        if child is None:
            child = self.enqueue(session, "verification", args, args["question"][:90])
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
        self.event(job["sid"], kind if success else "failure", PROVIDERS[job["kind"]], text,
                   {"job": job["id"], "kind": job["kind"], "basis": job["basis"], **result})
        self.wake(job["sid"])

    def pending_work(self, sid):
        return self.db.execute(
            "SELECT 1 FROM jobs WHERE sid=? AND kind IN ('engineering','verification','section') "
            "AND status IN ('ready','running','needs_key','waiting') LIMIT 1", (sid,),
        ).fetchone() is not None

    def wake(self, sid):
        for job in self.rows("SELECT * FROM jobs WHERE sid=? AND status='waiting'", sid):
            calls = self.calls(job)
            if any(call["child"] and call["result"] is None for call in calls):
                continue
            if calls or not self.pending_work(sid):
                self.db.execute("UPDATE jobs SET status='ready' WHERE id=?", (job["id"],))

    def snapshot(self, sid):
        return {
            "session": self.session(sid),
            "approvals": self.approvals(sid),
            "events": self.rows("SELECT * FROM events WHERE sid=? ORDER BY seq", sid),
            "tasks": self.rows("SELECT id,kind,status,title,model,effort,draft,error,basis,at FROM jobs WHERE sid=? ORDER BY rowid", sid),
            "artifacts": self.rows("SELECT id,job,name,digest,meta,at FROM artifacts WHERE sid=? ORDER BY rowid", sid),
        }
