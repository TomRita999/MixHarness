import re
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from linkify_it import LinkifyIt


WEB_LINKS = LinkifyIt().set({"fuzzy_link": False, "fuzzy_email": False})
LINE_SUFFIX = re.compile(r":([1-9]\d{0,8})(?::([1-9]\d{0,8}))?(?:[-–]\d+(?::\d+)?)?$")
HASH_LINE = re.compile(r"L([1-9]\d{0,8})(?:C([1-9]\d{0,8}))?(?:-L\d+(?:C\d+)?)?$", re.I)


def link_target(href, workspace=None):
    if not href or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in href):
        return ""
    if href.startswith("#"):
        return href
    try:
        url = urlsplit(href)
        if url.scheme in ("http", "https"):
            return href if url.hostname else ""
        if url.scheme == "mailto":
            return href
        if url.netloc and (url.scheme != "file" or url.netloc != "localhost"):
            return ""
        if url.scheme == "file":
            path = url2pathname(url.path)
        else:
            path = unquote(href.partition("#")[0])
            suffix = LINE_SUFFIX.search(path)
            if url.scheme and not Path(path).is_absolute() and not (suffix and suffix.start() == len(url.scheme)):
                return ""
        match = LINE_SUFFIX.search(path)
        location = HASH_LINE.fullmatch(url.fragment)
        if match:
            path = path[:match.start()]
            location = match
        path = Path(path).expanduser()
        if not path.is_absolute():
            if not workspace:
                return ""
            path = Path(workspace) / path
        target = path.as_uri()
        if location:
            return target + "#L" + location[1] + ("C" + location[2] if location[2] else "")
        return target
    except (ValueError, OSError, RuntimeError):
        return ""
