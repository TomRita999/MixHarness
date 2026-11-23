from functools import lru_cache

from pygments.lexers import get_lexer_by_name
from pygments.styles import get_style_by_name
from pygments.util import ClassNotFound
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument,
    QTextFormat, QTextFrameFormat, QTextLength, QTextOption, QTextTable,
)

from .chat_snapshot import display_markdown
from .markdown_links import WEB_LINKS, link_target

DIALECT = QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML
PROP = QTextFormat.Property


def link_plain_text(document, palette):
    text = document.toPlainText()
    last, position = 0, 0
    for match in WEB_LINKS.match(text) or ():
        target = link_target(match.url)
        if target:
            start = position + len(text[last:match.index].encode("utf-16-le")) // 2
            end = start + len(match.raw.encode("utf-16-le")) // 2
            last, position = match.last_index, end
            cursor = QTextCursor(document)
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            style = QTextCharFormat()
            style.setAnchor(True)
            style.setAnchorHref(target)
            style.setForeground(palette.link())
            style.setFontUnderline(True)
            cursor.mergeCharFormat(style)


def render_markdown(document, text, font, palette, workspace=None):
    font = QFont(font)
    font.setPixelSize(15)
    document.setDefaultFont(font)
    document.setIndentWidth(24)
    options = document.defaultTextOption()
    options.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    document.setDefaultTextOption(options)
    document.setLayoutEnabled(False)
    try:
        document.setMarkdown(display_markdown(text), DIALECT)
        style_blocks(document, palette, workspace)
        highlight_fences(document, palette)
        for frame in document.rootFrame().childFrames():
            if isinstance(frame, QTextTable):
                fmt = frame.format()
                fmt.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
                fmt.setBorder(1)
                fmt.setBorderCollapse(True)
                fmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
                fmt.setBorderBrush(palette.mid())
                fmt.setCellPadding(6)
                frame.setFormat(fmt)
                for column in range(frame.columns()):
                    cell = frame.cellAt(0, column)
                    style = cell.format()
                    style.setBackground(palette.alternateBase())
                    cell.setFormat(style)
    finally:
        document.setLayoutEnabled(True)


def style_blocks(document, palette, workspace):
    mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
    code_style = QTextCharFormat()
    code_style.setFontFamilies([mono, "monospace"])
    code_style.setProperty(PROP.FontPixelSize, 13)
    code_style.setBackground(palette.alternateBase())
    block = document.begin()
    previous_code = False
    while block.isValid():
        cursor = QTextCursor(block)
        fmt = block.blockFormat()
        heading = fmt.headingLevel()
        code = fmt.hasProperty(PROP.BlockCodeFence) or fmt.nonBreakableLines()
        fmt.setLineHeight(130 if code or heading else 145, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        fmt.setTopMargin(0)
        fmt.setBottomMargin(2 if block.textList() else 9)
        if code:
            fmt.setNonBreakableLines(False)
            fmt.setLeftMargin(10)
            fmt.setRightMargin(10)
            fmt.setTopMargin(0 if previous_code or block == document.begin() else 6)
            fmt.setBottomMargin(0 if block.next().blockFormat().hasProperty(PROP.BlockCodeFence) else 8)
            fmt.setBackground(palette.alternateBase())
        elif heading:
            fmt.setTopMargin(15 if block.previous().isValid() else 0)
            fmt.setBottomMargin(6)
        elif fmt.intProperty(PROP.BlockQuoteLevel):
            fmt.setLeftMargin(16 * fmt.intProperty(PROP.BlockQuoteLevel))
            fmt.setRightMargin(6)
            fmt.setBackground(palette.alternateBase())
        cursor.setBlockFormat(fmt)
        # 保留原有链接、列表和强调，仅修改当前文本片段的排版。
        changes = []
        fragment = block.begin()
        while not fragment.atEnd():
            part = fragment.fragment()
            original = part.charFormat()
            inline_code = original.fontFixedPitch() or "monospace" in (original.fontFamilies() or ())
            if heading:
                original.clearProperty(PROP.FontSizeAdjustment)
                original.setProperty(PROP.FontPixelSize, (21, 18, 16, 15, 15, 15)[min(heading, 6) - 1])
                original.setFontWeight(QFont.Weight.Bold if heading <= 3 else QFont.Weight.Normal)
                if heading == 1:
                    original.setFontUnderline(True)
                if heading >= 3:
                    original.setFontItalic(True)
            if code or inline_code:
                original.merge(code_style)
                if not code:
                    original.setForeground(palette.link())
            if original.anchorHref():
                target = link_target(original.anchorHref(), workspace)
                original.setAnchorHref(target)
                original.setAnchor(bool(target))
                original.setFontUnderline(bool(target))
                original.setForeground(palette.link() if target else palette.text())
            elif fmt.intProperty(PROP.BlockQuoteLevel) and not inline_code:
                original.setForeground(QColor("#78bc91" if palette.base().color().lightness() < 128 else "#386947"))
            elif not (heading or code or inline_code):
                fragment += 1
                continue
            changes.append((part.position(), part.length(), original))
            fragment += 1
        for start, length, style in changes:
            cursor.setPosition(start)
            cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
            cursor.setCharFormat(style)
        previous_code = code
        block = block.next()


@lru_cache(maxsize=64)
def fenced_lexer(language):
    try:
        return get_lexer_by_name(language)
    except ClassNotFound:
        return None


def highlight_fences(document, palette):
    theme = get_style_by_name("github-dark" if palette.base().color().lightness() < 128 else "default")
    block = document.begin()
    while block.isValid():
        language = block.blockFormat().stringProperty(PROP.BlockCodeLanguage)
        if not language:
            block = block.next()
            continue
        start = block.position()
        lines = []
        while block.isValid() and block.blockFormat().stringProperty(PROP.BlockCodeLanguage) == language:
            lines.append(block.text())
            block = block.next()
        lexer = fenced_lexer(language.split()[0]) if language.strip() else None
        if not lexer:
            continue
        cursor = QTextCursor(document)
        for _, token, value in lexer.get_tokens_unprocessed("\n".join(lines)):
            # QTextCursor 以 UTF-16 计数，不能把含 emoji 的 Python 字符索引直接当作位置。
            end = start + len(value.encode("utf-16-le")) // 2
            while token.parent is not None and not theme.styles_token(token):
                token = token.parent
            style = theme.style_for_token(token)
            if style["color"] and end > start:
                fmt = QTextCharFormat()
                fmt.setForeground(QColor("#" + style["color"]))
                if style["italic"]:
                    fmt.setFontItalic(True)
                if style["bold"]:
                    fmt.setFontWeight(QFont.Weight.Bold)
                cursor.setPosition(start)
                cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(fmt)
            start = end
