import asyncio
import json
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from . import clients
from .attachments import Attachment, MAX_ITEMS
from .agent_settings import available_speeds, skill_catalog
from .catalog import MODELS
from .reasoning import group_for, model_available, select_daybreak_model
from .store import new_id
from .usage import plan_usage
from .window_native import NativeWindows


async def settled(engine, sid, seconds, *, return_on_approval=False, keep_native=False):
    announced = set()
    async with asyncio.timeout(seconds):
        while True:
            state = engine.store.snapshot(sid)
            if return_on_approval and state["approvals"]:
                native = [a for a in state["approvals"] if a["source"] != "MixHarness"]
                if not native or not keep_native:
                    return state
                for a in state["approvals"]:
                    if a["id"] not in announced:
                        print(f"{a['source']} 等待批准　{a['title']}\n{a['detail']}\n"
                              f"mixharness command '/approve {a['id']}' --session {sid}", file=sys.stderr, flush=True)
                        announced.add(a["id"])
            tasks = state["tasks"]
            active = {"ready", "running", "waiting"}
            if not any(j["status"] in active for j in tasks) or (
                any(j["status"] == "needs_key" for j in tasks) and
                not any(j["status"] == "running" for j in tasks) and not any(j["status"] == "ready" for j in tasks)
            ):
                return state
            await asyncio.sleep(0.15)


async def default_models(engine, backend, preferred, *, daybreak=False):
    if backend == "api":
        return {p: preferred.get(p) or models[0] for p, models in MODELS.items()}
    if backend != "clients":
        raise ValueError("执行方式可选 api 或 clients")
    await asyncio.gather(*(c.refresh() for c in engine.client_catalogs.values()))
    models = {}
    for p, catalog in engine.client_catalogs.items():
        if not catalog.labels:
            raise ValueError(catalog.error or "客户端模型目录为空")
        models[p] = preferred.get(p) or next(iter(catalog.labels))
        if p == "openai" and not preferred.get(p):
            models[p] = select_daybreak_model(catalog.groups, models[p], daybreak)
    return models


async def new_session(engine, args):
    backend = args.get("backend", "clients")
    daybreak = args.get("daybreak", False)
    selected = await default_models(engine, backend, args, daybreak=daybreak)
    efforts = {p: args[p + "_effort"] for p in clients.CLIENTS if args.get(p + "_effort") is not None}
    directory = Path(args["cwd"]) if args.get("cwd_project") and not args.get("standalone") and not args.get("project_id") else None
    session = engine.create_session(**selected, directory=directory, project_id=args.get("project_id"), backend=backend, efforts=efforts,
                                    standalone=args.get("standalone", False), permission=args.get("permission", "ask"),
                                    speed=args.get("speed", "standard"), mode=args.get("mode", "default"), daybreak=daybreak)
    engine.notify(session["id"])
    return session


async def window_command(args, *, engine=None, home=None):
    action = args['window_action']
    if action in ('list', 'permissions'):
        native = engine.windows.native if engine else NativeWindows(home)
        if action == 'list':
            return await native.request('list')
        request = args.get('request')
        if request not in (None, 'screen', 'accessibility'):
            raise ValueError('请选择 screen 或 accessibility。')
        return await native.request('permissions', **({'request': request} if request else {}))
    sid = args['id']
    if engine:
        engine.store.session(sid)
    if action == 'status':
        return {'id': sid, 'share': engine.windows.status(sid) if engine else None}
    if action == 'stop':
        stopped = await engine.windows.stop(sid) if engine else False
        return {'id': sid, 'stopped': stopped}
    if action == 'bind':
        if engine is None:
            raise ValueError('共享窗口需要持续运行的引擎。请打开同一会话库的桌面端，或使用 mixharness serve 启动它。')
        share = await engine.windows.bind(sid, args['target'], args.get('access', 'view'))
        return {'id': sid, 'share': share}
    raise ValueError('未知的窗口指令')


