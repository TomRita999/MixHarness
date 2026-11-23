import io
import unittest
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QTextFormat, QTextTable
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QPlainTextEdit
from rich.console import Console

from mixharness.desktop_style import apply_theme
from mixharness.desktop_widgets import Document
from mixharness.terminal_markdown import THEME, TerminalMarkdown
from tests.qt_app import application


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "pyproject.toml"
PROJECT_LINE = PROJECT.read_text(encoding="utf-8").splitlines().index("[project]") + 1
REPOSITORY = "https://github.com/openai/codex"


class MarkdownTests(unittest.TestCase):
    def test_desktop_links_respond_to_hover_and_open_the_source_line(self):
        app = application()
        app.setQuitOnLastWindowClosed(False)
        previous = app.palette(), app.styleSheet()
        self.addCleanup(app.setPalette, previous[0])
        self.addCleanup(app.setStyleSheet, previous[1])
        for theme in ("dark", "light"):
            with self.subTest(theme=theme):
                apply_theme(app, theme)
                view = Document(ROOT)
                view.resize(640, 300)
                view.show()
                caption = f"项目配置 · {PROJECT.name}"
                view.show_markdown(f"📄 [{caption}](./{PROJECT.name}:{PROJECT_LINE})\n\n{REPOSITORY}")
                app.processEvents()
                doc = view.document()
                link = doc.find(PROJECT.name)
                style = link.charFormat()
                self.assertEqual(style.anchorHref(), PROJECT.as_uri() + f"#L{PROJECT_LINE}")
                self.assertEqual(style.foreground().color(), view.palette().link().color())
                self.assertNotEqual(style.foreground().color(), view.palette().text().color())
                self.assertTrue(style.fontUnderline())
                self.assertEqual(doc.find(REPOSITORY).charFormat().anchorHref(), REPOSITORY)
                end = view.cursorRect(link)
                link.setPosition(link.selectionStart())
                bounds = view.cursorRect(link).united(end)
                self.assertGreater(bounds.width(), 1, f"文件名的显示范围为 {bounds}")
                point = bounds.center()
                self.assertEqual(view.anchorAt(point), style.anchorHref(), f"文件名的显示范围为 {bounds}")
                # 窗口接收鼠标事件，离屏测试也能检查悬停效果。
                window = view.windowHandle()
                link_pos = view.viewport().mapTo(view, point)
                QTest.mouseMove(window, QPoint(view.width() - 12, view.height() - 12))
                QTest.mouseMove(window, link_pos)
                self.assertEqual("".join(s.cursor.selectedText() for s in view.extraSelections()), caption)
                QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=link_pos)
                dialog = view.findChild(QDialog)
                preview = dialog.findChild(QPlainTextEdit)
                self.assertEqual(preview.textCursor().blockNumber(), PROJECT_LINE - 1)
                self.assertEqual(preview.textCursor().block().text(), "[project]")
                dialog.close()
                view.close()

    def test_desktop_and_terminal_render_project_readme(self):
        app = application()
        source = (ROOT / "README.md").read_text(encoding="utf-8")
        view = Document(ROOT)
        view.show_markdown(source)
        doc = view.document()
        self.assertTrue(doc.begin().begin().fragment().charFormat().fontUnderline())
        self.assertTrue(any(isinstance(f, QTextTable) for f in doc.rootFrame().childFrames()))
        block = doc.begin()
        code_colors = set()
        while block.isValid():
            if block.blockFormat().stringProperty(QTextFormat.Property.BlockCodeLanguage) == "toml":
                span = block.begin()
                while not span.atEnd():
                    item = span.fragment()
                    if item.text().strip():
                        code_colors.add(item.charFormat().foreground().color().name())
                    span += 1
            block = block.next()
        self.assertGreater(len(code_colors), 1)
        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=True, color_system="standard", no_color=False,
                          legacy_windows=False, width=100, theme=THEME)
        console.print(TerminalMarkdown(f"{source}\n\n[项目配置](./{PROJECT.name})\n\n{REPOSITORY}", ROOT))
        rendered = buffer.getvalue()
        self.assertIn("# ", rendered)
        self.assertIn("\x1b[1;4mMixHarness", rendered)
        self.assertIn("\x1b]8;", rendered)
        self.assertIn("\x1b[4;94m", rendered)
        self.assertIn(REPOSITORY + "\x1b\\", rendered)
        self.assertIn(PROJECT.as_uri(), rendered)
        self.assertNotIn("](", rendered)
        view.close()
