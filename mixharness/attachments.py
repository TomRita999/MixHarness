import base64
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QMimeDatabase
from PySide6.QtGui import QImageReader


MAX_IMAGE = 8 * 1024 * 1024
MAX_IMAGE_TOTAL = 16 * 1024 * 1024
MAX_VIDEO = 64 * 1024 * 1024
MAX_VIDEO_TOTAL = 128 * 1024 * 1024
MAX_ITEMS = 12
MAX_REFERENCE = 24000
TYPES = {"image": "图片", "video": "视频", "chat": "会话", "tool": "工具", "skill": "技能"}
FORMATS = {b"png": "image/png", b"jpeg": "image/jpeg", b"webp": "image/webp"}
VIDEO_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}


def video_mime(name):
    return VIDEO_TYPES.get(Path(name).suffix.lower())


def inspect_video(name, stream):
    import av

    mime = video_mime(name)
    if not mime:
        raise ValueError("请选择 MP4、MOV 或 WebM 视频")
    container_format = "mov"
    if mime == "video/webm":
        # WebM 和 Matroska 共用解复用器，结合 Qt MIME 数据库与 EBML DocType 头部识别 WebM。
        header = stream.read(65536)
        stream.seek(0)
        detected = QMimeDatabase().mimeTypeForData(header).name()
        is_webm = detected in ("video/webm", "audio/webm") or (
            header.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in header[:64]
        )
        if not is_webm:
            raise ValueError("文件内容与 WebM 格式不符")
        container_format = "matroska"

    def reject_external(url, flags, options):
        raise ValueError("视频必须包含本地媒体数据")

    try:
        with av.open(stream, mode="r", format=container_format, io_open=reject_external,
                     options={"enable_drefs": "0", "use_absolute_path": "0"}) as media:
            brand = media.metadata.get("major_brand", "").strip()
            if container_format == "mov" and brand and ((brand == "qt") != (mime == "video/quicktime")):
                raise ValueError("视频容器与文件扩展名不符")
            tracks = [track for track in media.streams.video
                      if track.width > 0 and track.height > 0
                      and not (track.disposition & av.stream.Disposition.attached_pic)]
            if not tracks or not any(packet.size for packet in media.demux(*tracks)):
                raise ValueError("文件中没有包含画面的有效视频轨道")
    except av.error.FFmpegError as exc:
        raise ValueError("无法读取视频容器或视频轨道") from exc
    return mime


@dataclass(frozen=True)
class Attachment:
    kind: str
    name: str
    source: str
    target: str
    mime: str
    data: bytes

    @property
    def id(self):
        key = json.dumps([self.kind, self.name, self.source, self.target, self.mime]).encode()
        digest = hashlib.sha256(key + b"\0")
        digest.update(self.data)
        return digest.hexdigest()

    @classmethod
    def reference(cls, kind, name, source, target, text):
        if kind not in ("chat", "tool", "skill") or not text.strip():
            raise ValueError("引用没有可用的内容")
        if len(text) > 120000:
            raise ValueError("单个引用最多支持 120,000 个字符")
        return cls(kind, name, source, target, "text/plain", text.encode())

    @classmethod
    def image(cls, name, data):
        if not 0 < len(data) <= MAX_IMAGE:
            raise ValueError("每张图片需小于或等于 8 MiB")
        buffer = QBuffer()
        buffer.setData(data)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        mime = FORMATS.get(bytes(reader.format()).lower())
        if not mime:
            raise ValueError("请选择 PNG、JPEG 或 WebP 图片")
        size = reader.size()
        if not size.isValid() or size.width() * size.height() > 24000000:
            raise ValueError("图片尺寸无效或超过 2400 万像素")
        if reader.read().isNull():
            raise ValueError("图片无法解码")
        return cls("image", Path(name).name[:255] or "图片", "", "", mime, bytes(data))

    @classmethod
    def video(cls, name, data):
        if not 0 < len(data) <= MAX_VIDEO:
            raise ValueError("每个视频需大于零且小于或等于 64 MiB")
        name = Path(name).name
        if len(name) > 255:
            raise ValueError("视频文件名最多支持 255 个字符")
        with BytesIO(data) as stream:
            mime = inspect_video(name, stream)
        return cls("video", name, "", "", mime, bytes(data))

    @classmethod
    def from_file(cls, path):
        path = Path(path)
        with path.open("rb") as stream:
            if video_mime(path):
                return cls.video(path.name, stream.read(MAX_VIDEO + 1))
            return cls.image(path.name, stream.read(MAX_IMAGE + 1))


