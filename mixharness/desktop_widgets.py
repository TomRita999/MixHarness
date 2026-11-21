import os
import re
import shutil
import tempfile
from datetime import datetime
from math import ceil
from pathlib import Path

from dotenv import set_key
from PySide6.QtCore import QIODevice, QPoint, QSaveFile, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QTextDocument
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from .desktop_style import Glyph
from .reasoning import LEVEL_NAMES, api_group, group_for


ALIGN = Qt.AlignmentFlag
MARKDOWN = QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML
ACTORS = {"user": "你", "gemini": "Gemini", "openai": "GPT", "system": "工作记录"}


def label(text, name=""):
    result = QLabel()
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setText(text)
    result.setObjectName(name)
    return result


def icon(name, inverse=False):
    return QIcon(Glyph(QApplication.instance().property("iconFamily"), name, inverse))


def button(text, callback, glyph=None, name="", icon_only=False):
    result = QPushButton("" if icon_only else text)
    result.setObjectName(name)
    result.setAccessibleName(text)
    if glyph:
        result.setIcon(icon(glyph, name == "send"))
        result.setIconSize(QSize(17, 17))
    if icon_only:
        result.setToolTip(text)
        result.setFixedSize(30, 30)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    result.clicked.connect(callback)
    return result


class Editor(QPlainTextEdit):
    submitted = Signal()

    def __init__(self):
        super().__init__()
        self.preedit = False
        self.setObjectName("editor")
        self.setAccessibleName("消息")
        self.setPlaceholderText("输入需求、补充要求，或输入 /help 查看指令…")
        self.setTabChangesFocus(True)
        self.setFixedHeight(70)
        self.document().setDocumentMargin(3)

    def inputMethodEvent(self, event):
        self.preedit = bool(event.preeditString())
        super().inputMethodEvent(event)

    def keyPressEvent(self, event):
        enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if enter and modifiers == Qt.KeyboardModifier.NoModifier and not self.preedit:
            if not event.isAutoRepeat():
                self.submitted.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class Submenu(QMenu):
    def __init__(self, title, parent):
        super().__init__(title, parent)
        parent.addMenu(self)
        self.back = self.addAction("返回上一级")
        self.back.triggered.connect(self.hide)
        self.addSeparator()

    def mouseReleaseEvent(self, event):
        if self.actionAt(event.position().toPoint()) == self.back:
            self.hide()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if self.activeAction() == self.back and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)


