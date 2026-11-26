import argparse
import sys
from pathlib import Path


def main():
    args = sys.argv[1:]
    # 桌面端、Web 和 CLI 共用 --data-dir，由它后面的参数决定入口。
    index = 0
    while index < len(args) and args[index].partition("=")[0] == "--data-dir":
        index += 2 if args[index] == "--data-dir" else 1
    entry = args[index] if index < len(args) else ""
    cli_entries = ("cli", "--cli", "models", "usage", "chats", "history", "projects", "connections", "project-create",
                   "new", "run", "send", "wait", "status", "serve", "chat", "command", "settings", "window", "desktop",
                   "--library", "--version")
    if entry in cli_entries or entry.startswith("--library="):
        from .cli import main as cli
        if entry in ("cli", "--cli"):
            args.pop(index)
        return cli(args)
    web = "--web" in args or any(arg == "--port" or arg.startswith("--port=") for arg in args)
    if not web:
        from .desktop_main import main as desktop
        return desktop(args)
    import uvicorn
    from .app import create_app
    parser = argparse.ArgumentParser(description="启动 MixHarness 本地 HTTP 服务")
    parser.add_argument("--web", action="store_true")
    parser.add_argument("--port", type=int, default=8765, help="本地端口")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="包含 .env 和 mixharness.toml 的目录")
    parser.add_argument("--data-dir", type=Path, help="会话与验证记录目录")
    args = parser.parse_args()
    uvicorn.run(create_app(args.project, args.data_dir), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    raise SystemExit(main())
