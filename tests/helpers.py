"""Shared test helpers (mock WeCom API, settings factory, callback builder)."""

from __future__ import annotations

import base64
import shutil
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.crypto import WeComCrypto

ROOT = Path(__file__).resolve().parents[1]

CORP_ID = "wwtestcorp0001"
AGENT_ID = "1000002"
TOKEN = "test-callback-token"
AES_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii").rstrip("=")

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
FILE_BYTES = "季度报表\nrevenue: 42\n".encode("utf-8")
FILE_NAME = "2026Q1-报表.txt"


def make_settings(name: str, **overrides: Any) -> Settings:
    data_dir = ROOT / "data" / "tests" / name
    shutil.rmtree(data_dir, ignore_errors=True)
    params: dict[str, Any] = {
        "corp_id": CORP_ID,
        "agent_id": AGENT_ID,
        "app_secret": "test-secret",
        "callback_token": TOKEN,
        "callback_aes_key": AES_KEY,
        "data_dir": data_dir,
        "receipt_dir": data_dir / "receipt",
        "homework_dir": data_dir / "homework",
        "other_dir": data_dir / "other",
        "invoice_db": data_dir / "invoices.sqlite3",
        "worker_enabled": False,
        "processor": "noop",
        "auto_reply": "已收到",
        "log_level": "WARNING",
        "max_attempts": 3,
    }
    params.update(overrides)
    return Settings(**params)


def make_crypto() -> WeComCrypto:
    return WeComCrypto(TOKEN, AES_KEY, CORP_ID)