class ModelPicker(QWidget):
    changed = Signal(str, str, str)
    refresh_requested = Signal(str)
    usage_requested = Signal(str)

    def __init__(self, provider, title):
        super().__init__()
        self.provider = provider
        self.title = title
        self.model = ""
        self.effort = ""
        self.catalog = {}
        self.dialog = None
        self.menu = None
        self.loading = False
        row = QVBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(1)
        self.caption = label(title, "caption")
        self.caption.setIndent(4)
        heading = QHBoxLayout()
        heading.setSpacing(2)
        heading.addWidget(self.caption)
        self.effort_control = QToolButton()
        self.effort_control.setObjectName("effort")
        self.effort_control.setCursor(Qt.CursorShape.PointingHandCursor)
        self.effort_control.clicked.connect(self.open_effort)
        heading.addWidget(self.effort_control)
        heading.addStretch(1)
        row.addLayout(heading)
        self.control = QToolButton()
        self.control.setObjectName("model")
        self.control.setAccessibleName(title + " 模型")
        self.control.setIcon(icon(provider))
        self.control.setIconSize(QSize(14, 14))
        self.control.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.control.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.control.setCursor(Qt.CursorShape.PointingHandCursor)
        self.control.clicked.connect(self.open_models)
        row.addWidget(self.control)

    def select(self, model, catalog, effort=""):
        self.model = model
        self.effort = effort
        if model and catalog["source"] != "client" and model not in catalog["models"]:
            catalog = {**catalog, "groups": [*catalog["groups"], api_group(self.provider, model)]}
        self.catalog = catalog
        self.title = catalog.get("client") or ("Gemini" if self.provider == "gemini" else "OpenAI")
        self.caption.setText(self.title)
        self.control.setAccessibleName(self.title + " 模型")
        group = group_for(catalog["groups"], model)
        self.control.setText((group["name"] if group else model or "选择模型") + "  ▾")
        self.control.setToolTip(self.title + " · " + (model or "选择模型"))
        level = self.selected_level(group)
        self.effort_control.setText((LEVEL_NAMES.get(level, level) or "默认") + " ▾")
        self.effort_control.setAccessibleName(self.title + " 思考强度")
        self.effort_control.setToolTip("调整 " + self.title + " 的思考强度")
        self.effort_control.setVisible(bool(group and any(group["choices"])))
        self.effort_control.setEnabled(bool(group and (len(group["choices"]) > 1 or catalog["source"] != "client")))
        if group and len(group["choices"]) == 1 and catalog["source"] == "client":
            self.effort_control.setText(LEVEL_NAMES.get(level, level))
        if self.dialog:
            self.update_list()

    def selected_level(self, group):
        if not group:
            return ""
        if self.model in group["choices"].values() and self.model != group["id"]:
            return next(level for level, value in group["choices"].items() if value == self.model)
        return self.effort or group["default"]

    def new_menu(self, accessible):
        menu = QMenu(self)
        menu.setAccessibleName(accessible)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.menu = menu
        menu.aboutToHide.connect(lambda: setattr(self, "menu", None))
        return menu

    def level_actions(self, menu, group):
        current = self.model == group["id"] or self.model in group["choices"].values()
        variants = any(value != group["id"] for value in group["choices"].values())
        choices = group["choices"] if variants else {"": group["id"], **group["choices"]}
        level = self.selected_level(group) if variants else self.effort
        for value, model in choices.items():
            name = LEVEL_NAMES.get(value, value) if value else "模型默认"
            if not value and group["default"]:
                name += " · " + LEVEL_NAMES.get(group["default"], group["default"])
            action = menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(current and level == value)
            action.triggered.connect(lambda _, m=model, e=value: self.changed.emit(self.provider, m, e))

    def popup(self, menu, control):
        position = control.mapToGlobal(QPoint(0, 0))
        screen = control.screen().availableGeometry()
        menu.popup(QPoint(position.x(), max(screen.top(), position.y() - menu.sizeHint().height() - 4)))

    def open_effort(self):
        group = group_for(self.catalog["groups"], self.model)
        if group:
            menu = self.new_menu(self.title + " 思考强度")
            self.level_actions(menu, group)
            self.popup(menu, self.effort_control)

    def open_models(self):
        client = self.catalog["source"] == "client"
        if not client and self.provider == "openai":
            self.open_dialog()
            return
        menu = self.new_menu(self.title + " 模型")
        selected = group_for(self.catalog["groups"], self.model)
        if not client:
            origin = "当前密钥的模型" if self.catalog["source"] == "account" else "API 参考模型"
            menu.addSection(origin)
        for group in self.catalog["groups"]:
            levels = group["choices"]
            if not client and not levels and group is not selected:
                continue
            level = self.selected_level(group) if group is selected else group["default"]
            text = group["name"]
            if level:
                text += "    " + LEVEL_NAMES.get(level, level)
            if len(levels) > 1:
                sub = Submenu(text, menu)
                self.level_actions(sub, group)
                action = sub.menuAction()
            else:
                model = next(iter(levels.values()), group["id"])
                action = menu.addAction(text)
                action.triggered.connect(lambda _, m=model, e=level: self.changed.emit(self.provider, m, e))
            action.setCheckable(True)
            action.setChecked(group is selected)
        if client and not self.catalog["groups"]:
            menu.addAction(self.catalog["error"] or "正在读取模型目录…").setEnabled(False)
        menu.addSeparator()
        if not client:
            menu.addAction("搜索全部模型…", self.open_dialog)
        refresh = menu.addAction("刷新模型目录", lambda: self.refresh_requested.emit(self.provider))
        refresh.setEnabled(client or self.catalog["configured"])
        menu.addAction("查看套餐用量", lambda: self.usage_requested.emit(self.provider))
        self.popup(menu, self.control)

    def open_dialog(self):
        if self.dialog:
            self.dialog.raise_()
            return
        dialog = QDialog(self)
        self.dialog = dialog
        dialog.setWindowTitle("选择 " + self.title + " 模型")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.resize(460, 490)
        layout = QVBoxLayout(dialog)
        self.origin = label("", "caption")
        self.origin.setWordWrap(True)
        layout.addWidget(self.origin)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索模型 ID")
        self.search.setAccessibleName("搜索模型")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.update_list)
        layout.addWidget(self.search)
        self.options = QListWidget()
        self.options.setAccessibleName(self.title + " 模型列表")
        layout.addWidget(self.options, 1)
        actions = QHBoxLayout()
        self.reload = button("刷新目录", lambda: self.refresh_requested.emit(self.provider))
        actions.addWidget(self.reload)
        self.manual = button("输入模型 ID…", self.custom)
        actions.addWidget(self.manual)
        actions.addWidget(button("查看套餐用量", lambda: self.usage_requested.emit(self.provider)))
        actions.addStretch()
        actions.addWidget(button("取消", dialog.reject))
        self.apply = button("使用模型", self.choose, name="primary")
        actions.addWidget(self.apply)
        layout.addLayout(actions)
        self.options.itemActivated.connect(self.choose)
        self.options.currentItemChanged.connect(lambda current, _: self.apply.setEnabled(bool(current)))
        dialog.finished.connect(self.dialog_closed)
        self.update_list()
        dialog.open()
        self.search.setFocus()

    def dialog_closed(self):
        self.dialog = None

    def update_list(self):
        data = self.catalog
        source = "当前密钥返回的目录" if data["source"] == "account" else "官方参考目录"
        local = data["source"] == "client"
        if local:
            source = self.title + " 客户端目录"
        self.manual.setVisible(not local)
        message = data["error"]
        if self.loading:
            message = "正在读取模型目录…"
        elif not data["configured"]:
            message = "填写 API Key 后可查询自己的模型"
        elif data["source"] == "account":
            message = message or "此工作台需要支持文本输出和函数调用的模型"
        self.origin.setText(f"{source} · {len(data['models'])} 个模型\n{message}".rstrip())
        self.reload.setEnabled(data["configured"] and not self.loading)
        query = self.search.text().strip().casefold()
        current = self.options.currentItem()
        selected = current.data(Qt.ItemDataRole.UserRole) if current else self.model
        self.options.clear()
        values = data["models"] if local else dict.fromkeys([self.model, *data["models"]])
        for value in values:
            name = data.get("labels", {}).get(value, value)
            if query not in value.casefold() and query not in name.casefold():
                continue
            text = name
            if value == self.model:
                text += "   · 当前选择"
                if value not in data["models"]:
                    text += "（目录未列出）"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, value)
            self.options.addItem(item)
            if value == selected:
                self.options.setCurrentItem(item)
        if self.options.count() and not self.options.currentItem():
            self.options.setCurrentRow(0)
        self.apply.setEnabled(bool(self.options.currentItem()))

    def choose(self):
        item = self.options.currentItem()
        if item:
            model = item.data(Qt.ItemDataRole.UserRole)
            self.changed.emit(self.provider, model, self.effort if model == self.model else "")
            self.dialog.accept()

    def custom(self):
        dialog = QInputDialog(self.dialog or self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(self.title + " 模型")
        dialog.setLabelText("模型 ID")
        dialog.setTextValue(self.model)
        dialog.setOkButtonText("使用模型")
        dialog.setCancelButtonText("取消")

        def choose(value):
            value = value.strip()
            if not re.fullmatch(r"[a-zA-Z0-9._:/-]{1,150}", value):
                QMessageBox.warning(self, "模型 ID 无效", "请输入服务商提供的模型 ID。")
                return
            self.changed.emit(self.provider, value, self.effort if value == self.model else "")
            if self.dialog:
                self.dialog.accept()

        dialog.textValueSelected.connect(choose)
        dialog.open()


class Document(QTextBrowser):
    def __init__(self):
        super().__init__()
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.anchorClicked.connect(self.follow_link)
        self.document().setDocumentMargin(4)

    def loadResource(self, kind, url):
        return None

    def follow_link(self, url):
        if url.scheme() in ("https", "http"):
            QDesktopServices.openUrl(url)


class MessageRow(QWidget):
    def __init__(self, event):
        super().__init__()
        self.user = event["actor"] == "user"
        self.text = event["body"]
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 5, 2, 5)
        row.setSpacing(0)
        self.block = QFrame()
        self.block.setObjectName("userBubble" if self.user else "reply")
        box = QVBoxLayout(self.block)
        box.setSpacing(5)
        margins = (12, 8, 12, 8) if self.user else (0, 2, 0, 4)
        box.setContentsMargins(*margins)
        author = ACTORS.get(event["actor"], event["actor"])
        if self.user:
            row.addStretch()
        else:
            title = {"verification": "Gemini 请求 GPT 验证", "failure": author + " · 执行失败"}.get(event["kind"], author)
            box.addWidget(label(title, "messageAuthor"))
        self.body = Document()
        self.body.setObjectName("messageText")
        self.body.setReadOnly(True)
        self.body.setFrameShape(QFrame.Shape.NoFrame)
        self.body.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.body.document().setDocumentMargin(0)
        if self.user or event["kind"] != "message":
            self.body.setPlainText(self.text)
        else:
            self.body.document().setMarkdown(self.text, MARKDOWN)
        if event.get("at"):
            stamp = datetime.fromisoformat(event["at"]).astimezone().strftime("%Y-%m-%d %H:%M")
            self.block.setToolTip(author + " · " + stamp)
        box.addWidget(self.body)
        row.addWidget(self.block)
        metrics = self.body.fontMetrics()
        self.natural = max((metrics.horizontalAdvance(line[:1024]) for line in self.text.splitlines()), default=0)

    def fit(self, available):
        width = max(50, available - 6)
        if self.user:
            width = max(40, min(int(width * 0.8), self.natural + 30))
        padding = 24 if self.user else 0
        self.block.setFixedWidth(width)
        self.body.setFixedWidth(width - padding)
        self.body.document().setTextWidth(width - padding)
        self.body.setFixedHeight(max(self.body.fontMetrics().height(), ceil(self.body.document().size().height())) + 2)


