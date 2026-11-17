"""Filesystem layout for received files."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from .models import DownloadedMedia, Message, SavedFile

log = logging.getLogger(__name__)

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MAX_STEM = 100
_EXTENSION_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "video/mp4": ".mp4",
    "audio/amr": ".amr",
    "audio/speex": ".speex",
    "application/pdf": ".pdf",
}


def safe_filename(name: str | None, fallback: str = "file") -> str:
    """Return a filename that is safe on Windows/Linux and cannot escape a dir.

    Any directory component supplied by the remote side is dropped: only the
    basename survives, then characters that are illegal on Windows are replaced.
    """
    cleaned = (name or "").strip().replace("\r", " ").replace("\n", " ")
    cleaned = cleaned.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _INVALID_CHARS.sub("_", cleaned)
    cleaned = cleaned.strip(" .")

    if not cleaned:
        cleaned = fallback

    stem, dot, suffix = cleaned.rpartition(".")
    if not dot or not stem:
        stem, suffix = cleaned, ""
    else:
        suffix = _INVALID_CHARS.sub("", suffix)[:16]

    if stem.upper() in _RESERVED_NAMES:
        stem = f"_{stem}"
    if len(stem) > _MAX_STEM:
        stem = stem[:_MAX_STEM].rstrip(" .")

    result = f"{stem}.{suffix}" if suffix else stem
    return result or fallback


class FileStorage:
    def __init__(self, inbox_dir: Path) -> None:
        self.root = Path(inbox_dir)

    def save(self, message: Message, media: DownloadedMedia, received_at: datetime) -> SavedFile:
        day = received_at.strftime("%Y-%m-%d")
        folder = self.root / day / safe_filename(message.msg_id, "message")
        folder.mkdir(parents=True, exist_ok=True)

        name = media.filename or self._fallback_name(message, media)
        name = safe_filename(name, fallback=f"{message.msg_type}-{message.msg_id}")
        path = self._unique_path(folder / name)

        tmp = path.parent / f".{path.name}.part"
        tmp.write_bytes(media.content)
        os.replace(tmp, path)

        digest = hashlib.sha256(media.content).hexdigest()
        meta = {
            "msg_id": message.msg_id,
            "msg_type": message.msg_type,
            "from_user": message.from_user,
            "to_user": message.to_user,
            "agent_id": message.agent_id,
            "create_time": message.create_time,
            "received_at": received_at.isoformat(timespec="seconds"),
            "filename": path.name,
            "original_filename": media.filename,
            "size": len(media.content),
            "sha256": digest,
            "content_type": media.content_type,
            "raw": message.raw_public(),
        }
        meta_path = folder / "meta.json"
        tmp_meta = folder / ".meta.json.part"
        tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_meta, meta_path)

        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        if not Path(resolved).is_relative_to(self.root.resolve()):
            raise RuntimeError(f"文件路径越界: {resolved}")

        log.info("已保存文件 %s (%d bytes, sha256=%s)", resolved, len(media.content), digest[:12])
        return SavedFile(
            path=resolved,
            name=path.name,
            size=len(media.content),
            sha256=digest,
            content_type=media.content_type,
        )

    @staticmethod
    def _fallback_name(message: Message, media: DownloadedMedia) -> str:
        name = message.fallback_filename
        if "." not in Path(name).name:
            name += _EXTENSION_BY_TYPE.get(media.content_type, "")
        return name

    @staticmethod
    def _unique_path(path: Path) -> Path:
        if not path.exists():
            return path
        stem, suffix = path.stem, path.suffix
        for index in range(1, 1000):
            candidate = path.with_name(f"{stem} ({index}){suffix}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"无法为 {path} 生成唯一文件名")
