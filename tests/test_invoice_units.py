"""Unit tests for invoice parsing, storage and CLI."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import date

import pytest

from app.invoices import parsing
from app.invoices.cli import main as cli_main
from app.invoices.db import DuplicateInvoiceError, InvoiceDB

from tests.helpers import ROOT


class TestParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1,234円", 1234.0),
            ("¥1,234.56", 1234.56),
            ("12,345", 12345.0),
            (3280, 3280.0),
            ("  980 ", 980.0),
            (None, None),
            ("-", None),
            ("", None),
        ],
    )
    def test_amount(self, raw, expected):
        assert parsing.parse_amount(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("¥", "JPY"),
            ("円", "JPY"),
            ("人民币", "CNY"),
            ("RMB", "CNY"),
            ("usd", "USD"),
            ("", "JPY"),
            (None, "JPY"),
        ],
    )
    def test_currency(self, raw, expected):
        assert parsing.normalize_currency(raw, "JPY") == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-01-15", date(2026, 1, 15)),
            ("2026/1/5", date(2026, 1, 5)),
            ("2026年1月5日", date(2026, 1, 5)),
            ("20260105", date(2026, 1, 5)),
            ("01/15/2026", date(2026, 1, 15)),
            ("15/01/2026", date(2026, 1, 15)),
            ("not a date", None),
            ("2026-13-01", None),
        ],
    )
    def test_date(self, raw, expected):
        assert parsing.parse_date(raw) == expected

    def test_invoice_no(self):
        assert parsing.clean_invoice_no("No. INV-001") == "INV-001"
        assert parsing.clean_invoice_no("发票号码：12345") == "12345"
        assert parsing.clean_invoice_no(" #A-9 ") == "A-9"
        assert parsing.clean_invoice_no("null") is None
        assert parsing.clean_invoice_no("  ") is None

    def test_minor_units(self):
        assert parsing.to_minor(3280, "JPY") == 3280
        assert parsing.to_minor(12.34, "CNY") == 1234
        assert parsing.to_minor(0.1, "USD") == 10
        assert parsing.format_money(3280, "JPY") == "3,280 JPY"
        assert parsing.format_money(1234, "CNY") == "12.34 CNY"

    def test_items(self):
        items = parsing.parse_items(
            [{"name": "ノート", "qty": "2", "amount": "1,200"}, "笔"]
        )
        assert items[0] == {"name": "ノート", "qty": 2.0, "amount": 1200.0}
        assert items[1] == {"name": "笔"}


def _db(name: str) -> InvoiceDB:
    folder = ROOT / "data" / "tests" / "invoice_db" / name
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    db = InvoiceDB(folder / "invoices.sqlite3")
    db.init()
    return db


def _add(db: InvoiceDB, **overrides):
    params = dict(
        invoice_no="INV-001",
        invoice_date=date(2026, 1, 15),
        amount_minor=3280,
        currency="JPY",
        vendor="テスト商店",
        items=[{"name": "ノート", "qty": 2, "amount": 1200}],
        tax_minor=298,
        image_path="/tmp/a.jpg",
        source_msg_id="1",
        source_path="/tmp/src.jpg",
        source_sha256="a" * 64,
        segment_index=0,
        segment_sha256=None,
        ai_model="qwen2.5vl:7b",
        ai_confidence=0.9,
        needs_review=False,
        raw={"total": 3280},
    )
    params.update(overrides)
    return db.add(**params)


class TestInvoiceDB:
    def test_add_query_and_totals(self):
        db = _db("query")
        _add(db)
        _add(
            db,
            invoice_no="INV-002",
            invoice_date=date(2026, 2, 10),
            amount_minor=1200,
            source_sha256="b" * 64,
        )

        january = db.query(date(2026, 1, 1), date(2026, 1, 31))
        assert len(january) == 1
        assert january[0]["invoice_no"] == "INV-001"
        assert january[0]["amount_minor"] == 3280

        totals = db.totals(date(2026, 1, 1), date(2026, 12, 31))
        assert totals == [{"currency": "JPY", "total_minor": 4480, "count": 2}]

        assert db.query(date(2026, 3, 1), date(2026, 3, 31)) == []
        db.close()

    def test_duplicate_invoice_no(self):
        db = _db("dupe_no")
        _add(db)
        with pytest.raises(DuplicateInvoiceError) as exc:
            _add(db, source_sha256="c" * 64)
        assert exc.value.existing["invoice_no"] == "INV-001"
        db.close()

    def test_duplicate_segment_hash(self):
        db = _db("dupe_sha")
        _add(db, segment_sha256="s" * 64)
        with pytest.raises(DuplicateInvoiceError):
            _add(
                db,
                invoice_no="INV-999",
                source_sha256="b" * 64,
                segment_sha256="s" * 64,
            )
        # The same source image may produce several rows (multi-invoice photo).
        _add(db, invoice_no="INV-003", segment_sha256="t" * 64)
        assert db.stats()["total"] == 2
        db.close()

    def test_status_flags(self):
        db = _db("status")
        record_id = _add(db)
        row = db.get(record_id)
        assert row["status"] == "reviewed"
        assert row["checked"] is True

        db.set_status(record_id, status="review")
        row = db.get(record_id)
        assert row["status"] == "review"
        assert row["checked"] is False
        assert row["needs_review"] is True

        db.set_status(record_id, status="reviewed")
        row = db.get(record_id)
        assert row["status"] == "reviewed"
        assert row["checked"] is True
        assert row["checked_at"]

        db.set_status(record_id, status="reimbursed")
        row = db.get(record_id)
        assert row["status"] == "reimbursed"
        assert row["reimbursed"] is True
        assert row["reimbursed_at"]

        db.set_status(record_id, status="reviewed")
        row = db.get(record_id)
        assert row["status"] == "reviewed"
        assert row["reimbursed"] is False
        db.close()

    def test_filters(self):
        db = _db("filters")
        first = _add(db)
        _add(db, invoice_no="INV-002", source_sha256="d" * 64, needs_review=True)
        db.set_status(first, checked=True, reimbursed=True)

        assert len(db.query(date(2026, 1, 1), date(2026, 1, 31), only_unchecked=True)) == 1
        assert len(db.query(date(2026, 1, 1), date(2026, 1, 31), only_unreimbursed=True)) == 1
        assert len(db.query(date(2026, 1, 1), date(2026, 1, 31), needs_review=True)) == 1
        assert len(db.query(date(2026, 1, 1), date(2026, 1, 31), vendor="テスト")) == 2
        db.close()


OLD_SCHEMA = """
CREATE TABLE invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_no TEXT COLLATE NOCASE,
    invoice_date TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    vendor TEXT,
    items_json TEXT,
    tax_minor INTEGER,
    image_path TEXT,
    source_msg_id TEXT,
    source_path TEXT,
    source_sha256 TEXT,
    ai_model TEXT,
    ai_confidence REAL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    checked INTEGER NOT NULL DEFAULT 0,
    reimbursed INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT,
    reimbursed_at TEXT,
    notes TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_invoices_source ON invoices(source_sha256);
