import asyncio
import csv
import hashlib
import io
import json
from pathlib import Path
from datetime import datetime

from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict

from .agent_settings import codex_speeds
from .client_rpc import ClientError, Rpc, executable
from .providers import FUNCTIONS, PROMPTS, TOOLS, Plan, Turn, declarations
from .reasoning import antigravity_groups, model_group
from .store import new_id, pack


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
                await rpc.initialize(client)
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
                            groups.append(dict(group, speeds=codex_speeds(account, m)))
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
        if not catalog:
            raise ClientError(NAMES[client] + " 没有返回可选择的模型")
        return catalog, groups


async def models(client):
    labels, _ = await model_directory(client)
    return labels


async def chats(client, cursor=None):
    async with asyncio.timeout(60), connection(client) as rpc:
        await rpc.initialize(client)
        if client == "codex":
            params = {"limit": 35, "cursor": cursor, "currentAccountOnly": True,
                      "sourceKinds": ["cli", "vscode", "appServer", "exec"], "sortKey": "updated_at"}
            page = await rpc.request("thread/list", params)
            entries = [{"id": t["id"], "title": t.get("name") or t["preview"].split("\n", 1)[0][:100] or "未命名会话",
                        "cwd": t.get("cwd", ""), "updated": t.get("updatedAt")} for t in page["data"] if not t["ephemeral"]]
        else:
            page = await rpc.request("session/list", {"cursor": cursor} if cursor else {})
            entries = [{"id": t["sessionId"], "title": t.get("title") or "未命名会话", "cwd": t["cwd"],
                        "updated": t.get("updatedAt")} for t in page["sessions"]]
        return {"source": client, "data": [dict(t, source=client) for t in entries], "next": page.get("nextCursor")}


async def list_conversations(store, source, cursor=None, query=""):
    if source == "mixharness":
        return {"source": source, "data": store.sessions(query=query), "next": None}
    if source in ("codex", "antigravity"):
        return await chats(source, cursor)
    if source != "all":
        raise ValueError("未知的会话来源")
    pages = json.loads(cursor) if cursor else {"codex": None, "antigravity": None}
    if not isinstance(pages, dict) or any(c not in ("codex", "antigravity") for c in pages):
        raise ValueError("会话分页游标无效")
    results = await asyncio.gather(*(chats(c, token) for c, token in pages.items()), return_exceptions=True)
    entries = [] if cursor else store.sessions(query=query)
    following, errors = {}, {}
    for client, result in zip(pages, results):
        if isinstance(result, Exception):
            errors[client] = str(result) or type(result).__name__
        else:
            entries.extend(result["data"])
            if result["next"]:
                following[client] = result["next"]
    def timestamp(row):
        value = row.get("updated")
        return datetime.fromisoformat(value).timestamp() if isinstance(value, str) else value or 0
    entries.sort(key=timestamp, reverse=True)
    return {"source": "all", "data": entries, "next": pack(following) if following else None, "errors": errors}


def add_message(messages, role, text, *, chunk=False):
    if not text:
        return
    if chunk and messages and messages[-1]["role"] == role:
        messages[-1]["text"] += text
    else:
        messages.append({"role": role, "text": text})


async def history(client, identifier, cwd=None, cursor=None):
    async with asyncio.timeout(90), connection(client) as rpc:
        await rpc.initialize(client)
        messages, following = [], None
        if client == "codex":
            meta = (await rpc.request("thread/read", {"threadId": identifier}))["thread"]
            if meta.get("historyMode") == "legacy":
                turns = (await rpc.request("thread/read", {"threadId": identifier, "includeTurns": True}))["thread"]["turns"]
            else:
                page = await rpc.request("thread/turns/list", {"threadId": identifier, "itemsView": "full",
                                         "limit": 8, "sortDirection": "asc", "cursor": cursor})
                turns, following = page["data"], page.get("nextCursor")
            for turn in turns:
                for item in turn["items"]:
                    if item["type"] == "agentMessage":
                        add_message(messages, "assistant", item["text"])
                    elif item["type"] == "userMessage":
                        add_message(messages, "user", "\n".join(p.get("text", "") for p in item["content"] if p["type"] == "text"))
        else:
            if not cwd:
                page_cursor = None
                while True:
                    page = await rpc.request("session/list", {"cursor": page_cursor} if page_cursor else {})
                    found = next((t for t in page["sessions"] if t["sessionId"] == identifier), None)
                    if found:
                        cwd = found["cwd"]
                        break
                    page_cursor = page.get("nextCursor")
                    if not page_cursor:
                        raise ClientError("Antigravity 中找不到这条会话")
            def replay(method, params):
                update = params.get("update", {})
                role = {"user_message_chunk": "user", "agent_message_chunk": "assistant"}.get(update.get("sessionUpdate"))
                if method == "session/update" and role:
                    add_message(messages, role, update["content"].get("text", ""), chunk=True)
            await rpc.request("session/load", {"sessionId": identifier, "cwd": cwd, "mcpServers": []}, replay)
        return {"source": client, "id": identifier, "messages": messages, "next": following}


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


