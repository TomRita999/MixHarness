import asyncio

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from . import clients
from .desktop_widgets import Timeline, button, label


class ClientHistory(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.source = None
        self.cursor = None
        self.loading = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self.caption = label("", "section")
        head.addWidget(self.caption, 1)
        head.addWidget(button("刷新", lambda: self.load(), name="quiet"))
        layout.addLayout(head)
        self.list = QListWidget()
        self.list.setAccessibleName("客户端会话列表")
        self.list.itemActivated.connect(self.open_item)
        self.list.itemClicked.connect(self.open_item)
        layout.addWidget(self.list, 1)
        self.status = label("", "caption")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.more = button("加载更多会话", lambda: self.load(more=True))
        layout.addWidget(self.more)

    def select_source(self, source):
        if self.loading:
            self.loading.cancel()
        self.source = source
        if source:
            self.load()

    def load(self, more=False):
        if self.loading:
            self.loading.cancel()
        source = self.source
        if not more:
            self.list.clear()
            self.cursor = None
        self.caption.setText("全部会话" if source == "all" else clients.NAMES[source] + " 会话")
        self.status.setText("正在读取会话…")
        self.more.hide()
        async def fetch():
            try:
                data = await clients.list_conversations(self.window.engine.store, source, self.cursor)
            except (OSError, ValueError, RuntimeError) as exc:
                self.status.setText(str(exc) or "读取客户端会话超时")
                return
            for row in data["data"]:
                name = clients.NAMES[row["source"]]
                item = QListWidgetItem(name + "  ·  " + row["title"])
                item.setData(Qt.ItemDataRole.UserRole, row)
                item.setToolTip(name + " · " + (row.get("cwd") or row.get("workspace") or "") + "\n" + row["title"])
                self.list.addItem(item)
            self.cursor = data["next"]
            self.more.setVisible(bool(self.cursor))
            errors = [clients.NAMES[c] + " · " + msg for c, msg in data.get("errors", {}).items()]
            self.status.setText("\n".join(errors) if errors else ("" if self.list.count() else "客户端中暂无会话"))
        self.loading = self.window.run_task(fetch())

    def open_item(self, item):
        row = item.data(Qt.ItemDataRole.UserRole)
        if row["source"] == "mixharness":
            self.window.sources_box.setCurrentIndex(0)
            self.window.select_session(row["id"])
            return
        dialog = QDialog(self.window)
        dialog.setWindowTitle(clients.NAMES[row["source"]] + " · " + row["title"])
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.resize(760, 575)
        layout = QVBoxLayout(dialog)
        transcript = Timeline()
        layout.addWidget(transcript, 1)
        status = label("正在读取会话…", "caption")
        layout.addWidget(status)
        actions = QHBoxLayout()
        actions.addStretch()
        cursor = None
        task = None
        def load(reload=False):
            nonlocal task, cursor
            if reload:
                transcript.reset()
                cursor = None
            if task:
                task.cancel()
            more.setEnabled(False)
            status.setText("正在读取会话…")
            async def fetch():
                nonlocal cursor
                try:
                    page = await clients.history(row["source"], row["id"], row["cwd"], cursor)
                except (OSError, ValueError, RuntimeError) as exc:
                    status.setText(str(exc) or "读取会话正文超时")
                    return
                from .desktop_style import THEMES
                events = [{"seq": transcript.seen + i + 1, "kind": "message", "at": "",
                           "actor": "user" if msg["role"] == "user" else clients.NAMES[row["source"]],
                           "body": msg["text"]} for i, msg in enumerate(page["messages"])]
                transcript.update_events(events, THEMES[self.window.theme])
                cursor = page["next"]
                more.setEnabled(bool(cursor))
                more.setVisible(bool(cursor))
                status.setText("" if transcript.seen else "此会话还没有消息")
            task = self.window.run_task(fetch())
        more = button("加载后续消息", load)
        more.hide()
        actions.addWidget(more)
        actions.addWidget(button("刷新", lambda: load(reload=True)))
        actions.addWidget(button("关闭", dialog.accept))
        layout.addLayout(actions)
        dialog.finished.connect(lambda _: task.cancel() if task else None)
        dialog.open()
        load()
