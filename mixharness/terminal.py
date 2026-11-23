import asyncio
import sys
from contextlib import nullcontext

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

from .slash import ROLE_NAMES
from .slash_catalog import COMMANDS, matches
from .store import PROVIDERS
from .reasoning import model_available
from .terminal_markdown import print_message


CONTROL_CHARS = dict.fromkeys([*range(9), 11, 12, *range(13, 32), *range(127, 160)], "")


def display(value, *, file=None):
    print(str(value).translate(CONTROL_CHARS), file=file, flush=True)


def display_command(result):
    disabled = set(result.get("unavailable_models", ()))
    if not disabled or not sys.stdout.isatty():
        display(result["text"])
        return
    from rich.console import Console
    from rich.text import Text
    console = Console()
    for line in result["text"].translate(CONTROL_CHARS).splitlines():
        model = line.partition("  ")[0]
        console.print(Text(line, style="dim" if model in disabled else "none"))


class CommandCompletion(Completer):
    def __init__(self):
        self.names = set(COMMANDS) - {"fast", "daybreak"}

    def get_completions(self, document, complete_event):
        prefix = document.text_before_cursor
        if not prefix.startswith("/") or any(c.isspace() for c in prefix):
            return
        for name in matches(prefix[1:]):
            if name in self.names:
                yield Completion("/" + name, start_position=-len(prefix), display_meta=COMMANDS[name].title)


