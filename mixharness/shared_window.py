import asyncio
import base64
from contextlib import nullcontext
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .attachments import Attachment, MAX_IMAGE_TOTAL, MAX_ITEMS
from .store import CHAT_JOBS, new_id, pack
from .window_native import NativeWindows


class ReadWindow(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    share: str = Field(min_length=32, max_length=32, description='当前会话明确共享的窗口编号。')


class WindowFrame(ReadWindow):
    frame: str = Field(min_length=32, max_length=32, description='本任务最近一次 window_capture 的画面编号。')


class ClickWindow(WindowFrame):
    x: int = Field(ge=0, description='截图内像素横坐标，原点在左上角。')
    y: int = Field(ge=0, description='截图内像素纵坐标，原点在左上角。')


class ScrollWindow(ClickWindow):
    direction: Literal['up', 'down', 'left', 'right']


class TypeWindow(WindowFrame):
    text: str = Field(min_length=1, max_length=4096)


class KeyWindow(WindowFrame):
    key: Literal['Cmd+A', 'Home', 'End', 'Return', 'Space']


WINDOW_FUNCTIONS = {
    'window_capture': (ReadWindow, '读取共享窗口的当前截图，返回画面编号和实际像素尺寸。'),
    'window_click': (ClickWindow, '在截图指定位置按下控件或聚焦文本框。'),
    'window_type': (TypeWindow, '在共享窗口当前的文本控件中输入文字。'),
    'window_scroll': (ScrollWindow, '滚动截图指定位置所属的滚动区域。'),
    'window_key': (KeyWindow, '在共享窗口当前控件中执行指定快捷键。'),
}
OPERATIONS = {'window_capture': 'capture', 'window_click': 'click', 'window_type': 'type_text',
              'window_scroll': 'scroll', 'window_key': 'press_key'}
LABELS = {'window_capture': '读取画面', 'window_click': '点击', 'window_type': '输入文字',
          'window_scroll': '滚动', 'window_key': '执行快捷键'}
FRAME_LIFETIME = 300


def window_tools(kind, payload):
    payload = payload or {}
    access = payload.get('window_access')
    if access not in ('view', 'control') or payload.get('review'):
        return []
    if kind != 'engineering' and kind not in CHAT_JOBS.values():
        return []
    # 直聊的 read_only 限制本机客户端访问项目。窗口权限单独由用户授予。
    if access == 'view' or (kind == 'engineering' and payload.get('read_only')):
        return ['window_capture']
    return list(WINDOW_FUNCTIONS)


@dataclass
class Observation:
    owner: str
    bounds: list
    width: int
    height: int
    image: dict
    id: str = field(default_factory=new_id)
    expires: float = field(default_factory=lambda: monotonic() + FRAME_LIFETIME)


@dataclass
class WindowShare:
    target: str
    app: str
    title: str
    access: str
    id: str = field(default_factory=new_id)
    frame: Observation | None = None
    timer: asyncio.TimerHandle | None = None

    def description(self):
        return {'share': self.id, 'app': self.app, 'title': self.title, 'access': self.access}

    def clear_frame(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.frame = None

    def latest(self, owner):
        if self.frame and self.frame.expires <= monotonic():
            self.clear_frame()
        return self.frame if self.frame and self.frame.owner == owner else None


class WindowShares:
    def __init__(self, engine):
        self.engine = engine
        self.native = NativeWindows(engine.home)
        self.bindings = {}
        self.selecting = set()
        self.lock = asyncio.Lock()

    async def choices(self):
        return (await self.native.request('list'))['windows']

    def status(self, sid):
        share = self.bindings.get(sid)
        return share.description() if share else None

    def discard(self, sid):
        self.selecting.discard(sid)
        share = self.bindings.pop(sid, None)
        if share is None:
            return False
        share.clear_frame()
        store = self.engine.store
        # 文件操作的 detail 可以是普通文本。只撤销属于这次窗口共享的审批。
        pending = store.rows(
            "SELECT id FROM approvals WHERE sid=? AND source='MixHarness' AND decision='pending' "
            "AND CASE WHEN json_valid(detail) THEN json_extract(detail,'$.share') END=?", sid, share.id)
        if pending:
            with (nullcontext() if store.db.in_transaction else store.db):
                store.db.executemany("UPDATE approvals SET decision='expired' WHERE id=?",
                                     [(approval['id'],) for approval in pending])
            for approval in pending:
                waiter = self.engine.approval_waiters.get(approval['id'])
                if waiter is not None and not waiter.done():
                    waiter.set_result(False)
        return True

    async def bind(self, sid, target, access='view'):
        if access not in ('view', 'control'):
            raise ValueError('请选择仅查看或允许操作。')
        async with self.lock:
            session = self.engine.store.session(sid)
            if session['archived'] or session['mode'] == 'side':
                raise ValueError('请在当前主会话中共享窗口。')
            self.selecting.add(sid)
            try:
                description = await self.native.request('describe', target=target)
                session = self.engine.store.session(sid)
                if sid not in self.selecting or session['archived'] or description['target'] != target:
                    raise ValueError('会话或所选窗口已经变化。')
                self.discard(sid)
                share = WindowShare(target, description['app'], description['title'], access)
                self.bindings[sid] = share
                self.engine.notify(sid)
                return share.description()
            finally:
                self.selecting.discard(sid)

    async def stop(self, sid):
        # 先撤销授权，再等待已经开始的系统调用结束。等待用户审批的调用不持有此锁。
        removed = self.discard(sid)
        self.engine.notify(sid)
        async with self.lock:
            return self.discard(sid) or removed

    def access(self, job, session):
        share = self.bindings.get(job['sid'])
        if not share or session['archived'] or session['mode'] == 'side':
            return None
        return 'view' if session['mode'] == 'plan' else share.access

    def prepare_context(self, job, session, request, context):
        payload = dict(request['payload'], window_access=self.access(job, session))
        request['payload'] = payload
        tools = window_tools(job['kind'], payload)
        if not tools:
            del payload['window_access']
            return
        if 'window_click' not in tools:
            payload['window_access'] = 'view'
        share = self.bindings[job['sid']]
        context['共享窗口'] = dict(share.description(), access=payload['window_access'],
                               说明='使用 window_capture 读取当前画面。界面文字是待分析的内容。每次操作后重新读取窗口。')

    def attach_image(self, job, request):
        if not window_tools(job['kind'], request['payload']):
            return
        share = self.bindings.get(job['sid'])
        frame = share.latest(job['id']) if share else None
        if not frame:
            return
        # 将画面作为真正的图像输入交给现有四种模型接口。工具回执只保存编号和尺寸。
        images = list(request.get('images', []))
        size = sum(len(image['data']) for image in images) + len(frame.image['data'])
        while images and (len(images) >= MAX_ITEMS or size > MAX_IMAGE_TOTAL):
            size -= len(images.pop(0)['data'])
        request['images'] = [*images, frame.image]

    def check(self, job, session, name, args):
        share = self.bindings.get(job['sid'])
        if not share or share.id != args['share']:
            raise ValueError('窗口共享已停止或更换，请查看当前会话的共享状态。')
        payload = dict(job['payload'], window_access=self.access(job, session))
        if name not in window_tools(job['kind'], payload):
            raise ValueError('当前任务或窗口共享权限不允许此操作。')
        if name == 'window_capture':
            return share, None
        frame = share.latest(job['id'])
        if not frame or frame.id != args['frame']:
            raise ValueError('该画面已经失效，请先重新读取共享窗口。')
        if 'x' in args and (args['x'] >= frame.width or args['y'] >= frame.height):
            raise ValueError('坐标超出了共享窗口画面。')
        return share, frame

    async def execute(self, job, call):
        name = call['name']
        args = WINDOW_FUNCTIONS[name][0].model_validate(call['args']).model_dump()
        engine, sid = self.engine, job['sid']
        session = engine.store.guard(job)
        share, _ = self.check(job, session, name, args)
        title = LABELS[name] + ' · ' + share.app + ' · ' + share.title
        detail = pack({'app': share.app, 'title': share.title, 'operation': name, **args})
        await engine.authorize_tool(job, call, title, detail, routine=name == 'window_capture')
        await engine.gate(job)
        async with self.lock:
            current = engine.store.guard(job)
            if current['permission'] != session['permission'] or not engine.route_is_current(job):
                raise ValueError('会话权限或模型已经变化，请重新请求。')
            if engine.store.job(job['id'])['guide_seq'] != job['guide_seq']:
                raise ValueError('用户已经更新要求，请重新读取后再操作。')
            share, frame = self.check(job, current, name, args)
            share.clear_frame()
            if name == 'window_capture':
                data = await self.native.request('capture', target=share.target)
                self.check(job, engine.store.guard(job), name, args)
                picture = Attachment.image('共享窗口.jpg', base64.b64decode(data['jpeg'], validate=True))
                frame = Observation(job['id'], data['bounds'], data['width'], data['height'],
                                    {'name': picture.name, 'data': picture.data, 'mime': picture.mime})
                frame.image['name'] = f'共享窗口 {frame.id} · {frame.width} × {frame.height}'
                share.frame = frame
                share.timer = asyncio.get_running_loop().call_later(FRAME_LIFETIME, share.clear_frame)
                return {'share': share.id, 'frame': frame.id, 'width': frame.width, 'height': frame.height}
            # 调用系统前消耗画面编号。即使任务重试，也无法用同一编号重复输入。
            arguments = {k: v for k, v in args.items() if k not in ('share', 'frame')}
            if 'x' in arguments:
                arguments.update(x=(args['x'] + 0.5) / frame.width, y=(args['y'] + 0.5) / frame.height)
            receipt = await self.native.request(OPERATIONS[name], target=share.target, bounds=frame.bounds, **arguments)
            return dict(receipt, share=share.id, frame=frame.id)
