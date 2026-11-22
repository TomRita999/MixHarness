from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication


RESOURCES = Path(__file__).with_name("resources")
GLYPHS = {
    "sidebar": 0xE12A, "inspector": 0xE431, "new": 0xE172, "search": 0xE151,
    "folder": 0xE0D7, "settings": 0xE154, "sun": 0xE178, "moon": 0xE11E,
    "send": 0xE04A, "chat": 0xE117, "gemini": 0xE47E, "openai": 0xE040, "close": 0xE1B2,
    "plus": 0xE13D, "more": 0xE0B6, "archive": 0xE041, "pin": 0xE259, "trash": 0xE18E,
    "chevron-down": 0xE06D, "chevron-right": 0xE06F,
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
    "light": dict(canvas="#ffffff", side="#f8f8f7", hover="#efefed", selected="#e7e7e4", field="#ffffff",
                  line="#e6e6e3", strong="#cfcfcb", ink="#1b1b1b", secondary="#555553", muted="#8b8b88",
                  inverse="#ffffff", bubble="#f2f2f0", shadow="#14000000"),
    "dark": dict(canvas="#161616", side="#0f0f0f", hover="#222222", selected="#2b2b2b", field="#1d1d1d",
                 line="#2a2a2a", strong="#3d3d3d", ink="#ededed", secondary="#b8b8b8", muted="#7f7f7f",
                 inverse="#161616", bubble="#262626", shadow="#66000000"),
}


def glyph_file(name, color):
    """把图标字体中的箭头渲染成小图片，供样式表的下拉箭头和树节点使用。"""
    folder = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation)) / "mixharness-glyphs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{color.lstrip('#')}.png"
    if not path.exists():
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setPen(QColor(color))
        font = QFont(QApplication.instance().property("iconFamily"))
        font.setPixelSize(26)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, chr(GLYPHS[name]))
        painter.end()
        pixmap.save(str(path))
    return path.as_posix()


