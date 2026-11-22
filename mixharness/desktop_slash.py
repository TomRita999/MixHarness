from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QPalette, QTextCursor
from PySide6.QtWidgets import QAbstractItemView, QLabel, QListWidget, QListWidgetItem, QStyle, QStyledItemDelegate

from .slash_catalog import COMMANDS, matches


ROLE = Qt.ItemDataRole.UserRole
NEEDS_CHAT = {"btw", "share", "fork", "compact", "archive", "implement", "tools", "status", "rename",
              "export", "approvals", "approve", "deny", "stop", "retry"}


class CommandRow(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(0, max(32, option.fontMetrics.height() + 14))

    def paint(self, painter, option, index):
        name = index.data(ROLE)
        if not name:
            return
        command = COMMANDS[name]
        rect = option.rect.adjusted(3, 1, -3, -1)
        palette = option.palette
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(palette.highlight())
            painter.drawRoundedRect(rect, 6, 6)
        align = Qt.AlignmentFlag.AlignVCenter
        bounds = rect.adjusted(10, 0, -10, 0)
        token = "/" + name
        regular = option.font
        metrics = option.fontMetrics
        token_width = metrics.horizontalAdvance(token)
        token_left = bounds.right() - token_width
        painter.setFont(regular)
        painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
        painter.drawText(bounds, align | Qt.AlignmentFlag.AlignRight, token)
        title_font = painter.font()
        title_font.setBold(True)
        painter.setFont(title_font)
        title_width = min(painter.fontMetrics().horizontalAdvance(command.title), max(0, token_left - bounds.left() - 12))
        title = QRect(bounds.x(), bounds.y(), title_width + 1, bounds.height())
        enabled = bool(index.flags() & Qt.ItemFlag.ItemIsEnabled)
        painter.setPen(palette.text().color() if enabled else palette.color(QPalette.ColorRole.PlaceholderText))
        painter.drawText(title, align, painter.fontMetrics().elidedText(command.title, Qt.TextElideMode.ElideRight, title.width()))
        detail = title.right() + 12
        painter.setFont(regular)
        painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
        width = token_left - detail - 12
        if width > metrics.height() * 3:
            painter.drawText(QRect(detail, bounds.y(), width, bounds.height()), align,
                             metrics.elidedText(command.description if enabled else "先打开一段会话", Qt.TextElideMode.ElideRight, width))
        painter.restore()


class SlashMenu(QListWidget):
    chosen = Signal(str)

    def __init__(self, host):
        super().__init__(host.content)
        self.host = host
        self.editor = host.editor
        self.prefix = None
        self.names = []
        self.available = []
        self.setObjectName("slashMenu")
        self.setAccessibleName("快捷指令")
        self.hint = QLabel("↑↓ 选择     Enter 执行     Tab 填参数     Esc 收起", self)
        self.hint.setObjectName("caption")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setViewportMargins(0, 0, 0, 24)
        self.setItemDelegate(CommandRow(self))
        self.setUniformItemSizes(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.itemClicked.connect(self.choose)
        self.itemEntered.connect(lambda item: self.setCurrentItem(item) if item.flags() & Qt.ItemFlag.ItemIsEnabled else None)
        self.editor.textChanged.connect(self.refresh_matches)
        self.editor.cursorPositionChanged.connect(self.refresh_matches)
        self.editor.focus_changed.connect(self.focus_changed)
        host.inputs.changed.connect(self.refresh_matches)
        for widget in (self.editor, host.composer, host.composer.parentWidget(), host.content):
            widget.installEventFilter(self)
        self.hide()

    def focus_changed(self, focused):
        if focused:
            self.refresh_matches()
        else:
            self.hide()

    def refresh_matches(self):
        text = self.editor.toPlainText()
        cursor = self.editor.textCursor()
        if (not text.startswith(("/", "／")) or self.host.inputs.items or cursor.hasSelection() or not cursor.atEnd()
                or any(c.isspace() or c in "/\\" for c in text[1:]) or not self.editor.hasFocus()):
            self.prefix = None
            self.hide()
            return
        show_daybreak = not self.host.daybreak_button.isHidden()
        key = (text, self.host.sid, show_daybreak)
        if key != self.prefix:
            self.prefix = key
            self.names = [name for name in matches(text[1:]) if name != "exit" and (name != "daybreak" or show_daybreak)]
            self.available = []
            self.selectionModel().clear()
            for row, name in enumerate(self.names):
                command = COMMANDS[name]
                item = self.item(row) or QListWidgetItem(self)
                enabled = bool(self.host.sid) or name not in NEEDS_CHAT
                if enabled:
                    self.available.append(row)
                item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled if enabled else Qt.ItemFlag.NoItemFlags)
                item.setText(f"{command.title}　/{name}　{command.description if enabled else '先打开一段会话'}")
                item.setData(ROLE, name)
                item.setToolTip("/" + name + " " + command.usage())
                item.setHidden(False)
            for row in range(len(self.names), self.count()):
                self.item(row).setHidden(True)
            self.doItemsLayout()
            self.setCurrentRow(self.available[0] if self.available else -1)
            self.scrollToTop()
        self.setVisible(bool(self.names))
        if self.isVisible():
            self.reposition()

    def reposition(self):
        anchor = self.host.composer
        parent = self.parentWidget()
        point = anchor.mapTo(parent, QPoint(0, 0))
        above = point.y() - 8
        below_y = point.y() + anchor.height() + 4
        below = parent.height() - below_y - 4
        on_top = above >= 120 or above >= below
        height = min(max(0, above if on_top else below), self.sizeHintForRow(0) * min(11, len(self.names)) + 36)
        self.setGeometry(point.x(), point.y() - height - 4 if on_top else below_y, anchor.width(), height)
        self.hint.setGeometry(14, height - 27, max(0, self.width() - 28), 23)
        self.raise_()

    def complete(self, name):
        cursor = self.editor.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText("/" + name + " ")
        self.editor.setTextCursor(cursor)

    def choose(self, item):
        if item and item.flags() & Qt.ItemFlag.ItemIsEnabled:
            name = item.data(ROLE)
            self.hide()
            if name in ("approve", "deny", "retry"):
                self.complete(name)
            else:
                self.chosen.emit(name)

    def eventFilter(self, watched, event):
        if self.isVisible():
            if watched is self.editor and event.type() == QEvent.Type.KeyPress and not self.editor.preedit:
                mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
                if mods == Qt.KeyboardModifier.NoModifier:
                    key = event.key()
                    if key == Qt.Key.Key_Escape:
                        self.hide()
                    elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                        step = 1 if key == Qt.Key.Key_Down else -1
                        if self.available:
                            index = self.available.index(self.currentRow()) if self.currentRow() in self.available else 0
                            self.setCurrentRow(self.available[(index + step) % len(self.available)])
                    elif key == Qt.Key.Key_Tab:
                        if self.currentItem():
                            self.complete(self.currentItem().data(ROLE))
                    elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                        if not event.isAutoRepeat():
                            self.choose(self.currentItem())
                    else:
                        return super().eventFilter(watched, event)
                    event.accept()
                    return True
            elif event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
                self.reposition()
        return super().eventFilter(watched, event)