class Timeline(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setAccessibleName("共享会话记录")
        self.setObjectName("timeline")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.canvas = QWidget()
        self.canvas.setObjectName("timelineCanvas")
        self.column = QVBoxLayout(self.canvas)
        self.column.setContentsMargins(0, 6, 2, 10)
        self.column.setSpacing(5)
        self.column.addStretch()
        self.setWidget(self.canvas)
        self.rows = []
        self.records = []
        self.group_button = None
        self.group_body = None
        self.seen = 0

    def reset(self):
        while self.column.count() > 1:
            item = self.column.takeAt(0)
            item.widget().hide()
            item.widget().deleteLater()
        self.rows.clear()
        self.records.clear()
        self.group_button = self.group_body = None
        self.seen = 0

    def toPlainText(self):
        return "\n\n".join(self.records)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for row in self.rows:
            row.fit(self.viewport().width() - 4)

    def progress(self, text):
        if self.group_button is None:
            group = QFrame()
            box = QVBoxLayout(group)
            box.setContentsMargins(1, 0, 1, 0)
            box.setSpacing(4)
            control = QToolButton()
            control.setObjectName("activity")
            control.setCheckable(True)
            control.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            control.setArrowType(Qt.ArrowType.RightArrow)
            details = label("", "caption")
            details.setWordWrap(True)
            details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            details.hide()
            control.toggled.connect(details.setVisible)
            control.toggled.connect(lambda opened: control.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow))
            box.addWidget(control, 0, ALIGN.AlignLeft)
            box.addWidget(details)
            self.group_button, self.group_body = control, details
            self.column.insertWidget(self.column.count() - 1, group)
        prior = self.group_body.text()
        self.group_body.setText(prior + "\n" + text if prior else text)
        self.group_button.setText("工作记录")

    def update_events(self, events, colors):
        bar = self.verticalScrollBar()
        follow = bar.maximum() - bar.value() < 30
        changed = False
        for event in events:
            if event["seq"] <= self.seen:
                continue
            self.seen = event["seq"]
            kind = event["kind"]
            if kind not in ("message", "verification", "failure", "progress", "requirements"):
                continue
            self.records.append(event["body"])
            changed = True
            if kind in ("progress", "requirements"):
                self.progress(event["body"])
                continue
            self.group_button = self.group_body = None
            row = MessageRow(event)
            row.fit(self.viewport().width() - 4)
            self.rows.append(row)
            self.column.insertWidget(self.column.count() - 1, row)
        if changed and follow:
            self.column.activate()
            self.canvas.adjustSize()
            bar.setValue(bar.maximum())


