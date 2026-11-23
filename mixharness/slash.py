import asyncio
import json
import sqlite3
import unicodedata
from pathlib import Path

from .agent_settings import PERMISSIONS, SPEEDS, available_speeds, encode_context, skill_catalog, tokens
from .client_rpc import executable
from .chat_snapshot import ACTORS as ROLE_NAMES, feedback_url, snapshot_link, transcript
from .clients import CLIENTS, codex_mcp_servers
from .commands import default_models, new_session
from .reasoning import api_levels, group_for, model_available, select_daybreak_model
from .slash_catalog import COMMANDS, command_name
from .store import GOAL_STATES, MODES, MODE_TITLES, STATUSES, new_id
from .usage import CODEX_BILLING, plan_usage


PROVIDER_NAMES = {"gemini": "Gemini", "openai": "GPT"}


def width(text):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def table(rows, gap=2):
    """按显示宽度对齐两列文本，中文按两个字符宽计算。"""
    rows = [(str(k), str(v)) for k, v in rows if v not in (None, "")]
    size = max((width(k) for k, _ in rows), default=0) + gap
    return "\n".join(k + " " * (size - width(k)) + v for k, v in rows)


def short_path(path):
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


def split(text):
    parts = text.strip().split(maxsplit=1)
    return (parts + ["", ""])[:2]


def path_at(value, args):
    path = Path(value.strip().strip('"')).expanduser()
    return (Path(args.get("cwd") or Path.cwd()) / path).resolve()


def sessions_text(store, query=""):
    return "\n".join(f"{s['id']}  {s['title']} · {s['project_name'] or '独立会话'}"
                     for s in store.sessions(query=query)) or "没有找到会话"


def skill_command(engine, argument, args):
    action, value = split(argument)
    directory = args.get("cwd") if args.get("cwd_project") else None
    folders = engine.skill_folders(args.get("id"), args.get("project_id"), directory)
    settings = engine.settings_for(folders)
    if not action or action == "list":
        catalog = skill_catalog(settings, folders)
        return table((("● " if name in settings.skills else "○ ") + name, short_path(str(Path(s["path"]).parent)))
                     for name, s in catalog.items()) or "没有找到技能 · /skills add <目录> 添加"
    if action not in ("on", "off", "add", "remove") or not value:
        raise ValueError("/skills " + COMMANDS["skills"].usage())
    field = "skills" if action in ("on", "off") else "skill_dirs"
    item = value if field == "skills" else str(path_at(value, args))
    values = list(getattr(settings, field))
    if action in ("on", "add"):
        if item not in values:
            values.append(item)
    elif item in values:
        values.remove(item)
    else:
        raise ValueError("当前设置中没有 " + item)
    engine.save_settings({field: values}, args.get("id"), args.get("project_id"), directory)
    return "已保存 · " + item


def context_command(engine, argument, sid, pid):
    if argument:
        limit = 0 if argument.lower() in ("auto", "default") else int(argument)
        engine.save_settings({"context_window": limit}, sid, pid)
    limit = engine.settings.context_window
    lines = [("上下文窗口", f"{limit:,} token" if limit else "自动")]
    if sid:
        session = engine.store.session(sid)
        jobs = engine.store.rows("SELECT j.*,s.backend FROM jobs j JOIN sessions s ON s.id=j.sid WHERE sid=? "
                                 "AND j.status!='obsolete' ORDER BY j.rowid DESC LIMIT 1", sid)
        size = 0
        if jobs:
            calls = [c for c in engine.store.calls(jobs[0]) if c["result"] is not None]
            _, results, content, _ = engine.turn_input(jobs[0], session, calls)
            size = tokens(content) + sum(tokens(encode_context(c["result"])) for c in results)
        lines.append(("最近输入", f"共享输入约 {size:,} token"))
        if session["context_after"]:
            lines.append(("已压缩至", f"第 {session['context_after']} 条记录"))
    return table(lines)


