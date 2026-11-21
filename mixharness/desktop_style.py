from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication


RESOURCES = Path(__file__).with_name("resources")
GLYPHS = {
    "sidebar": 0xE12A, "inspector": 0xE431, "new": 0xE172, "search": 0xE151,
    "folder": 0xE0D7, "settings": 0xE154, "sun": 0xE178, "moon": 0xE11E,
    "send": 0xE04A, "chat": 0xE117, "gemini": 0xE47E, "openai": 0xE040, "close": 0xE1B2,
    "plus": 0xE13D, "more": 0xE0B6, "archive": 0xE041, "pin": 0xE259, "trash": 0xE18E,
}


def load_icons():
    font_id = QFontDatabase.addApplicationFont(str(RESOURCES / "lucide.ttf"))
    if font_id < 0:
        raise RuntimeError("无法加载应用图标字体")
    return QFontDatabase.applicationFontFamilies(font_id)[0]


class Glyph(QIconEngine):
    def __init__(self, family, name, inverse):
        super().__init__()
        self.family, self.name, self.inverse = family, name, inverse

    def clone(self):
        return Glyph(self.family, self.name, self.inverse)

    def paint(self, painter, rect, mode, state):
        role = QPalette.ColorRole.Base if self.inverse else QPalette.ColorRole.Text
        group = QPalette.ColorGroup.Disabled if mode == QIcon.Mode.Disabled else QPalette.ColorGroup.Active
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setPen(QApplication.palette().color(group, role))
        font = QFont(self.family)
        font.setPixelSize(min(rect.width(), rect.height()))
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, chr(GLYPHS[self.name]))
        painter.restore()

    def pixmap(self, size, mode, state):
        result = QPixmap(size)
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        self.paint(painter, result.rect(), mode, state)
        painter.end()
        return result


THEMES = {
    "light": dict(canvas="#ffffff", side="#f7f7f7", hover="#eeeeee", selected="#e7e7e7",
                  line="#dedede", ink="#202020", secondary="#555555", muted="#6b6b6b", inverse="#ffffff"),
    "dark": dict(canvas="#161616", side="#101010", hover="#292929", selected="#343434",
                 line="#383838", ink="#efefef", secondary="#c2c2c2", muted="#a2a2a2", inverse="#161616"),
}