async def dispatch(engine, args):
    cmd = args["command"]
    source = args.get("source", "mixharness")
    if cmd == "command":
        from .slash import execute_command
        return await execute_command(engine, args)
    if cmd == 'window':
        return await window_command(args, engine=engine)
    if cmd == "updates":
        sid = args["id"]
        try:
            session = engine.store.session(sid)
        except KeyError:
            return {"deleted": True}
        version = engine.changes.get(sid, 0)
        if args.get("version") == version:
            return {"version": version}
        catalog = engine.client_catalogs["openai"]
        native = session["backend"] == "clients"
        if native and args.get("version") is None and not catalog.groups and not catalog.error:
            await catalog.refresh()
        events = engine.store.rows("SELECT seq,actor,kind,body FROM events WHERE sid=? AND seq>? ORDER BY seq", sid, args.get("after", 0))
        jobs = engine.store.rows("SELECT id,kind,status,title,model FROM jobs WHERE sid=? AND status IN "
                                 "('ready','running','waiting','needs_key') ORDER BY rowid", sid)
        options = available_speeds(group_for(catalog.groups, session["openai"])) if native else {}
        daybreak_options = native and any(model_available(g, True) for g in catalog.groups)
        return {"version": version, "events": events, "approvals": engine.store.approvals(sid),
                "needs_key": [j for j in jobs if j["status"] == "needs_key"], "active_count": len(jobs),
                "running": [j for j in jobs if j["status"] == "running"], "stopping": sid in engine.stopping,
                "can_continue": bool(session["paused_work"]),
                "speed_options": list(options), "daybreak_available": daybreak_options or session["daybreak"],
                "workspace": session["workspace"]}
    if cmd == "settings":
        sid, pid, directory = args.get("id"), args.get("project_id"), args.get("cwd")
        folders = engine.skill_folders(sid, pid, directory)
        updates = {}
        if args.get("context_window") is not None:
            value = args["context_window"]
            updates["context_window"] = 0 if value == "auto" else int(value)
        if args.get("skill_dir"):
            updates["skill_dirs"] = list(dict.fromkeys([*engine.settings.skill_dirs, *args["skill_dir"]]))
        if args.get("enable_skill") or args.get("disable_skill"):
            enabled = set(engine.settings_for(folders).skills)
            updates["skills"] = sorted((enabled | set(args.get("enable_skill") or [])) - set(args.get("disable_skill") or []))
        if updates:
            engine.save_settings(updates, sid, pid, directory)
        value = engine.settings_for(folders)
        result = value.model_dump(exclude={"project_skills"})
        if args.get("list_skills"):
            result["available"] = list(skill_catalog(value, folders).values())
        return result
    if cmd == "usage":
        return await plan_usage(source, engine.home)
    if cmd == "models":
        if source in ("antigravity", "codex"):
            labels, groups = await clients.model_directory(source)
            return {"source": source, "models": labels, "groups": groups}
        catalog = engine.catalogs[source]
        await catalog.refresh()
        return catalog.view()
    if cmd == "projects":
        return engine.store.projects()
    if cmd == "connections":
        return {"file": str(engine.project / ".env"), "providers": {
            p: {"configured": bool(value), "source": engine.key_sources[p]} for p, value in engine.keys.items()}}
    if cmd == "project-create":
        project = engine.store.save_project(args["name"], args["folder"])
        engine.notify(project["id"])
        return project
    if cmd == "chats":
        return await clients.list_conversations(engine.store, source, args.get("cursor"), args.get("query", ""))
    if cmd == "history" and source != "mixharness":
        if source not in clients.NAMES:
            raise ValueError("未知的会话来源")
        return await clients.history(source, args["id"], args.get("cwd"), args.get("cursor"))
    if cmd in ("history", "status"):
        return engine.store.snapshot(args["id"])
    if cmd in ("send", "run"):
        message = args["message"].strip()
        paths = args.get("image") or []
        if not message and not paths or len(message) > 120000:
            raise ValueError("请输入消息或添加图片，消息最多 120,000 个字符")
        if cmd == "run" and args.get("mode") == "goal" and not message:
            raise ValueError("请用文字说明要达成的目标")
        if len(paths) > MAX_ITEMS:
            raise ValueError(f"每次最多添加 {MAX_ITEMS} 项")
        attachments = [Attachment.from_file(path) for path in paths]
        engine.store.inputs.validate([{"kind": item.kind, "size": len(item.data)} for item in attachments])
    efforts = {p: args[p + "_effort"] for p in clients.CLIENTS if args.get(p + "_effort") is not None}
    if cmd in ("new", "run"):
        session = await new_session(engine, args)
        sid = session["id"]
        if cmd == "new":
            return session
    else:
        sid = args["id"]
        if cmd == "send" and efforts:
            session = engine.store.session(sid)
            if session["backend"] == "clients":
                await asyncio.gather(*(engine.client_catalogs[p].refresh() for p in efforts))
            engine.set_choices(sid, {p: (session[p], e) for p, e in efforts.items()})
    if cmd in ("send", "run"):
        seq, _ = engine.submit(sid, message, args.get("request_id") or new_id(), attachments=attachments)
        if not args.get("wait"):
            return {"source": "mixharness", "id": sid, "seq": seq}
    elif cmd != "wait":
        raise ValueError("未知的命令")
    return await settled(engine, sid, args.get("timeout"), return_on_approval=True, keep_native=args.get("keep_native", False))


@asynccontextmanager
async def control_server(engine):
    token = secrets.token_urlsafe(32)
    active = set()
    async def handle(reader, writer):
        task = asyncio.current_task()
        active.add(task)
        try:
            try:
                async with asyncio.timeout(8):
                    request = json.loads(await reader.readline())
                if not secrets.compare_digest(request["token"], token):
                    raise ValueError("连接凭据无效")
                result = {"result": await dispatch(engine, request["args"])}
            except (KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
                result = {"error": str(exc) or type(exc).__name__}
            writer.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        finally:
            active.discard(task)
            writer.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=1024*1024)
    path = engine.home / "control.json"
    staging = engine.home / ("control-" + new_id())
    try:
        with os.fdopen(os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            json.dump({"port": server.sockets[0].getsockname()[1], "token": token}, f)
        os.replace(staging, path)
        async with server:
            yield
    finally:
        path.unlink(missing_ok=True)
        staging.unlink(missing_ok=True)
        server.close()
        await server.wait_closed()
        tasks = tuple(active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def remote_command(home, args):
    try:
        config = json.loads((home / "control.json").read_text(encoding="utf-8"))
        reader, writer = await asyncio.open_connection("127.0.0.1", config["port"], limit=64*1024*1024)
    except (FileNotFoundError, ConnectionRefusedError):
        return False, None
    try:
        writer.write(json.dumps({"token": config["token"], "args": args}).encode() + b"\n")
        await writer.drain()
        data = await reader.readline()
        if not data:
            raise clients.ClientError("任务引擎在返回结果前关闭了连接")
        result = json.loads(data)
        if "error" in result:
            raise clients.ClientError(result["error"])
        return True, result["result"]
    finally:
        writer.close()
        await writer.wait_closed()
