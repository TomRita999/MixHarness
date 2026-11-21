import argparse
import asyncio
import json
import sys
from pathlib import Path

from filelock import Timeout

from .commands import control_server, dispatch, remote_command
from .engine import open_engine


def parser():
    root = argparse.ArgumentParser(prog="mixharness", description="MixHarness 项目、会话与客户端协作工具")
    root.add_argument("--library", type=Path, help="包含 .env 和 .mixharness 的目录，默认使用桌面端的会话库")
    root.add_argument("--data-dir", type=Path, help="指定会话数据库所在目录")
    root.add_argument("--version", action="version", version="MixHarness 0.7.0")
    sub = root.add_subparsers(dest="command")
    chat = sub.add_parser("chat", help="打开交互式命令行，支持补充需求、斜杠指令和操作审批")
    chat.add_argument("id", nargs="?", help="继续已有会话")
    chat.add_argument("--standalone", action="store_true", help="在会话库中创建独立工作目录")
    cmd = sub.add_parser("command", help="执行一条斜杠指令")
    cmd.add_argument("line", help="例如 /help 或 '/context 32000'")
    cmd.add_argument("--session", dest="id", help="当前会话编号")
    cmd.add_argument("--json", action="store_true", help="同时返回会话编号等结构化数据")
    for entry in (chat, cmd):
        entry.add_argument("--backend", choices=("clients", "api"), default="clients")
        entry.add_argument("--project-id", help="当前项目编号")
        entry.add_argument("--permission", choices=("ask", "auto", "full"), default="ask")
    sub.add_parser("projects", help="列出项目")
    sub.add_parser("connections", help="查看当前会话库的密钥配置状态与来源")
    settings = sub.add_parser("settings", help="配置上下文窗口与启用的技能")
    settings.add_argument("--session", dest="id", help="读取该会话所属项目的技能")
    settings.add_argument("--project-id", help="读取指定项目的技能")
    settings.add_argument("--context-window", help="共享输入的 token 预算，auto 使用自动设置")
    settings.add_argument("--list-skills", action="store_true", help="列出可以启用的技能")
    settings.add_argument("--skill-dir", action="append", help="添加技能目录，可重复")
    settings.add_argument("--enable-skill", action="append", help="启用指定技能，可重复")
    settings.add_argument("--disable-skill", action="append", help="停用指定技能，可重复")
    create = sub.add_parser("project-create", help="创建本地项目")
    create.add_argument("name")
    create.add_argument("--folder", action="append", default=[], help="来源文件夹，可重复")
    catalog = sub.add_parser("models", help="读取服务商或本机客户端的模型目录")
    catalog.add_argument("--source", required=True, choices=("codex", "antigravity", "openai", "gemini"))
    usage = sub.add_parser("usage", help="读取套餐用量或获取对应的 API 用量页面")
    usage.add_argument("--source", required=True, choices=("codex", "antigravity", "openai", "gemini"))
    for name in ("chats", "history"):
        cmd = sub.add_parser(name, help="列出会话" if name == "chats" else "读取会话正文")
        sources = ("mixharness", "codex", "antigravity", "all") if name == "chats" else ("mixharness", "codex", "antigravity")
        cmd.add_argument("--source", choices=sources, default="mixharness")
        cmd.add_argument("--cursor", help="读取上一页返回的 next 游标")
        if name == "history":
            cmd.add_argument("id")
            cmd.add_argument("--cwd", help="Antigravity 会话列表返回的原始目录")
        else:
            cmd.add_argument("--query", default="", help="查询 MixHarness 消息或标题")
    for name, description in (("new", "创建主会话"), ("run", "开始协作并等待结果"), ("send", "向已有会话补充需求"),
                              ("status", "查看任务和成果"), ("wait", "执行并等待已保存的任务")):
        cmd = sub.add_parser(name, help=description)
        if name in ("new", "run"):
            cmd.add_argument("--project-id", help="关联本地项目")
            cmd.add_argument("--backend", choices=("clients", "api"), default="clients")
            cmd.add_argument("--gemini", help="写作客户端或 Gemini API 的模型 ID")
            cmd.add_argument("--openai", help="工程客户端或 OpenAI API 的模型 ID")
            cmd.add_argument("--permission", choices=("ask", "auto", "full"), default="ask")
            cmd.add_argument("--speed", default="standard", help="账户与模型支持的 Codex 速度")
        else:
            cmd.add_argument("id")
        if name in ("new", "run", "send"):
            cmd.add_argument("--gemini-effort", help="Gemini 或 Antigravity 的思考强度")
            cmd.add_argument("--openai-effort", help="OpenAI 或 Codex 的思考强度")
        if name in ("run", "send"):
            cmd.add_argument("message", help="消息正文；使用 - 从标准输入读取")
            cmd.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True, help="等待生成结束")
            cmd.add_argument("--request-id", help="同一会话中重试发送时复用的标识")
        if name in ("run", "send", "wait"):
            cmd.add_argument("--timeout", type=int, default=600, help="等待秒数")
    sub.add_parser("serve", help="在后台任务引擎中持续处理会话，按 Ctrl+C 关闭")
    sub.add_parser("desktop", help="打开桌面应用")
    return root


