import asyncio
import csv
import hashlib
import io
import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime

from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict

from . import antigravity_index
from .agent_settings import codex_speeds
from .attachments import Attachment, MAX_REFERENCE, image_parts
from .client_rpc import ClientError, Rpc, executable
from .providers import FUNCTIONS, PROMPTS, Plan, Turn, declarations, instructions, tool_names
from .reasoning import antigravity_groups, model_available, model_group
from .store import CHAT_JOBS, PROVIDERS, new_id, pack


CLIENTS = {"gemini": "antigravity", "openai": "codex"}
NAMES = {"codex": "Codex", "antigravity": "Antigravity", "mixharness": "MixHarness"}


class ToolChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    arguments: str


class ClientReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    calls: list[ToolChoice]


def private_directory(home, sid):
    path = home.absolute() / "client-sessions" / sid
    if path.is_relative_to(Path.home()):
        return str(path.relative_to(Path.home()))
    key = hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:24]
    return str(Path(".mixharness") / "clients" / key / sid)


def connection(client, *, private=None, permission=None):
    args = [executable(client)]
    if client == "codex":
        args.append("app-server")
    else:
        args.extend(["acp", "--app-data-dir", "antigravity"])
        if private:
            args.extend(["--gemini-dir", private])
        if permission == "full":
            args.append("--dangerously-skip-permissions")
    return Rpc(args)


async def model_directory(client):
    async with asyncio.timeout(45):
        if client == "antigravity":
            process = await asyncio.create_subprocess_exec(
                executable(client), "models", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                raw, _ = await process.communicate()
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            if process.returncode:
                raise ClientError("Antigravity 无法读取模型目录，请检查客户端登录状态")
            rows = csv.reader(io.StringIO(raw.decode("utf-8")), delimiter="\t")
            catalog = {r[0]: r[1] for r in rows if len(r) == 2 and r[0]}
            groups = antigravity_groups(catalog)
        else:
            async with connection(client) as rpc:
                await rpc.initialize(client, experimental=True)
                account = (await rpc.request("account/read", {"refreshToken": False})).get("account") or {}
                catalog, groups, cursor = {}, [], None
                while True:
                    page = await rpc.request("model/list", {"cursor": cursor, "includeHidden": False})
                    catalog.update({m["model"]: m["displayName"] for m in page["data"] if not m["hidden"]})
                    for m in page["data"]:
                        if not m["hidden"]:
                            group = model_group(m["model"], m["displayName"],
                                                [opt["reasoningEffort"] for opt in m["supportedReasoningEfforts"]],
                                                m["defaultReasoningEffort"])
                            groups.append(dict(group, speeds=codex_speeds(account, m),
                                               access_programs=(m.get("availableAccessPrograms") or {}).get("cyber")))
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
        if not catalog:
            raise ClientError(NAMES[client] + " 没有返回可选择的模型")
        return catalog, groups


async def models(client):
    labels, _ = await model_directory(client)
    return labels


async def codex_mcp_servers():
    result = []
    async with asyncio.timeout(40), connection("codex") as rpc:
        await rpc.initialize("codex")
        cursor = None
        while True:
            page = await rpc.request("mcpServerStatus/list", {"detail": "toolsAndAuthOnly", "cursor": cursor})
            result.extend(page["data"])
            cursor = page.get("nextCursor")
            if not cursor:
                return result


async def codex_tool_references():
    result = []
    for server in await codex_mcp_servers():
        for key, tool in server["tools"].items():
            name = tool.get("name") or key
            target = server["name"] + "/" + name
            description = tool.get("description") or name
            result.append(Attachment.reference("tool", target, "Codex MCP", target, description))
    return result


async def chats(client, cursor=None, query=""):
    if client == "antigravity" and (not cursor or cursor.startswith("index:")):
        try:
            index = await asyncio.to_thread(antigravity_index.conversations, cursor, query)
        except sqlite3.Error as exc:
            raise ClientError(f"无法读取 Antigravity 会话索引：{exc}") from exc
        if index is not None:
            return index
        if cursor:
            raise ClientError("Antigravity 会话索引已更换，请刷新列表")
    async with asyncio.timeout(60), connection(client) as rpc:
        await rpc.initialize(client)
        if client == "codex":
            params = {"limit": 35, "cursor": cursor, "currentAccountOnly": True,
                      "sourceKinds": ["cli", "vscode", "appServer", "exec"], "sortKey": "updated_at"}
            if query:
                params["searchTerm"] = query
            page = await rpc.request("thread/list", params)
            entries = [{"id": t["id"], "title": t.get("name") or t["preview"].split("\n", 1)[0][:100] or "未命名会话",
                        "cwd": t.get("cwd", ""), "updated": t.get("updatedAt")} for t in page["data"] if not t["ephemeral"]]
        else:
            page = await rpc.request("session/list", {"cursor": cursor} if cursor else {})
            entries = [{"id": t["sessionId"], "title": t.get("title") or "未命名会话", "cwd": t.get("cwd") or "",
                        "updated": t.get("updatedAt")} for t in page["sessions"] if query.casefold() in (t.get("title") or "").casefold()]
        return {"source": client, "data": [dict(t, source=client) for t in entries], "next": page.get("nextCursor")}


def updated_time(row):
    value = row.get("updated")
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).timestamp()
        except (ValueError, OverflowError):
            pass
    return 0