"""


class TestCLI:
    def _seed(self, name: str) -> InvoiceDB:
        db = _db(name)
        _add(db)
        _add(
            db,
            invoice_no="INV-002",
            invoice_date=date(2026, 2, 10),
            amount_minor=1200,
            source_sha256="e" * 64,
        )
        return db

    def test_migrates_old_single_invoice_database(self):
        folder = ROOT / "data" / "tests" / "invoice_db" / "migrate"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "old.sqlite3"

        connection = sqlite3.connect(path)
        connection.executescript(OLD_SCHEMA)
        connection.execute(
            """
            INSERT INTO invoices (invoice_no, invoice_date, amount_minor, currency, vendor,
                                  source_sha256, created_at)
            VALUES ("OLD-1", "2025-12-01", 100, "JPY", "Old Shop", "oldsha",
                    "2025-12-01T00:00:00+00:00")
            """
        )
        connection.commit()
        connection.close()

        db = InvoiceDB(path)
        db.init()
        migrated = db.get(1)
        assert migrated is not None
        assert migrated["segment_sha256"] == "oldsha"
        assert migrated["segment_index"] == 0
        assert migrated["status"] == "reviewed"
        assert migrated["source"] == "invoice"

        # A multi-invoice photo reuses source_sha256 across segments.
        record_id = db.add(
            invoice_no="NEW-1",
            invoice_date=date(2026, 1, 1),
            amount_minor=200,
            currency="JPY",
            vendor="New Shop",
            items=[],
            tax_minor=None,
            image_path="",
            source_msg_id="m1",
            source_path="",
            source_sha256="oldsha",
            segment_index=1,
            segment_sha256="newseg",
            ai_model="test",
            ai_confidence=1.0,
            needs_review=False,
            raw={},
        )
        assert record_id == 2
        assert db.stats()["total"] == 2
        db.close()

    def test_query_output(self, capsys):
        db = self._seed("cli_query")
        db.close()
        code = cli_main(
            ["--db", str(db.path), "query", "--from", "2026-01-01", "--to", "2026-01-31"]
        )
        output = capsys.readouterr().out
        assert code == 0
        assert "INV-001" in output
        assert "3,280 JPY" in output
        assert "合计" in output
        assert "INV-002" not in output

    def test_total_json(self, capsys):
        db = self._seed("cli_total")
        db.close()
        code = cli_main(
            ["--db", str(db.path), "total", "--from", "2026", "--to", "2026", "--json"]
        )
        output = json.loads(capsys.readouterr().out)
        assert code == 0
        assert output["totals"][0]["total_minor"] == 4480
        assert output["count"] == 2

    def test_mark_and_show(self, capsys):
        db = self._seed("cli_mark")
        record_id = db.query(date(2026, 1, 1), date(2026, 1, 31))[0]["id"]
        db.close()

        code = cli_main(["--db", str(db.path), "mark", str(record_id), "--checked"])
        assert code == 0
        assert "状态=已校对" in capsys.readouterr().out

        code = cli_main(["--db", str(db.path), "show", str(record_id)])
        shown = json.loads(capsys.readouterr().out)
        assert shown["checked"] is True

    def test_export_csv(self, capsys, tmp_path=None):
        db = self._seed("cli_export")
        db.close()
        output = ROOT / "data" / "tests" / "invoice_db" / "cli_export" / "out.csv"
        code = cli_main(
            ["--db", str(db.path), "export", "--from", "2026", "--to", "2026", "--output", str(output)]
        )
        assert code == 0
        text = output.read_text(encoding="utf-8-sig")
        assert "INV-001" in text
        assert "4,480" not in text  # amounts are per row
        assert "3,280 JPY" in text
