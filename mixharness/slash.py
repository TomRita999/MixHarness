import asyncio
import json
from pathlib import Path

from .agent_settings import PERMISSIONS, SPEEDS, available_speeds, encode_context, skill_catalog, tokens
from .client_rpc import executable
from .clients import CLIENTS
from .commands import default_models, new_session
from .reasoning import api_levels, group_for
from .store import STATUSES, new_id
from .usage import plan_usage


COMMANDS = {
    "help": "查看指令和参数",
    "new": "在当前项目开始新会话", "resume": "[会话编号]　列出或继续会话",
    "history": "[关键词]　搜索已有会话", "rename": "名称　重命名当前会话", "fork": "[名称]　从当前对话创建分支",
    "projects": "列出项目", "project": "项目编号或文件夹　切换项目并开始会话",
    "add-dir": "文件夹　加入当前项目", "tools": "查看可用工具与项目验证命令",
    "model": "[gemini|openai] [模型 ID]　查看或切换模型",
    "effort": "gemini|openai [档位|default]　设置思考强度",
    "backend": "[clients|api]　切换执行方式", "permissions": "[ask|auto|full]　查看或更改操作权限",
    "fast": "[off|fast|ultrafast]　设置 Codex 速度",
    "status": "查看会话、模型、权限和任务", "usage": "[codex|antigravity|openai|gemini]　查看用量",
    "skills": "[on 名称|off 名称|add 目录|remove 目录]　配置技能",
    "context": "[auto|token 数]　查看或设置共享上下文窗口", "compact": "压缩本会话的历史上下文",
    "settings": "查看当前会话库的 Agent 设置", "plan": "[要求]　查看当前计划或请求制定方案",
    "review": "[关注点]　让 GPT 检查项目并提供依据", "diff": "查看项目的 Git 改动",
    "init": "让模型阅读当前项目并维护 AGENTS.md", "mcp": "[codex|antigravity]　查看客户端的 MCP 服务",
    "approvals": "查看待批准操作", "approve": "审批编号　允许本次操作", "deny": "审批编号　拒绝本次操作",
    "stop": "停止本会话正在处理的任务", "retry": "任务编号　重试失败任务",
    "export": "[文件路径]　导出会话 Markdown", "exit": "退出交互式命令行",
}
ALIASES = {"": "help", "?": "help", "quit": "exit", "clear": "new", "chats": "history", "models": "model",
           "config": "settings", "approvals-mode": "permissions", "sandbox": "permissions",
           "stats": "usage", "stop-agent": "stop"}
ROLE_NAMES = {"user": "你", "openai": "GPT", "gemini": "Gemini", "system": "MixHarness"}


def split(text):
    parts = text.strip().split(maxsplit=1)
    return (parts + ["", ""])[:2]


def path_at(value, args):
    path = Path(value.strip().strip('"')).expanduser()
    return (Path(args.get("cwd") or Path.cwd()) / path).resolve()


def sessions_text(store, query=""):
    return "\n".join(f"{s['id']}  {s['title']}  ·  {s['project_name'] or '独立会话'}"
                     for s in store.sessions(query=query)) or "没有找到会话"


def skill_command(engine, argument, args):
    action, value = split(argument)
    directory = args.get("cwd") if args.get("cwd_project") else None
    folders = engine.skill_folders(args.get("id"), args.get("project_id"), directory)
    settings = engine.settings_for(folders)
    if not action or action == "list":
        catalog = skill_catalog(settings, folders)
        return "\n".join(f"{'已启用' if name in settings.skills else '未启用'}  {name}  {s['path']}"
                         for name, s in catalog.items()) or "当前目录中没有找到技能，可用 /skills add 添加目录"
    if action not in ("on", "off", "add", "remove") or not value:
        raise ValueError("/skills " + COMMANDS["skills"])
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
    return "已保存技能设置　" + item


