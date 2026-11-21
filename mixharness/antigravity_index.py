import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname


PAGE_SIZE = 35


def workspace_path(value):
    for uri in json.loads(value or "[]"):
        parts = urlsplit(uri)
        if parts.scheme == "file":
            host = "//" + parts.netloc if parts.netloc and parts.netloc != "localhost" else ""
            return url2pathname(host + parts.path)
    return ""


def conversations(cursor=None, query=""):
    path = Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        columns = {r[1] for r in db.execute("PRAGMA table_info(conversation_summaries)")}
        if not {"conversation_id", "title", "last_modified_time", "workspace_uris"} <= columns:
            return None
        conditions = []
        args = []
        if "nesting_depth" in columns:
            conditions.append("nesting_depth=0")
        if "app_data_dir" in columns:
            conditions.append("app_data_dir IN ('','antigravity')")
        if cursor:
            stamp, key = json.loads(cursor.removeprefix("index:"))
            conditions.append("(last_modified_time,conversation_id) < (?,?)")
            args.extend([stamp, key])
        if query:
            conditions.append("instr(lower(title),lower(?))>0")
            args.append(query)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = db.execute("SELECT conversation_id,title,last_modified_time,workspace_uris FROM conversation_summaries" +
                          where + " ORDER BY last_modified_time DESC,conversation_id DESC LIMIT ?", (*args, PAGE_SIZE + 1)).fetchall()
    entries = [{"id": key, "title": title or "未命名会话", "updated": stamp, "cwd": workspace_path(uris),
                "source": "antigravity"} for key, title, stamp, uris in rows[:PAGE_SIZE]]
    following = None
    if len(rows) > PAGE_SIZE:
        following = "index:" + json.dumps([entries[-1]["updated"], entries[-1]["id"]])
    return {"source": "antigravity", "data": entries, "next": following}
