"""End-to-end tests: encrypted callback -> download -> job -> processor."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import xmlmsg
from app.main import create_app

from tests.helpers import (
    FILE_BYTES,
    FILE_NAME,
    PNG_BYTES,
    file_message,
    image_message,
    make_crypto,
    make_settings,
    media_transport,
    post_encrypted,
    text_message,
)


def test_url_verification():
    settings = make_settings("verify")
    app = create_app(settings, transport=media_transport()[0])
    crypto = make_crypto()
    echostr = crypto.encrypt("verify-ok")
    sig = crypto.signature("1700000000", "n1", echostr)

    with TestClient(app) as client:
        response = client.get(
            "/wecom/callback",
            params={"msg_signature": sig, "timestamp": "1700000000", "nonce": "n1", "echostr": echostr},
        )
    assert response.status_code == 200
    assert response.text == "verify-ok"


def test_url_verification_rejects_bad_signature():
    app = create_app(make_settings("verify_bad"), transport=media_transport()[0])
    crypto = make_crypto()
    echostr = crypto.encrypt("verify-ok")
    with TestClient(app) as client:
        response = client.get(
            "/wecom/callback",
            params={"msg_signature": "0" * 40, "timestamp": "1700000000", "nonce": "n1", "echostr": echostr},
        )
    assert response.status_code == 403


def test_image_message_end_to_end():
    settings = make_settings("e2e_image")
    transport, state = media_transport()
    app = create_app(settings, transport=transport)

    with TestClient(app) as client:
        ctx = app.state.ctx
        response = post_encrypted(client, ctx.crypto, image_message("1001"))

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/xml")

        # The passive reply is encrypted; it must decrypt to our auto-reply text.
        envelope = xmlmsg._parse_fields(response.text)
        reply_xml = ctx.crypto.decrypt(envelope["Encrypt"])
        assert "已收到" in reply_xml
        assert "<ToUserName><![CDATA[zhangsan]]></ToUserName>" in reply_xml

        # Queued immediately, downloaded by the worker.
        assert ctx.store.stats()["pending"] == 1
        assert ctx.worker.process_once() == 1

        row = ctx.store.get("1001")
        assert row["status"] == "done"
        assert row["filename"] == "screenshot.png"
        path = Path(row["file_path"])
        assert path.read_bytes() == PNG_BYTES
        assert path.is_relative_to(settings.inbox_dir)

        job = json.loads((settings.jobs_dir / "1001.json").read_text(encoding="utf-8"))
        assert job["schema"] == "wecom.receiver.job/v1"
        assert job["msg_type"] == "image"
        assert job["from_user"] == "zhangsan"
        assert job["file"]["path"] == str(path)
        assert job["file"]["sha256"] == row["sha256"]
        assert job["raw"]["MediaId"] == "IMG_1"

        meta = json.loads((path.parent / "meta.json").read_text(encoding="utf-8"))
        assert meta["msg_id"] == "1001"
        assert meta["sha256"] == row["sha256"]

        assert state["token_calls"] == 1
        assert state["media_calls"] == 1


def test_file_message_keeps_server_filename():
    settings = make_settings("e2e_file")
    app = create_app(settings, transport=media_transport()[0])

    with TestClient(app) as client:
        ctx = app.state.ctx
        response = post_encrypted(client, ctx.crypto, file_message("2001"))
        assert response.status_code == 200

        assert ctx.worker.process_once() == 1
        row = ctx.store.get("2001")
        assert row["status"] == "done"
        assert row["filename"] == FILE_NAME
        assert Path(row["file_path"]).read_bytes() == FILE_BYTES


def test_duplicate_callback_is_idempotent():
    settings = make_settings("e2e_dupe")
    app = create_app(settings, transport=media_transport()[0])
    with TestClient(app) as client:
        ctx = app.state.ctx
        first = post_encrypted(client, ctx.crypto, image_message("1002"))
        second = post_encrypted(client, ctx.crypto, image_message("1002"))
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.text == ""
        assert ctx.store.stats()["total"] == 1


def test_bad_signature_is_rejected():
    settings = make_settings("e2e_badsig")
    app = create_app(settings, transport=media_transport()[0])
    with TestClient(app) as client:
        ctx = app.state.ctx
        response = post_encrypted(client, ctx.crypto, image_message("1003"), signature="0" * 40)
        assert response.status_code == 403
        assert ctx.store.stats().get("total", 0) == 0


def test_text_message_is_ignored():
    settings = make_settings("e2e_text")
    app = create_app(settings, transport=media_transport()[0])
    with TestClient(app) as client:
        ctx = app.state.ctx
        response = post_encrypted(client, ctx.crypto, text_message("3001"))
        assert response.status_code == 200
        assert response.text == ""
        assert ctx.store.stats() == {"ignored": 1, "total": 1}
        assert ctx.worker.process_once() == 0


def test_oversized_media_fails_permanently():
    settings = make_settings("e2e_big", max_file_mb=1)
    app = create_app(settings, transport=media_transport()[0])
    with TestClient(app) as client:
        ctx = app.state.ctx
        post_encrypted(client, ctx.crypto, image_message("1004", media_id="BIG_1"))
        assert ctx.worker.process_once() == 1
        row = ctx.store.get("1004")
        assert row["status"] == "failed"
        assert "大小上限" in row["error"]


def test_path_traversal_filename_is_contained():
    settings = make_settings("e2e_evil")
    app = create_app(settings, transport=media_transport()[0])
    with TestClient(app) as client:
        ctx = app.state.ctx
        post_encrypted(client, ctx.crypto, image_message("1005", media_id="EVIL_1"))
        ctx.worker.process_once()
        row = ctx.store.get("1005")
        assert row["status"] == "done"
        path = Path(row["file_path"])
        assert path.parent.parent.parent.is_relative_to(settings.inbox_dir)
        assert ".." not in path.name


def test_api_error_is_retried_then_failed():
    settings = make_settings("e2e_retry", max_attempts=2)
    transport, state = media_transport()
    state["fail_media"] = 2
    app = create_app(settings, transport=transport)

    with TestClient(app) as client:
        ctx = app.state.ctx
        post_encrypted(client, ctx.crypto, image_message("1006", media_id="IMG_1"))
        assert ctx.worker.process_once() == 1
        row = ctx.store.get("1006")
        assert row["status"] == "pending"
        assert row["attempts"] == 1

        # Force the next attempt to be due immediately instead of waiting ~10s.
        ctx.store._conn.execute(
            "UPDATE messages SET next_attempt_at = '2000-01-01T00:00:00+00:00' WHERE msg_id = ?",
            ("1006",),
        )
        assert ctx.worker.process_once() == 1
        row = ctx.store.get("1006")
        assert row["status"] == "failed"
        assert row["attempts"] == 2
