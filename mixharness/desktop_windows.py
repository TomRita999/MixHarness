from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QListWidget, QListWidgetItem, QVBoxLayout

from .desktop_widgets import button, label
from .window_native import supported


ACCESS_NAMES = {'view': '仅查看', 'control': '允许操作'}


def window_caption(window):
    return ' · '.join(filter(None, (window['app'], window['title']))) or '无标题窗口'


def session_for_share(host):
    if host.backend_task:
        raise ValueError('正在切换模型连接，请稍后再共享窗口。')
    if host.sid:
        return host.sid
    if not host.models_ready():
        raise ValueError('请先选择当前会话使用的模型。')
    old_key = host.draft_key()
    models = {name: picker.model for name, picker in host.pickers.items()}
    efforts = {name: picker.effort for name, picker in host.pickers.items()}
    session = host.engine.create_session(**models, project_id=host.pid, standalone=host.pid is None,
                                         backend=host.backend_box.currentData(), permission=host.draft_permission,
                                         speed=host.draft_speed, mode=host.draft_mode, daybreak=host.draft_daybreak,
                                         recipient=host.recipient, efforts=efforts)
    sid = session['id']
    new_key = 'drafts/' + sid
    # 用户可以在第一条消息发出前共享窗口，将输入框里的文字和附件一起移入新会话。
    with host.engine.store.db:
        host.engine.store.db.execute('UPDATE draft_inputs SET draft=? WHERE draft=?', (new_key, old_key))
    host.settings.setValue(new_key, host.editor.toPlainText())
    host.settings.setValue('recipient/' + new_key, host.recipient)
    host.select_session(sid)
    for key in (old_key, 'recipient/' + old_key, 'mode/' + old_key):
        host.settings.remove(key)
    return sid


class WindowPicker(QDialog):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.owner = host.draft_key()
        self.pending = None
        self.dismissed = False
        self.permissions = {}
        self.setWindowTitle('与 AI 共享窗口')
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(550, 420)
        layout = QVBoxLayout(self)
        notice = label('选择一个窗口。本次会话使用的模型可以按需读取该窗口的画面。')
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.choices = QListWidget()
        self.choices.setAccessibleName('可共享的窗口')
        layout.addWidget(self.choices, 1)
        self.access = QComboBox()
        self.access.setAccessibleName('窗口共享权限')
        for value, caption in ACCESS_NAMES.items():
            self.access.addItem(caption, value)
        layout.addWidget(self.access)
        self.status = label('', 'caption')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        controls = QHBoxLayout()
        self.reload_button = button('刷新窗口', self.reload)
        self.screen_button = button('授权屏幕录制', lambda: self.reload('screen'))
        self.control_button = button('授权辅助功能', lambda: self.reload('accessibility'))
        controls.addWidget(self.reload_button)
        controls.addWidget(self.screen_button)
        controls.addWidget(self.control_button)
        controls.addStretch()
        controls.addWidget(button('取消', self.reject))
        self.confirm = button('共享所选窗口', self.bind, name='primary')
        controls.addWidget(self.confirm)
        layout.addLayout(controls)
        self.access.currentIndexChanged.connect(self.update_controls)
        self.choices.itemSelectionChanged.connect(self.update_controls)
        self.finished.connect(self.finish_picker)
        self.update_controls()

    def update_controls(self):
        idle = self.pending is None
        control = self.access.currentData() == 'control'
        self.reload_button.setEnabled(idle and supported())
        self.screen_button.setVisible(self.permissions.get('screen') is False)
        self.control_button.setVisible(control and self.permissions.get('accessibility') is False)
        self.screen_button.setEnabled(idle)
        self.control_button.setEnabled(idle)
        self.access.setEnabled(idle)
        self.choices.setEnabled(idle)
        allowed = self.permissions.get('screen') and (not control or self.permissions.get('accessibility'))
        self.confirm.setEnabled(bool(idle and allowed and self.choices.selectedItems()))

    def finish_picker(self):
        self.dismissed = True
        if self.pending is not None:
            self.pending.cancel()

    def start(self, operation):
        if self.pending is not None or self.dismissed:
            return
        async def run():
            try:
                await operation()
            except (KeyError, OSError, RuntimeError, ValueError) as exc:
                if not self.dismissed:
                    self.status.setText(str(exc))
            finally:
                self.pending = None
                if not self.dismissed:
                    self.update_controls()
        self.pending = self.host.run_task(run())
        self.update_controls()

    def reload(self, permission=None):
        if not supported():
            self.status.setText('共享窗口需要 macOS 14.2 或更新版本。')
            return
        async def load():
            self.choices.clear()
            self.status.setText('正在读取可共享的窗口…')
            windows = self.host.engine.windows
            options = {'request': permission} if permission in ('screen', 'accessibility') else {}
            self.permissions = await windows.native.request('permissions', **options)
            if not self.permissions['screen']:
                self.status.setText('请在系统设置中允许屏幕录制，然后刷新窗口列表。')
                return
            for choice in await windows.choices():
                item = QListWidgetItem(window_caption(choice))
                item.setData(Qt.ItemDataRole.UserRole, choice['target'])
                self.choices.addItem(item)
            self.status.setText('' if self.choices.count() else '当前没有可共享的窗口。')
        self.start(load)

    def bind(self):
        selection = self.choices.selectedItems()
        if not selection or not self.confirm.isEnabled():
            return
        target = selection[0].data(Qt.ItemDataRole.UserRole)
        access = self.access.currentData()
        async def select():
            if self.host.draft_key() != self.owner:
                raise ValueError('当前会话已经变化，请在目标会话中重新选择窗口。')
            sid = session_for_share(self.host)
            self.owner = self.host.draft_key()
            self.status.setText('正在确认所选窗口…')
            await self.host.engine.windows.bind(sid, target, access)
            self.host.inputs.restore()
            self.pending = None
            self.accept()
        self.start(select)
