"""SQLite storage for parsed invoices."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_no      TEXT COLLATE NOCASE,
    invoice_date    TEXT NOT NULL,
    amount_minor    INTEGER NOT NULL,
    currency        TEXT NOT NULL,
    vendor          TEXT,
    items_json      TEXT,
    tax_minor       INTEGER,
    image_path      TEXT,
    source_msg_id   TEXT,
    source_path     TEXT,
    source_sha256   TEXT,
    segment_index   INTEGER NOT NULL DEFAULT 0,
    segment_sha256  TEXT,
    ai_model        TEXT,
    ai_confidence   REAL,
    needs_review    INTEGER NOT NULL DEFAULT 0,
    checked         INTEGER NOT NULL DEFAULT 0,
    reimbursed      INTEGER NOT NULL DEFAULT 0,
    checked_at      TEXT,
    reimbursed_at   TEXT,
    notes           TEXT,
    raw_json        TEXT,
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_invoices_no
    ON invoices(invoice_no) WHERE invoice_no IS NOT NULL AND invoice_no <> '';
CREATE INDEX IF NOT EXISTS ix_invoices_date ON invoices(invoice_date);
CREATE INDEX IF NOT EXISTS ix_invoices_status ON invoices(checked, reimbursed);
"""


class DuplicateInvoiceError(RuntimeError):
    def __init__(self, existing: dict[str, Any]) -> None:
        self.existing = existing
        label = existing.get("invoice_no") or existing.get("id")
        super().__init__(f"发票已存在: {label}")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["items"] = json.loads(data.get("items_json") or "[]")
    except ValueError:
        data["items"] = []
    data["checked"] = bool(data.get("checked"))
    data["reimbursed"] = bool(data.get("reimbursed"))
    data["needs_review"] = bool(data.get("needs_review"))
    return data


class InvoiceDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), timeout=30.0, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")

    def init(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Make databases created by the single-invoice version compatible."""
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(invoices)")}
        if "segment_index" not in columns:
            self._conn.execute(
                "ALTER TABLE invoices ADD COLUMN segment_index INTEGER NOT NULL DEFAULT 0"
            )
        if "segment_sha256" not in columns:
            self._conn.execute("ALTER TABLE invoices ADD COLUMN segment_sha256 TEXT")

        # One message may now produce several invoices, so source_sha256 is no
        # longer unique; the cropped segment hash is the per-image identity.
        self._conn.execute("DROP INDEX IF EXISTS ux_invoices_source")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_invoices_source ON invoices(source_sha256)"
        )
        try:
            self._conn.execute(
                """
                UPDATE invoices SET segment_sha256 = source_sha256
                 WHERE segment_sha256 IS NULL AND source_sha256 IS NOT NULL
                   AND source_sha256 <> ''
                """
            )
        except sqlite3.IntegrityError:
            pass
        self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_invoices_segment
                ON invoices(segment_sha256)
             WHERE segment_sha256 IS NOT NULL AND segment_sha256 <> ''
            """
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- write
    def add(
        self,
        *,
        invoice_no: str | None,
        invoice_date: date,
        amount_minor: int,
        currency: str,
        vendor: str | None,
        items: list[dict[str, Any]],
        tax_minor: int | None,
        image_path: str,
        source_msg_id: str,
        source_path: str,
        source_sha256: str,
        segment_index: int = 0,
        segment_sha256: str | None = None,
        ai_model: str,
        ai_confidence: float,
        needs_review: bool,
        raw: dict[str, Any],
        notes: str | None = None,
    ) -> int:
        existing = self.find_duplicate(invoice_no, segment_sha256)
        if existing is not None:
            raise DuplicateInvoiceError(existing)

        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO invoices (
                        invoice_no, invoice_date, amount_minor, currency, vendor,
                        items_json, tax_minor, image_path, source_msg_id, source_path,
                        source_sha256, segment_index, segment_sha256, ai_model,
                        ai_confidence, needs_review, notes, raw_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        invoice_no,
                        invoice_date.isoformat(),
                        int(amount_minor),
                        currency.upper(),
                        vendor,
                        json.dumps(items, ensure_ascii=False),
                        tax_minor,
                        image_path,
                        source_msg_id,
                        source_path,
                        source_sha256,
                        int(segment_index),
                        segment_sha256,
                        ai_model,
                        float(ai_confidence),
                        1 if needs_review else 0,
                        notes,
                        json.dumps(raw, ensure_ascii=False),
                        utcnow_iso(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                existing = self.find_duplicate(invoice_no, segment_sha256)
                if existing is not None:
                    raise DuplicateInvoiceError(existing) from exc
                raise

        return int(cursor.lastrowid or 0)

    def set_status(
        self,
        record_id: int,
        *,
        checked: bool | None = None,
        reimbursed: bool | None = None,
        notes: str | None = None,
    ) -> dict[str, Any] | None:
        assignments: list[str] = []
        params: list[Any] = []
        now = utcnow_iso()

        if checked is not None:
            assignments.append("checked = ?, checked_at = ?")
            params.extend([1 if checked else 0, now if checked else None])
        if reimbursed is not None:
            assignments.append("reimbursed = ?, reimbursed_at = ?")
            params.extend([1 if reimbursed else 0, now if reimbursed else None])
        if notes is not None:
            assignments.append("notes = ?")
            params.append(notes)

        if not assignments:
            return self.get(record_id)

        params.append(record_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE invoices SET {', '.join(assignments)} WHERE id = ?", params
            )
        return self.get(record_id)

    # -------------------------------------------------------------- read
    def get(self, record_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM invoices WHERE id = ?", (record_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def get_by_invoice_no(self, invoice_no: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM invoices WHERE invoice_no = ? COLLATE NOCASE", (invoice_no,)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def find_duplicate(
        self, invoice_no: str | None, segment_sha256: str | None
    ) -> dict[str, Any] | None:
        if not invoice_no and not segment_sha256:
            return None

        clauses: list[str] = []
        params: list[Any] = []
        if invoice_no:
            clauses.append("invoice_no = ? COLLATE NOCASE")
            params.append(invoice_no)
        if segment_sha256:
            clauses.append("segment_sha256 = ?")
            params.append(segment_sha256)

        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM invoices WHERE {' OR '.join(clauses)} LIMIT 1", params
            ).fetchone()
        return _row_to_dict(row) if row else None

    def _where(
        self,
        start: date,
        end: date,
        *,
        only_unchecked: bool,
        only_unreimbursed: bool,
        needs_review: bool,
        vendor: str | None,
    ) -> tuple[str, list[Any]]:
        clauses = ["invoice_date >= ?", "invoice_date <= ?"]
        params: list[Any] = [start.isoformat(), end.isoformat()]
        if only_unchecked:
            clauses.append("checked = 0")
        if only_unreimbursed:
            clauses.append("reimbursed = 0")
        if needs_review:
            clauses.append("needs_review = 1")
        if vendor:
            clauses.append("vendor LIKE ?")
            params.append(f"%{vendor}%")
        return " AND ".join(clauses), params

    def query(
        self,
        start: date,
        end: date,
        *,
        only_unchecked: bool = False,
        only_unreimbursed: bool = False,
        needs_review: bool = False,
        vendor: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._where(
            start,
            end,
            only_unchecked=only_unchecked,
            only_unreimbursed=only_unreimbursed,
            needs_review=needs_review,
            vendor=vendor,
        )
        sql = f"SELECT * FROM invoices WHERE {where} ORDER BY invoice_date, id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_dict(row) for row in rows]

    def totals(
        self,
        start: date,
        end: date,
        *,
        only_unchecked: bool = False,
        only_unreimbursed: bool = False,
        needs_review: bool = False,
        vendor: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._where(
            start,
            end,
            only_unchecked=only_unchecked,
            only_unreimbursed=only_unreimbursed,
            needs_review=needs_review,
            vendor=vendor,
        )
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT currency, SUM(amount_minor) AS total_minor, COUNT(*) AS count
                  FROM invoices WHERE {where}
                 GROUP BY currency ORDER BY currency
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN checked = 0 THEN 1 ELSE 0 END), 0) AS unchecked,
                       COALESCE(SUM(CASE WHEN reimbursed = 0 THEN 1 ELSE 0 END), 0) AS unreimbursed,
                       COALESCE(SUM(CASE WHEN needs_review = 1 THEN 1 ELSE 0 END), 0) AS needs_review
                  FROM invoices
                """
            ).fetchone()
            totals = self._conn.execute(
                "SELECT currency, SUM(amount_minor) AS total_minor FROM invoices GROUP BY currency"
            ).fetchall()
        data = dict(row) if row else {}
        data["totals"] = [dict(item) for item in totals]
        return data
