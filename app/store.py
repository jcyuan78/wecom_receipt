"""SQLite-backed message/job state.

One row per inbound WeCom message.  The row doubles as the work queue:

    pending     -- waiting for the file to be downloaded
    working     -- claimed by the worker (crash-safe; reset on startup)
    downloaded  -- file on disk, waiting for the downstream processor
    done        -- downloaded and processed successfully
    failed      -- gave up after ``max_attempts`` (or a permanent error)
    ignored     -- not a media message (text/event), kept for observability
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Message

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id          TEXT PRIMARY KEY,
    msg_type        TEXT NOT NULL,
    from_user       TEXT NOT NULL,
    to_user         TEXT NOT NULL DEFAULT '',
    agent_id        TEXT NOT NULL DEFAULT '',
    create_time     INTEGER NOT NULL DEFAULT 0,
    received_at     TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    status          TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    media_id        TEXT,
    filename        TEXT,
    file_path       TEXT,
    file_size       INTEGER,
    content_type    TEXT,
    sha256          TEXT,
    error           TEXT,
    raw_json        TEXT,
    raw_xml         TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_pickup ON messages(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment: datetime) -> str:
    return moment.isoformat()


class MessageStore:
    def __init__(self, path: Path, max_attempts: int = 3) -> None:
        self.path = Path(path)
        self.max_attempts = max_attempts
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path), timeout=30.0, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")

    # ------------------------------------------------------------- lifecycle
    def init(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --------------------------------------------------------------- writes
    def add_message(self, message: Message, raw_xml: str) -> bool:
        """Insert a message.  Returns False when the MsgId was already seen."""
        if message.is_media:
            status = "pending"
        else:
            status = "ignored"

        now = iso(utcnow())
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO messages (
                    msg_id, msg_type, from_user, to_user, agent_id, create_time,
                    received_at, updated_at, status, attempts, media_id, raw_json, raw_xml
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    message.msg_id,
                    message.msg_type,
                    message.from_user,
                    message.to_user,
                    message.agent_id,
                    message.create_time,
                    now,
                    now,
                    status,
                    message.media_id,
                    json.dumps(message.raw_public(), ensure_ascii=False),
                    raw_xml,
                ),
            )
            return cursor.rowcount == 1

    def claim_due(self, limit: int = 5) -> list[dict]:
        """Atomically claim up to ``limit`` due rows for this worker."""
        now = iso(utcnow())
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    """
                    UPDATE messages
                       SET status = 'working', updated_at = ?
                     WHERE msg_id IN (
                           SELECT msg_id FROM messages
                            WHERE status IN ('pending', 'downloaded')
                              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                            ORDER BY received_at
                            LIMIT ?
                     )
                    RETURNING *
                    """,
                    (now, now, limit),
                ).fetchall()
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return [dict(row) for row in rows]

    def mark_downloaded(
        self,
        msg_id: str,
        *,
        filename: str,
        file_path: str,
        file_size: int,
        content_type: str,
        sha256: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE messages
                   SET status = 'downloaded', attempts = 0, next_attempt_at = NULL,
                       filename = ?, file_path = ?, file_size = ?, content_type = ?,
                       sha256 = ?, error = NULL, updated_at = ?
                 WHERE msg_id = ?
                """,
                (filename, file_path, file_size, content_type, sha256, iso(utcnow()), msg_id),
            )

    def mark_done(self, msg_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET status = 'done', error = NULL, next_attempt_at = NULL, updated_at = ? WHERE msg_id = ?",
                (iso(utcnow()), msg_id),
            )

    def mark_retry(self, msg_id: str, error: str) -> None:
        row = self.get(msg_id)
        if row is None:
            return
        attempts = int(row["attempts"]) + 1
        now = utcnow()

        if attempts >= self.max_attempts:
            status = "failed"
            next_attempt = None
        else:
            # Retry the download when no file exists yet, otherwise only the processor.
            status = "downloaded" if row["file_path"] else "pending"
            delay = min(300, 10 * (2 ** (attempts - 1)))
            next_attempt = iso(now + timedelta(seconds=delay))

        with self._lock:
            self._conn.execute(
                "UPDATE messages SET status = ?, attempts = ?, error = ?, next_attempt_at = ?, updated_at = ? WHERE msg_id = ?",
                (status, attempts, error[:2000], next_attempt, iso(now), msg_id),
            )
        log.warning("消息 %s 处理失败（第 %d 次）: %s", msg_id, attempts, error)

    def mark_permanent_failure(self, msg_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET status = 'failed', error = ?, next_attempt_at = NULL, updated_at = ? WHERE msg_id = ?",
                (error[:2000], iso(utcnow()), msg_id),
            )
        log.error("消息 %s 永久失败: %s", msg_id, error)

    def reset_stuck(self, older_than_seconds: int = 600) -> int:
        """Return rows stuck in ``working`` (e.g. after a crash) to the queue."""
        cutoff = iso(utcnow() - timedelta(seconds=older_than_seconds))
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE messages
                   SET status = CASE WHEN file_path IS NULL THEN 'pending' ELSE 'downloaded' END,
                       next_attempt_at = NULL, updated_at = ?
                 WHERE status = 'working' AND updated_at < ?
                """,
                (iso(utcnow()), cutoff),
            )
        return cursor.rowcount

    # ---------------------------------------------------------------- reads
    def get(self, msg_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM messages WHERE msg_id = ?", (msg_id,)).fetchone()
        return dict(row) if row else None

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages ORDER BY received_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS count FROM messages GROUP BY status"
            ).fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
        counts["total"] = sum(counts.values())
        return counts