def receipt_image_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (820, 1120), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([40, 40, 780, 120], fill=(20, 20, 20))
    for index in range(10):
        draw.rectangle([70, 200 + index * 70, 750, 230 + index * 70], fill=(60, 60, 60))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def ollama_transport(
    category: str = "receipt",
    extraction: dict[str, Any] | None = None,
    extractions: list[dict[str, Any]] | None = None,
):
    """Mock Ollama: answers classification and invoice extraction."""
    default_payload = extraction or {
        "document_type": "receipt",
        "invoice_no": "INV-2026-001",
        "date": "2026-01-15",
        "vendor": "テスト商店",
        "currency": "JPY",
        "total": 3280,
        "tax": 298,
        "items": [{"name": "ノート", "qty": 2, "amount": 1200}],
        "rotation": 0,
        "confidence": 0.93,
    }
    payloads = extractions or [default_payload]
    calls = {"classify": 0, "extract": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5vl:7b"}]})
        if request.url.path == "/api/chat":
            body = _json.loads(request.content)
            prompt = body["messages"][-1]["content"]
            if "Categories:" in prompt:
                calls["classify"] += 1
                answer = {"category": category, "confidence": 0.96, "reason": "测试分类"}
            else:
                calls["extract"] += 1
                answer = payloads[min(calls["extract"] - 1, len(payloads) - 1)]
            return httpx.Response(
                200, json={"message": {"role": "assistant", "content": _json.dumps(answer, ensure_ascii=False)}}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


def two_invoice_image_bytes() -> bytes:
    """Two receipts side by side on a dark desk (multi-invoice input)."""
    from io import BytesIO

    from PIL import Image, ImageDraw

    def page(title: str) -> Image.Image:
        image = Image.new("RGB", (700, 1000), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle([30, 30, 670, 100], fill=(10, 10, 10))
        for index in range(10):
            draw.rectangle([50, 160 + index * 70, 650, 190 + index * 70], fill=(50, 50, 50))
        draw.text((40, 120), title, fill=(0, 0, 0))
        return image

    canvas = Image.new("RGB", (1600, 1200), (25, 30, 35))
    canvas.paste(page("LEFT"), (80, 90))
    canvas.paste(page("RIGHT"), (820, 90))
    buffer = BytesIO()
    canvas.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def media_transport(
    extra: dict[str, tuple[bytes, str | None, str]] | None = None,
) -> tuple[httpx.MockTransport, dict[str, int]]:
    """Mock of the two WeCom endpoints this service uses."""
    state = {"token_calls": 0, "media_calls": 0, "fail_media": 0, "oversized": 0}
    extra = extra or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            state["token_calls"] += 1
            return httpx.Response(
                200, json={"errcode": 0, "errmsg": "ok", "access_token": "mock-token", "expires_in": 7200}
            )

        if request.url.path == "/cgi-bin/media/get":
            state["media_calls"] += 1
            if state["fail_media"] > 0:
                state["fail_media"] -= 1
                return httpx.Response(500, json={"errcode": -1, "errmsg": "temporary"})
            media_id = request.url.params.get("media_id")
            if media_id in extra:
                content, filename, content_type = extra[media_id]
                headers = {"Content-Type": content_type}
                if filename:
                    headers["Content-Disposition"] = f'attachment; filename="{filename}"'
                return httpx.Response(200, content=content, headers=headers)
            if media_id == "IMG_1":
                return httpx.Response(
                    200,
                    content=PNG_BYTES,
                    headers={
                        "Content-Type": "image/png",
                        "Content-Disposition": 'attachment; filename="screenshot.png"',
                    },
                )
            if media_id == "FILE_1":
                return httpx.Response(
                    200,
                    content=FILE_BYTES,
                    headers={
                        "Content-Type": "text/plain",
                        "Content-Disposition": "attachment; filename*=UTF-8''2026Q1-%E6%8A%A5%E8%A1%A8.txt",
                    },
                )
            if media_id == "BIG_1":
                return httpx.Response(
                    200,
                    content=b"x" * (2 * 1024 * 1024),
                    headers={"Content-Type": "application/octet-stream", "Content-Disposition": 'attachment; filename="big.bin"'},
                )
            if media_id == "EVIL_1":
                return httpx.Response(
                    200,
                    content=b"evil",
                    headers={"Content-Type": "application/octet-stream", "Content-Disposition": 'attachment; filename="../../evil.bin"'},
                )
            if media_id == "NO_PERMISSION":
                return httpx.Response(200, json={"errcode": 48001, "errmsg": "api unauthorized"})
            return httpx.Response(200, json={"errcode": 40007, "errmsg": "invalid media_id"})

        return httpx.Response(404)

    return httpx.MockTransport(handler), state


def post_encrypted(
    client: httpx.Client,
    crypto: WeComCrypto,
    inner_xml: str,
    *,
    timestamp: str = "1700000000",
    nonce: str = "nonce12345",
    signature: str | None = None,
) -> httpx.Response:
    encrypt = crypto.encrypt(inner_xml)
    body = (
        "<xml>"
        f"<ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
        f"<AgentID><![CDATA[{AGENT_ID}]]></AgentID>"
        "</xml>"
    )
    sig = signature or crypto.signature(timestamp, nonce, encrypt)
    return client.post(
        "/wecom/callback",
        params={"msg_signature": sig, "timestamp": timestamp, "nonce": nonce},
        content=body,
        headers={"Content-Type": "text/xml"},
    )


def image_message(msg_id: str = "1001", media_id: str = "IMG_1") -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
        "<CreateTime>1700000000</CreateTime>"
        "<MsgType><![CDATA[image]]></MsgType>"
        f"<PicUrl><![CDATA[https://example.com/pic/{media_id}.png]]></PicUrl>"
        f"<MediaId><![CDATA[{media_id}]]></MediaId>"
        f"<MsgId>{msg_id}</MsgId>"
        f"<AgentID>{AGENT_ID}</AgentID>"
        "</xml>"
    )


def file_message(msg_id: str = "2001", media_id: str = "FILE_1") -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        "<FromUserName><![CDATA[lisi]]></FromUserName>"
        "<CreateTime>1700000000</CreateTime>"
        "<MsgType><![CDATA[file]]></MsgType>"
        f"<MediaId><![CDATA[{media_id}]]></MediaId>"
        "<FileName><![CDATA[原始文件名.txt]]></FileName>"
        "<FileSize>1234</FileSize>"
        f"<MsgId>{msg_id}</MsgId>"
        f"<AgentID>{AGENT_ID}</AgentID>"
        "</xml>"
    )


def text_message(msg_id: str = "3001") -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        "<FromUserName><![CDATA[wangwu]]></FromUserName>"
        "<CreateTime>1700000000</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[hello]]></Content>"
        f"<MsgId>{msg_id}</MsgId>"
        f"<AgentID>{AGENT_ID}</AgentID>"
        "</xml>"
    )