def save_keys(project, values):
    if not values:
        return
    destination = project / ".env"
    with tempfile.NamedTemporaryFile(dir=project, prefix=".mixharness-", delete=False) as stream:
        staging = Path(stream.name)
    try:
        if destination.exists():
            shutil.copyfile(destination, staging)
        for key, value in values.items():
            set_key(staging, key, value)
        os.replace(staging, destination)
    finally:
        staging.unlink(missing_ok=True)


class SettingsDialog(QDialog):
    saved = Signal()

    def __init__(self, parent, engine):
        super().__init__(parent)
        self.setWindowTitle("连接设置")
        self.setMinimumWidth(405)
        self.engine = engine
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(9)
        self.fields = {}
        for name, title, url in (
            ("gemini", "Gemini", "https://aistudio.google.com/apikey"),
            ("openai", "OpenAI", "https://platform.openai.com/api-keys"),
        ):
            variable = name.upper() + "_API_KEY"
            header = QHBoxLayout()
            header.addWidget(label(title + " API Key", "section"))
            header.addWidget(label("已配置" if engine.keys[name] else "未配置", "caption"))
            header.addStretch()
            header.addWidget(button("获取密钥 ↗", lambda checked=False, url=url: QDesktopServices.openUrl(QUrl(url)),
                                    name="quiet"))
            layout.addLayout(header)
            field = QLineEdit()
            field.setAccessibleName(title + " API Key")
            field.setEchoMode(QLineEdit.EchoMode.Password)
            field.setPlaceholderText("已配置，留空保留" if engine.keys[name] else "填写 API Key")
            if engine.key_sources[name].startswith("环境变量"):
                field.setPlaceholderText(engine.key_sources[name])
                field.setEnabled(False)
            self.fields[variable] = field
            layout.addWidget(field)
            if engine.key_sources[name]:
                origin = label("来源　" + engine.key_sources[name], "caption")
                origin.setWordWrap(True)
                layout.addWidget(origin)
        layout.addSpacing(10)
        path = label("配置文件\n" + str(engine.project / ".env"), "caption")
        path.setWordWrap(True)
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(path)
        def agent_settings():
            from .desktop_agent import AgentDialog
            AgentDialog(self, engine, parent.sid, parent.pid).open()
        layout.addWidget(button("技能与上下文窗口…", agent_settings), 0, ALIGN.AlignLeft)
        actions = QHBoxLayout()
        actions.addWidget(button("重新读取文件", self.reload))
        actions.addStretch()
        actions.addWidget(button("取消", self.reject))
        actions.addWidget(button("保存", self.save, name="primary"))
        layout.addLayout(actions)

    def reload(self):
        try:
            self.engine.reload_config()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法读取配置", str(exc))
            return
        self.saved.emit()
        self.accept()

    def save(self):
        values = {key: field.text().strip() for key, field in self.fields.items() if field.text().strip()}
        try:
            save_keys(self.engine.project, values)
        except OSError as exc:
            QMessageBox.warning(self, "无法保存密钥", str(exc))
            return
        self.reload()


