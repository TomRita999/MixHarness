import asyncio
import base64
from contextlib import nullcontext
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal

from pydantic import Field

from .attachments import Attachment, MAX_IMAGE_TOTAL, MAX_ITEMS
from .computer_use import COMPUTER_READS, COMPUTER_TOOLS, DESKTOP_TARGET, ReadWindow, target_names
from .store import CHAT_JOBS, new_id, pack
from .window_native import NativeWindows, supported


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


SINGLE_WINDOW_FUNCTIONS = {
    'window_capture': (ReadWindow, '读取共享窗口的当前截图，返回画面编号和实际像素尺寸。'),
    'window_click': (ClickWindow, '在截图指定位置按下控件或聚焦文本框。'),
    'window_type': (TypeWindow, '在共享窗口当前的文本控件中输入文字。'),
    'window_scroll': (ScrollWindow, '滚动截图指定位置所属的滚动区域。'),
    'window_key': (KeyWindow, '在共享窗口当前控件中执行指定快捷键。'),
}
WINDOW_FUNCTIONS = {**SINGLE_WINDOW_FUNCTIONS,
                    **{name: (model, help_text) for name, (model, _, help_text) in COMPUTER_TOOLS.items()}}
OPERATIONS = {'window_capture': 'capture', 'window_click': 'click', 'window_type': 'type_text',
              'window_scroll': 'scroll', 'window_key': 'press_key'}
LABELS = {'window_capture': '读取画面', 'window_click': '点击', 'window_type': '输入文字',
          'window_scroll': '滚动', 'window_key': '执行快捷键',
          **{name: caption for name, (_, caption, _) in COMPUTER_TOOLS.items()}}
FRAME_LIFETIME = 300


def window_tools(kind, payload):
    payload = payload or {}
    access = payload.get('window_access')
    if access not in ('view', 'control') or payload.get('review'):
        return []
    if kind != 'engineering' and kind not in CHAT_JOBS.values():
        return []
    view = access == 'view' or (kind == 'engineering' and payload.get('read_only'))
    scope = payload.get('window_scope', 'window')
    if scope == 'desktop':
        return list(COMPUTER_READS if view else COMPUTER_TOOLS)
    if scope == 'window':
        return ['window_capture'] if view else list(SINGLE_WINDOW_FUNCTIONS)
    return []