def compact(engine, session):
    sid = session["id"]
    previous = session["context_summary"]
    events = engine.store.rows("SELECT seq,actor,body FROM events WHERE sid=? AND kind IN ('message','verification','failure') "
                               "AND seq>? ORDER BY seq", sid, session["context_after"])
    if not events:
        return "没有需要压缩的新消息"
    recent = events[:1] + events[1:][-10:]
    lines = [ROLE_NAMES.get(e["actor"], e["actor"]) + "　" + e["body"][:320] for e in recent]
    summary = "\n".join(([previous[-1000:]] if previous else []) + lines)
    with engine.store.db:
        engine.store.db.execute("UPDATE sessions SET context_after=?,context_summary=? WHERE id=?",
                                (events[-1]["seq"], summary, sid))
    engine.notify(sid)
    return f"已压缩 {len(events)} 条消息（原记录仍保留）"


async def model_command(engine, cmd, arg, session):
    provider, value = split(arg)
    provider = {"codex": "openai", "antigravity": "gemini", "gpt": "openai"}.get(provider.lower(), provider.lower())
    if not provider:
        return table((PROVIDER_NAMES[p], f"{session[p]} · {session['efforts'].get(p) or '默认'}") for p in CLIENTS)
    if provider not in CLIENTS:
        raise ValueError("请选择 gemini 或 openai")
    native = session["backend"] == "clients"
    catalog = (engine.client_catalogs if native else engine.catalogs)[provider]
    await catalog.refresh()
    data = catalog.view()
    if data["error"]:
        raise ValueError(data["error"])
    model = session[provider]
    if not value:
        if cmd == "model":
            return "\n".join(g["id"] + ("  " + g["name"] if g["name"] != g["id"] else "") + (
                "  · 当前模式不可用" if native and provider == "openai" and not model_available(g, session["daybreak"]) else ""
            ) + ("  ✓" if g["id"] == model or model in g["choices"].values() else "") for g in data["groups"])
        group = group_for(data["groups"], model)
        levels = group["choices"] if group else api_levels(provider, model)
        return "思考强度  " + " · ".join(["default", *(v for v in levels if v)])
    if cmd == "effort":
        effort = "" if value == "default" else value.lower()
        group = group_for(data["groups"], model)
        if native and not effort and group:
            model = group["id"]
    else:
        model, effort = value, None
        if not native and (len(model) > 150 or any(ch.isspace() for ch in model)):
            raise ValueError("请输入有效的模型 ID")
    engine.set_choices(session["id"], {provider: (model, effort)})
    updated = engine.store.session(session["id"])
    return f"{PROVIDER_NAMES[provider]}  {updated[provider]} · {updated['efforts'].get(provider) or '默认'}"


async def output_of(argv, cwd, *, timeout=30):
    process = await asyncio.create_subprocess_exec(*argv, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(timeout):
            out, err = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        raise ValueError(err.decode(errors="replace").strip() or "命令执行失败")
    return out.decode(errors="replace").strip()


async def diff(engine, session):
    sections = []
    for name, folder in engine.sources(session).folders.items():
        status = await output_of(["git", "status", "--short"], folder)
        staged = await output_of(["git", "diff", "--no-ext-diff", "--cached"], folder)
        changes = await output_of(["git", "diff", "--no-ext-diff"], folder)
        sections.append("\n".join(s for s in (name, status or "工作目录没有改动", staged, changes) if s))
    return "\n\n".join(sections)


async def fork_worktree(engine, session, destination):
    folders = engine.sources(session).folders
    if len(folders) != 1:
        raise ValueError("新工作树需要一个单目录 Git 项目")
    source = next(iter(folders.values()))
    prefix = await output_of(["git", "rev-parse", "--show-prefix"], source)
    if destination.exists():
        raise ValueError("请选择尚不存在的新工作树目录")
    await output_of(["git", "worktree", "add", "--detach", str(destination), "HEAD"], source, timeout=None)
    try:
        workspace = (destination / prefix).resolve(strict=True)
        branch = engine.store.fork_session(session["id"], "", workspace=workspace)
    except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error):
        await output_of(["git", "worktree", "remove", str(destination)], source)
        raise
    return branch