def apply_theme(app, name):
    colors = dict(THEMES[name])
    colors["chevron"] = glyph_file("chevron-down", colors["muted"])
    colors["branch_closed"] = glyph_file("chevron-right", colors["muted"])
    colors["branch_open"] = glyph_file("chevron-down", colors["muted"])
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
        QWidget { color: $ink; font-size: 13px; }
        QMainWindow, QDialog, QWidget#main, QWidget#content, QTextBrowser, QPlainTextEdit { background: $canvas; }
        QFrame#sidebar { background: $side; border-right: 1px solid $line; }
        QFrame#inspector { background: $side; border-left: 1px solid $line; }
        QFrame#composer { background: $field; border: 1px solid $line; border-radius: 20px; }
        QFrame#composer[focused="true"] { border-color: $strong; }
        QFrame#headerLine { background: $line; max-height: 1px; border: none; }
        QScrollArea#timeline, QWidget#timelineCanvas, QFrame#reply { background: $canvas; border: none; }
        QFrame#userBubble { background: $bubble; border-radius: 16px; }
        QFrame#trace { background: $field; border-left: 2px solid $strong; }
        QTextBrowser#messageText { background: transparent; border: none; padding: 0; font-size: 14px; }
        QLabel#messageAuthor { font-size: 12px; color: $muted; font-weight: 600; }
        QToolButton#activity { font-size: 12px; color: $muted; padding: 2px 6px; border-radius: 6px; }
        QLabel { background: transparent; }
        QLabel#brand { font-size: 15px; font-weight: 650; }
        QLabel#heading { font-size: 26px; font-weight: 600; color: $ink; }
        QLabel#subheading { font-size: 13px; color: $muted; }
        QLabel#title { font-weight: 600; }
        QLabel#panelTitle { font-weight: 600; font-size: 13px; }
        QLabel#caption, QLabel#context, QLabel#counter { color: $muted; font-size: 12px; }
        QLabel#section { color: $muted; font-size: 11px; font-weight: 600; }

        QPushButton, QToolButton { background: transparent; border: none; border-radius: 8px; padding: 6px 8px; }
        QPushButton:hover, QToolButton:hover { background: $hover; }
        QPushButton:pressed, QToolButton:pressed, QToolButton:checked { background: $selected; }
        QPushButton:focus, QToolButton:focus { outline: none; }
        QPushButton:disabled { color: $muted; }
        QPushButton#nav { text-align: left; padding: 7px 10px; color: $secondary; }
        QPushButton#nav:hover { color: $ink; }
        QPushButton#quiet { color: $muted; text-align: left; font-size: 12px; padding: 4px 8px; }
        QPushButton#quiet:hover { color: $ink; }
        QPushButton#primary { background: $ink; color: $inverse; border-radius: 9px; padding: 7px 18px; font-weight: 600; }
        QPushButton#primary:hover { background: $secondary; }
        QPushButton#primary:disabled { background: $line; color: $muted; }
        QDialog QPushButton { border: 1px solid $line; padding: 6px 14px; }
        QDialog QPushButton#primary, QDialog QPushButton#quiet, QDialog QPushButton#rowAction { border: none; }
        QPushButton#rowAction { padding: 2px; border-radius: 6px; }
        QPushButton#send { background: $ink; border-radius: 16px; padding: 0; }
        QPushButton#send:hover { background: $secondary; }
        QPushButton#send:disabled { background: $line; }
        QToolButton#model { text-align: left; padding: 3px 8px 3px 6px; font-size: 13px; font-weight: 500; border-radius: 8px; }
        QToolButton#effort { color: $muted; padding: 3px 8px; font-size: 12px; border-radius: 8px; }
        QToolButton#effort:hover { color: $ink; }
        QToolButton#effort:disabled { color: $muted; }

        QComboBox { background: transparent; border: 1px solid $line; border-radius: 8px;
                    padding: 4px 24px 4px 10px; font-size: 12px; color: $secondary; min-height: 18px; }
        QComboBox:hover { background: $hover; color: $ink; }
        QComboBox:disabled { color: $muted; }
        QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right; width: 20px; border: none; }
        QComboBox::down-arrow { image: url($chevron); width: 12px; height: 12px; }
        QComboBox QAbstractItemView { background: $canvas; border: 1px solid $line; border-radius: 10px; padding: 4px;
                                      outline: none; selection-background-color: $hover; selection-color: $ink; }
        QComboBox#flat { border: none; }

        QLineEdit { border: 1px solid $line; background: $field; border-radius: 9px; padding: 7px 10px; }
        QLineEdit:focus { border-color: $strong; }
        QLineEdit:disabled { color: $muted; }
        QSpinBox { border: 1px solid $line; background: $field; border-radius: 8px; padding: 4px 8px; }
        QPlainTextEdit, QTextBrowser, QListWidget, QTreeWidget { border: none; }
        QPlainTextEdit#editor { background: transparent; font-size: 14px; selection-background-color: $selected; }
        QTextBrowser { selection-background-color: $selected; }

        QListWidget, QTreeWidget { background: transparent; outline: none; }
        QListWidget#slashMenu { background: $field; border: 1px solid $strong; border-radius: 10px; padding: 4px; }
        QTreeWidget::item { height: 30px; padding-left: 2px; color: $secondary; }
        QTreeWidget::item:hover { background: $hover; color: $ink; }
        QTreeWidget::item:selected { background: $selected; color: $ink; }
        QTreeWidget::branch { background: transparent; }
        QTreeWidget::branch:has-children:closed { image: url($branch_closed); }
        QTreeWidget::branch:has-children:open { image: url($branch_open); }
        QListWidget::item { padding: 8px 10px; border-radius: 8px; margin: 1px 0; }
        QListWidget::item:hover { background: $hover; }
        QListWidget::item:selected { background: $selected; color: $ink; }

        QMenu, QMenuBar { color: $ink; background: $canvas; }
        QMenu { border: 1px solid $line; border-radius: 10px; padding: 5px; }
        QMenu::item { padding: 6px 18px 6px 12px; border-radius: 6px; }
        QMenu::item:selected, QMenuBar::item:selected { background: $hover; color: $ink; }
        QMenu::item:disabled { color: $muted; }
        QMenu::separator { height: 1px; background: $line; margin: 4px 6px; }
        QMenu::section { color: $muted; font-size: 11px; padding: 6px 12px 2px; }

        QSplitter#panes::handle { background: transparent; }
        QSplitter#panes::handle:hover { background: $line; }
        QProgressBar { background: $line; border: none; border-radius: 2px; }
        QProgressBar::chunk { background: $ink; border-radius: 2px; }
        QTabWidget::pane { border: none; }
        QTabBar::tab { color: $muted; background: transparent; padding: 6px 10px; margin-right: 2px; border-radius: 7px; }
        QTabBar::tab:hover { color: $ink; }
        QTabBar::tab:selected { color: $ink; background: $selected; }

        QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
        QScrollBar::handle:vertical { background: $strong; min-height: 28px; border-radius: 2px; }
        QScrollBar::handle:vertical:hover { background: $muted; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        QScrollBar:horizontal { height: 0; }
        QToolTip { background: $ink; color: $inverse; padding: 5px 8px; border: none; border-radius: 6px; }
    """
    for token in sorted(colors, key=len, reverse=True):
        sheet = sheet.replace("$" + token, colors[token])
    app.setStyleSheet(sheet)
