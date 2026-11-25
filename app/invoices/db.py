"""SQLite storage for parsed invoices and manual reimbursement records."""

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
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'new',
    source          TEXT NOT NULL DEFAULT 'invoice'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_invoices_no
    ON invoices(invoice_no) WHERE invoice_no IS NOT NULL AND invoice_no <> '';
CREATE INDEX IF NOT EXISTS ix_invoices_date ON invoices(invoice_date);
CREATE INDEX IF NOT EXISTS ix_invoices_status ON invoices(checked, reimbursed);
"""

STATUS_NEW = "new"
STATUS_REVIEW = "review"
STATUS_REVIEWED = "reviewed"
STATUS_REIMBURSED = "reimbursed"
STATUSES = (STATUS_NEW, STATUS_REVIEW, STATUS_REVIEWED, STATUS_REIMBURSED)
SOURCES = ("invoice", "manual")

# Human-facing labels used by the CLI and text replies.
STATUS_LABELS = {
    STATUS_NEW: "新添加",
    STATUS_REVIEW: "待校对",
    STATUS_REVIEWED: "已校对",
    STATUS_REIMBURSED: "已报销",
}
SOURCE_LABELS = {"invoice": "发票", "manual": "手工"}


class DuplicateInvoiceError(RuntimeError):
    def __init__(self, existing: dict[str, Any]) -> None:
        self.existing = existing
        label = existing.get("invoice_no") or existing.get("id")
        super().__init__(f"发票已存在: {label}")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_status(value: str | None, default: str = STATUS_NEW) -> str:
    status = str(value or "").strip().lower()
    return status if status in STATUSES else default


def normalize_source(value: str | None) -> str:
    source = str(value or "").strip().lower()
    return source if source in SOURCES else "invoice"


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["items"] = json.loads(data.get("items_json") or "[]")
    except ValueError:
        data["items"] = []
    data["checked"] = bool(data.get("checked"))
    data["reimbursed"] = bool(data.get("reimbursed"))
    data["needs_review"] = bool(data.get("needs_review"))
    data["status"] = normalize_status(data.get("status"))
    data["source"] = normalize_source(data.get("source"))
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
        """Make databases from the single-invoice / boolean-status era compatible."""
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

        # Status workflow: new -> review/reviewed -> reimbursed.
        if "status" not in columns:
            self._conn.execute(
                "ALTER TABLE invoices ADD COLUMN status TEXT NOT NULL DEFAULT 'new'"
            )
            self._conn.execute(
                """
                UPDATE invoices
                   SET status = CASE
                         WHEN reimbursed = 1 THEN 'reimbursed'
                         WHEN checked = 1 THEN 'reviewed'
                         WHEN needs_review = 1 THEN 'review'
                         ELSE 'reviewed'
                       END
                """
            )
        if "source" not in columns:
            self._conn.execute(
                "ALTER TABLE invoices ADD COLUMN source TEXT NOT NULL DEFAULT 'invoice'"
            )

        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_invoices_state ON invoices(status, source)"
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
        status: str | None = None,
        source: str = "invoice",
    ) -> int:
        existing = self.find_duplicate(invoice_no, segment_sha256)
        if existing is not None:
            raise DuplicateInvoiceError(existing)

        status = normalize_status(status or (STATUS_REVIEW if needs_review else STATUS_REVIEWED))
        source = normalize_source(source)
        checked = status in {STATUS_REVIEWED, STATUS_REIMBURSED}
        reimbursed = status == STATUS_REIMBURSED
        needs_review = status == STATUS_REVIEW
        now = utcnow_iso()

        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO invoices (
                        invoice_no, invoice_date, amount_minor, currency, vendor,
                        items_json, tax_minor, image_path, source_msg_id, source_path,
                        source_sha256, segment_index, segment_sha256, ai_model,
                        ai_confidence, needs_review, checked, reimbursed, checked_at,
                        reimbursed_at, notes, raw_json, created_at, status, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        invoice_no,
                        invoice_date.isoformat(),
                        int(amount_minor),
                        currency.upper(),
                        vendor,
                        json.dumps(items, ensure_ascii=False),
                        tax_minor,
                        image_path or None,
                        source_msg_id,
                        source_path or None,
                        source_sha256 or None,
                        int(segment_index),
                        segment_sha256,
                        ai_model,
                        float(ai_confidence),
                        1 if needs_review else 0,
                        1 if checked else 0,
                        1 if reimbursed else 0,
                        now if checked else None,
                        now if reimbursed else None,
                        notes,
                        json.dumps(raw, ensure_ascii=False),
                        now,
                        status,
                        source,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                existing = self.find_duplicate(invoice_no, segment_sha256)
                if existing is not None:
                    raise DuplicateInvoiceError(existing) from exc
                raise

        return int(cursor.lastrowid or 0)

    def add_manual(
        self,
        *,
        expense_date: date,
        amount_minor: int,
        currency: str,
        payee: str | None,
        category: str | None,
        description: str,
        source_msg_id: str = "",
        notes: str | None = None,
        reimbursed: bool = False,
        status: str | None = None,
    ) -> int:
        """Create a reimbursement record that has no invoice image."""
        status = status or (STATUS_REIMBURSED if reimbursed else STATUS_REVIEWED)
        items = [item for item in (category, description) if item]
        raw = {
            "manual": True,
            "category": category,
            "description": description,
        }
        return self.add(
            invoice_no=None,
            invoice_date=expense_date,
            amount_minor=amount_minor,
            currency=currency,
            vendor=payee or (category or "手工报销"),
            items=[{"name": " / ".join(items) or "手工报销"}],
            tax_minor=None,
            image_path="",
            source_msg_id=source_msg_id,
            source_path="",
            source_sha256="",
            segment_index=0,
            segment_sha256=None,
            ai_model="manual",
            ai_confidence=1.0,
            needs_review=False,
            raw=raw,
            notes=notes,
            status=status,
            source="manual",
        )

    def set_status(
        self,
        record_id: int,
        *,
        status: str | None = None,
        checked: bool | None = None,
        reimbursed: bool | None = None,
        notes: str | None = None,
    ) -> dict[str, Any] | None:
        row = self.get(record_id)
        if row is None:
            return None

        if status is None:
            if reimbursed is True:
                status = STATUS_REIMBURSED
            elif reimbursed is False and row["status"] == STATUS_REIMBURSED:
                status = STATUS_REVIEWED
            elif checked is True:
                status = STATUS_REVIEWED
            elif checked is False:
                status = STATUS_REVIEW
            else:
                status = row["status"]

        status = normalize_status(status, row["status"])
        checked_flag = status in {STATUS_REVIEWED, STATUS_REIMBURSED}
        reimbursed_flag = status == STATUS_REIMBURSED
        needs_review = status == STATUS_REVIEW
        now = utcnow_iso()

        assignments = [
            "status = ?",
            "checked = ?",
            "reimbursed = ?",
            "needs_review = ?",
            "checked_at = ?",
            "reimbursed_at = ?",
        ]
        params: list[Any] = [
            status,
            1 if checked_flag else 0,
            1 if reimbursed_flag else 0,
            1 if needs_review else 0,
            now if checked_flag else None,
            now if reimbursed_flag else None,
        ]
        if notes is not None:
            assignments.append("notes = ?")
            params.append(notes)
        params.append(record_id)

        with self._lock:
            self._conn.execute(
                f"UPDATE invoices SET {', '.join(assignments)} WHERE id = ?", params
            )
        return self.get(record_id)

    def update_status_by_ids(self, ids: list[int], status: str) -> int:
        updated = 0
        for record_id in ids:
            if self.set_status(int(record_id), status=status) is not None:
                updated += 1
        return updated

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
        only_unchecked: bool = False,
        only_unreimbursed: bool = False,
        needs_review: bool = False,
        vendor: str | None = None,
        status: str | None = None,
        source: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses = ["invoice_date >= ?", "invoice_date <= ?"]
        params: list[Any] = [start.isoformat(), end.isoformat()]

        if only_unchecked:
            clauses.append("status IN ('new', 'review')")
        if only_unreimbursed:
            clauses.append("status != 'reimbursed'")
        if needs_review:
            clauses.append("status = 'review'")
        if vendor:
            clauses.append("vendor LIKE ?")
            params.append(f"%{vendor}%")
        if status == "unpaid":
            clauses.append("status != 'reimbursed'")
        elif status:
            clauses.append("status = ?")
            params.append(normalize_status(status))
        if source:
            clauses.append("source = ?")
            params.append(normalize_source(source))

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
        status: str | None = None,
        source: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._where(
            start,
            end,
            only_unchecked=only_unchecked,
            only_unreimbursed=only_unreimbursed,
            needs_review=needs_review,
            vendor=vendor,
            status=status,
            source=source,
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
        status: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._where(
            start,
            end,
            only_unchecked=only_unchecked,
            only_unreimbursed=only_unreimbursed,
            needs_review=needs_review,
            vendor=vendor,
            status=status,
            source=source,
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

    def aggregate(
        self,
        start: date,
        end: date,
        *,
        group_by: str = "currency",
        status: str | None = None,
        source: str | None = None,
        vendor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Group totals by month/vendor/status/source/currency."""
        expressions = {
            "month": "substr(invoice_date, 1, 7)",
            "vendor": "COALESCE(vendor, '')",
            "status": "status",
            "source": "source",
            "currency": "currency",
        }
        expression = expressions.get(group_by, "currency")
        where, params = self._where(
            start,
            end,
            vendor=vendor,
            status=status,
            source=source,
        )
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT {expression} AS group_key, currency,
                       SUM(amount_minor) AS total_minor, COUNT(*) AS count
                  FROM invoices WHERE {where}
                 GROUP BY group_key, currency
                 ORDER BY group_key, currency
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN status = 'new' THEN 1 ELSE 0 END), 0) AS new,
                       COALESCE(SUM(CASE WHEN status = 'review' THEN 1 ELSE 0 END), 0) AS review,
                       COALESCE(SUM(CASE WHEN status = 'reviewed' THEN 1 ELSE 0 END), 0) AS reviewed,
                       COALESCE(SUM(CASE WHEN status = 'reimbursed' THEN 1 ELSE 0 END), 0) AS reimbursed,
                       COALESCE(SUM(CASE WHEN source = 'manual' THEN 1 ELSE 0 END), 0) AS manual
                  FROM invoices
                """
            ).fetchone()
            totals = self._conn.execute(
                "SELECT currency, SUM(amount_minor) AS total_minor FROM invoices GROUP BY currency"
            ).fetchall()
        data = dict(row) if row else {}
        data["totals"] = [dict(item) for item in totals]
        return data