class ArtifactDialog(QDialog):
    def __init__(self, parent, asset):
        super().__init__(parent)
        self.asset = asset
        self.setWindowTitle(asset["name"])
        self.resize(700, 530)
        layout = QVBoxLayout(self)
        text = Document()
        if asset["name"].lower().endswith(".md"):
            text.document().setMarkdown(asset["content"], MARKDOWN)
        else:
            text.setPlainText(asset["content"])
        layout.addWidget(text)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("另存为…", self.choose_path))
        actions.addWidget(button("关闭", self.accept))
        layout.addLayout(actions)

    def choose_path(self):
        dialog = QFileDialog(self, "保存成果")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.selectFile(Path(self.asset["name"]).name)
        dialog.fileSelected.connect(self.export)
        dialog.open()

    def export(self, path):
        target = QSaveFile(path)
        if not target.open(QIODevice.OpenModeFlag.WriteOnly):
            QMessageBox.warning(self, "无法保存", target.errorString())
            return
        try:
            source = self.asset["meta"].get("log")
            if source:
                with Path(source).open("rb") as log:
                    for chunk in iter(lambda: log.read(1024 * 1024), b""):
                        if target.write(chunk) != len(chunk):
                            raise OSError(target.errorString())
            else:
                data = self.asset["content"].encode("utf-8")
                if target.write(data) != len(data):
                    raise OSError(target.errorString())
            if not target.commit():
                raise OSError(target.errorString())
        except OSError as exc:
            target.cancelWriting()
            QMessageBox.warning(self, "无法保存", str(exc))