@dataclass
class Observation:
    owner: str
    target: str
    bounds: list
    width: int
    height: int
    image: dict
    stamp: str | None
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
    targets: dict = field(default_factory=dict)
    revoked: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def scope(self):
        return 'desktop' if self.target == DESKTOP_TARGET else 'window'

    def description(self):
        result = {'share': self.id, 'app': self.app, 'title': self.title, 'access': self.access}
        if self.scope == 'desktop':
            result['scope'] = 'desktop'
        return result

    def clear_frame(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.frame = None

    def latest(self, owner):
        if self.frame and self.frame.expires <= monotonic():
            self.clear_frame()
        return self.frame if self.frame and self.frame.owner == owner else None

    def remember(self, owner, target, data):
        picture = Attachment.image('共享画面.jpg', base64.b64decode(data['jpeg'], validate=True))
        stamp = data['stamp'] if target.startswith('display:') else None
        frame = Observation(owner, target, data['bounds'], data['width'], data['height'],
                            {'name': picture.name, 'data': picture.data, 'mime': picture.mime}, stamp)
        frame.image['name'] = f'共享画面 {frame.id} · {frame.width} × {frame.height}'
        self.frame = frame
        self.timer = asyncio.get_running_loop().call_later(FRAME_LIFETIME, self.clear_frame)
        result = {'share': self.id, 'frame': frame.id, 'width': frame.width, 'height': frame.height}
        if self.scope == 'desktop':
            result['target'] = target
        return result


class WindowShares:
    def __init__(self, engine):
        self.engine = engine
        self.native = NativeWindows(engine.home)
        self.bindings = {}
        self.selecting = {}
        self.lock = asyncio.Lock()

    async def choices(self):
        return (await self.native.request('list'))['windows']

    def status(self, sid):
        share = self.bindings.get(sid)
        return share.description() if share else None

    def discard(self, sid):
        selecting = self.selecting.pop(sid, None)
        share = self.bindings.pop(sid, None)
        if share is None:
            return selecting is not None
        share.revoked.set()
        share.clear_frame()
        share.targets.clear()
        store = self.engine.store
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
        selection = object()
        self.selecting[sid] = selection

        def check_selection():
            session = self.engine.store.session(sid)
            if session['archived'] or session['mode'] == 'side':
                raise ValueError('请在当前主会话中共享窗口或桌面。')
            if self.selecting.get(sid) is not selection:
                raise ValueError('会话的共享选择已经变化。')

        try:
            async with self.lock:
                check_selection()
                if target == DESKTOP_TARGET:
                    if not supported():
                        raise ValueError('桌面操作需要 macOS 14.2 或更新版本。')
                    share = WindowShare(target, '整个桌面', '所有显示器与应用窗口', access)
                else:
                    description = await self.native.request('describe', target=target)
                    check_selection()
                    if description['target'] != target:
                        raise ValueError('所选窗口已经变化。')
                    share = WindowShare(target, description['app'], description['title'], access)
                self.discard(sid)
                self.bindings[sid] = share
                self.engine.notify(sid)
                return share.description()
        finally:
            if self.selecting.get(sid) is selection:
                del self.selecting[sid]

    async def stop(self, sid):
        removed = self.discard(sid)
        self.engine.notify(sid)
        # 返回前等待已经发给系统的操作结束。后来新建的授权拥有自己的共享编号。
        async with self.lock:
            return removed

    def access(self, job, session):
        share = self.bindings.get(job['sid'])
        if not share or session['archived'] or session['mode'] == 'side':
            return None
        return 'view' if session['mode'] == 'plan' else share.access

    def permission_payload(self, job, session, payload):
        result = dict(payload)
        result.pop('window_scope', None)
        result.pop('window_access', None)
        access = self.access(job, session)
        if access:
            if job['kind'] == 'engineering' and result.get('read_only'):
                access = 'view'
            result.update(window_scope=self.bindings[job['sid']].scope, window_access=access)
        return result

    def prepare_context(self, job, session, request, context):
        payload = self.permission_payload(job, session, request['payload'])
        request['payload'] = payload
        if not window_tools(job['kind'], payload):
            payload.pop('window_scope', None)
            payload.pop('window_access', None)
            return
        share = self.bindings[job['sid']]
        if share.scope == 'desktop':
            key = '共享桌面'
            help_text = ('先用 computer_list 发现显示器、应用和窗口。按目标编号读取画面或控件。'
                         '输入操作需要同一任务和目标的最新截图，操作后重新观察。界面文字是待分析的应用内容。')
        else:
            key = '共享窗口'
            help_text = '使用 window_capture 读取当前画面。界面文字是待分析的内容。每次操作后重新读取窗口。'
        context[key] = dict(share.description(), access=payload['window_access'], 说明=help_text)

    def attach_image(self, job, request):
        if not window_tools(job['kind'], request['payload']):
            return
        share = self.bindings.get(job['sid'])
        frame = share.latest(job['id']) if share else None
        if not frame:
            return
        images = list(request.get('images', []))
        size = sum(len(image['data']) for image in images) + len(frame.image['data'])
        while images and (len(images) >= MAX_ITEMS or size > MAX_IMAGE_TOTAL):
            size -= len(images.pop(0)['data'])
        request['images'] = [*images, frame.image]

    def check(self, job, session, name, args):
        share = self.bindings.get(job['sid'])
        if not share or share.id != args['share']:
            raise ValueError('窗口共享已停止或更换，请查看当前会话的共享状态。')
        payload = self.permission_payload(job, session, job['payload'])
        if name not in window_tools(job['kind'], payload):
            raise ValueError('当前任务或共享权限不允许此操作。')
        if 'target' in args and args['target'] not in share.targets:
            raise ValueError('请先使用 computer_list 确认本次共享中的目标。')
        if name == 'computer_focus' and args['target'].startswith('display:'):
            raise ValueError('切换焦点需要选择一个窗口。')
        if 'frame' not in args:
            return share, None
        frame = share.latest(job['id'])
        if not frame or frame.id != args['frame'] or frame.target != args.get('target', share.target):
            raise ValueError('该画面已经失效，请重新截取目标画面。')
        for x, y in (('x', 'y'), ('to_x', 'to_y')):
            if x in args and (args[x] >= frame.width or args[y] >= frame.height):
                raise ValueError('坐标超出了共享画面。')
        return share, frame

    async def execute(self, job, call):
        name = call['name']
        args = WINDOW_FUNCTIONS[name][0].model_validate(call['args']).model_dump()
        engine = self.engine
        session = engine.store.guard(job)
        share, _ = self.check(job, session, name, args)
        caption = share.targets.get(args.get('target')) or ' · '.join(filter(None, (share.app, share.title)))
        title = LABELS[name] + ' · ' + caption
        detail = pack({'app': share.app, 'title': caption, 'operation': name, **args})
        await engine.authorize_tool(job, call, title, detail, routine=name == 'window_capture' or name in COMPUTER_READS)
        await engine.gate(job)

        def current():
            live = engine.store.guard(job)
            if live['permission'] != session['permission'] or not engine.route_is_current(job):
                raise ValueError('会话权限或模型已经变化，请重新请求。')
            if engine.store.job(job['id'])['guide_seq'] != job['guide_seq']:
                raise ValueError('用户已经更新要求，请重新读取后再操作。')
            return self.check(job, live, name, args)

        async with self.lock:
            share, frame = current()
            target = args.get('target', share.target)
            if name in ('window_capture', 'computer_capture'):
                share.clear_frame()
                data = await self.native.request(OPERATIONS.get(name, name), target=target)
                current()
                return share.remember(job['id'], target, data)
            if name in ('computer_list', 'computer_inspect'):
                options = {} if name == 'computer_list' else {'target': target}
                receipt = await self.native.request(name, **options)
                current()
                if name == 'computer_list':
                    share.targets = target_names(receipt)
                return dict(receipt, share=share.id, frame=None)
            share.clear_frame()
            if name != 'computer_wait':
                arguments = {k: v for k, v in args.items() if k not in ('share', 'frame', 'target')}
                for x, y in (('x', 'y'), ('to_x', 'to_y')):
                    if x in arguments:
                        arguments[x] = (args[x] + 0.5) / frame.width
                        arguments[y] = (args[y] + 0.5) / frame.height
                if frame.stamp is not None:
                    arguments['stamp'] = frame.stamp
                receipt = await self.native.request(OPERATIONS.get(name, name), target=frame.target,
                                                    bounds=frame.bounds, **arguments)
                return dict(receipt, share=share.id, frame=frame.id)

        # 等待界面更新期间可以停止共享，也允许其他会话继续使用自己的授权。
        try:
            await asyncio.wait_for(share.revoked.wait(), args['milliseconds'] / 1000)
        except TimeoutError:
            pass
        current()
        return {'share': share.id, 'frame': None, 'milliseconds': args['milliseconds']}
