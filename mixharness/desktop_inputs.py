import frontmatter
from PySide6.QtCore import QEvent, QPoint, QRectF, Qt, QTimer, QSize, Signal
from PySide6.QtGui import QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from . import clients
from .agent_settings import skill_catalog
from .attachments import Attachment, MAX_ITEMS, TYPES
from .desktop_clients import ClientHistory
from .desktop_widgets import Document, button, label
from .providers import FUNCTIONS


ROLE = Qt.ItemDataRole.UserRole


class ImagePreview(QPushButton):
    def __init__(self, parent, picture):
        super().__init__(parent)
        self.setFixedSize(120, 120)
        self.setObjectName("imagePreview")
        self.picture = picture
        self.remove_button = None

    def add_remove_button(self, control):
        self.remove_button = control
        control.setParent(self)
        control.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        control.move(self.width() - control.width() - 4, 4)
        control.hide()

    def event(self, event):
        result = super().event(event)
        if (getattr(self, "remove_button", None) and event.type() in
                (QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.FocusIn, QEvent.Type.FocusOut)):
            self.remove_button.setVisible(self.underMouse() or self.hasFocus())
        return result

    def keyPressEvent(self, event):
        if self.remove_button and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.remove_button.click()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        bounds = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        shape = QPainterPath()
        shape.addRoundedRect(bounds, 9, 9)
        painter.setClipPath(shape)
        picture = self.picture.rect()
        side = min(picture.width(), picture.height())
        source = QRectF((picture.width() - side) / 2, (picture.height() - side) / 2, side, side)
        painter.drawPixmap(bounds, self.picture, source)
        painter.setPen(QPen(self.palette().text() if self.hasFocus() else self.palette().mid(), 2 if self.hasFocus() else 1))
        painter.drawPath(shape)


def attachment_button(parent, library, item, *, large=False):
    caption = TYPES[item["kind"]] + " · " + item["name"]
    if item["kind"] == "image":
        thumb = QPixmap()
        thumb.loadFromData(library.data(item["id"]))
    if large and item["kind"] == "image":
        control = ImagePreview(parent, thumb)
    else:
        control = QPushButton(parent)
        control.setObjectName("quiet")
        name = control.fontMetrics().elidedText(item["name"], Qt.TextElideMode.ElideMiddle, 130)
        if item["kind"] == "image":
            control.setIcon(QIcon(thumb.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)))
            control.setIconSize(QSize(30, 30))
            control.setText(name)
        else:
            control.setText(TYPES[item["kind"]] + " · " + name)
    control.setCursor(Qt.CursorShape.PointingHandCursor)
    control.setAccessibleName(caption)
    control.setToolTip(caption + ("\n" + item["source"] if item["source"] else ""))

    def preview():
        dialog = QDialog(parent)
        dialog.setWindowTitle(caption)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.resize(650, 450)
        layout = QVBoxLayout(dialog)
        data = library.data(item["id"])
        if item["kind"] == "image":
            scroll = QScrollArea()
            picture = QLabel()
            pixmap = QPixmap()
            pixmap.loadFromData(data)
            picture.setPixmap(pixmap)
            scroll.setWidget(picture)
            scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(scroll)
        else:
            document = Document()
            document.setPlainText(data.decode())
            layout.addWidget(document)
        layout.addWidget(button("关闭", dialog.accept), 0, Qt.AlignmentFlag.AlignRight)
        dialog.open()
    control.clicked.connect(preview)
    return control