async def list_conversations(store, source, cursor=None, query="", on_page=None):
    if source == "mixharness":
        page = {"source": source, "data": store.sessions(query=query), "next": None}
        if on_page:
            on_page(page["data"])
        return page
    if source in ("codex", "antigravity"):
        page = await chats(source, cursor, query)
        if on_page:
            on_page(page["data"])
        return page
    if source != "all":
        raise ValueError("未知的会话来源")
    pages = json.loads(cursor) if cursor else {"codex": None, "antigravity": None}
    if not isinstance(pages, dict) or any(c not in ("codex", "antigravity") for c in pages):
        raise ValueError("会话分页游标无效")
    entries = [] if cursor else store.sessions(query=query)
    if on_page:
        on_page(entries)
    following, errors = {}, {}
    async def fetch(client, token):
        try:
            result = await chats(client, token, query)
        except (OSError, ValueError, ClientError) as exc:
            errors[client] = str(exc) or "读取会话超时"
            return
        entries.extend(result["data"])
        if result["next"]:
            following[client] = result["next"]
        if on_page:
            on_page(result["data"])
    async with asyncio.TaskGroup() as group:
        for client, token in pages.items():
            group.create_task(fetch(client, token))
    entries.sort(key=updated_time, reverse=True)
    return {"source": "all", "data": entries, "next": pack(following) if following else None, "errors": errors}


def add_message(messages, role, text, *, chunk=False):
    if not text:
        return
    if chunk and messages and messages[-1]["role"] == role:
        messages[-1]["text"] += text
    else:
        messages.append({"role": role, "text": text})


