from PySide6.QtGui import (
    QFont, QFontDatabase, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument,
    QTextFormat, QTextFrameFormat, QTextLength, QTextOption, QTextTable,
)

from .chat_snapshot import display_markdown

DIALECT = QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML
PROP = QTextFormat.Property


def render_markdown(document, text, font, palette):
    font = QFont(font)
    font.setPixelSize(15)
    document.setDefaultFont(font)
    document.setIndentWidth(22)
    options = document.defaultTextOption()
    options.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    document.setDefaultTextOption(options)
    document.setLayoutEnabled(False)
    try:
        document.setMarkdown(display_markdown(text), DIALECT)
        style_blocks(document, palette)
        for frame in document.rootFrame().childFrames():
            if isinstance(frame, QTextTable):
                fmt = frame.format()
                fmt.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
                fmt.setBorder(1)
                fmt.setBorderCollapse(True)
                fmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
                fmt.setBorderBrush(palette.mid())
                fmt.setCellPadding(7)
                frame.setFormat(fmt)
    finally:
        document.setLayoutEnabled(True)


def style_blocks(document, palette):
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
        fmt.setLineHeight(132 if code or heading else 148, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        fmt.setTopMargin(0)
        fmt.setBottomMargin(4 if block.textList() else 9)
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
            fmt.setLeftMargin(16)
            fmt.setBackground(palette.alternateBase())
        cursor.setBlockFormat(fmt)
        # 保留原有链接、列表和强调，仅修改当前文本片段的排版。
        changes = []
        fragment = block.begin()
        while not fragment.atEnd():
            part = fragment.fragment()
            original = part.charFormat()
            if code or original.fontFixedPitch() or "monospace" in (original.fontFamilies() or ()):
                original.merge(code_style)
            elif heading:
                original.clearProperty(PROP.FontSizeAdjustment)
                original.setProperty(PROP.FontPixelSize, (23, 20, 17, 16, 15, 15)[min(heading, 6) - 1])
                original.setFontWeight(QFont.Weight.DemiBold)
            else:
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