class ComposerInputs(QScrollArea):
    changed = Signal()

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.library = window.engine.store.inputs
        self.items = []
        self.dialog = None
        self.setObjectName("composerInputs")
        self.setAccessibleName("待发送的附件")
        self.viewport().setAutoFillBackground(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setFixedHeight(48)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.hide()

    def restore(self):
        self.items = self.library.metadata(self.window.draft_key())
        row = QWidget()
        row.setObjectName("inputStrip")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        for item in self.items:
            preview = attachment_button(row, self.library, item, large=True)
            layout.addWidget(preview)
            remove = button("移除 " + item["name"], lambda _, key=item["id"]: self.remove(key), "close", "removeInput", True)
            remove.setFixedSize(20, 20)
            remove.setIconSize(QSize(12, 12))
            if isinstance(preview, ImagePreview):
                preview.add_remove_button(remove)
            else:
                layout.addWidget(remove)
        layout.addStretch()
        self.setFixedHeight(126 if any(item["kind"] == "image" for item in self.items) else 40)
        self.setWidget(row)
        row.setAutoFillBackground(False)
        self.setVisible(bool(self.items))
        self.changed.emit()

    def remove(self, identifier):
        self.library.unstage(self.window.draft_key(), identifier)
        self.restore()

    def stage(self, items, owner=None):
        owner = owner or self.window.draft_key()
        self.library.stage(owner, items)
        if owner == self.window.draft_key():
            self.restore()

    def add_files(self, paths):
        try:
            if len(paths) > MAX_ITEMS:
                raise ValueError(f"每次最多添加 {MAX_ITEMS} 项")
            self.stage([Attachment.from_file(p) for p in paths])
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self.window, "无法添加图片", str(exc))

    def paste_image(self, data):
        try:
            self.stage([Attachment.image("剪贴板图片.png", data)])
        except ValueError as exc:
            QMessageBox.warning(self.window, "无法添加图片", str(exc))

    def menu(self):
        menu = QMenu(self.window)
        menu.setAccessibleName("添加附件或引用")
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.addAction("添加图片…", self.choose_images)
        menu.addSeparator()
        menu.addAction("引用会话…", self.choose_chat)
        menu.addAction("引用工具…", lambda: self.choose_reference("tool"))
        menu.addAction("使用技能…", lambda: self.choose_reference("skill"))
        control = self.window.add_input
        menu.popup(control.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 3)))

    def choose_images(self):
        dialog = QFileDialog(self.window, "添加图片")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
        dialog.setNameFilter("图片 (*.png *.jpg *.jpeg *.webp *.PNG *.JPG *.JPEG *.WEBP)")
        dialog.filesSelected.connect(self.add_files)
        dialog.open()

    def picker(self, title):
        dialog = QDialog(self.window)
        dialog.setWindowTitle(title)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.resize(550, 460)
        self.dialog = dialog
        dialog.finished.connect(lambda _: setattr(self, "dialog", None))
        return dialog

    def choose_chat(self):
        dialog = self.picker("引用会话")
        layout = QVBoxLayout(dialog)
        head = QHBoxLayout()
        sources = QComboBox()
        for value in ("all", "mixharness", "codex", "antigravity"):
            sources.addItem(clients.NAMES.get(value, "全部来源"), value)
        head.addWidget(sources)
        search = QLineEdit()
        search.setPlaceholderText("搜索会话")
        search.setClearButtonEnabled(True)
        head.addWidget(search, 1)
        layout.addLayout(head)
        browser = ClientHistory(self.window, selecting=True)
        layout.addWidget(browser, 1)
        layout.addWidget(label("引用最近的对话，最多 32 条消息、24,000 字。", "caption"))
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button("取消", dialog.reject))
        add = button("引用", lambda: self.attach_chat(browser.list.currentItem().data(ROLE), dialog), name="primary")
        add.setEnabled(False)
        row.addWidget(add)
        layout.addLayout(row)
        browser.list.currentItemChanged.connect(lambda item, _: add.setEnabled(item is not None))
        browser.list.itemActivated.connect(lambda item: self.attach_chat(item.data(ROLE), dialog))
        timer = QTimer(dialog)
        timer.setSingleShot(True)
        timer.setInterval(220)
        def reload():
            timer.stop()
            browser.query = search.text().strip()
            browser.select_source(sources.currentData())
        timer.timeout.connect(reload)
        search.textChanged.connect(lambda _: timer.start())
        sources.currentIndexChanged.connect(reload)
        dialog.finished.connect(lambda _: browser.select_source(None))
        dialog.open()
        reload()
        search.setFocus()

    def attach_chat(self, row, dialog):
        owner = self.window.draft_key()
        if getattr(dialog, "attach_task", None) and not dialog.attach_task.done():
            return
        dialog.setWindowTitle("正在读取会话…")
        async def attach():
            try:
                item = await clients.chat_reference(self.window.engine.store, row)
                self.stage([item], owner)
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                dialog.setWindowTitle("引用会话")
                QMessageBox.warning(dialog, "无法引用会话", str(exc))
                return
            dialog.attach_task = None
            dialog.accept()
        task = self.window.run_task(attach())
        dialog.attach_task = task
        dialog.finished.connect(lambda _: task.cancel() if dialog.attach_task is task else None)

    def choose_reference(self, kind):
        window = self.window
        owner = window.draft_key()
        skills = kind == "skill"
        dialog = self.picker("使用技能" if skills else "引用工具")
        layout = QVBoxLayout(dialog)
        search = QLineEdit()
        search.setPlaceholderText("搜索技能" if skills else "搜索工具")
        layout.addWidget(search)
        choices = QListWidget()
        layout.addWidget(choices)
        status = label("", "caption")
        status.setWordWrap(True)
        layout.addWidget(status)
        options = []
        def add_option(name, source, target, description):
            item = QListWidgetItem(source + " · " + name)
            item.setData(ROLE, len(options))
            options.append((name, source, target, description))
            item.setToolTip(description)
            choices.addItem(item)
            item.setHidden(search.text().casefold() not in item.text().casefold())
        folders = window.engine.skill_folders(window.sid, window.pid)
        if skills:
            for name, s in sorted(skill_catalog(window.engine.settings_for(folders), folders).items()):
                add_option(name, "技能", s["path"], s["description"])
            if not options:
                status.setText("尚未发现技能，可在「技能与上下文」中添加目录。")
        else:
            for name, (_, description) in FUNCTIONS.items():
                add_option(name, "MixHarness", name, description)
            from .workspace import check_commands
            for folder in folders:
                for name in check_commands(folder / "mixharness.toml", window.engine.commands):
                    add_option(name, folder.name, "run_check", f"使用 run_check 在 {folder} 执行已注册的验证 {name}。")
            if window.backend_box.currentData() == "clients":
                status.setText("正在读取 Codex 的 MCP 工具…")
                async def external():
                    try:
                        tools = await clients.codex_tool_references()
                    except (OSError, RuntimeError, ValueError) as exc:
                        status.setText("Codex MCP · " + (str(exc) or "读取超时"))
                        return
                    for tool in tools:
                        add_option(tool.name, tool.source, tool.target, tool.data.decode())
                    status.clear()
                task = window.run_task(external())
                dialog.finished.connect(lambda _: task.cancel())
        def filter_items(query):
            for i in range(choices.count()):
                item = choices.item(i)
                item.setHidden(query.casefold() not in item.text().casefold())
            if choices.currentItem() and choices.currentItem().isHidden():
                choices.setCurrentRow(-1)
        search.textChanged.connect(filter_items)
        def apply():
            selected = choices.currentItem()
            if not selected:
                return
            name, source, target, description = options[selected.data(ROLE)]
            try:
                content = frontmatter.load(target).content if skills else description
                self.stage([Attachment.reference(kind, name, source, target, content)], owner)
            except (OSError, ValueError) as exc:
                status.setText(str(exc))
                return
            dialog.accept()
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button("取消", dialog.reject))
        accept = button("使用" if skills else "引用", apply, name="primary")
        accept.setEnabled(False)
        choices.currentItemChanged.connect(lambda item, _: accept.setEnabled(item is not None))
        choices.itemActivated.connect(apply)
        row.addWidget(accept)
        layout.addLayout(row)
        dialog.open()
        search.setFocus()
