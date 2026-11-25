"""Text-message command execution tests."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from app.invoices.db import InvoiceDB
from app.processor import Job
from app.textmsg_processor import TextMessageProcessor
from tests.helpers import ROOT, make_settings


class FakeOllama:
    model = "fake-model"

    def __init__(self, command: dict, reply: str = "处理完成") -> None:
        self.command = command
        self.reply = reply

    def generate_json(self, system_prompt: str, prompt: str) -> dict:
        return self.command

    def generate_text(self, system_prompt: str, prompt: str) -> str:
        return self.reply


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def reply(self, to_user: str, text: str) -> None:
        self.messages.append((to_user, text))


def _db(name: str) -> InvoiceDB:
    folder = ROOT / "data" / "tests" / "textmsg" / name
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    db = InvoiceDB(folder / "invoices.sqlite3")
    db.init()
    return db


def _job(content: str = "查询未报销的记录") -> Job:
    data = {
        "schema": "wecom.receiver.job/v1",
        "msg_id": "text-1",
        "msg_type": "text",
        "from_user": "u1",
        "to_user": "corp",
        "agent_id": "1",
        "created_at": None,
        "received_at": "2026-11-25T12:00:00+09:00",
        "content": content,
        "file": {"path": None, "name": None, "size": None, "sha256": None, "content_type": None},
        "raw": {},
    }
    return Job(data, Path("data/jobs/text-1.json"))


def _seed(db: InvoiceDB) -> None:
    db.add(
        invoice_no="INV-001",
        invoice_date=date(2026, 11, 18),
        amount_minor=3280,
        currency="JPY",
        vendor="测试商店",
        items=[{"name": "午餐"}],
        tax_minor=298,
        image_path="/tmp/inv-001.jpg",
        source_msg_id="m1",
        source_path="",
        source_sha256="a" * 64,
        segment_index=0,
        segment_sha256="b" * 64,
        ai_model="qwen",
        ai_confidence=0.9,
        needs_review=False,
        raw={},
    )
    db.add_manual(
        expense_date=date(2026, 11, 20),
        amount_minor=1200,
        currency="JPY",
        payee="JR",
        category="交通",
        description="A站到B站",
        source_msg_id="m2",
    )


def test_query_unpaid_returns_invoice_and_manual():
    db = _db("query")
    _seed(db)
    command = {
        "action": "query_invoices",
        "confidence": 0.95,
        "arguments": {"filters": {"status": "unpaid"}, "limit": 10},
    }
    processor = TextMessageProcessor(make_settings("textmsg_query"), db, FakeOllama(command))
    outcome = processor.process(_job())
    assert outcome["result"]["kind"] == "list"
    assert outcome["result"]["count"] == 2
    assert {row["source"] for row in outcome["result"]["rows"]} == {"invoice", "manual"}
    db.close()


def test_manual_expense_creates_reimbursement_record():
    db = _db("manual")
    command = {
        "action": "create_manual_expense",
        "confidence": 0.95,
        "arguments": {
            "expense_date": "2026-11-21",
            "amount_minor": 480,
            "currency": "JPY",
            "payee": "地铁",
            "category": "交通",
            "description": "A站到B站",
            "reimbursed": False,
        },
    }
    notifier = FakeNotifier()
    processor = TextMessageProcessor(
        make_settings("textmsg_manual"), db, FakeOllama(command, "已记录"), notifier
    )
    outcome = processor.process(_job("报销电车费 480 日元"))
    assert outcome["result"]["affected"] == 1
    assert notifier.messages[0][0] == "u1"
    rows = db.query(date(2026, 11, 1), date(2026, 11, 30))
    assert len(rows) == 1
    assert rows[0]["source"] == "manual"
    assert rows[0]["status"] == "reviewed"
    assert rows[0]["amount_minor"] == 480
    assert rows[0]["image_path"] in (None, "")
    db.close()


def test_batch_mark_week_as_reimbursed():
    db = _db("batch")
    _seed(db)
    command = {
        "action": "mark_invoices",
        "confidence": 0.95,
        "arguments": {
            "status": "reimbursed",
            "filters": {"date_from": "2026-11-17", "date_to": "2026-11-23"},
        },
    }
    processor = TextMessageProcessor(make_settings("textmsg_batch"), db, FakeOllama(command))
    outcome = processor.process(_job("把这周的发票设为已报销"))
    assert outcome["result"]["affected"] == 2
    rows = db.query(date(2026, 11, 1), date(2026, 11, 30))
    assert all(row["status"] == "reimbursed" for row in rows)
    db.close()


def test_query_invoice_returns_image_path_for_manual_check():
    db = _db("detail")
    _seed(db)
    command = {
        "action": "query_invoices",
        "confidence": 0.95,
        "arguments": {"filters": {"invoice_no": "INV-001"}, "limit": 5},
    }
    processor = TextMessageProcessor(make_settings("textmsg_detail"), db, FakeOllama(command))
    outcome = processor.process(_job("检查 INV-001 的发票"))
    assert outcome["result"]["rows"][0]["image_path"] == "/tmp/inv-001.jpg"
    db.close()


def test_missing_manual_amount_asks_for_clarification():
    db = _db("clarify")
    command = {
        "action": "create_manual_expense",
        "confidence": 0.9,
        "arguments": {"description": "电车费", "currency": "JPY"},
    }
    processor = TextMessageProcessor(make_settings("textmsg_clarify"), db, FakeOllama(command))
    outcome = processor.process(_job("报销电车费"))
    assert outcome["result"]["kind"] == "clarification"
    assert "金额" in outcome["result"]["message"]
    db.close()


def test_batch_mark_without_range_asks_for_range():
    db = _db("range")
    _seed(db)
    command = {
        "action": "mark_invoices",
        "confidence": 0.9,
        "arguments": {"status": "reimbursed", "filters": {}},
    }
    processor = TextMessageProcessor(make_settings("textmsg_range"), db, FakeOllama(command))
    outcome = processor.process(_job("把所有发票设为已报销"))
    assert outcome["result"]["kind"] == "clarification"
    assert "哪段时间" in outcome["result"]["message"]
    db.close()


def test_help_action_falls_back_to_manual_rule():
    db = _db("fallback")
    command = {
        "action": "help",
        "confidence": 0.9,
        "arguments": {},
    }
    processor = TextMessageProcessor(
        make_settings("textmsg_fallback"),
        db,
        FakeOllama(command, "已记录"),
    )
    outcome = processor.process(_job("报销11月21日坐电车从A站到B站，480日元"))
    assert outcome["action"] == "create_manual_expense"
    assert outcome["result"]["affected"] == 1
    rows = db.query(date(2026, 11, 1), date(2026, 11, 30))
    assert rows[0]["amount_minor"] == 480
    assert rows[0]["source"] == "manual"
    db.close()