async def history(client, identifier, cwd=None, cursor=None, *, recent=False):
    async with asyncio.timeout(90), connection(client) as rpc:
        await rpc.initialize(client)
        messages, following = [], None
        if client == "codex":
            meta = (await rpc.request("thread/read", {"threadId": identifier}))["thread"]
            if meta.get("historyMode") == "legacy":
                turns = (await rpc.request("thread/read", {"threadId": identifier, "includeTurns": True}))["thread"]["turns"]
            else:
                page = await rpc.request("thread/turns/list", {"threadId": identifier, "itemsView": "full",
                                         "limit": 16 if recent else 8, "sortDirection": "desc" if recent else "asc", "cursor": cursor})
                turns, following = page["data"], page.get("nextCursor")
                if recent:
                    turns.reverse()
            for turn in turns:
                for item in turn["items"]:
                    if item["type"] == "agentMessage":
                        add_message(messages, "assistant", item["text"])
                    elif item["type"] == "userMessage":
                        pieces = []
                        for part in item["content"]:
                            if part["type"] == "text":
                                pieces.append(part["text"])
                            elif part["type"] in ("localImage", "image"):
                                pieces.append("[图片]")
                            elif part["type"] in ("skill", "mention"):
                                pieces.append("@" + part["name"])
                        add_message(messages, "user", "\n".join(pieces))
        else:
            if not cwd:
                page_cursor = None
                while True:
                    page = await rpc.request("session/list", {"cursor": page_cursor} if page_cursor else {})
                    found = next((t for t in page["sessions"] if t["sessionId"] == identifier), None)
                    if found:
                        cwd = found.get("cwd") or str(Path.home())
                        break
                    page_cursor = page.get("nextCursor")
                    if not page_cursor:
                        raise ClientError("Antigravity 中找不到这条会话")
            def replay(method, params):
                update = params.get("update", {})
                role = {"user_message_chunk": "user", "agent_message_chunk": "assistant"}.get(update.get("sessionUpdate"))
                if method == "session/update" and role:
                    content = update["content"]
                    text = "[图片]" if content.get("type") == "image" else content.get("text", "")
                    add_message(messages, role, text, chunk=True)
            await rpc.request("session/load", {"sessionId": identifier, "cwd": cwd, "mcpServers": []}, replay)
        return {"source": client, "id": identifier, "messages": messages, "next": following}


async def chat_reference(store, row):
    source = row["source"]
    if source == "mixharness":
        messages = store.rows("SELECT seq,actor AS role,body AS text FROM events WHERE sid=? AND kind='message' ORDER BY seq DESC LIMIT 33",
                              row["id"])
        messages.reverse()
        inputs = store.inputs.for_session(row["id"], messages[0]["seq"] - 1) if messages else {}
        for msg in messages:
            names = [i["name"] for i in inputs.get(msg["seq"], [])]
            if names:
                msg["text"] += "\n[附件：" + "、".join(names) + "]"
        earlier = False
    else:
        page = await history(source, row["id"], row.get("cwd"), recent=True)
        messages, earlier = page["messages"], bool(page["next"])
    if not messages:
        raise ValueError("这条会话还没有可引用的消息")
    text = "\n\n".join(m["role"] + " · " + m["text"] for m in messages[-32:])
    if len(text) > MAX_REFERENCE or len(messages) > 32 or earlier:
        note = "（节选自会话末尾）\n"
        text = note + text[-(MAX_REFERENCE - len(note)):]
    return Attachment.reference("chat", row["title"], NAMES[source], row["id"], text)


class ClientCatalog:
    def __init__(self, client):
        self.client = client
        self.labels = {}
        self.groups = []
        self.error = ""
        self.lock = asyncio.Lock()

    def view(self):
        return {"source": "client", "client": NAMES[self.client], "models": list(self.labels), "labels": self.labels, "groups": self.groups,
                "configured": True, "loading": self.lock.locked(), "error": self.error}

    async def refresh(self):
        async with self.lock:
            try:
                self.labels, self.groups = await model_directory(self.client)
                self.error = ""
            except (ClientError, OSError, TimeoutError, ValueError) as exc:
                self.labels = {}
                self.groups = []
                self.error = str(exc) or "读取客户端目录超时"