class Terminal:
    def __init__(self, invoke, args):
        self.invoke = invoke
        self.options = {k: args[k] for k in ("backend", "project_id", "cwd", "permission", "mode", "daybreak") if args.get(k)}
        self.options.update(standalone=bool(args.get("standalone")), cwd_project=not args.get("standalone"))
        self.sid = args.get("id")
        self.after = 0
        self.version = None
        self.pending = set()
        self.missing = set()
        self.running = set()
        self.activity = ""
        self.sent = set()
        self.completion = CommandCompletion()
        self.prompt = None
        if sys.stdin.isatty() and sys.stdout.isatty():
            keys = KeyBindings()
            @keys.add("escape", "enter")
            def newline(event):
                event.current_buffer.insert_text("\n")
            @keys.add("c-c", filter=Condition(lambda: bool(self.activity)))
            def interrupt(event):
                event.app.create_background_task(self.stop(self.sid))
            self.prompt = PromptSession(completer=self.completion, complete_while_typing=True,
                                        reserve_space_for_menu=4, key_bindings=keys, bottom_toolbar=lambda: self.activity)

    async def stop(self, sid):
        try:
            await self.invoke({"command": "command", "id": sid, "line": "/stop"})
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            display(exc, file=sys.stderr)

    def select(self, sid):
        self.sid = sid
        self.after, self.version = 0, None
        self.pending.clear()
        self.missing.clear()
        self.running.clear()
        self.activity = ""
        self.sent.clear()

    async def update(self):
        sid = self.sid
        if not sid:
            return
        data = await self.invoke(dict(command="updates", id=sid, after=self.after, version=self.version))
        if sid != self.sid:
            return
        if data.get("deleted"):
            self.select(None)
            print("会话已删除 · /resume 选择其他会话", flush=True)
            return
        self.version = data["version"]
        if "events" not in data:
            return
        if data["speed_options"]:
            self.completion.names.add("fast")
        else:
            self.completion.names.discard("fast")
        if data.get("daybreak_available"):
            self.completion.names.add("daybreak")
        else:
            self.completion.names.discard("daybreak")
        for event in data["events"]:
            if event["seq"] > self.after and event["seq"] not in self.sent and event["kind"] in (
                    "message", "verification", "failure", "progress", "artifact"):
                author = ROLE_NAMES.get(event["actor"], event["actor"])
                if sys.stdout.isatty() and event["kind"] == "message" and event["actor"] in ("openai", "gemini"):
                    print_message(author, event["body"].translate(CONTROL_CHARS), data.get("workspace"))
                elif event["kind"] in ("progress", "artifact"):
                    display("· " + event["body"])
                else:
                    display(author + "  " + event["body"])
            self.after = max(self.after, event["seq"])
        for item in data["approvals"]:
            if item["id"] not in self.pending:
                display(f"待批准 · {item['source']} · {item['title']}\n{item['detail']}\n"
                        f"/approve {item['id']}  ·  /deny {item['id']}")
        self.pending = {a["id"] for a in data["approvals"]}
        for job in data["needs_key"]:
            if job["id"] not in self.missing:
                provider = "OpenAI" if PROVIDERS[job["kind"]] == "openai" else "Gemini"
                print(f"等待 {provider} API Key", flush=True)
        self.missing = {j["id"] for j in data["needs_key"]}
        for job in data["running"]:
            if job["id"] not in self.running:
                actor = ROLE_NAMES.get(PROVIDERS.get(job.get("kind")), "模型")
                display(f"进行中 · {actor} · {job['title']} · {job['model']}　/stop 停止")
        self.running = {j["id"] for j in data["running"]}
        if data.get("stopping"):
            self.activity = "正在停止任务…"
        elif data["approvals"]:
            self.activity = "等待批准"
        elif data["running"]:
            self.activity = "进行中 · " + "、".join(dict.fromkeys(j["title"] for j in data["running"]))
        elif self.missing:
            self.activity = "等待 API Key"
        else:
            self.activity = "等待执行" if data.get("active_count") else ""
        if self.activity and not data.get("stopping"):
            self.activity += "　Ctrl+C 停止"
        if self.prompt:
            self.prompt.app.invalidate()

    async def monitor(self):
        if self.prompt and not self.sid and self.options.get("backend", "clients") == "clients":
            try:
                catalog = await self.invoke({"command": "models", "source": "codex"})
            except (OSError, ValueError, RuntimeError):
                catalog = {}
            if not self.sid and any(model_available(g, True) for g in catalog.get("groups", [])):
                self.completion.names.add("daybreak")
        while True:
            await self.update()
            await asyncio.sleep(0.4)

    async def line(self):
        if self.prompt:
            try:
                return await self.prompt.prompt_async("› ")
            except KeyboardInterrupt:
                return ""
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            raise EOFError
        return line

    async def run(self):
        if self.sid:
            session = (await self.invoke(dict(command="status", id=self.sid)))["session"]
            self.options.update(project_id=session["project_id"], backend=session["backend"])
        print("MixHarness · /help 查看指令", flush=True)
        with patch_stdout(raw=True) if self.prompt else nullcontext():
            monitor = asyncio.create_task(self.monitor())
            try:
                while True:
                    reading = asyncio.create_task(self.line())
                    try:
                        done, _ = await asyncio.wait((monitor, reading), return_when=asyncio.FIRST_COMPLETED)
                        if monitor in done:
                            await monitor
                        line = reading.result().strip()
                    except EOFError:
                        break
                    finally:
                        if not reading.done():
                            reading.cancel()
                            await asyncio.gather(reading, return_exceptions=True)
                    if not line:
                        continue
                    try:
                        if line.startswith("/"):
                            result = await self.invoke(dict(self.options, command="command", id=self.sid, line=line))
                            if result.get("exit"):
                                break
                            if result["id"] != self.sid:
                                self.select(result["id"])
                            self.options["project_id"] = result["project_id"]
                            if result["text"]:
                                display_command(result)
                        else:
                            if not self.sid:
                                session = await self.invoke(dict(self.options, command="new"))
                                self.select(session["id"])
                                print("新会话 · " + self.sid, flush=True)
                            sent = await self.invoke(dict(command="send", id=self.sid, message=line, wait=False))
                            if isinstance(sent, dict) and sent.get("seq"):
                                self.sent.add(sent["seq"])
                        await self.update()
                    except (OSError, ValueError, KeyError, RuntimeError) as exc:
                        display(exc, file=sys.stderr)
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