async def client_approval(engine, job, method, params):
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
    allowed = await engine.native_approval(job, "Codex", methods[method], params)
    if method == "item/permissions/requestApproval":
        return {"permissions": params["permissions"] if allowed else {}, "scope": "turn"}
    if method in ("execCommandApproval", "applyPatchApproval"):
        return {"decision": "approved" if allowed else "abort"}
    return {"decision": "accept" if allowed else "decline"}


async def codex_turn(engine, job, cwd, prompt, schema):
    model, effort = job["model"], job["effort"]
    mode = job["permission"]
    speed = job["speed"]
    async with connection("codex") as rpc:
        await rpc.initialize("codex")
        tier = "default"
        if speed != "standard":
            account = (await rpc.request("account/read", {})).get("account") or {}
            cursor = None
            while True:
                page = await rpc.request("model/list", {"cursor": cursor, "includeHidden": False})
                record = next((m for m in page["data"] if m["model"] == model and not m["hidden"]), None)
                cursor = page.get("nextCursor")
                if record or not cursor:
                    break
            tier = codex_speeds(account, record or {}).get(speed)
            if not tier:
                raise ClientError("当前 Codex 账户或模型不支持所选速度，请改用标准速度")
        options = {"model": model, "cwd": str(cwd), "ephemeral": True, "serviceTier": tier,
                   "sandbox": "danger-full-access" if mode == "full" else "workspace-write",
                   "approvalPolicy": "never" if mode == "full" else "on-request",
                   "approvalsReviewer": "auto_review" if mode == "auto" else "user"}
        if job["context_window"]:
            options["config"] = {"model_context_window": job["context_window"]}
        rpc.handle_request = lambda method, params: client_approval(engine, job, method, params)
        started = await rpc.request("thread/start", options)
        tid = started["thread"]["id"]
        if not started["thread"]["ephemeral"]:
            raise ClientError("Codex 未启用临时线程，已取消本次调用")
        fragments, complete, final = [], None, ""
        def event(method, params):
            nonlocal complete, final
            if params.get("threadId") != tid:
                return
            if method == "item/agentMessage/delta":
                fragments.append(params["delta"])
            elif method == "item/completed" and params["item"]["type"] == "agentMessage":
                final = params["item"]["text"]
            elif method == "turn/completed":
                complete = params["turn"]
        params = {"threadId": tid, "model": model, "serviceTier": tier, "outputSchema": schema,
                  "input": [{"type": "text", "text": prompt}]}
        if effort:
            params["effort"] = effort
        await rpc.request("turn/start", params, event)
        while complete is None:
            await rpc.dispatch(await rpc.read(), event)
        if complete["status"] != "completed":
            raise ClientError((complete.get("error") or {}).get("message") or "Codex 任务未完成")
        return final or "".join(fragments)


async def antigravity_turn(engine, job, cwd, prompt):
    # 一个主会话只关联一条独立存储的 Antigravity 会话；并行的报告和章节顺序续接。
    sid, model, permission = job["sid"], job["model"], job["permission"]
    profile = private_directory(engine.home, sid)
    async with engine.client_locks.setdefault(sid, asyncio.Lock()), connection("antigravity", private=profile, permission=permission) as rpc:
        await rpc.initialize("antigravity")
        rpc.handle_request = lambda method, params: client_approval(engine, job, method, params)
        bound = engine.store.session(sid)["antigravity_session"]
        spec = {"cwd": str(cwd), "mcpServers": []}
        if bound:
            state = await rpc.request("session/load", dict(spec, sessionId=bound))
        else:
            state = await rpc.request("session/new", spec)
            bound = state["sessionId"]
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
        fragments = []
        def event(method, params):
            update = params.get("update", {})
            if method == "session/update" and params.get("sessionId") == bound and update.get("sessionUpdate") == "agent_message_chunk":
                fragments.append(update["content"].get("text", ""))
        result = await rpc.request("session/prompt", {"sessionId": bound, "prompt": [{"type": "text", "text": prompt}]}, event)
        if result["stopReason"] != "end_turn":
            raise ClientError("Antigravity 返回状态 " + result["stopReason"])
        return "".join(fragments)


async def workflow_turn(engine, job, context, cwd):
    kind, sid = job["kind"], job["sid"]
    schema = (Plan if kind == "plan" else ClientReply).model_json_schema()
    tools = declarations(kind) if kind != "plan" else []
    prompt = (PROMPTS[kind] + "\n请严格返回符合下方 JSON Schema 的 JSON 对象。"
              "需要调用 MixHarness 工具时，在 calls 中填写工具名与 JSON 字符串 arguments，结果会在下一轮返回。"
              "\n输出格式\n" + pack(schema) + "\n可用工具\n" + pack(tools) + "\n共享会话\n" + context)
    if kind in ("engineering", "verification"):
        raw = await codex_turn(engine, job, cwd, prompt, schema)
    else:
        raw = await antigravity_turn(engine, job, cwd, prompt)
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
        if call.name not in TOOLS[kind]:
            raise ClientError(f"任务无法使用工具 {call.name}")
        args = FUNCTIONS[call.name][0].model_validate(json.loads(call.arguments))
        calls.append({"cid": new_id(), "name": call.name, "args": args.model_dump()})
    return Turn(new_id(), reply.text, calls)