def context_command(engine, argument, sid, pid):
    if argument:
        limit = 0 if argument.lower() in ("auto", "default") else int(argument)
        engine.save_settings({"context_window": limit}, sid, pid)
    limit = engine.settings.context_window
    lines = ["共享上下文窗口　" + (f"{limit:,} token" if limit else "自动")]
    if sid:
        session = engine.store.session(sid)
        jobs = engine.store.rows("SELECT j.*,s.backend FROM jobs j JOIN sessions s ON s.id=j.sid WHERE sid=? "
                                 "AND j.status!='obsolete' ORDER BY j.rowid DESC LIMIT 1", sid)
        size = 0
        if jobs:
            calls = [c for c in engine.store.calls(jobs[0]) if c["result"] is not None]
            _, results, content, _ = engine.turn_input(jobs[0], session, calls)
            size = tokens(content) + sum(tokens(encode_context(c["result"])) for c in results)
        lines.append(f"最近任务按当前设置组装的共享输入约 {size:,} token")
        if session["context_after"]:
            lines.append(f"历史已压缩至第 {session['context_after']} 条记录")
    return "\n".join(lines)


def compact(engine, session):
    sid = session["id"]
    previous = session["context_summary"]
    events = engine.store.rows("SELECT seq,actor,body FROM events WHERE sid=? AND kind IN ('message','verification','failure') "
                               "AND seq>? ORDER BY seq", sid, session["context_after"])
    if not events:
        return "当前没有新的消息需要压缩"
    recent = events[:1] + events[1:][-10:]
    lines = [ROLE_NAMES.get(e["actor"], e["actor"]) + "　" + e["body"][:320] for e in recent]
    summary = "\n".join(([previous[-1000:]] if previous else []) + lines)
    with engine.store.db:
        engine.store.db.execute("UPDATE sessions SET context_after=?,context_summary=? WHERE id=?",
                                (events[-1]["seq"], summary, sid))
    engine.notify(sid)
    return f"已将 {len(events)} 条消息整理为历史摘要。完整消息可通过会话历史和 /export 查看。"


async def model_command(engine, cmd, arg, session):
    provider, value = split(arg)
    provider = {"codex": "openai", "antigravity": "gemini", "gpt": "openai"}.get(provider.lower(), provider.lower())
    if not provider:
        return "\n".join(f"{p}  {session[p]}  ·  {session['efforts'].get(p) or '默认'}" for p in CLIENTS)
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
            return "\n".join(f"{g['id']}  {g['name']}" for g in data["groups"])
        group = group_for(data["groups"], model)
        levels = group["choices"] if group else api_levels(provider, model)
        return "可选思考强度　" + " / ".join(["default", *(v for v in levels if v)])
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
    return f"{provider}　{updated[provider]}  ·  {updated['efforts'].get(provider) or '默认'}"


