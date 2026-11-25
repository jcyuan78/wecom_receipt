"""The job document handed to the downstream processing stage.

Every successfully downloaded file produces one JSON document.  The next
stage of the pipeline (image analysis, OCR, archiving, ...) can either read
the file from ``data/jobs/`` or receive the document via HTTP / a command.
"""

from __future__ import annotations

import json
from loguru import logger as log
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


JOB_SCHEMA = "wecom.receiver.job/v1"


def _epoch_to_iso(value: int | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def build_job(row: dict[str, Any]) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    try:
        raw = json.loads(row.get("raw_json") or "{}")
    except ValueError:
        raw = {}

    return {
        "schema": JOB_SCHEMA,
        "msg_id": row["msg_id"],
        "msg_type": row["msg_type"],
        "from_user": row["from_user"],
        "to_user": row["to_user"],
        "agent_id": row["agent_id"],
        "created_at": _epoch_to_iso(row.get("create_time")),
        "received_at": row["received_at"],
        "file": {
            "path": row["file_path"],
            "name": row["filename"],
            "size": row["file_size"],
            "sha256": row["sha256"],
            "content_type": row["content_type"],
        },
        "raw": raw,
    }


def write_job(jobs_dir: Path, job: dict[str, Any]) -> Path:
    jobs_dir = Path(jobs_dir)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = jobs_dir / f"{job['msg_id']}.json"
    tmp = jobs_dir / f".{path.name}.part"
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path