def apply_theme(app, name):
    colors = THEMES[name]
    scheme = Qt.ColorScheme.Dark if name == "dark" else Qt.ColorScheme.Light
    app.styleHints().setColorScheme(scheme)
    palette = QPalette()
    roles = {
        "Window": "canvas", "Base": "canvas", "WindowText": "ink", "Text": "ink", "ButtonText": "ink",
        "Button": "side", "AlternateBase": "side", "Highlight": "selected", "HighlightedText": "ink",
        "Link": "ink", "LinkVisited": "secondary", "PlaceholderText": "muted", "ToolTipBase": "ink",
        "ToolTipText": "inverse", "BrightText": "inverse", "Light": "side", "Midlight": "hover",
        "Mid": "line", "Dark": "muted", "Shadow": "line", "Accent": "ink",
    }
    for role, color in roles.items():
        palette.setColor(QPalette.ColorRole[role], QColor(colors[color]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(colors["muted"]))
    app.setPalette(palette)
    sheet = """
        QWidget { color: $ink; font-size: 14px; }
        QMainWindow, QDialog, QWidget#main, QWidget#content, QTextBrowser, QPlainTextEdit { background: $canvas; }
        QFrame#sidebar { background: $side; border-right: 1px solid $line; }
        QFrame#inspector { background: $side; border-left: 1px solid $line; }
        QFrame#composer { background: $canvas; border: 1px solid $line; border-radius: 18px; }
        QScrollArea#timeline, QWidget#timelineCanvas, QFrame#reply { background: $canvas; border: none; }
        QFrame#userBubble { background: $hover; border-radius: 13px; }
        QTextBrowser#messageText { background: transparent; border: none; padding: 0; font-size: 15px; }
        QLabel#messageAuthor { font-size: 12px; color: $secondary; font-weight: 600; }
        QToolButton#activity { font-size: 12px; color: $muted; padding: 2px 5px; }
        QComboBox { background: $side; border: 1px solid $line; border-radius: 6px; padding: 3px 8px; font-size: 12px; }
        QComboBox:disabled { color: $secondary; }
        QLabel { background: transparent; }
        QLabel#brand { font-size: 16px; font-weight: 650; }
        QLabel#heading { font-size: 29px; font-weight: 600; }
        QLabel#title, QLabel#panelTitle { font-weight: 600; }
        QLabel#caption, QLabel#context, QLabel#counter { color: $muted; font-size: 12px; }
        QLabel#section { color: $secondary; font-size: 12px; font-weight: 600; }
        QPushButton, QToolButton { background: transparent; border: none; border-radius: 7px; padding: 6px; }
        QPushButton:hover, QToolButton:hover { background: $hover; }
        QPushButton:pressed, QToolButton:pressed, QToolButton:checked { background: $selected; }
        QPushButton:focus, QToolButton:focus { outline: none; background: $hover; }
        QPushButton#nav { text-align: left; padding: 7px 9px; }
        QPushButton#quiet { color: $secondary; text-align: left; font-size: 12px; }
        QPushButton#primary { background: $ink; color: $inverse; border-radius: 8px; padding: 8px 18px; }
        QPushButton#primary:disabled { background: $line; color: $muted; }
        QPushButton#rowAction { padding: 2px; border-radius: 4px; }
        QPushButton#send { background: $ink; border-radius: 15px; }
        QPushButton#send:disabled { background: $line; }
        QToolButton#model { text-align: left; padding: 2px 14px 2px 4px; font-size: 12px; }
        QToolButton#model::menu-indicator { subcontrol-position: right center; width: 10px; }
        QToolButton#effort { color: $secondary; padding: 1px 4px; font-size: 12px; }
        QLineEdit { border: 1px solid $line; background: $canvas; border-radius: 7px; padding: 7px 8px; }
        QLineEdit:focus { border-color: $muted; }
        QPlainTextEdit, QTextBrowser, QListWidget, QTreeWidget { border: none; }
        QPlainTextEdit#editor { background: transparent; font-size: 15px; selection-background-color: $selected; }
        QTextBrowser { selection-background-color: $selected; }
        QListWidget, QTreeWidget { background: transparent; outline: none; }
        QTreeWidget::item { height: 29px; padding-left: 3px; }
        QTreeWidget::item:hover { background: $hover; }
        QTreeWidget::item:selected { background: $selected; color: $ink; }
        QListWidget::item { padding: 7px 6px; border-radius: 7px; margin: 1px 0; }
        QListWidget::item:hover { background: $hover; }
        QListWidget::item:selected { background: $selected; color: $ink; }
        QMenu, QMenuBar { color: $ink; background: $canvas; }
        QMenu { border: 1px solid $line; padding: 5px; }
        QMenu::item { padding: 6px 22px; }
        QMenu::item:selected, QMenuBar::item:selected { background: $hover; color: $ink; }
        QProgressBar { background: $line; border: none; border-radius: 2px; }
        QProgressBar::chunk { background: $secondary; border-radius: 2px; }
        QTabWidget::pane { border: none; }
        QTabBar::tab { color: $muted; background: transparent; padding: 7px 9px; border-bottom: 1px solid $line; }
        QTabBar::tab:selected { color: $ink; border-bottom: 2px solid $ink; }
        QScrollBar:vertical { background: transparent; width: 7px; }
        QScrollBar::handle:vertical { background: $line; min-height: 22px; border-radius: 3px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QToolTip { background: $ink; color: $inverse; padding: 4px; border: none; }
    """
    for token, value in colors.items():
        sheet = sheet.replace("$" + token, value)
    app.setStyleSheet(sheet)
