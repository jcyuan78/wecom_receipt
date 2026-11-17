"""End-to-end auto pipeline: WeCom callback -> classify -> segment -> DB."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from app.invoices.db import InvoiceDB
from app.main import create_app
from tests.helpers import (
    image_message,
    make_settings,
    media_transport,
    ollama_transport,
    post_encrypted,
    receipt_image_bytes,
    two_invoice_image_bytes,
)


def _auto_settings(name: str, **overrides):
    params = {
        "processor": "auto",
        "ollama_model": "qwen2.5vl:7b",
        "invoice_min_confidence": 0.5,
    }
    params.update(overrides)
    return make_settings(name, **params)


def _open_db(settings) -> InvoiceDB:
    db = InvoiceDB(settings.invoice_db_path)
    db.init()
    return db


def test_single_receipt_is_routed_extracted_and_stored():
    settings = _auto_settings("auto_receipt")
    media = {"RECEIPT_1": (receipt_image_bytes(), "receipt.jpg", "image/jpeg")}
    transport, _ = media_transport(media)
    ollama, calls = ollama_transport("receipt")
    app = create_app(settings, transport=transport, ollama_transport=ollama)

    with TestClient(app) as client:
        ctx = app.state.ctx
        response = post_encrypted(client, ctx.crypto, image_message("5001", "RECEIPT_1"))
        assert response.status_code == 200
        assert ctx.worker.process_once() == 1

        row = ctx.store.get("5001")
        assert row["status"] == "done"
        assert calls == {"classify": 1, "extract": 1}

        db = _open_db(settings)
        records = db.query(date(2026, 1, 1), date(2026, 12, 31))
        assert len(records) == 1
        record = records[0]
        assert record["invoice_no"] == "INV-2026-001"
        assert record["invoice_date"] == "2026-01-15"
        assert record["amount_minor"] == 3280
        assert record["currency"] == "JPY"
        assert record["vendor"] == "テスト商店"
        assert record["checked"] is False
        assert record["reimbursed"] is False
        assert record["needs_review"] is False
        assert record["source_msg_id"] == "5001"
        assert record["segment_index"] == 0

        image_path = Path(record["image_path"])
        assert image_path.exists()
        assert image_path.is_relative_to(settings.receipt_dir)
        assert image_path.suffix == ".jpg"

        sidecar = json.loads(image_path.with_suffix(".json").read_text(encoding="utf-8"))
        assert sidecar["fields"]["invoice_no"] == "INV-2026-001"
        assert sidecar["invoice_id"] == record["id"]

        originals = list((settings.receipt_dir / "_originals").iterdir())
        assert len(originals) == 1

        job = json.loads((settings.jobs_dir / "5001.json").read_text(encoding="utf-8"))
        processing = job["processing"]
        assert processing["category"] == "receipt"
        assert processing["segments"] == 1
        assert processing["invoice_count"] == 1
        assert processing["invoices"][0]["invoice_id"] == record["id"]
        assert job["file"]["path"] == str(image_path)
        assert not Path(row["file_path"]).exists(), "inbox 原文件应已移走"

        db.close()


def test_multiple_invoices_are_split_and_stored_separately():
    settings = _auto_settings("auto_multi")
    media = {"MULTI_1": (two_invoice_image_bytes(), "two-invoices.jpg", "image/jpeg")}
    transport, _ = media_transport(media)
    first = {
        "invoice_no": "INV-A-001",
        "date": "2026-01-10",
        "vendor": "A商店",
        "currency": "JPY",
        "total": 1000,
        "items": [{"name": "A"}],
        "rotation": 0,
        "confidence": 0.95,
    }
    second = {
        "invoice_no": "INV-B-002",
        "date": "2026-01-11",
        "vendor": "B商店",
        "currency": "JPY",
        "total": 2000,
        "items": [{"name": "B"}],
        "rotation": 0,
        "confidence": 0.95,
    }
    ollama, calls = ollama_transport("receipt", extractions=[first, second])
    app = create_app(settings, transport=transport, ollama_transport=ollama)

    with TestClient(app) as client:
        ctx = app.state.ctx
        post_encrypted(client, ctx.crypto, image_message("5401", "MULTI_1"))
        assert ctx.worker.process_once() == 1

        db = _open_db(settings)
        records = db.query(date(2026, 1, 1), date(2026, 12, 31))
        assert len(records) == 2
        assert [record["invoice_no"] for record in records] == ["INV-A-001", "INV-B-002"]
        assert [record["segment_index"] for record in records] == [0, 1]
        assert records[0]["source_msg_id"] == records[1]["source_msg_id"] == "5401"
        assert records[0]["source_sha256"] == records[1]["source_sha256"]

        paths = [Path(record["image_path"]) for record in records]
        assert paths[0] != paths[1]
        assert all(path.exists() for path in paths)
        assert "_p1" in paths[0].stem and "_p2" in paths[1].stem
        assert calls["extract"] == 2

        job = json.loads((settings.jobs_dir / "5401.json").read_text(encoding="utf-8"))
        processing = job["processing"]
        assert processing["segments"] == 2
        assert processing["invoice_count"] == 2
        assert len(processing["invoices"]) == 2
        assert job["file"]["path"] == str(paths[0])

        # One original photo, two cropped invoices.
        assert len(list((settings.receipt_dir / "_originals").iterdir())) == 1

        db.close()


def test_duplicate_invoice_number_inside_one_photo():
    settings = _auto_settings("auto_multi_dupe")
    media = {"MULTI_1": (two_invoice_image_bytes(), "two-invoices.jpg", "image/jpeg")}
    transport, _ = media_transport(media)
    payload = {
        "invoice_no": "INV-SAME",
        "date": "2026-01-10",
        "vendor": "A商店",
        "currency": "JPY",
        "total": 1000,
        "items": [],
        "rotation": 0,
        "confidence": 0.95,
    }
    ollama, _ = ollama_transport("receipt", extraction=payload)
    app = create_app(settings, transport=transport, ollama_transport=ollama)

    with TestClient(app) as client:
        ctx = app.state.ctx
        post_encrypted(client, ctx.crypto, image_message("5501", "MULTI_1"))
        ctx.worker.process_once()

        db = _open_db(settings)
        assert len(db.query(date(2026, 1, 1), date(2026, 12, 31))) == 1
        db.close()

        job = json.loads((settings.jobs_dir / "5501.json").read_text(encoding="utf-8"))
        assert job["processing"]["invoice_count"] == 1
        assert len(job["processing"]["duplicates"]) == 1
        assert job["processing"]["duplicates"][0]["duplicate_of"] == 1


def test_duplicate_message_is_moved_aside():
    settings = _auto_settings("auto_duplicate")
    media = {"RECEIPT_1": (receipt_image_bytes(), "receipt.jpg", "image/jpeg")}
    transport, _ = media_transport(media)
    ollama, calls = ollama_transport("receipt")
    app = create_app(settings, transport=transport, ollama_transport=ollama)

    with TestClient(app) as client:
        ctx = app.state.ctx
        post_encrypted(client, ctx.crypto, image_message("5101", "RECEIPT_1"))
        post_encrypted(client, ctx.crypto, image_message("5102", "RECEIPT_1"))
        assert ctx.worker.process_once() == 2

        db = _open_db(settings)
        assert len(db.query(date(2026, 1, 1), date(2026, 12, 31))) == 1

        job = json.loads((settings.jobs_dir / "5102.json").read_text(encoding="utf-8"))
        processing = job["processing"]
        assert processing["invoice_count"] == 0
        assert len(processing["duplicates"]) == 1
        assert processing["duplicates"][0]["duplicate_of"] == 1
        moved = Path(processing["duplicates"][0]["moved_to"])
        assert moved.exists()
        assert moved.is_relative_to(settings.receipt_dir / "_duplicates")

        # The duplicate costs a classification call, but no extraction.
        assert calls["extract"] == 1
        db.close()


def test_homework_is_routed_without_invoice_row():
    settings = _auto_settings("auto_homework")
    media = {"HW_1": (receipt_image_bytes(), "homework.jpg", "image/jpeg")}
    transport, _ = media_transport(media)
    ollama, calls = ollama_transport("homework")
    app = create_app(settings, transport=transport, ollama_transport=ollama)

    with TestClient(app) as client:
        ctx = app.state.ctx
        post_encrypted(client, ctx.crypto, image_message("5201", "HW_1"))
        assert ctx.worker.process_once() == 1

        row = ctx.store.get("5201")
        assert row["status"] == "done"

        files = [path for path in settings.homework_dir.rglob("*") if path.is_file()]
        assert len(files) == 1
        assert files[0].suffix == ".jpg"

        job = json.loads((settings.jobs_dir / "5201.json").read_text(encoding="utf-8"))
        assert job["processing"]["category"] == "homework"
        assert job["file"]["path"] == str(files[0])

        db = _open_db(settings)
        assert db.query(date(2026, 1, 1), date(2026, 12, 31)) == []
        assert calls["extract"] == 0
        db.close()


def test_low_confidence_marks_needs_review():
    settings = _auto_settings("auto_review", invoice_min_confidence=0.99)
    media = {"RECEIPT_1": (receipt_image_bytes(), "receipt.jpg", "image/jpeg")}
    transport, _ = media_transport(media)
    ollama, _ = ollama_transport("receipt")
    app = create_app(settings, transport=transport, ollama_transport=ollama)

    with TestClient(app) as client:
        ctx = app.state.ctx
        post_encrypted(client, ctx.crypto, image_message("5301", "RECEIPT_1"))
        ctx.worker.process_once()

        db = _open_db(settings)
        record = db.query(date(2026, 1, 1), date(2026, 12, 31))[0]
        assert record["needs_review"] is True
        db.close()
