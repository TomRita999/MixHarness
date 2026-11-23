import os
import re
import shutil
import tempfile
from datetime import datetime
from math import ceil
from pathlib import Path

from dotenv import set_key
from PySide6.QtCore import QBuffer, QEasingCurve, QIODevice, QPoint, QPointF, QPropertyAnimation, QSaveFile, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFontDatabase, QFontMetrics, QIcon, QImage, QPainter, QPalette, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QTextBrowser, QTextEdit, QToolButton, QToolTip,
    QVBoxLayout, QWidget,
)

from .desktop_style import Glyph
from .desktop_markdown import link_plain_text, render_markdown
from .markdown_links import HASH_LINE
from .attachments import TYPES
from .store import LABELS
from .reasoning import LEVEL_NAMES, api_group, group_for, model_available


ALIGN = Qt.AlignmentFlag
ACTORS = {"user": "你", "gemini": "Gemini", "openai": "GPT", "system": "过程"}


def label(text, name=""):
    result = QLabel()
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setText(text)
    result.setObjectName(name)
    return result


def icon(name, inverse=False, scale=1.0, *, role=QPalette.ColorRole.Text):
    return QIcon(Glyph(QApplication.instance().property("iconFamily"), name,
                       QPalette.ColorRole.Base if inverse else role, scale))


def menu_icon(name):
    # 菜单图标略小于文字行高，视觉上与文字对齐。
    return icon(name, scale=0.78)


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


def disclosure(text):
    control = QToolButton()
    control.setObjectName("activity")
    control.setText(text)
    control.setCheckable(True)
    control.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    control.setArrowType(Qt.ArrowType.RightArrow)
    control.toggled.connect(lambda opened: control.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow))
    return control


class Editor(QPlainTextEdit):
    image_files = Signal(list)
    image_bytes = Signal(bytes)
    submitted = Signal()
    interrupted = Signal()
    focus_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.preedit = False
        self.can_interrupt = False
        self.setObjectName("editor")
        self.setAccessibleName("消息")
        self.setPlaceholderText("输入消息，或用 / 调用指令…")
        self.setTabChangesFocus(True)
        self.setFixedHeight(66)
        self.document().setDocumentMargin(3)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.focus_changed.emit(True)

    def canInsertFromMimeData(self, source):
        return source.hasImage() or source.hasUrls() or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        paths = [url.toLocalFile() for url in source.urls() if url.isLocalFile()]
        if paths:
            self.image_files.emit(paths)
        elif source.hasImage():
            picture = source.imageData()
            if isinstance(picture, QPixmap):
                picture = picture.toImage()
            if isinstance(picture, QImage) and not picture.isNull():
                buffer = QBuffer()
                buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                if picture.save(buffer, "PNG"):
                    self.image_bytes.emit(bytes(buffer.data()))
        else:
            super().insertFromMimeData(source)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.focus_changed.emit(False)

    def inputMethodEvent(self, event):
        self.preedit = bool(event.preeditString())
        super().inputMethodEvent(event)

    def keyPressEvent(self, event):
        enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if (event.key() == Qt.Key.Key_Escape and modifiers == Qt.KeyboardModifier.NoModifier
                and self.can_interrupt and not self.preedit):
            if not event.isAutoRepeat():
                self.interrupted.emit()
            event.accept()
            return
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


