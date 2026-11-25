"""Domain objects shared by the callback, worker and processors."""

from __future__ import annotations

from dataclasses import dataclass, field

MEDIA_TYPES = frozenset({"image", "file", "voice", "video"})
SUPPORTED_TYPES = MEDIA_TYPES | {"text", "location", "link"}


@dataclass(slots=True)
class Message:
    """A normalized inbound WeCom message."""

    msg_id: str
    msg_type: str
    from_user: str
    to_user: str = ""
    agent_id: str = ""
    create_time: int = 0
    media_id: str | None = None
    file_name: str | None = None
    file_size: int | None = None
    pic_url: str | None = None
    media_format: str | None = None
    content: str | None = None
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def has_media(self) -> bool:
        return bool(self.media_id)

    @property
    def is_media(self) -> bool:
        return self.msg_type in MEDIA_TYPES and self.has_media

    @property
    def is_text(self) -> bool:
        return self.msg_type == "text" and bool(self.content)

    @property
    def fallback_filename(self) -> str:
        if self.file_name:
            return self.file_name
        suffix = ""
        if self.media_format:
            suffix = f".{self.media_format.lstrip('.')}"
        elif self.msg_type == "video":
            suffix = ".mp4"
        return f"{self.msg_type}-{self.msg_id}{suffix}"

    def raw_public(self) -> dict[str, str]:
        # """Fields that are safe/useful to persist alongside the file."""
        kept = {
            "MsgId",
            "MsgType",
            "FromUserName",
            "ToUserName",
            "CreateTime",
            "AgentID",
            "MediaId",
            "FileName",
            "FileSize",
            "PicUrl",
            "Format",
        }
        return {k: v for k, v in self.raw.items() if k in kept}


@dataclass(slots=True)
class DownloadedMedia:
    content: bytes
    filename: str | None
    content_type: str

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass(slots=True)
class SavedFile:
    path: str
    name: str
    size: int
    sha256: str
    content_type: str