async def usage_text(engine, source):
    data = await plan_usage(source, engine.home)
    if "dashboard" in data:
        return table([(source + " API 用量", data["dashboard"])])
    rows = [(source, str(data.get("plan") or "客户端套餐"))]
    for bucket in data["buckets"]:
        amount = f"{bucket['remaining_percent']}%" if "remaining_percent" in bucket else str(bucket.get("remaining_amount", "未提供"))
        rows.append((bucket["name"], "剩余 " + amount + ("　重置 " + bucket["reset"] if bucket.get("reset") else "")))
    if source == "codex":
        rows.append(("套餐账单", CODEX_BILLING))
    return table(rows)


async def mcp_text(engine, source, sid, pid):
    folder = engine.skill_folders(sid, pid)[0]
    if source == "antigravity":
        text = await output_of([executable(source), "mcp", "list"], folder)
    elif source == "codex":
        command = [executable(source), "mcp", "list", "--json"]
        live, configured = await asyncio.gather(codex_mcp_servers(), output_of(command, folder), return_exceptions=True)
        if isinstance(live, Exception) and isinstance(configured, Exception):
            raise ValueError(str(live) or str(configured) or "读取 Codex MCP 服务超时")
        statuses = {}
        if not isinstance(configured, Exception):
            statuses.update({s["name"]: "已启用" if s["enabled"] else "已停用" for s in json.loads(configured)})
        if isinstance(live, list):
            auth = {"bearertoken": "令牌认证", "oauth": "OAuth", "notloggedin": "待登录"}
            for server in live:
                info = [f"{len(server['tools'])} 个工具", auth.get((server.get("authStatus") or "").casefold())]
                statuses[server["name"]] = " · ".join(filter(None, info))
        text = table(statuses.items())
        if isinstance(live, Exception):
            text += "\n运行状态不可用 · " + (str(live) or "读取超时")
    else:
        raise ValueError("请选择 codex 或 antigravity")
    return text.strip() or "客户端没有配置 MCP 服务"


def status_text(engine, session):
    rows = [("会话", session["title"] + " · " + session["id"]), ("目录", short_path(session["workspace"])),
            ("执行方式", "本机客户端" if session["backend"] == "clients" else "API"),
            ("权限", PERMISSIONS[session["permission"]]), ("模式", MODE_TITLES[session["mode"]]),
            *((PROVIDER_NAMES[p], f"{session[p]} · {session['efforts'].get(p) or '默认'}") for p in CLIENTS)]
    if session["speed"] != "standard":
        rows.append(("Codex 速度", SPEEDS[session["speed"]]))
    if session["daybreak"]:
        rows.append(("Daybreak", "已开启"))
    if session["objective"]:
        rows.append(("目标", goal_text(session).replace("\n", " · ")))
    tasks = [f"{row['id']}  {row['title']} · {STATUSES[row['status']]}" + (" · " + row["error"] if row["error"] else "")
             for row in engine.store.snapshot(session["id"])["tasks"] if row["status"] != "obsolete"]
    return table(rows) + ("\n\n任务\n" + "\n".join(tasks) if tasks else "")


def goal_text(session):
    if not session["objective"]:
        return "未设置目标 · /goal <目标> 开始"
    return "\n".join(filter(None, [GOAL_STATES[session["goal_status"]] + " · " + session["objective"], session["goal_note"],
                                      "下一步：" + session["goal_next"] if session["goal_next"] else ""]))