class FitCombo(QComboBox):
    """宽度跟随当前选项，避免短文字后面留出大段空白。"""

    def __init__(self):
        super().__init__()
        self.currentIndexChanged.connect(self.updateGeometry)

    def sizeHint(self):
        self.ensurePolished()
        text = self.fontMetrics().horizontalAdvance(self.currentText())
        return QSize(text + 30, max(26, self.fontMetrics().height() + 10))

    def minimumSizeHint(self):
        return self.sizeHint()

    def showPopup(self):
        view = self.view()
        view.ensurePolished()
        view.setTextElideMode(Qt.TextElideMode.ElideNone)
        margins = view.contentsMargins()
        view.setMinimumWidth(view.sizeHintForColumn(self.modelColumn()) + margins.left() + margins.right())
        super().showPopup()

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event):
        # 和“默认模式 ▾”这类文字按钮保持一致：文字后紧跟箭头，悬停时出现浅色底。
        palette = QApplication.palette()
        hover = self.underMouse() and self.isEnabled()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(palette.color(palette.ColorRole.Midlight))
            painter.drawRoundedRect(self.rect().adjusted(0, 1, 0, -1), 7, 7)
        color = palette.color(palette.ColorRole.Text if hover else palette.ColorRole.PlaceholderText)
        painter.setPen(color)
        painter.setFont(self.font())
        rect = self.rect().adjusted(8, 0, -8, 0)
        painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.currentText())
        x = rect.left() + self.fontMetrics().horizontalAdvance(self.currentText()) + 5
        y = self.height() / 2
        pen = painter.pen()
        pen.setWidthF(1.3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawPolyline([QPointF(x, y - 1.5), QPointF(x + 3.5, y + 2), QPointF(x + 7, y - 1.5)])
        painter.end()


class ModelPicker(QWidget):
    changed = Signal(str, str, str)
    refresh_requested = Signal(str)
    usage_requested = Signal(str)

    def __init__(self, provider, title, daybreak_action=None):
        super().__init__()
        self.provider = provider
        self.title = title
        self.model = ""
        self.daybreak_action = daybreak_action
        self.effort = ""
        self.catalog = {}
        self.dialog = None
        self.menu = None
        self.loading = False
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        # 服务商名称放在提示里，界面上只保留图标、模型和思考强度。
        self.caption = label(title, "caption")
        self.caption.hide()
        self.control = QToolButton()
        self.control.setObjectName("model")
        self.control.setAccessibleName(title + " 模型")
        if provider != "openai":
            self.control.setIcon(icon(provider))
        self.control.setIconSize(QSize(14, 14))
        self.control.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.control.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.control.setCursor(Qt.CursorShape.PointingHandCursor)
        self.control.clicked.connect(self.open_models)
        row.addWidget(self.control)
        self.effort_control = QToolButton()
        self.effort_control.setObjectName("effort")
        self.effort_control.setCursor(Qt.CursorShape.PointingHandCursor)
        self.effort_control.clicked.connect(self.open_effort)
        row.addWidget(self.effort_control)

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
        self.control.setText((group["name"] if group else model or "选择模型") + " ▾")
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

    def accepts(self, group):
        if self.provider != "openai" or self.catalog.get("source") != "client":
            return True
        return model_available(group, bool(self.daybreak_action and self.daybreak_action.isChecked()))

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
            name = LEVEL_NAMES.get(value, value) if value else "默认"
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
        if group and self.accepts(group):
            menu = self.new_menu(self.title + " 思考强度")
            self.level_actions(menu, group)
            self.popup(menu, self.effort_control)

    def refresh_open_models(self):
        menu = self.menu
        if menu and menu.objectName() == "modelList" and QApplication.activePopupWidget() is menu:
            menu.close()
            self.open_models()

    def open_models(self):
        client = self.catalog["source"] == "client"
        if not client and self.provider == "openai":
            self.open_dialog()
            return
        menu = self.new_menu(self.title + " 模型")
        menu.setObjectName("modelList")
        daybreak = self.daybreak_action
        if client and daybreak and daybreak.isVisible():
            switch = menu.addAction(daybreak.icon(), daybreak.text())
            switch.setCheckable(True)
            switch.setChecked(daybreak.isChecked())
            switch.setEnabled(daybreak.isEnabled())

            def toggle_daybreak():
                daybreak.trigger()
                QTimer.singleShot(0, self.control, self.open_models)

            switch.triggered.connect(toggle_daybreak)
            menu.addSeparator()
        selected = group_for(self.catalog["groups"], self.model)
        for group in self.catalog["groups"]:
            levels = group["choices"]
            if not client and not levels and group is not selected:
                continue
            level = self.selected_level(group) if group is selected else group["default"]
            text = group["name"]
            if level:
                text += "    " + LEVEL_NAMES.get(level, level)
            if not self.accepts(group):
                action = menu.addAction(text)
                action.setEnabled(False)
            elif len(levels) > 1:
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
            menu.addAction(self.catalog["error"] or "正在读取…").setEnabled(False)
        menu.addSeparator()
        if not client:
            menu.addAction("搜索全部模型…", self.open_dialog)
        refresh = menu.addAction("刷新列表", lambda: self.refresh_requested.emit(self.provider))
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
        self.reload = button("刷新", lambda: self.refresh_requested.emit(self.provider))
        actions.addWidget(self.reload)
        self.manual = button("自定义 ID…", self.custom)
        actions.addWidget(self.manual)
        actions.addWidget(button("查看套餐用量", lambda: self.usage_requested.emit(self.provider)))
        actions.addStretch()
        actions.addWidget(button("取消", dialog.reject))
        self.apply = button("确定", self.choose, name="primary")
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
        local = data["source"] == "client"
        self.manual.setVisible(not local)
        if self.loading:
            info = "正在读取模型…"
        elif data["error"]:
            info = data["error"]
        elif not local and not data["configured"]:
            info = f"常用模型（共 {len(data['models'])} 个，配置密钥后可拉取账户列表）"
        else:
            info = f"共 {len(data['models'])} 个模型"
        self.origin.setText(info)
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
                text += "  · 当前"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, value)
            if not self.accepts(group_for(data["groups"], value)):
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.options.addItem(item)
            if value == selected:
                self.options.setCurrentItem(item)
        if self.options.count() and not self.options.currentItem():
            self.options.setCurrentRow(0)
        self.apply.setEnabled(bool(self.options.currentItem()))

    def choose(self):
        item = self.options.currentItem()
        if item and item.flags() & Qt.ItemFlag.ItemIsEnabled:
            model = item.data(Qt.ItemDataRole.UserRole)
            self.changed.emit(self.provider, model, self.effort if model == self.model else "")
            self.dialog.accept()

    def custom(self):
        dialog = QInputDialog(self.dialog or self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(self.title + " 模型")
        dialog.setLabelText("模型 ID")
        dialog.setTextValue(self.model)
        dialog.setOkButtonText("确定")
        dialog.setCancelButtonText("取消")

        def choose(value):
            value = value.strip()
            if not re.fullmatch(r"[a-zA-Z0-9._:/-]{1,150}", value):
                QMessageBox.warning(self, "模型 ID 无效", "模型 ID 格式无效。")
                return
            self.changed.emit(self.provider, value, self.effort if value == self.model else "")
            if self.dialog:
                self.dialog.accept()

        dialog.textValueSelected.connect(choose)
        dialog.open()


class Document(QTextBrowser):
    def __init__(self, workspace=None):
        super().__init__()
        self.workspace = workspace
        self.hovered_link = None
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.viewport().setMouseTracking(True)
        self.anchorClicked.connect(self.follow_link)
        self.document().setDocumentMargin(4)

    def loadResource(self, kind, url):
        return None

    def show_markdown(self, text):
        self.hovered_link = None
        self.setExtraSelections([])
        render_markdown(self.document(), text, self.font(), self.palette(), self.workspace)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        point = event.position().toPoint()
        href = self.anchorAt(point)
        block = self.cursorForPosition(point).block()
        key = (block.position(), href) if href else None
        if self.hovered_link == key:
            return
        self.hovered_link = key
        self.setToolTip(href)
        ranges = []
        if href:
            frag = block.begin()
            while not frag.atEnd():
                span = frag.fragment()
                if span.charFormat().anchorHref() == href:
                    hit = QTextEdit.ExtraSelection()
                    hit.cursor = QTextCursor(self.document())
                    hit.cursor.setPosition(span.position())
                    hit.cursor.setPosition(span.position() + span.length(), QTextCursor.MoveMode.KeepAnchor)
                    accent = self.palette().link().color()
                    hit.format.setForeground(accent)
                    accent.setAlpha(36)
                    hit.format.setBackground(accent)
                    ranges.append(hit)
                frag += 1
        self.setExtraSelections(ranges)

    def leaveEvent(self, event):
        self.hovered_link = None
        self.setExtraSelections([])
        self.setToolTip("")
        super().leaveEvent(event)

    def follow_link(self, url):
        if url.scheme() in ("https", "http", "mailto"):
            QDesktopServices.openUrl(url)
        elif url.isLocalFile() and not url.host():
            file = Path(url.toLocalFile())
            try:
                if not file.is_file():
                    raise ValueError("文件不存在：" + str(file))
                with file.open("rb") as stream:
                    data = stream.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024 or b"\0" in data:
                    raise ValueError("此文件无法作为文本预览：" + str(file))
                text = data.decode("utf-8-sig")
            except (OSError, ValueError) as exc:
                QMessageBox.information(self, "打开文件", str(exc))
                return
            dialog = QDialog(self)
            dialog.setWindowTitle(file.name)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.resize(780, 540)
            view = QPlainTextEdit()
            view.setReadOnly(True)
            view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
            view.setPlainText(text)
            layout = QVBoxLayout(dialog)
            layout.addWidget(view)
            dialog.open()
            location = HASH_LINE.fullmatch(url.fragment())
            if location:
                block = view.document().findBlockByNumber(min(int(location[1]), view.blockCount()) - 1)
                cursor = QTextCursor(block)
                cursor.setPosition(block.position() + min(int(location[2] or 1) - 1, block.length() - 1))
                view.setTextCursor(cursor)
                view.centerCursor()
        elif not url.scheme() and url.fragment():
            self.scrollToAnchor(url.fragment())


class MessageRow(QWidget):
    def __init__(self, event, input_library=None, retry=None, workspace=None, expanded_jobs=None):
        super().__init__()
        self.user = event["actor"] == "user"
        self.text = event["body"]
        self.result_toggle = None
        self.trace = None
        self.intermediate = None
        self.rendered = None
        self.available = None
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 5, 2, 5)
        row.setSpacing(0)
        self.block = QFrame()
        self.block.setObjectName("userBubble" if self.user else "reply")
        box = QVBoxLayout(self.block)
        box.setSpacing(5)
        margins = (14, 9, 14, 9) if self.user else (0, 2, 0, 4)
        box.setContentsMargins(*margins)
        author = ACTORS.get(event["actor"], event["actor"])
        if self.user:
            row.addStretch()
            if event.get("meta", {}).get("guide"):
                box.addWidget(label("任务引导", "messageAuthor"))
        else:
            title = {"requirements": "Gemini · 需求整理", "verification": "Gemini 请求 GPT 验证",
                     "failure": author + " · 失败"}.get(event["kind"], author)
            meta = event.get("meta", {})
            model = meta.get("model")
            scope = meta.get("kind") or meta.get("report_input")
            if model:
                title += " · " + model
            self.author_label = label(title, "messageAuthor")
            box.addWidget(self.author_label)
            if (expanded_jobs is not None and event["actor"] == "openai" and event["kind"] == "message"
                    and scope in ("engineering", "verification") and not meta.get("needs_input")):
                self.expanded_jobs = expanded_jobs
                self.job = meta.get("job") or event["seq"]
                self.result_toggle = disclosure("GPT 中间输出 · " + LABELS[scope])
                self.result_toggle.setToolTip(title)
                self.result_toggle.setChecked(self.job in expanded_jobs)
                self.result_toggle.toggled.connect(self.toggle_result)
                box.addWidget(self.result_toggle, 0, ALIGN.AlignLeft)
        if event.get("notice"):
            notice = label(event["notice"])
            notice.setWordWrap(True)
            box.addWidget(notice)
        if event.get("trace"):
            self.trace = trace = QFrame()
            trace.setObjectName("trace")
            details = QVBoxLayout(trace)
            details.setSpacing(3)
            details.setContentsMargins(12, 8, 12, 9)
            for i, (title, value) in enumerate(event["trace"]):
                if i:
                    details.addSpacing(5)
                details.addWidget(label(title, "messageAuthor"))
                line = label(value)
                line.setWordWrap(True)
                line.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                line.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                details.addWidget(line)
            box.addWidget(trace)
        self.body = Document(workspace)
        self.body.setObjectName("messageText")
        self.body.setReadOnly(True)
        self.body.setFrameShape(QFrame.Shape.NoFrame)
        self.body.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.body.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.body.document().setDocumentMargin(0)
        self.body.document().setIndentWidth(18)
        # 先应用样式表字体，再测量宽高，避免首次显示时被截断。
        self.body.ensurePolished()
        self.body.document().setDefaultFont(self.body.font())
        if self.user or event["kind"] != "message":
            self.body.setPlainText(self.text)
            if self.user:
                link_plain_text(self.body.document(), self.body.palette())
        elif self.result_toggle is None:
            self.body.show_markdown(self.text)
        self.body.setAccessibleName(author + "的消息")
        self.body.setAccessibleDescription(self.body.toPlainText())
        if event.get("at"):
            stamp = datetime.fromisoformat(event["at"]).astimezone().strftime("%Y-%m-%d %H:%M")
            self.block.setToolTip(author + " · " + stamp)
        if event.get("notice"):
            control = disclosure("原始错误")
            control.toggled.connect(self.body.setVisible)
            box.addWidget(control, 0, ALIGN.AlignLeft)
            self.body.hide()
        elif event["kind"] == "requirements" and event.get("trace"):
            self.body.hide()
        box.addWidget(self.body)
        self.retry_actions = None
        jid = event.get("meta", {}).get("job")
        if retry and jid and event["kind"] == "failure":
            self.retry_actions = QWidget()
            actions = QHBoxLayout(self.retry_actions)
            actions.setContentsMargins(0, 0, 0, 0)
            if event["meta"].get("kind") in ("engineering", "verification"):
                actions.addWidget(button("只读重试 GPT", lambda: retry(jid, True), name="quiet"))
            actions.addWidget(button("重试 " + author, lambda: retry(jid, False), name="quiet"))
            actions.addStretch()
            self.retry_actions.setVisible(bool(event.get("retryable")))
            box.addWidget(self.retry_actions)
        inputs = event.get("attachments", [])
        if inputs and input_library:
            from .desktop_inputs import attachment_button
            for item in inputs:
                control = attachment_button(self, input_library, item)
                box.addWidget(control, 0, ALIGN.AlignLeft)
            if not self.text:
                self.body.hide()
        row.addWidget(self.block)
        self.natural = 0
        if self.user:
            metrics = QFontMetrics(self.body.font())
            self.natural = max((metrics.horizontalAdvance(line[:1024]) for line in self.text.splitlines()), default=0)
        if inputs:
            self.natural = max(self.natural, 150)
            if not self.text:
                self.text = "、".join(item["name"] for item in inputs)
        if self.result_toggle is not None:
            self.set_intermediate(bool(event.get("intermediate")))

    def set_intermediate(self, intermediate):
        if self.intermediate == intermediate:
            return
        self.intermediate = intermediate
        self.author_label.setVisible(not intermediate)
        self.result_toggle.setVisible(intermediate)
        self.show_result()

    def toggle_result(self, opened):
        if opened:
            self.expanded_jobs.add(self.job)
        else:
            self.expanded_jobs.discard(self.job)
        self.show_result()

    def show_result(self):
        visible = not self.intermediate or self.result_toggle.isChecked()
        self.body.setVisible(visible)
        if self.trace is not None:
            self.trace.setVisible(visible)
        if visible and self.rendered != self.text:
            self.body.show_markdown(self.text)
            self.body.setAccessibleDescription(self.body.toPlainText())
            self.rendered = self.text
            if self.available is not None:
                self.fit(self.available)

    def fit(self, available):
        self.available = available
        width = max(50, available - 6)
        if self.user:
            width = max(40, min(int(width * 0.78), self.natural + 34))
        padding = 28 if self.user else 0
        self.block.setFixedWidth(width)
        self.body.setFixedWidth(width - padding)
        self.body.document().setTextWidth(width - padding)
        size = self.body.document().size()
        bar = self.body.horizontalScrollBar().sizeHint().height() if size.width() > width - padding else 0
        self.body.setFixedHeight(max(self.body.fontMetrics().height(), ceil(size.height())) + bar + 4)


