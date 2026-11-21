import argparse
import sys
from pathlib import Path


def main():
    args = sys.argv[1:]
    if args and args[0] in ("cli", "--cli", "models", "usage", "chats", "history", "projects", "connections", "project-create", "new",
                            "run", "send", "wait", "status", "serve", "chat", "command", "settings", "desktop", "--library", "--version"):
        from .cli import main as cli
        return cli(args[1:] if args[0] in ("cli", "--cli") else args)
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
