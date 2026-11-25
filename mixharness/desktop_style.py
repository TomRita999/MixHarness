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
    "chevron-down": 0xE06D, "chevron-right": 0xE06F, "pencil": 0xE12F,
    "zap": 0xE1B4, "toggle-left": 0xE18B, "toggle-right": 0xE18C,
    "back": 0xE048, "forward": 0xE049, "stop": 0xE167,
    "server": 0xE153, "review": 0xE0CB, "usage": 0xE1BF, "share": 0xE155,
    "fork": 0xE28D, "book": 0xE05F, "compact": 0xE529, "terminal": 0xE181,
    "plan": 0xE086, "goal": 0xE345,
}


def load_icons():
    font_id = QFontDatabase.addApplicationFont(str(RESOURCES / "lucide.ttf"))
    if font_id < 0:
        raise RuntimeError("无法加载应用图标字体")
    return QFontDatabase.applicationFontFamilies(font_id)[0]


class Glyph(QIconEngine):
    def __init__(self, family, name, role, scale=1.0):
        super().__init__()
        self.family, self.name, self.role, self.scale = family, name, role, scale

    def clone(self):
        return Glyph(self.family, self.name, self.role, self.scale)

    def paint(self, painter, rect, mode, state):
        group = QPalette.ColorGroup.Disabled if mode == QIcon.Mode.Disabled else QPalette.ColorGroup.Active
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setPen(QApplication.palette().color(group, self.role))
        font = QFont(self.family)
        font.setPixelSize(max(1, round(min(rect.width(), rect.height()) * self.scale)))
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
    "light": dict(canvas="#ffffff", side="#f9f9f9", hover="#f3f3f3", selected="#ededed", field="#ffffff",
                  line="#ededed", strong="#dfdfdf", ink="#0d0d0d", secondary="#505050", muted="#737373",
                  inverse="#ffffff", bubble="#f3f3f3", link="#0969da", shadow="#19000000"),
    "dark": dict(canvas="#181818", side="#000000", hover="#282828", selected="#303030", field="#212121",
                 line="#303030", strong="#404040", ink="#ededed", secondary="#cdcdcd", muted="#999999",
                 inverse="#181818", bubble="#303030", link="#75b6ff", shadow="#30000000"),
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
        "Link": "link", "LinkVisited": "link", "PlaceholderText": "muted", "ToolTipBase": "ink",
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
        QFrame#composer { background: $field; border: 1px solid $strong; border-radius: 22px; }
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
        QLabel#heading { font-size: 28px; font-weight: 500; color: $ink; }
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
        QPushButton#nav { text-align: left; min-height: 22px; padding: 4px 8px; color: $ink; }
        QPushButton#nav:hover { color: $ink; }
        QPushButton#quiet { color: $muted; text-align: left; font-size: 12px; padding: 4px 8px; }
        QPushButton#quiet:hover { color: $ink; }
        QPushButton#workStatus { color: $ink; text-align: left; font-size: 12px; padding: 4px 2px; }
        QPushButton#stopWork { border: 1px solid $strong; border-radius: 10px; color: $ink; padding: 3px 12px; }
        QPushButton#stopWork:hover { background: $hover; }
        QPushButton#stopWork:disabled { color: $muted; }
        QPushButton#speed { padding: 0; border-radius: 6px; }
        QPushButton#speed:focus { border: 1px solid $strong; }
        QToolButton#daybreak { padding: 3px 7px; font-size: 12px; color: $secondary; }
        QToolButton#daybreak:hover, QToolButton#daybreak:checked { color: $ink; }
        QPushButton#primary { background: $ink; color: $inverse; border-radius: 9px; padding: 7px 18px; font-weight: 600; }
        QPushButton#primary:hover { background: $secondary; }
        QPushButton#primary:disabled { background: $line; color: $muted; }
        QDialog QPushButton { border: 1px solid $line; padding: 6px 14px; }
        QDialog QPushButton#primary, QDialog QPushButton#quiet, QDialog QPushButton#rowAction { border: none; }
        QPushButton#rowAction { padding: 2px; border-radius: 6px; }
        QPushButton#round { border: none; border-radius: 14px; padding: 0; }
        QPushButton#round:hover { border-color: $strong; }
        QPushButton#send, QPushButton#stopRound { background: $ink; border-radius: 14px; padding: 0; }
        QPushButton#recipient { color: $secondary; padding: 3px 6px; font-size: 12px; }
        QPushButton#send:hover { background: $secondary; }
        QPushButton#send:disabled { background: $line; }
        QToolButton#model { text-align: left; padding: 3px 4px 3px 6px; font-size: 13px; font-weight: 500; border-radius: 8px; }
        QToolButton#effort { color: $muted; padding: 3px 8px 3px 3px; font-size: 12px; border-radius: 8px; }
        QToolButton#effort:hover { color: $ink; }
        QToolButton#effort:disabled { color: $muted; }

        QComboBox { background: transparent; border: 1px solid $line; border-radius: 8px; combobox-popup: 0;
                    padding: 4px 24px 4px 10px; font-size: 12px; color: $secondary; min-height: 18px; }
        QComboBox:hover { background: $hover; color: $ink; }
        QComboBox:disabled { color: $muted; }
        QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right; width: 20px; border: none; }
        QComboBox::down-arrow { image: url($chevron); width: 12px; height: 12px; }
        QComboBox QAbstractItemView { background: $canvas; border: 1px solid $line; border-radius: 10px; padding: 5px;
                                      outline: none; selection-background-color: $hover; selection-color: $ink; }
        QComboBox QAbstractItemView::item { min-height: 28px; padding: 0 10px; border-radius: 6px; color: $ink; }
        QComboBox QAbstractItemView::item:hover, QComboBox QAbstractItemView::item:selected { background: $hover; }
        QComboBox#flat { border: none; color: $muted; padding: 4px 22px 4px 8px; }
        QComboBox#flat:hover { color: $ink; }

        QLineEdit { border: 1px solid $line; background: $field; border-radius: 9px; padding: 7px 10px; }
        QLineEdit:focus { border-color: $strong; }
        QLineEdit:disabled { color: $muted; }
        QSpinBox { border: 1px solid $line; background: $field; border-radius: 8px; padding: 4px 8px; }
        QPlainTextEdit, QTextBrowser, QListWidget, QTreeWidget { border: none; }
        QPlainTextEdit#editor { background: transparent; font-size: 14px; selection-background-color: $selected; }
        QTextBrowser { selection-background-color: $selected; }

        QListWidget, QTreeWidget { background: transparent; outline: none; }
        QListWidget#slashMenu { background: $field; border: 1px solid $strong; border-radius: 14px; padding: 4px; }
        QTreeWidget::item { height: 30px; padding-left: 2px; color: $secondary; }
        QTreeWidget::item:hover { background: $hover; color: $ink; }
        QTreeWidget::item:selected { background: transparent; color: $ink; }
        QTreeWidget::branch { background: transparent; }
        QTreeWidget::branch:has-children:closed { image: url($branch_closed); }
        QTreeWidget::branch:has-children:open { image: url($branch_open); }
        QListWidget::item { padding: 8px 10px; border-radius: 8px; margin: 1px 0; }
        QListWidget::item:hover { background: $hover; }
        QListWidget::item:selected { background: $selected; color: $ink; }

        QMenu, QMenuBar { color: $ink; background: $canvas; }
        QMenu { background: $field; border: 1px solid $strong; border-radius: 12px; padding: 5px; }
        QMenu::item { padding: 7px 22px 7px 12px; border-radius: 7px; min-width: 120px; }
        QMenu::icon { padding-left: 10px; }
        QMenu::indicator { width: 14px; height: 14px; padding-left: 6px; }
        QMenu::right-arrow { image: url($branch_closed); width: 12px; height: 12px; padding-right: 8px; }
        QMenu::item:selected, QMenuBar::item:selected { background: $hover; color: $ink; }
        QMenu::item:disabled { color: $muted; }
        QMenu::separator { height: 1px; background: $line; margin: 5px 8px; }
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