async def client_approval(engine, job, method, params, *, read_only=False):
    if method == "session/request_permission":
        call = params["toolCall"]
        requested = {"操作": call.get("kind"), "位置": call.get("locations"), "参数": call.get("rawInput"),
                     "内容": call.get("content")}
        allowed = await engine.native_approval(job, "Antigravity", call.get("title") or "工具操作", requested)
        kind = "allow_once" if allowed else "reject_once"
        choice = next((v for v in params["options"] if v["kind"] == kind), None)
        outcome = {"outcome": "selected", "optionId": choice["optionId"]} if choice else {"outcome": "cancelled"}
        return {"outcome": outcome}
    methods = {"item/commandExecution/requestApproval": "运行命令", "item/fileChange/requestApproval": "修改文件",
               "item/permissions/requestApproval": "临时扩大权限", "execCommandApproval": "运行命令",
               "applyPatchApproval": "应用文件修改"}
    if method not in methods:
        return None
    allowed = not read_only and await engine.native_approval(job, "Codex", methods[method], params)
    if method == "item/permissions/requestApproval":
        return {"permissions": params["permissions"] if allowed else {}, "scope": "turn"}
    if method in ("execCommandApproval", "applyPatchApproval"):
        return {"decision": "approved" if allowed else "abort"}
    return {"decision": "accept" if allowed else "decline"}


def project_reads(engine, job, cwd, paths, client, directory=None):
    files = []
    root = Path(cwd).resolve()
    for path in paths:
        if not path:
            continue
        target = (root / (directory or ".") / path).resolve()
        if target.is_relative_to(root) and target.is_file():
            files.append(target.relative_to(root).as_posix())
    if files:
        engine.record_reads(job, client, files)


@asynccontextmanager
async def interruptible_turn(engine, job, client, rpc, interrupt, checkpoint):
    try:
        yield
    except asyncio.CancelledError:
        engine.remember_interruption(job, *checkpoint())
        rpc.handle_request = None
        try:
            async with asyncio.timeout(3):
                status = await interrupt()
        except (TimeoutError, OSError, ClientError):
            status = "unconfirmed"
        if engine.store.rows("SELECT 1 FROM jobs WHERE id=? AND status='stopped'", job["id"]):
            description = {"unconfirmed": " 未确认中断，正在关闭本次连接", "interrupted": " 已确认中断",
                           "cancelled": " 已确认中断"}.get(status, " 已结束本轮任务")
            with engine.store.db:
                engine.store.event(job["sid"], "progress", "system", client + description,
                                   {"job": job["id"], "client": client, "stop_status": status})
            engine.notify(job["sid"])
        raise


def is_dot_model(model):
    return model == "gpt-dot" or model.startswith("gpt-") and model.endswith("-dot")