async def execute_command(engine, args):
    first, arg = split(args["line"])
    if not first.startswith("/"):
        raise ValueError("指令以 / 开头")
    cmd = command_name(first[1:])
    if cmd not in COMMANDS:
        raise ValueError("未知指令 " + first + " · /help 查看全部")
    sid, pid = args.get("id"), args.get("project_id")
    session = engine.store.session(sid) if sid else None
    if cmd == "mode" and arg and arg not in MODES:
        raise ValueError("/mode " + COMMANDS["mode"].usage())
    if cmd == "daybreak" and arg.lower() not in ("", "on", "off"):
        raise ValueError("用 /daybreak on 开启，/daybreak off 关闭")
    if not session and cmd in ("permissions", "backend", "model", "effort", "fast", "daybreak", "init", "plan", "review", "diff", "mode", "goal"):
        options = dict(args)
        if cmd == "daybreak" and arg:
            options["daybreak"] = arg.lower() == "on"
            if options.get("backend", "clients") == "clients":
                catalog = engine.client_catalogs["openai"]
                await catalog.refresh()
                options["openai"] = select_daybreak_model(catalog.groups, options.get("openai", ""), options["daybreak"])
                if options["openai"] != args.get("openai"):
                    options.pop("openai_effort", None)
        if cmd == "backend" and arg:
            if arg != options.get("backend"):
                for p in CLIENTS:
                    options.pop(p, None)
                    options.pop(p + "_effort", None)
            options["backend"] = arg
        if cmd == "fast" and arg:
            options["speed"] = {"on": "fast", "off": "standard"}.get(arg.lower(), arg.lower())
        if cmd == "permissions" and arg and arg.lower() not in ("ask", "default", "auto", "full", "turbo", *PERMISSIONS.values()):
            raise ValueError("请选择 ask、auto 或 full")
        session = await new_session(engine, options)
        sid = session["id"]
    if session:
        pid = session["project_id"]
    result = {"id": sid, "project_id": pid}
    if cmd == "exit":
        return dict(result, exit=True, text="")
    if cmd == "help":
        speeds = {}
        can_daybreak = False
        if (session or args).get("backend", "clients") == "clients":
            catalog = engine.client_catalogs["openai"]
            if not catalog.groups:
                await catalog.refresh()
            if session:
                speeds = available_speeds(group_for(catalog.groups, session["openai"]))
            can_daybreak = (session or {}).get("daybreak") or any(model_available(g, True) for g in catalog.groups)
        shown = [(name, spec) for name, spec in COMMANDS.items()
                 if (name != "fast" or speeds) and (name != "daybreak" or can_daybreak)]
        size = max(width(spec.title) for _, spec in shown) + 2
        text = "\n".join((f"/{name:<14}" + spec.title + " " * (size - width(spec.title)) + spec.arguments).rstrip()
                          for name, spec in shown)
    elif cmd == "history" or (cmd == "resume" and not arg):
        text = sessions_text(engine.store, arg)
    elif cmd == "resume":
        target = engine.store.session(arg)
        result.update(id=target["id"], project_id=target["project_id"])
        text = "继续 · " + target["title"]
    elif cmd == "projects":
        text = "\n".join(p["id"] + "  " + p["name"] for p in engine.store.projects()) or "还没有项目"
    elif cmd in ("new", "project"):
        if cmd == "project":
            if not arg:
                raise ValueError("请输入项目编号或文件夹路径")
            selected = next((p for p in engine.store.projects() if p["id"] == arg), None)
            pid = (selected or engine.store.open_project(path_at(arg, args)))["id"]
        options = dict(args, project_id=pid, standalone=(session["project_id"] is None if session and not pid else
                                                        args.get("standalone", not args.get("cwd_project"))))
        if session:
            options.update(backend=session["backend"], gemini=session["gemini"], openai=session["openai"], daybreak=session["daybreak"],
                           **{p + "_effort": level for p, level in session["efforts"].items()})
        created = await new_session(engine, options)
        result.update(id=created["id"], project_id=created["project_id"])
        text = "新会话 · " + created["id"]
    elif cmd == "skills":
        text = skill_command(engine, arg, args)
    elif cmd == "context":
        text = context_command(engine, arg, sid, pid)
    elif cmd == "settings":
        settings = engine.settings_for(engine.skill_folders(sid, pid, args.get("cwd") if args.get("cwd_project") else None))
        text = "\n".join((context_command(engine, "", sid, pid), table([
            ("已启用技能", ", ".join(settings.skills) or "无"),
            ("技能目录", "、".join(map(short_path, engine.settings.skill_dirs)) or "无")])))
    elif cmd == "usage":
        text = await usage_text(engine, arg or "codex")
    elif cmd == "feedback":
        text = feedback_url(arg)
    elif cmd == "mcp":
        text = await mcp_text(engine, arg or "codex", sid, pid)
    elif not session:
        raise ValueError("还没有会话 · /new 新建或 /resume 继续")
    elif cmd == "status":
        text = status_text(engine, session)
    elif cmd == "fork":
        option, value = split(arg)
        if option == "--worktree":
            if not value:
                raise ValueError("请输入 /fork --worktree 新目录")
            branch = await fork_worktree(engine, session, path_at(value, args))
        else:
            branch = engine.store.fork_session(sid, arg)
        engine.notify(branch["id"])
        result.update(id=branch["id"], project_id=branch["project_id"])
        text = "已创建分支 · " + branch["title"] + " · " + branch["id"]
    elif cmd == "btw":
        if len(arg) > 120000:
            raise ValueError("问题最多可包含 120,000 个字符")
        side = engine.store.fork_session(sid, "", side=True)
        if arg:
            engine.submit(side["id"], arg, new_id())
        engine.notify(side["id"])
        result.update(id=side["id"], project_id=side["project_id"])
        text = "已打开侧边聊天 · /resume " + sid + " 返回"
    elif cmd == "archive":
        engine.store.archive_session(sid)
        engine.notify(sid)
        result.update(id=None)
        text = "已归档 · " + session["title"]
    elif cmd == "share":
        text = snapshot_link(session["title"], transcript(engine.store, sid))
    elif cmd == "add-dir":
        if not pid or not arg:
            raise ValueError("请在项目会话中输入 /add-dir 文件夹路径")
        engine.store.attach_folder(pid, path_at(arg, args))
        engine.notify(sid)
        text = "\n".join(f["path"] for f in engine.store.project(pid)["folders"])
    elif cmd == "tools":
        from .providers import TOOLS
        from .store import LABELS
        tools = [(LABELS[k], " · ".join(names)) for k, names in TOOLS.items()]
        checks = [(f"{name}/{check}", " ".join(argv)) for name, items in engine.checks(engine.sources(session)).items()
                  for check, argv in items.items()]
        text = table(tools) + ("\n\n验证命令\n" + table(checks) if checks else "")
    elif cmd in ("model", "effort"):
        text = await model_command(engine, cmd, arg, session)
        if cmd == "model" and arg.lower() in ("gpt", "openai", "codex") and session["backend"] == "clients":
            result["unavailable_models"] = [g["id"] for g in engine.client_catalogs["openai"].groups
                                            if not model_available(g, session["daybreak"])]
    elif cmd == "permissions":
        if arg:
            modes = {"default": "ask", "turbo": "full", **{name: key for key, name in PERMISSIONS.items()}}
            engine.set_access(sid, permission=modes.get(arg.lower(), arg.lower()))
        text = "操作权限  " + PERMISSIONS[engine.store.session(sid)["permission"]]
    elif cmd == "daybreak":
        if session["backend"] != "clients":
            raise ValueError("Daybreak 需要本机 Codex 客户端")
        catalog = engine.client_catalogs["openai"]
        await catalog.refresh()
        if arg:
            engine.set_daybreak(sid, arg.lower() == "on")
        session = engine.store.session(sid)
        text = ("Daybreak Blue 已开启" if session["daybreak"] else "Daybreak 已关闭") + " · " + session["openai"]
        if not any(model_available(g, True) for g in catalog.groups):
            text += " · " + (catalog.error or "当前账户没有可用的 Daybreak Blue 模型")
    elif cmd == "fast":
        if session["backend"] == "clients":
            await engine.client_catalogs["openai"].refresh()
        tiers = available_speeds(group_for(engine.client_catalogs["openai"].groups, session["openai"])) if session["backend"] == "clients" else {}
        if arg:
            speed = {"on": "fast", "off": "standard"}.get(arg.lower(), arg.lower())
            engine.set_access(sid, speed=speed)
        text = "Codex 速度  " + SPEEDS[engine.store.session(sid)["speed"]]
        if tiers:
            text += " · 可选 " + " / ".join(SPEEDS[t] for t in ("standard", *tiers))
    elif cmd == "backend":
        if arg and arg != session["backend"]:
            saved = session["backend_models"].get(arg, {})
            models = await default_models(engine, arg, saved)
            if arg == "clients":
                models["openai"] = select_daybreak_model(engine.client_catalogs["openai"].groups, models["openai"], False)
                if saved.get("openai") != models["openai"]:
                    saved = {**saved, "efforts": {k: v for k, v in saved.get("efforts", {}).items() if k != "openai"}}
            engine.switch_backend(sid, arg, models, saved.get("efforts", {}))
        text = "执行方式  " + ("本机客户端" if engine.store.session(sid)["backend"] == "clients" else "API")
    elif cmd in ("approve", "deny"):
        engine.decide(sid, arg, cmd == "approve")
        text = "已批准本次操作" if cmd == "approve" else "已拒绝本次操作"
    elif cmd == "approvals":
        text = "\n\n".join(a["id"] + " · " + a["source"] + " · " + a["title"] + "\n" + a["detail"]
                           for a in engine.store.approvals(sid)) or "没有待批准操作"
    elif cmd == "compact":
        text = compact(engine, session)
    elif cmd == "rename":
        engine.store.rename_session(sid, arg)
        engine.notify(sid)
        text = "已重命名 · " + arg
    elif cmd == "retry":
        engine.retry(sid, arg)
        text = "任务已重新排队"
    elif cmd == "diff":
        text = await diff(engine, session)
    elif cmd == "stop":
        with engine.store.db:
            count = engine.store.stop_jobs(sid)
            if session["goal_status"] == "active":
                engine.store.db.execute("UPDATE sessions SET goal_status='paused',mode='default' WHERE id=?", (sid,))
            if count:
                engine.store.event(sid, "progress", "system", f"用户停止了 {count} 项任务")
        engine.notify(sid)
        text = f"已停止 {count} 项任务"
    elif cmd == "export":
        text = transcript(engine.store, sid)
        if arg:
            path = path_at(arg, args)
            with path.open("x", encoding="utf-8") as f:
                f.write(text + "\n")
            text = "已导出 · " + str(path)
    elif cmd in ("mode", "plan"):
        target = "plan" if cmd == "plan" else arg
        if target:
            engine.store.change_mode(sid, target)
            if cmd == "plan" and arg:
                engine.submit(sid, arg, new_id())
            engine.notify(sid)
        text = MODE_TITLES[engine.store.session(sid)["mode"]]
    elif cmd == "implement":
        engine.store.implement_plan(sid)
        engine.notify(sid)
        text = "已开始执行当前计划"
    elif cmd == "goal":
        if arg in ("pause", "resume", "clear"):
            engine.store.goal_action(sid, arg)
        elif arg and arg != "status":
            engine.store.change_mode(sid, "goal", objective=arg)
        engine.notify(sid)
        text = goal_text(engine.store.session(sid))
    elif cmd == "init":
        request = "阅读项目文件，按构建方式、测试命令与已有约定在根目录创建或更新 AGENTS.md，保留现有规则。"
        engine.submit(sid, request, new_id())
        await engine.steer(sid, request)
        text = "已提交项目说明任务"
    elif cmd == "review":
        question = ("审查任务所列的 Git 暂存区、工作区和新增文件。用快照核对改动，给出具体问题、位置和依据。"
                    "普通目录按关注点审查项目文件。" + arg)
        with engine.store.db:
            title = "代码审查"
            jid = engine.store.enqueue(session, "verification", {"question": question, "direct": True, "review": True}, title)
            engine.store.event(sid, "message", "user", (first + " " + arg).strip(),
                               {"job": jid, "direct": True, "revision": session["revision"]})
            if session["title"] == "新会话":
                engine.store.db.execute("UPDATE sessions SET title=? WHERE id=?", ((title + " " + arg).strip()[:36], sid))
        engine.notify(sid)
        text = "已提交审查 · " + jid
    else:
        raise ValueError("当前会话无法执行 " + first)
    return dict(result, text=text)