def library():
    from PySide6.QtCore import QCoreApplication, QSettings, QStandardPaths
    app = QCoreApplication.instance() or QCoreApplication(["MixHarness"])
    app.setOrganizationDomain("mixharness.local")
    app.setOrganizationName("MixHarness")
    app.setApplicationName("MixHarness")
    default = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    selected = Path(str(QSettings().value("project", default)))
    return selected if selected.is_dir() else Path(default)


async def execute(project, home, args):
    from .terminal import Terminal
    interactive = args["command"] == "chat"
    async def remote(options):
        connected, result = await remote_command(home, options)
        if not connected:
            raise RuntimeError("与会话库的连接已关闭")
        return result
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        if args["command"] != "serve":
            connected, result = await remote_command(home, {"command": "projects"} if interactive else args)
            if connected:
                return await Terminal(remote, args).run() if interactive else result
        manager = open_engine(project, home)
        try:
            engine = manager.__enter__()
            break
        except Timeout:
            if args["command"] == "serve" or asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.12)
    try:
        async with control_server(engine):
            worker = asyncio.create_task(engine.serve())
            try:
                if args["command"] == "serve":
                    await worker
                    return
                if interactive:
                    return await Terminal(lambda options: dispatch(engine, options), args).run()
                return await dispatch(engine, dict(args, keep_native=True))
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
    finally:
        manager.__exit__(None, None, None)


def main(argv=None):
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    options = vars(parser().parse_args(argv))
    if not options["command"]:
        options.update(command="chat", backend="clients")
    options["cwd"] = str(Path.cwd())
    base = options.pop("library")
    data_dir = options.pop("data_dir")
    if options["command"] == "desktop":
        from .desktop_main import main as desktop
        args = ["--project", str(base)] if base else []
        if data_dir:
            args.extend(["--data-dir", str(data_dir)])
        return desktop(args)
    project = (base or library()).expanduser().resolve()
    home = (data_dir or project / ".mixharness").expanduser().resolve()
    if options.get("message") == "-":
        options["message"] = sys.stdin.read()
    if options.get("folder"):
        options["folder"] = [str(Path(f).expanduser().resolve()) for f in options["folder"]]
    if options.get("skill_dir"):
        options["skill_dir"] = [str(Path(f).expanduser().resolve()) for f in options["skill_dir"]]
    try:
        result = asyncio.run(execute(project, home, options))
    except Timeout:
        print("会话库已被占用，当前进程尚未提供命令行连接。", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(str(exc) or type(exc).__name__, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    if options["command"] == "command" and not options["json"]:
        print(result["text"])
    elif result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and options["command"] in ("wait", "send", "run"):
        if any(j["status"] in ("failed", "needs_key") for j in result.get("tasks", [])):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