async def codex_turn(engine, job, cwd, prompt, schema):
    model, effort = job["model"], job["effort"]
    mode = job["permission"]
    speed = job["speed"]
    proposal = job["kind"] == "proposal"
    readonly = (is_dot_model(model) or proposal or job["kind"] in ("goal_check", "side")
                or job["payload"].get("review") or job["payload"].get("read_only"))
    async with connection("codex") as rpc:
        await rpc.initialize("codex", experimental=True)
        tier = "default"
        cursor = None
        while True:
            page = await rpc.request("model/list", {"cursor": cursor, "includeHidden": False})
            record = next((m for m in page["data"] if m["model"] == model and not m["hidden"]), None)
            cursor = page.get("nextCursor")
            if record or not cursor:
                break
        programs = ((record or {}).get("availableAccessPrograms") or {}).get("cyber")
        daybreak = bool(job.get("daybreak"))
        if not model_available({"access_programs": programs}, daybreak):
            raise ClientError("当前账户或模型不支持所选 Codex 模式，请刷新模型目录后选择兼容模型")
        if speed != "standard":
            account = (await rpc.request("account/read", {})).get("account") or {}
            tier = codex_speeds(account, record or {}).get(speed)
            if not tier:
                raise ClientError("当前 Codex 账户或模型不支持所选速度，请改用标准速度")
        options = {"model": model, "cwd": str(cwd), "ephemeral": True, "serviceTier": tier,
                   "sandbox": "danger-full-access" if mode == "full" else "workspace-write",
                   "approvalPolicy": "never" if mode == "full" else "on-request",
                   "approvalsReviewer": "auto_review" if mode == "auto" else "user",
                   "config": {"sandbox_workspace_write.network_access": True}}
        if job["context_window"]:
            options["config"]["model_context_window"] = job["context_window"]
        if readonly:
            options.update(sandbox="read-only", approvalPolicy="never", approvalsReviewer="user")
            config = options.setdefault("config", {})
            config.update({"features.apps": False, "features.plugins": False})
            effective = (await rpc.request("config/read", {"cwd": str(cwd)}))["config"]
            config["mcp_servers"] = {name: {"enabled": False} for name in effective.get("mcp_servers") or {}}
        rpc.handle_request = lambda method, params: client_approval(engine, job, method, params, read_only=readonly)
        started = await rpc.request("thread/start", options)
        tid = started["thread"]["id"]
        if not started["thread"]["ephemeral"]:
            raise ClientError("Codex 未启用临时线程，已取消本次调用")
        if readonly and (started["sandbox"]["type"] != "readOnly" or started["approvalPolicy"] != "never"):
            raise ClientError("Codex 未确认只读权限，任务已停止")
        fragments, actions, complete, final, plan_text = [], [], None, "", ""
        turn_id = None
        def event(method, params):
            nonlocal complete, final, plan_text, turn_id
            if params.get("threadId") != tid:
                return
            if method == "turn/started":
                turn_id = params["turn"]["id"]
                engine.set_activity(job, "Codex 已开始生成")
            elif method == "item/agentMessage/delta":
                fragments.append(params["delta"])
                if job["kind"] == "gpt_chat" and schema is None:
                    engine.previews[job["id"]] = "".join(fragments)
            elif method == "item/completed" and params["item"]["type"] == "agentMessage":
                final = params["item"]["text"]
            elif method == "item/completed" and params["item"]["type"] == "plan":
                plan_text = params["item"]["text"]
            elif method == "item/completed" and params["item"]["type"] == "commandExecution":
                item = params["item"]
                actions.append({"命令": item["command"][:500], "退出码": item.get("exitCode")})
                if item.get("exitCode") == 0:
                    project_reads(engine, job, cwd, (a["path"] for a in item["commandActions"] if a["type"] == "read"),
                                  "Codex", item.get("cwd"))
            elif method == "item/completed" and params["item"]["type"] == "fileChange":
                item = params["item"]
                actions.append({"文件变更": [c["path"] for c in item["changes"]][:20], "状态": item["status"]})
            elif method == "turn/completed":
                complete = params["turn"]
        params = {"threadId": tid, "model": model, "serviceTier": tier, "outputSchema": schema,
                  "input": [{"type": "text", "text": prompt}, *image_parts(job.get("images", []), "codex")]}
        if readonly:
            params["sandboxPolicy"] = {"type": "readOnly", "networkAccess": True}
        elif mode != "full":
            params["sandboxPolicy"] = {"type": "workspaceWrite", "networkAccess": True}
        if programs:
            params["cyberAccessProgram"] = "daybreakBlue" if daybreak else "standard"
        if effort:
            params["effort"] = effort
        if proposal:
            params.pop("outputSchema")
            params["collaborationMode"] = {"mode": "plan", "settings": {"model": model, "reasoning_effort": effort or None}}
            async def planning_input(method, data):
                if method == "item/tool/requestUserInput":
                    raise PlanQuestion("\n".join(q["question"] + ("\n" + " / ".join(o["label"] for o in q["options"])
                                            if q.get("options") else "") for q in data["questions"]))
                return await client_approval(engine, job, method, data, read_only=True)
            rpc.handle_request = planning_input

        async def interrupt():
            def terminal_event(method, data):
                if method in ("turn/started", "turn/completed"):
                    event(method, data)
            current = turn_id
            if current is None:
                current = (await rpc.response(rpc.counter, terminal_event))["turn"]["id"]
            if complete is None:
                await rpc.request("turn/interrupt", {"threadId": tid, "turnId": current}, terminal_event)
                while complete is None:
                    await rpc.dispatch(await rpc.read(), terminal_event)
            return complete["status"]

        async with interruptible_turn(engine, job, "Codex", rpc, interrupt,
                                      lambda: (plan_text or final or "".join(fragments), actions)):
            turn_id = (await rpc.request("turn/start", params, event))["turn"]["id"]
            async def steer(text, images):
                result = await rpc.request_during_turn("turn/steer", {
                    "threadId": tid, "expectedTurnId": turn_id,
                    "input": [{"type": "text", "text": text}, *image_parts(images, "codex")]})
                if result["turnId"] != turn_id:
                    raise ClientError("Codex 返回了其他轮次的引导回执")
            engine.register_steering(job, steer)
            engine.set_activity(job, "Codex 已开始生成")
            while complete is None:
                await rpc.dispatch(await rpc.read(), event)
        if complete["status"] != "completed":
            error = complete.get("error") or {}
            raise CodexTurnError(error.get("message") or "Codex 任务未完成", started["sandbox"]["type"],
                                 error.get("codexErrorInfo"), tid, complete["id"])
        return plan_text or final or "".join(fragments)