async def output_of(argv, cwd):
    process = await asyncio.create_subprocess_exec(*argv, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(30):
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


async def usage_text(engine, source):
    data = await plan_usage(source, engine.home)
    if "dashboard" in data:
        return source + " API 用量　" + data["dashboard"]
    lines = [source + "　" + str(data.get("plan") or "客户端套餐")]
    for bucket in data["buckets"]:
        amount = f"{bucket['remaining_percent']}%" if "remaining_percent" in bucket else str(bucket.get("remaining_amount", "未提供"))
        lines.append(bucket["name"] + "　剩余 " + amount + ("　重置 " + bucket["reset"] if bucket.get("reset") else ""))
    return "\n".join(lines)


def status_text(engine, session):
    lines = [session["title"] + "　" + session["id"], session["workspace"], "执行方式　" + session["backend"],
             "权限　" + PERMISSIONS[session["permission"]], *(f"{p}　{session[p]}  ·  {session['efforts'].get(p) or '默认'}" for p in CLIENTS)]
    if session["speed"] != "standard":
        lines.append("Codex 速度　" + SPEEDS[session["speed"]])
    for row in engine.store.snapshot(session["id"])["tasks"]:
        if row["status"] != "obsolete":
            lines.append(f"{row['id']}  {row['title']}  {STATUSES[row['status']]}" + ("　" + row["error"] if row["error"] else ""))
    return "\n".join(lines)


async def execute_command(engine, args):
    first, arg = split(args["line"])
    if not first.startswith("/"):
        raise ValueError("指令以 / 开头")
    cmd = ALIASES.get(first[1:].lower(), first[1:].lower())
    if cmd not in COMMANDS:
        raise ValueError("未知指令 " + first + "，输入 /help 查看可用指令")
    sid, pid = args.get("id"), args.get("project_id")
    session = engine.store.session(sid) if sid else None
    if not session and cmd in ("permissions", "backend", "model", "effort", "fast", "init", "plan", "review", "diff"):
        options = dict(args)
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
        if session and session["backend"] == "clients":
            catalog = engine.client_catalogs["openai"]
            if not catalog.groups:
                await catalog.refresh()
            speeds = available_speeds(group_for(catalog.groups, session["openai"]))
        text = "\n".join("/" + name + "　" + help_text for name, help_text in COMMANDS.items() if name != "fast" or speeds)
    elif cmd == "history" or (cmd == "resume" and not arg):
        text = sessions_text(engine.store, arg)
    elif cmd == "resume":
        target = engine.store.session(arg)
        result.update(id=target["id"], project_id=target["project_id"])
        text = "继续会话　" + target["title"]
    elif cmd == "projects":
        text = "\n".join(p["id"] + "  " + p["name"] for p in engine.store.projects()) or "尚未创建项目"
    elif cmd in ("new", "project"):
        if cmd == "project":
            if not arg:
                raise ValueError("请输入项目编号或文件夹路径")
            selected = next((p for p in engine.store.projects() if p["id"] == arg), None)
            pid = (selected or engine.store.open_project(path_at(arg, args)))["id"]
        options = dict(args, project_id=pid, standalone=(session["project_id"] is None if session and not pid else
                                                        args.get("standalone", not args.get("cwd_project"))))
        if session:
            options.update(backend=session["backend"], gemini=session["gemini"], openai=session["openai"],
                           **{p + "_effort": level for p, level in session["efforts"].items()})
        created = await new_session(engine, options)
        result.update(id=created["id"], project_id=created["project_id"])
        text = "新会话　" + created["id"]
    elif cmd == "skills":
        text = skill_command(engine, arg, args)
    elif cmd == "context":
        text = context_command(engine, arg, sid, pid)
    elif cmd == "settings":
        settings = engine.settings_for(engine.skill_folders(sid, pid, args.get("cwd") if args.get("cwd_project") else None))
        text = "\n".join((context_command(engine, "", sid, pid), "已启用技能　" + (", ".join(settings.skills) or "无"),
                          "附加技能目录　" + ("、".join(engine.settings.skill_dirs) or "无")))
    elif cmd == "usage":
        text = await usage_text(engine, arg or "codex")
    elif cmd == "mcp":
        source = arg or "codex"
        if source not in ("codex", "antigravity"):
            raise ValueError("请选择 codex 或 antigravity")
        cwd = engine.skill_folders(sid, pid)[0]
        command = [executable(source), "mcp", "list"]
        if source == "codex":
            servers = json.loads(await output_of([*command, "--json"], cwd))
            text = "\n".join(s["name"] + ("　已启用" if s["enabled"] else "　已停用") for s in servers)
        else:
            text = await output_of(command, cwd)
        text = text or "客户端没有配置 MCP 服务"
    elif not session:
        raise ValueError("请先输入 /new 创建会话，或输入 /resume 选择已有会话")
    elif cmd == "status":
        text = status_text(engine, session)
    elif cmd == "fork":
        branch = engine.store.fork_session(sid, arg)
        engine.notify(branch["id"])
        result.update(id=branch["id"], project_id=branch["project_id"])
        text = "已复制消息与当前需求到 " + branch["title"] + "　" + branch["id"]
    elif cmd == "add-dir":
        if not pid or not arg:
            raise ValueError("请在项目会话中输入 /add-dir 文件夹路径")
        engine.store.attach_folder(pid, path_at(arg, args))
        engine.notify(sid)
        text = "\n".join(f["path"] for f in engine.store.project(pid)["folders"])
    elif cmd == "tools":
        from .providers import TOOLS
        from .store import LABELS
        tools = [LABELS[k] + "　" + " / ".join(names) for k, names in TOOLS.items()]
        for name, checks in engine.checks(engine.sources(session)).items():
            tools.extend(name + "　" + check + "　" + json.dumps(argv, ensure_ascii=False) for check, argv in checks.items())
        text = "\n".join(tools)
    elif cmd in ("model", "effort"):
        text = await model_command(engine, cmd, arg, session)
    elif cmd == "permissions":
        if arg:
            modes = {"default": "ask", "turbo": "full", **{name: key for key, name in PERMISSIONS.items()}}
            engine.set_access(sid, permission=modes.get(arg.lower(), arg.lower()))
        text = "操作权限　" + PERMISSIONS[engine.store.session(sid)["permission"]]
    elif cmd == "fast":
        if session["backend"] == "clients":
            await engine.client_catalogs["openai"].refresh()
        tiers = available_speeds(group_for(engine.client_catalogs["openai"].groups, session["openai"])) if session["backend"] == "clients" else {}
        if arg:
            speed = {"on": "fast", "off": "standard"}.get(arg.lower(), arg.lower())
            engine.set_access(sid, speed=speed)
        text = "Codex 速度　" + SPEEDS[engine.store.session(sid)["speed"]]
        if tiers:
            text += "　可选 " + " / ".join(SPEEDS[t] for t in ("standard", *tiers))
    elif cmd == "backend":
        if arg and arg != session["backend"]:
            saved = session["backend_models"].get(arg, {})
            models = await default_models(engine, arg, saved)
            engine.switch_backend(sid, arg, models, saved.get("efforts", {}))
        text = "当前执行方式　" + engine.store.session(sid)["backend"]
    elif cmd in ("approve", "deny"):
        engine.decide(sid, arg, cmd == "approve")
        text = "已批准本次操作" if cmd == "approve" else "已拒绝本次操作"
    elif cmd == "approvals":
        text = "\n\n".join(a["id"] + "　" + a["source"] + "　" + a["title"] + "\n" + a["detail"]
                           for a in engine.store.approvals(sid)) or "没有待批准操作"
    elif cmd == "compact":
        text = compact(engine, session)
    elif cmd == "rename":
        engine.store.rename_session(sid, arg)
        engine.notify(sid)
        text = "当前会话　" + arg
    elif cmd == "retry":
        engine.retry(sid, arg)
        text = "任务已重新排队"
    elif cmd == "diff":
        text = await diff(engine, session)
    elif cmd == "stop":
        with engine.store.db:
            count = engine.store.db.execute("UPDATE jobs SET status='obsolete' WHERE sid=? AND status IN "
                                            "('ready','running','waiting','needs_key')", (sid,)).rowcount
            if count:
                engine.store.db.execute("UPDATE sessions SET planned=revision WHERE id=?", (sid,))
                engine.store.event(sid, "progress", "system", f"用户停止了 {count} 项任务")
        engine.notify(sid)
        text = f"已停止 {count} 项任务"
    elif cmd == "export":
        state = engine.store.snapshot(sid)
        text = "# " + session["title"] + "\n\n" + "\n\n".join(
            "## " + ROLE_NAMES.get(e["actor"], e["actor"]) + "\n\n" + e["body"] for e in state["events"] if e["kind"] == "message")
        if arg:
            path = path_at(arg, args)
            with path.open("x", encoding="utf-8") as f:
                f.write(text + "\n")
            text = "会话已导出至 " + str(path)
    elif cmd == "plan" and not arg:
        text = "\n".join(name + "　" + session["brief"][k] for k, name in (
            ("engineering", "开发与研究"), ("verification", "验证"), ("report", "报告")) if session["brief"].get(k)) or "当前尚未制定计划"
    elif cmd == "init":
        request = "请先阅读当前项目的真实文件，依据构建方式、测试命令与已有约定，在项目根目录创建或更新 AGENTS.md，保留用户现有规则。"
        engine.submit(sid, request, new_id())
        await engine.steer(sid, request)
        text = "已提交项目说明任务"
    elif cmd in ("plan", "review"):
        question = ("只制定实施方案，给出待验证事项。用户要求为 " + arg if cmd == "plan"
                    else "审查项目的正确性，指出具体文件、问题及依据。" + arg)
        with engine.store.db:
            title = "制定方案" if cmd == "plan" else "代码审查"
            jid = engine.store.enqueue(session, "verification", {"question": question, "direct": True}, title)
            engine.store.event(sid, "message", "user", (first + " " + arg).strip(),
                               {"job": jid, "direct": True, "revision": session["revision"]})
            if session["title"] == "新会话":
                engine.store.db.execute("UPDATE sessions SET title=? WHERE id=?", ((title + " " + arg).strip()[:36], sid))
        engine.notify(sid)
        text = "任务已提交　" + jid
    else:
        raise ValueError("当前会话无法执行 " + first)
    return dict(result, text=text)
