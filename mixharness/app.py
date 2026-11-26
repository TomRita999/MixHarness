import asyncio
import base64
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .attachments import Attachment, InputLibrary, MAX_IMAGE, MAX_ITEMS, MAX_VIDEO, MAX_VIDEO_TOTAL, VIDEO_TYPES, video_mime
from .chat_snapshot import markdown
from .engine import open_engine
from .commands import control_server
from .providers import MODELS
from .store import pack
from .usage import plan_usage
from .web_media import document_media, local_video, router as media_router, video_document


def view_state(store, sid):
    state = store.snapshot(sid)
    for event in state["events"]:
        if event["kind"] != "message" or event["actor"] == "user":
            continue
        if event["actor"] in ("openai", "gemini"):
            event["html"], event["videos"] = document_media(
                sid, 'messages', event['seq'], event['body'], state['session']['workspace'])
        else:
            event["html"] = markdown(event["body"])
    return state


class Models(BaseModel):
    gemini: str = Field(default=MODELS["gemini"][0], pattern=r"^[a-zA-Z0-9._:/-]{1,150}$")
    openai: str = Field(default=MODELS["openai"][0], pattern=r"^[a-zA-Z0-9._:/-]{1,150}$")
    gemini_effort: str | None = None
    openai_effort: str | None = None


class NewSession(Models):
    project_id: str | None = None
    standalone: bool = False
    backend: Literal["api", "clients"] = "api"
    permission: Literal["ask", "auto", "full"] = "ask"
    speed: Literal["standard", "fast", "ultrafast"] = "standard"
    daybreak: bool = False
    mode: Literal["default", "plan", "goal"] = "default"


class ModeChange(BaseModel):
    mode: Literal["default", "plan", "goal"]
    objective: str | None = Field(default=None, min_length=1, max_length=120000)


class Access(BaseModel):
    permission: Literal["ask", "auto", "full"] | None = None
    speed: Literal["standard", "fast", "ultrafast"] | None = None


class Decision(BaseModel):
    allow: bool


class ProjectFolder(BaseModel):
    directory: Path


