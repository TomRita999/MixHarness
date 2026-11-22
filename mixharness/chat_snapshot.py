import base64
from functools import lru_cache
from html import escape
from urllib.parse import urlencode

from markdown_it import MarkdownIt


ACTORS = {"user": "你", "openai": "GPT", "gemini": "Gemini", "system": "MixHarness"}
MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")


def display_markdown(text):
    trimmed = text.strip()
    if not trimmed.startswith(("```", "~~~")):
        return text
    outer = MARKDOWN.parse(trimmed)
    if len(outer) == 1 and outer[0].type == "fence" and outer[0].info.strip().lower() in ("md", "markdown"):
        fence = outer[0]
        closing = trimmed.splitlines()[-1].strip()
        if len(closing) >= len(fence.markup) and not closing.strip(fence.markup[0]):
            inner = MARKDOWN.parse(fence.content)
            if inner and inner[0].type == "table_open" and sum(t.level == 0 for t in inner) == 2:
                return fence.content
    return text


@lru_cache(maxsize=128)
def markdown(text):
    return MARKDOWN.render(display_markdown(text))


def transcript(store, sid):
    title = store.session(sid)["title"]
    events = store.rows("SELECT seq,actor,body FROM events WHERE sid=? AND kind='message' ORDER BY seq", sid)
    inputs = store.inputs.for_session(sid)
    sections = ["# " + title]
    for event in events:
        sections.append("## " + ACTORS.get(event["actor"], event["actor"]))
        if event["body"]:
            sections.append(event["body"])
        names = [item["name"] for item in inputs.get(event["seq"], ())]
        if names:
            sections.append("附件：" + "、".join(names))
    return "\n\n".join(sections)


def snapshot_link(title, text):
    content = MARKDOWN.render(text)
    page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
            '<title>' + escape(title) + '</title>'
            '<style>:root{color-scheme:light dark}body{max-width:52rem;margin:3rem auto;padding:0 1.5rem;'
            'font:1rem/1.65 system-ui,sans-serif;overflow-wrap:anywhere}pre{white-space:pre-wrap}</style>' + content + '</html>')
    url = "data:text/html;charset=utf-8;base64," + base64.b64encode(page.encode()).decode("ascii")
    if len(url) > 2_000_000:
        raise ValueError("快照较长，请用 Markdown 文件分享。")
    return url


def feedback_url(message):
    query = urlencode({"body": message}) if message else ""
    return "https://github.com/TomRita999/MixHarness/issues/new" + ("?" + query if query else "")
