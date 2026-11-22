from markdown_it import MarkdownIt
from rich.console import Console
from rich.markdown import CodeBlock, Heading, HorizontalRule, Markdown
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme

from .chat_snapshot import display_markdown
from .markdown_links import WEB_LINKS, link_target


THEME = Theme({
    "markdown.h1": "bold underline", "markdown.h2": "bold", "markdown.h3": "bold italic",
    "markdown.h4": "italic", "markdown.h5": "italic", "markdown.h6": "italic",
    "markdown.link_url": "bright_blue underline", "markdown.code": "bright_blue",
    "markdown.list": "none", "markdown.item.bullet": "none", "markdown.item.number": "bright_blue",
    "markdown.block_quote": "green", "markdown.table.border": "dim", "markdown.table.header": "bold",
})
PARSER = MarkdownIt("gfm-like", {"html": False})
PARSER.linkify = WEB_LINKS
validate_link = PARSER.validateLink
PARSER.validateLink = lambda url: url.startswith("file:///") or validate_link(url)


class TerminalHeading(Heading):
    def __rich_console__(self, console, options):
        text = Text("#" * int(self.tag[1]) + " ", style=self.style_name)
        text.append_text(self.text)
        yield text


class TerminalRule(HorizontalRule):
    def __rich_console__(self, console, options):
        yield Text("———", style="markdown.hr")


class TerminalCode(CodeBlock):
    def __rich_console__(self, console, options):
        yield Syntax(self.text.plain.removesuffix("\n"), self.lexer_name, theme="ansi_dark",
                     background_color="default", word_wrap=True)


class TerminalMarkdown(Markdown):
    elements = {**Markdown.elements, "heading_open": TerminalHeading, "hr": TerminalRule,
                "fence": TerminalCode, "code_block": TerminalCode}

    def __init__(self, source, workspace):
        super().__init__("")
        self.parsed = PARSER.parse(display_markdown(source))
        for block in self.parsed:
            for span in block.children or ():
                if span.type == "link_open":
                    span.attrSet("href", link_target(span.attrGet("href") or "", workspace))


def print_message(author, body, workspace):
    console = Console(theme=THEME, highlight=False)
    console.print(Text(author, style="bold"))
    console.print(TerminalMarkdown(body, workspace))