class Timeline(QScrollArea):
    navigation_changed = Signal()

    def __init__(self, input_library=None, retry=None):
        super().__init__()
        self.input_library = input_library
        self.workspace = None
        self.retry = retry
        self.event_rows = {}
        self.draft_rows = {}
        self.expanded_jobs = set()
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
        self.scroller = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self.scroller.setDuration(260)
        self.scroller.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.verticalScrollBar().valueChanged.connect(self.navigation_changed)
        self.verticalScrollBar().rangeChanged.connect(self.navigation_changed)

    def reset(self):
        while self.column.count() > 1:
            item = self.column.takeAt(0)
            item.widget().hide()
            item.widget().deleteLater()
        self.rows.clear()
        self.event_rows.clear()
        self.draft_rows.clear()
        self.records.clear()
        self.group_button = self.group_body = None
        self.seen = 0
        self.navigation_changed.emit()

    def toPlainText(self):
        return "\n\n".join(self.records)

    def user_rows(self):
        return [row for row in self.rows if row.user]

    def current_index(self):
        rows = self.user_rows()
        bar = self.verticalScrollBar()
        if rows and bar.value() >= bar.maximum() - 2:
            return len(rows) - 1
        line = bar.value() + 48
        return max((i for i, row in enumerate(rows) if row.y() <= line), default=0)

    def jump(self, index):
        rows = self.user_rows()
        if 0 <= index < len(rows):
            bar = self.verticalScrollBar()
            self.scroller.stop()
            self.scroller.setStartValue(bar.value())
            self.scroller.setEndValue(min(bar.maximum(), max(0, rows[index].y() - 12)))
            self.scroller.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for row in (*self.rows, *self.draft_rows.values()):
            row.fit(self.viewport().width() - 4)
        self.navigation_changed.emit()

    def progress(self, text, title=None):
        if self.group_button is None:
            group = QFrame()
            box = QVBoxLayout(group)
            box.setContentsMargins(1, 0, 1, 0)
            box.setSpacing(4)
            control = disclosure("")
            details = label("", "caption")
            details.setWordWrap(True)
            details.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            details.hide()
            control.toggled.connect(details.setVisible)
            box.addWidget(control, 0, ALIGN.AlignLeft)
            box.addWidget(details)
            self.group_button, self.group_body = control, details
            self.column.insertWidget(self.column.count() - 1 - len(self.draft_rows), group)
        prior = self.group_body.text()
        self.group_body.setText(prior + "\n" + text if prior else text)
        self.group_button.setText(title or "过程")

    def update_events(self, events, colors):
        bar = self.verticalScrollBar()
        follow = bar.maximum() - bar.value() < 30
        changed = False
        for event in events:
            if event["seq"] <= self.seen:
                row = self.event_rows.get(event["seq"])
                if row is not None and row.retry_actions is not None:
                    row.retry_actions.setVisible(bool(event.get("retryable")))
                if row is not None and row.result_toggle is not None:
                    row.set_intermediate(bool(event.get("intermediate")))
                continue
            self.seen = event["seq"]
            kind = event["kind"]
            if kind not in ("message", "verification", "failure", "progress", "requirements"):
                continue
            names = [TYPES[i["kind"]] + " · " + i["name"] for i in event.get("attachments", [])]
            trace = [" · ".join(part) for part in event.get("trace", ())]
            self.records.append("\n".join(s for s in [event.get("notice"), event["body"], *trace, *names] if s))
            changed = True
            if kind == "progress":
                self.progress(event["body"], event.get("approval_label"))
                continue
            self.group_button = self.group_body = None
            row = MessageRow(event, self.input_library, self.retry, self.workspace, self.expanded_jobs)
            self.event_rows[event["seq"]] = row
            row.fit(self.viewport().width() - 4)
            self.rows.append(row)
            self.column.insertWidget(self.column.count() - 1 - len(self.draft_rows), row)
        if changed and follow:
            self.column.activate()
            self.canvas.adjustSize()
            bar.setValue(bar.maximum())
        if changed:
            self.navigation_changed.emit()

    def update_drafts(self, drafts):
        bar = self.verticalScrollBar()
        follow = bar.maximum() - bar.value() < 30
        changed = False
        for jid in self.draft_rows.keys() - drafts.keys():
            row = self.draft_rows.pop(jid)
            self.column.removeWidget(row)
            row.hide()
            row.deleteLater()
            changed = True
        for jid, (task, text) in drafts.items():
            row = self.draft_rows.get(jid)
            if row is None:
                event = {"actor": "openai", "kind": "message", "body": text, "intermediate": True,
                         "meta": {"job": jid, "kind": task["kind"], "model": task["model"]}}
                row = MessageRow(event, workspace=self.workspace, expanded_jobs=self.expanded_jobs)
                self.draft_rows[jid] = row
                row.fit(self.viewport().width() - 4)
                self.column.insertWidget(self.column.count() - 1, row)
                changed = True
            elif row.text != text:
                row.text = text
                row.show_result()
                changed |= row.result_toggle.isChecked()
        if changed and follow:
            self.column.activate()
            self.canvas.adjustSize()
            bar.setValue(bar.maximum())


