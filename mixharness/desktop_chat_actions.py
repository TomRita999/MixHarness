from PySide6.QtCore import QIODevice, QSaveFile, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDockWidget, QFileDialog, QHBoxLayout, QVBoxLayout, QWidget,
)

from .chat_snapshot import snapshot_link, transcript
from .desktop_style import THEMES
from .desktop_widgets import Document, Editor, Timeline, button, label
from .slash import mcp_text
from .store import ACTIVE_STATUSES, new_id


class McpDialog(QDialog):
    def __init__(self, host, source):
        super().__init__(host)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("MCP 服务")
        self.resize(490, 350)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        clients = QComboBox()
        clients.setAccessibleName("MCP 客户端")
        clients.addItem("Codex", "codex")
        clients.addItem("Antigravity", "antigravity")
        clients.setCurrentIndex(clients.findData(source))
        row.addWidget(clients)
        row.addStretch()
        layout.addLayout(row)
        output = Document()
        output.setAccessibleName("MCP 服务状态")
        layout.addWidget(output)
        layout.addWidget(button("关闭", self.reject), 0, Qt.AlignmentFlag.AlignRight)
        sid, pid = host.sid, host.pid
        task = None
        def refresh():
            nonlocal task
            if task:
                task.cancel()
            selected = clients.currentData()
            output.setPlainText("正在读取 " + clients.currentText() + " 的 MCP 服务…")
            async def fetch():
                try:
                    value = await mcp_text(host.engine, selected, sid, pid)
                except (OSError, ValueError, RuntimeError, KeyError) as exc:
                    value = str(exc) or "读取客户端超时"
                output.setPlainText(value)
            task = host.run_task(fetch())
        clients.currentIndexChanged.connect(refresh)
        row.addWidget(button("刷新", refresh))
        self.finished.connect(lambda _: task.cancel() if task else None)
        refresh()


class ShareDialog(QDialog):
    def __init__(self, host, sid):
        super().__init__(host)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("分享聊天快照")
        self.resize(620, 430)
        self.title = host.engine.store.session(sid)["title"]
        self.markdown = transcript(host.engine.store, sid)
        layout = QVBoxLayout(self)
        preview = Document()
        preview.setAccessibleName("待分享的文字快照")
        preview.setPlainText(self.markdown)
        layout.addWidget(preview, 1)
        self.status = label("链接内含此刻的对话文字和附件名称。收件人可粘贴到浏览器地址栏打开。", "caption")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        row.addWidget(button("保存 Markdown…", lambda: save_transcript(self, self.markdown, self.status.setText)))
        row.addStretch()
        row.addWidget(button("关闭", self.reject))
        row.addWidget(button("复制快照链接", self.copy_link, name="primary"))
        layout.addLayout(row)

    def copy_link(self):
        try:
            url = snapshot_link(self.title, self.markdown)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        QApplication.clipboard().setText(url)
        self.status.setText("已复制快照链接。")


def save_transcript(parent, markdown, report):
    picker = QFileDialog(parent, "导出聊天")
    picker.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    picker.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
    picker.setDefaultSuffix("md")
    picker.setNameFilter("Markdown (*.md)")
    picker.selectFile("MixHarness 对话.md")
    def write(path):
        data = (markdown + "\n").encode("utf-8")
        file = QSaveFile(path)
        if not file.open(QIODevice.OpenModeFlag.WriteOnly) or file.write(data) != len(data) or not file.commit():
            report(file.errorString())
            return
        report("已保存至 " + path)
    picker.fileSelected.connect(write)
    picker.open()


class SideChat(QDockWidget):
    def __init__(self, host, origin):
        super().__init__("侧边聊天", host)
        self.host, self.origin = host, origin
        self.sid = None
        self.version = None
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea)
        body = QWidget()
        body.setMinimumWidth(220)
        layout = QVBoxLayout(body)
        context = label(host.engine.store.session(origin)["title"], "section")
        context.setWordWrap(True)
        layout.addWidget(context)
        self.timeline = Timeline(host.engine.store.inputs)
        layout.addWidget(self.timeline, 1)
        self.status = label("", "caption")
        self.status.setWordWrap(True)
        activity = QHBoxLayout()
        activity.addWidget(self.status, 1)
        self.stop_button = button("停止", self.stop, name="stopWork")
        self.stop_button.setAccessibleName("停止侧边聊天任务")
        self.stop_button.setToolTip("停止侧边聊天任务（输入框内按 Esc）")
        activity.addWidget(self.stop_button)
        self.stop_button.hide()
        layout.addLayout(activity)
        self.editor = Editor()
        self.editor.setAccessibleName("侧边问题")
        self.editor.setPlaceholderText("基于主对话问 GPT…")
        self.editor.submitted.connect(self.send)
        self.editor.interrupted.connect(self.stop)
        layout.addWidget(self.editor)
        self.send_button = button("提问", self.send, name="primary")
        layout.addWidget(self.send_button, 0, Qt.AlignmentFlag.AlignRight)
        self.setWidget(body)
        self.editor.textChanged.connect(self.update_button)
        self.update_button()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(300)

    def update_button(self):
        self.send_button.setEnabled(bool(self.editor.toPlainText().strip()))

    def stop(self):
        if self.sid and self.stop_button.isEnabled():
            self.stop_button.setEnabled(False)
            self.editor.can_interrupt = False
            self.status.setText("正在停止任务…")
            self.host.run_task(self.host.engine.stop_session(self.sid))

    def send(self):
        question = self.editor.toPlainText().strip()
        if not question:
            return
        if len(question) > 120000:
            self.status.setText("单条问题上限为 120,000 字符。")
            return
        engine = self.host.engine
        try:
            if self.sid is None:
                self.sid = engine.store.fork_session(self.origin, "", side=True)["id"]
                self.timeline.seen = engine.store.one("SELECT MAX(seq) AS seq FROM events WHERE sid=?", self.sid)["seq"]
            if engine.store.session(self.sid)["mode"] != "side":
                raise ValueError("这段侧边会话已切换模式，请在主窗口中继续。")
            engine.submit(self.sid, question, new_id())
        except (ValueError, KeyError) as exc:
            self.status.setText(str(exc))
            return
        self.editor.clear()
        self.refresh()

    def refresh(self):
        if not self.sid or not self.isVisible():
            return
        engine = self.host.engine
        if engine.changes.get(self.sid, 0) == self.version:
            return
        self.version = engine.changes.get(self.sid, 0)
        try:
            state = engine.store.snapshot(self.sid)
        except KeyError:
            self.status.setText("这段会话已被删除。")
            self.editor.setEnabled(False)
            self.send_button.setEnabled(False)
            self.stop_button.hide()
            return
        self.timeline.update_events(state["events"], THEMES[self.host.theme])
        latest = state["tasks"][-1] if state["tasks"] else None
        status = latest["status"] if latest else None
        stopping = self.sid in engine.stopping and status not in ACTIVE_STATUSES
        self.status.setText("正在停止任务…" if stopping else {
            "ready": "问题已排队", "running": "GPT 正在回答", "waiting": "正在等待前置任务",
            "needs_key": "等待 OpenAI API Key", "stopped": "任务已停止",
        }.get(status, ""))
        self.stop_button.setVisible(stopping or status in ACTIVE_STATUSES)
        self.stop_button.setEnabled(status in ACTIVE_STATUSES)
        self.editor.can_interrupt = self.stop_button.isEnabled()