class CodexTurnError(ClientError):
    def __init__(self, message, sandbox, code, thread_id, turn_id):
        super().__init__(message)
        self.details = {"client": "Codex", "stage": "turn/completed", "sandbox": sandbox,
                        "code": next(iter(code), None) if isinstance(code, dict) else code,
                        "thread_id": thread_id, "turn_id": turn_id}


class PlanQuestion(Exception):
    pass


async def antigravity_turn(engine, job, cwd, prompt, *, preview_text=True):
    # 一个主会话只关联一条独立存储的 Antigravity 会话；并行的报告和章节顺序续接。
    sid, model, permission = job["sid"], job["model"], job["permission"]
    direct = job["kind"] in ("gemini_chat", "supervision")
    profile = private_directory(engine.home, sid)
    if direct:
        profile = str(Path(profile) / ("supervise" if job["kind"] == "supervision" else "chat"))
    lock = asyncio.Lock() if direct else engine.client_locks.setdefault(sid, asyncio.Lock())
    async with lock, connection("antigravity", private=profile, permission=permission) as rpc:
        info = await rpc.initialize("antigravity")
        if job.get("images") and not info.get("agentCapabilities", {}).get("promptCapabilities", {}).get("image"):
            raise ClientError("当前 Antigravity 客户端不支持图片输入")
        rpc.handle_request = lambda method, params: client_approval(engine, job, method, params)
        bound = None if direct else engine.store.session(sid)["antigravity_session"]
        spec = {"cwd": str(cwd), "mcpServers": []}
        if bound:
            state = await rpc.request("session/load", dict(spec, sessionId=bound))
        else:
            state = await rpc.request("session/new", spec)
            bound = state["sessionId"]
            if not direct:
                with engine.store.db:
                    engine.store.db.execute("UPDATE sessions SET antigravity_session=? WHERE id=?", (bound, sid))
        if permission != "full":
            mode = "auto" if permission == "auto" else "default"
            if state.get("modes", {}).get("currentModeId") != mode:
                await rpc.request("session/set_mode", {"sessionId": bound, "modeId": mode})
        available = state["models"]
        if model not in {m["modelId"] for m in available["availableModels"]}:
            raise ClientError("当前 Antigravity 客户端未提供所选模型，请刷新模型目录")
        if available["currentModelId"] != model:
            await rpc.request("session/set_model", {"sessionId": bound, "modelId": model})
        fragments, reading = [], {}
        def event(method, params):
            if method != "session/update" or params.get("sessionId") != bound:
                return
            update = params.get("update", {})
            engine.set_activity(job, "Antigravity 正在处理请求")
            if update.get("sessionUpdate") == "agent_message_chunk":
                fragments.append(update["content"].get("text", ""))
                if direct and preview_text:
                    engine.previews[job["id"]] = "".join(fragments)
            elif update.get("sessionUpdate") in ("tool_call", "tool_call_update"):
                key = update["toolCallId"]
                if update.get("kind") == "read":
                    reading.setdefault(key, [])
                if key in reading:
                    reading[key].extend(loc["path"] for loc in update.get("locations") or () if loc.get("path"))
                    if update.get("status") == "completed":
                        project_reads(engine, job, cwd, reading.pop(key), "Antigravity")
                    elif update.get("status") == "failed":
                        reading.pop(key)
        parts = [{"type": "text", "text": prompt}, *image_parts(job.get("images", []), "antigravity")]
        async def interrupt():
            await rpc.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": bound}})
            result = await rpc.response(rpc.counter)
            return result["stopReason"]

        async with interruptible_turn(engine, job, "Antigravity", rpc, interrupt, lambda: ("".join(fragments), ())):
            engine.set_activity(job, "正在向 Antigravity 发送请求")
            result = await rpc.request("session/prompt", {"sessionId": bound, "prompt": parts}, event)
        if result["stopReason"] != "end_turn":
            raise ClientError("Antigravity 返回状态 " + result["stopReason"])
        return "".join(fragments)