class MessageNav(QWidget):
    """会话右侧的快速定位条：每个圆点对应一条你发送的消息，点击即可跳转。"""
    STEP = 14

    def __init__(self, timeline, parent):
        super().__init__(parent)
        self.timeline = timeline
        self.hovered = -1
        self.setAccessibleName("消息快速定位")
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        timeline.navigation_changed.connect(self.refresh)
        self.hide()

    def refresh(self):
        count = len(self.timeline.user_rows())
        self.setFixedSize(22, count * self.STEP + 10)
        self.setVisible(count >= 2 and self.timeline.isVisible())
        self.reposition()
        self.update()

    def reposition(self):
        host = self.parentWidget()
        if host:
            self.move(host.width() - self.width() - 8, max(48, (host.height() - self.height()) // 2))
            self.raise_()

    def index_at(self, y):
        index = int((y - 5) // self.STEP)
        return index if 0 <= index < len(self.timeline.user_rows()) else -1

    def paintEvent(self, event):
        rows = self.timeline.user_rows()
        current = self.timeline.current_index()
        palette = QApplication.palette()
        ink, muted = palette.color(palette.ColorRole.Text), palette.color(palette.ColorRole.PlaceholderText)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(len(rows)):
            color = QColor(ink if i in (current, self.hovered) else muted)
            if i not in (current, self.hovered):
                color.setAlphaF(0.55)
            radius = 3.6 if i == current else (3.2 if i == self.hovered else 2.6)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(self.width() / 2, 5 + self.STEP * i + self.STEP / 2), radius, radius)
        painter.end()

    def mouseMoveEvent(self, event):
        index = self.index_at(event.position().y())
        if index != self.hovered:
            self.hovered = index
            self.update()
            if index >= 0:
                text = " ".join(self.timeline.user_rows()[index].text.split())
                QToolTip.showText(event.globalPosition().toPoint(), text[:80] + ("…" if len(text) > 80 else ""), self)
            else:
                QToolTip.hideText()

    def leaveEvent(self, event):
        self.hovered = -1
        self.update()

    def mousePressEvent(self, event):
        index = self.index_at(event.position().y())
        if index >= 0:
            self.timeline.jump(index)


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
        self.setWindowTitle("设置")
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
        layout.addSpacing(8)
        path = label("配置文件：" + str(engine.project / ".env"), "caption")
        path.setWordWrap(True)
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(path)
        def agent_settings():
            from .desktop_agent import AgentDialog
            AgentDialog(self, engine, parent.sid, parent.pid).open()
        layout.addWidget(button("技能与上下文…", agent_settings), 0, ALIGN.AlignLeft)
        actions = QHBoxLayout()
        actions.addWidget(button("重新加载", self.reload))
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
    def __init__(self, parent, asset, workspace=None):
        super().__init__(parent)
        self.asset = asset
        self.setWindowTitle(asset["name"])
        self.resize(700, 530)
        layout = QVBoxLayout(self)
        text = Document(workspace)
        if asset["name"].lower().endswith(".md"):
            text.show_markdown(asset["content"])
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