class ProjectDetails(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    folders: list[Path] = Field(default_factory=list)


class ChatDetails(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    pinned: bool | None = None
    archived: bool | None = None


class ImageInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    data: str = Field(max_length=4 * ((MAX_IMAGE + 2) // 3))


class VideoInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    data: str = Field(max_length=4 * ((MAX_VIDEO + 2) // 3))


class Message(BaseModel):
    text: str = Field(default="", max_length=120000)
    intent: Literal["auto", "steer", "revise"] = "auto"
    images: list[ImageInput] = Field(default_factory=list, max_length=MAX_ITEMS)
    videos: list[VideoInput] = Field(default_factory=list, max_length=MAX_ITEMS)
    client_id: str = Field(min_length=1, max_length=100)

    def attachments(self):
        media = [('image', item) for item in self.images] + [('video', item) for item in self.videos]
        InputLibrary.validate([{'kind': kind, 'size': len(item.data) * 3 // 4 - item.data[-2:].count('=')}
                               for kind, item in media])
        constructors = {'image': Attachment.image, 'video': Attachment.video}
        return [constructors[kind](item.name, base64.b64decode(item.data, validate=True)) for kind, item in media]


class ModelChoice(BaseModel):
    provider: Literal["gemini", "openai"]
    model: str = Field(pattern=r"^[a-zA-Z0-9._:/-]{1,150}$")
    effort: str | None = None


def create_app(project=None, home=None):
    project = Path(project or Path.cwd()).resolve()
    home = Path(home or project / ".mixharness").resolve()

    @asynccontextmanager
    async def lifespan(app):
        with open_engine(project, home) as engine:
            app.state.engine = engine
            worker = asyncio.create_task(engine.serve())
            try:
                async with control_server(engine):
                    yield
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    app = FastAPI(title="MixHarness", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
    app.include_router(media_router)

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "请求来源与本地服务不一致"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "记录不存在"}, status_code=404)

    @app.get("/api/config")
    async def config():
        e = app.state.engine
        catalogs = {p: c.view() for p, c in e.catalogs.items()}
        return {"models": {p: c["models"] for p, c in catalogs.items()}, "catalogs": catalogs,
                "keys": {k: bool(v) for k, v in e.keys.items()},
                "video_upload": {"formats": VIDEO_TYPES, "max_items": MAX_ITEMS,
                                 "max_bytes": MAX_VIDEO, "max_total": MAX_VIDEO_TOTAL},
                "project": str(project), "workspace": str(e.workspace) if e.workspace else None}

    @app.post("/api/config/reload")
    async def reload():
        try:
            app.state.engine.reload_config()
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return await config()

    @app.get("/api/usage/{source}")
    async def usage(source: Literal["antigravity", "codex", "gemini", "openai"]):
        try:
            return await plan_usage(source, app.state.engine.home)
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/models/{provider}/refresh")
    async def refresh_models(provider: Literal["gemini", "openai"]):
        engine = app.state.engine
        await engine.catalogs[provider].refresh()
        return engine.catalogs[provider].view()

    @app.get("/api/projects")
    async def projects():
        return app.state.engine.store.projects()

    @app.post("/api/projects")
    async def open_project(data: ProjectFolder | ProjectDetails):
        try:
            if isinstance(data, ProjectDetails):
                return app.state.engine.store.save_project(data.name, data.folders)
            return app.state.engine.store.open_project(data.directory)
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/api/projects/{pid}")
    async def edit_project(pid: str, data: ProjectDetails):
        try:
            return app.state.engine.store.save_project(data.name, data.folders, pid)
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/sessions")
    async def sessions(archived: bool = False, query: str = ""):
        return app.state.engine.store.sessions(archived, query)

    @app.post("/api/sessions", status_code=201)
    async def create(models: NewSession):
        try:
            engine = app.state.engine
            if models.backend == "clients":
                await asyncio.gather(*(c.refresh() for c in engine.client_catalogs.values()))
            return engine.create_session(models.gemini, models.openai, project_id=models.project_id, standalone=models.standalone,
                                         permission=models.permission, speed=models.speed, mode=models.mode, daybreak=models.daybreak,
                                         backend=models.backend, efforts={"gemini": models.gemini_effort, "openai": models.openai_effort})
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/sessions/{sid}")
    async def session(sid: str):
        return view_state(app.state.engine.store, sid)

    @app.put("/api/sessions/{sid}/mode")
    async def change_mode(sid: str, data: ModeChange):
        e = app.state.engine
        try:
            e.store.change_mode(sid, data.mode, objective=data.objective)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        e.notify(sid)
        return e.store.session(sid)

    @app.post("/api/sessions/{sid}/mode/{action}")
    async def mode_action(sid: str, action: Literal["implement", "pause", "resume", "clear"]):
        e = app.state.engine
        try:
            if action == "implement":
                e.store.implement_plan(sid)
            else:
                e.store.goal_action(sid, action)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        e.notify(sid)
        return e.store.session(sid)

    @app.patch("/api/sessions/{sid}/access")
    async def access(sid: str, data: Access):
        e = app.state.engine
        if data.speed and data.speed != "standard":
            await e.client_catalogs["openai"].refresh()
        try:
            e.set_access(sid, data.permission, data.speed)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return e.store.session(sid)

    @app.post("/api/sessions/{sid}/approvals/{identifier}")
    async def decide(sid: str, identifier: str, data: Decision):
        try:
            app.state.engine.decide(sid, identifier, data.allow)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"accepted": data.allow}

    @app.patch("/api/sessions/{sid}")
    async def edit_session(sid: str, data: ChatDetails):
        engine = app.state.engine
        try:
            if data.title is not None:
                engine.store.rename_session(sid, data.title)
            if data.pinned is not None:
                engine.store.pin_session(sid, data.pinned)
            if data.archived is not None:
                engine.store.archive_session(sid, data.archived)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        engine.notify(sid)
        return engine.store.session(sid)

    @app.delete("/api/sessions/{sid}", status_code=204)
    async def delete_session(sid: str):
        app.state.engine.delete_session(sid)
        return Response(status_code=204)

    @app.put("/api/sessions/{sid}/models")
    async def models(sid: str, data: Models):
        e = app.state.engine
        try:
            if e.store.session(sid)["backend"] == "clients":
                await asyncio.gather(*(c.refresh() for c in e.client_catalogs.values()))
            e.set_choices(sid, {"gemini": (data.gemini, data.gemini_effort), "openai": (data.openai, data.openai_effort)})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return e.store.session(sid)

    @app.patch("/api/sessions/{sid}/models")
    async def choose_model(sid: str, data: ModelChoice):
        e = app.state.engine
        try:
            if e.store.session(sid)["backend"] == "clients":
                await e.client_catalogs[data.provider].refresh()
            e.set_choices(sid, {data.provider: (data.model, data.effort)})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return view_state(e.store, sid)

    @app.post("/api/sessions/{sid}/messages", status_code=202)
    async def message(sid: str, data: Message):
        e = app.state.engine
        e.store.session(sid)
        try:
            attachments = await asyncio.to_thread(data.attachments)
            seq, _ = e.submit(sid, data.text.strip(), data.client_id, attachments=attachments, intent=data.intent)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"seq": seq}

    @app.post("/api/sessions/{sid}/stop")
    async def stop_work(sid: str):
        return {"stopped": await app.state.engine.stop_session(sid)}

    @app.post("/api/sessions/{sid}/continue")
    async def continue_work(sid: str):
        return {"continued": app.state.engine.continue_session(sid)}

    @app.post("/api/sessions/{sid}/tasks/{jid}/retry")
    async def retry(sid: str, jid: str, read_only: bool = False):
        try:
            app.state.engine.retry(sid, jid, read_only=read_only)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "ready"}

    @app.get("/api/sessions/{sid}/events")
    async def events(sid: str, request: Request):
        e = app.state.engine
        e.store.session(sid)

        async def stream():
            version, last_preview, heartbeat = -1, None, 0
            while not await request.is_disconnected():
                current = e.changes.get(sid, 0)
                if current != version:
                    if not e.store.rows("SELECT 1 FROM sessions WHERE id=?", sid):
                        yield "event: deleted\ndata: {}\n\n"
                        return
                    state = view_state(e.store, sid)
                    last_seq = state["events"][-1]["seq"] if state["events"] else 0
                    yield f"id: {last_seq}\nevent: state\ndata: {pack(state)}\n\n"
                    version = current
                preview = {jid: e.previews[jid] for jid in e.previews if e.store.job(jid)["sid"] == sid}
                if preview != last_preview:
                    yield f"event: preview\ndata: {pack(preview)}\n\n"
                    last_preview = preview
                heartbeat += 1
                if heartbeat % 60 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.24)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/sessions/{sid}/artifacts/{aid}")
    async def artifact(sid: str, aid: str, download: bool = False):
        store = app.state.engine.store
        data = store.one("SELECT * FROM artifacts WHERE id=? AND sid=?", aid, sid)
        filename = data['name']
        if not download:
            html, videos = document_media(sid, 'artifacts', aid, data['content'], store.session(sid)['workspace'], filename)
            data['videos'] = videos
            if filename.lower().endswith('.md'):
                data['html'] = html
            return data
        if video_mime(filename):
            workspace = store.session(sid)['workspace']
            path = local_video(filename, workspace)
            if path is None:
                raise HTTPException(404, '成果中没有此本地视频文件')
            prefix = f'/api/sessions/{sid}/artifacts/{aid}/videos/'
            _, paths = video_document(data['content'], workspace, prefix, filename)
            return RedirectResponse(prefix + str(paths.index(path)) + '?download=true')
        name = Path(filename).name
        if data["meta"].get("log"):
            return FileResponse(data["meta"]["log"], filename=name, media_type="text/plain")
        return Response(data["content"], media_type="text/plain",
                        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe="")})

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="interface")
    return app