def client_prompt(job, context):
    kind = job["kind"]
    if kind == "proposal":
        return PROMPTS[kind] + " 必要时使用客户端的提问功能。\n共享会话\n" + context, None
    tools = declarations(kind, job["payload"]) if kind != "plan" else []
    if kind in CHAT_JOBS.values() and not tools:
        return PROMPTS[kind] + "\n共享会话\n" + context, None
    schema = (Plan if kind == "plan" else ClientReply).model_json_schema()
    extra = ""
    if kind in CHAT_JOBS.values():
        extra += "\n将面向用户的文字或 Markdown 写在 text 中。"
    if kind in ("engineering", "verification") and is_dot_model(job["model"]) and not job["payload"].get("read_only"):
        extra += ("\nCodex 在只读环境中分析项目。需要修改文件或运行验证时，在 calls 中提出已列出的 MixHarness 工具请求。"
                  "MixHarness 按当前会话权限审批并执行。收到工具回执后再报告实际结果。")
    prompt = (instructions(job) + "\n请严格返回符合下方 JSON Schema 的 JSON 对象。"
              "需要调用 MixHarness 工具时，在 calls 中填写工具名与 JSON 字符串 arguments，结果会在下一轮返回。"
              + extra + "\n输出格式\n" + pack(schema) + "\n可用工具\n" + pack(tools) + "\n共享会话\n" + context)
    return prompt, schema


async def workflow_turn(engine, job, context, cwd):
    kind = job["kind"]
    prompt, schema = client_prompt(job, context)
    if PROVIDERS[kind] == "openai":
        try:
            raw = await codex_turn(engine, job, cwd, prompt, schema)
        except PlanQuestion as question:
            return Turn(new_id(), "", [{"cid": new_id(), "name": "ask_clarification", "args": {"question": str(question)}}])
    else:
        raw = await antigravity_turn(engine, job, cwd, prompt, preview_text=schema is None)
    if schema is None:
        return Turn(new_id(), raw)
    raw = raw.strip()
    if raw.startswith("~~~") or raw.startswith(chr(96) * 3):
        blocks = MarkdownIt().parse(raw)
        if len(blocks) == 1 and blocks[0].type == "fence":
            raw = blocks[0].content
    if kind == "plan":
        return Plan.model_validate_json(raw)
    reply = ClientReply.model_validate_json(raw)
    calls = []
    for call in reply.calls:
        if call.name not in tool_names(kind, job["payload"]):
            raise ClientError(f"任务无法使用工具 {call.name}")
        args = FUNCTIONS[call.name][0].model_validate(json.loads(call.arguments))
        calls.append({"cid": new_id(), "name": call.name, "args": args.model_dump()})
    return Turn(new_id(), reply.text, calls)