class InputLibrary:
    def __init__(self, db):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS input_items(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, source TEXT NOT NULL,
                target TEXT NOT NULL, mime TEXT NOT NULL, data BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS draft_inputs(
                draft TEXT NOT NULL, item TEXT NOT NULL REFERENCES input_items(id), PRIMARY KEY(draft,item)
            );
            CREATE TABLE IF NOT EXISTS message_inputs(
                seq INTEGER NOT NULL REFERENCES events(seq) ON DELETE CASCADE,
                item TEXT NOT NULL REFERENCES input_items(id), PRIMARY KEY(seq,item)
            );
        """)

    def metadata(self, draft):
        return [dict(r) for r in self.db.execute(
            "SELECT i.id,i.kind,i.name,i.source,i.target,i.mime,length(i.data) AS size FROM draft_inputs d "
            "JOIN input_items i ON d.item=i.id WHERE d.draft=? ORDER BY d.rowid", (draft,))]

    def put(self, item):
        identifier = item.id
        self.db.execute("INSERT OR IGNORE INTO input_items VALUES(?,?,?,?,?,?,?)",
                        (identifier, item.kind, item.name, item.source, item.target, item.mime, item.data))
        return identifier

    @staticmethod
    def validate(items):
        if len(items) > MAX_ITEMS:
            raise ValueError(f"每条消息最多可附加 {MAX_ITEMS} 项")
        if sum(i["size"] for i in items if i["kind"] == "image") > MAX_IMAGE_TOTAL:
            raise ValueError("一条消息的图片总大小最多为 16 MiB")
        if sum(i["size"] for i in items if i["kind"] == "video") > MAX_VIDEO_TOTAL:
            raise ValueError("一条消息的视频总大小最多为 128 MiB")

    def stage(self, draft, items):
        with self.db:
            for item in items:
                self.db.execute("INSERT OR IGNORE INTO draft_inputs VALUES(?,?)", (draft, self.put(item)))
            self.validate(self.metadata(draft))

    def unstage(self, draft, identifier):
        with self.db:
            self.db.execute("DELETE FROM draft_inputs WHERE draft=? AND item=?", (draft, identifier))
            self.prune()

    def commit_message(self, seq, draft, items):
        if draft:
            self.db.execute("INSERT INTO message_inputs SELECT ?,item FROM draft_inputs WHERE draft=? ORDER BY rowid",
                            (seq, draft))
            self.db.execute("DELETE FROM draft_inputs WHERE draft=?", (draft,))
        for item in items:
            self.db.execute("INSERT OR IGNORE INTO message_inputs VALUES(?,?)", (seq, self.put(item)))
        self.validate([dict(r) for r in self.db.execute(
            "SELECT i.kind,length(i.data) AS size FROM message_inputs m JOIN input_items i ON m.item=i.id WHERE m.seq=?",
            (seq,))])

    def prune(self):
        self.db.execute("DELETE FROM input_items WHERE id NOT IN (SELECT item FROM draft_inputs UNION SELECT item FROM message_inputs)")

    def for_session(self, sid, after=0):
        rows = self.db.execute(
            "SELECT m.seq,i.id,i.kind,i.name,i.source,i.target,i.mime,length(i.data) AS size FROM events e "
            "JOIN message_inputs m ON e.seq=m.seq JOIN input_items i ON m.item=i.id "
            "WHERE e.sid=? AND e.seq>? ORDER BY m.seq,m.rowid", (sid, after))
        result = defaultdict(list)
        for row in rows:
            item = dict(row)
            result[item.pop("seq")].append(item)
        return result

    def media_for_session(self, sid, identifier):
        row = self.db.execute(
            "SELECT i.name,i.mime,length(i.data) AS size FROM input_items i "
            "WHERE i.id=? AND i.kind IN ('image','video') AND EXISTS (SELECT 1 FROM message_inputs m "
            "JOIN events e ON e.seq=m.seq WHERE m.item=i.id AND e.sid=?)", (identifier, sid)).fetchone()
        if row is None:
            raise KeyError("会话中没有此媒体附件")
        return dict(row)

    def data_range(self, identifier, start, length):
        row = self.db.execute("SELECT substr(data,?,?) FROM input_items WHERE id=?",
                              (start + 1, length, identifier)).fetchone()
        if row is None:
            raise KeyError("附件不存在")
        return row[0]

    def data(self, identifier):
        row = self.db.execute("SELECT data FROM input_items WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError("附件不存在")
        return row[0]

    def describe(self, items):
        descriptions = []
        for item in items:
            kind = item["kind"]
            description = {"类型": TYPES[kind], "名称": item["name"]}
            if kind == "video":
                description.update(媒体类型=item["mime"], 字节数=item["size"], 附件编号=item["id"])
            elif kind in ("chat", "tool", "skill"):
                description.update(来源=item["source"], 标识=item["target"], 内容=self.data(item["id"]).decode())
            descriptions.append(description)
        return descriptions


def image_parts(images, provider):
    parts = []
    for image in images:
        data = base64.b64encode(image["data"]).decode("ascii")
        mime = image["mime"]
        parts.append({"type": "input_text" if provider == "openai" else "text", "text": "图片：" + image["name"]})
        if provider == "gemini":
            parts.append({"type": "image", "mime_type": mime, "data": data})
        elif provider == "antigravity":
            parts.append({"type": "image", "mimeType": mime, "data": data})
        elif provider == "codex":
            parts.append({"type": "image", "url": f"data:{mime};base64,{data}"})
        elif provider == "openai":
            parts.append({"type": "input_image", "image_url": f"data:{mime};base64,{data}", "detail": "auto"})
        else:
            raise ValueError("未知的图片接收方")
    return parts
