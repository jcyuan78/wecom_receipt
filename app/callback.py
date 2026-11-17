"""FastAPI routes: WeCom URL verification + message callback + health."""

from __future__ import annotations

import hmac
import logging
import time
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from . import xmlmsg
from .client import WeComClient
from .config import Settings
from .crypto import WeComCrypto, WeComCryptoError
from .storage import FileStorage
from .store import MessageStore
from .worker import Worker

log = logging.getLogger(__name__)
router = APIRouter()


@dataclass(slots=True)
class AppContext:
    settings: Settings
    crypto: WeComCrypto
    store: MessageStore
    storage: FileStorage
    client: WeComClient
    worker: Worker


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


@router.get("/wecom/callback", response_class=PlainTextResponse)
def verify_callback_url(
    request: Request,
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
) -> PlainTextResponse:
    """Answer the URL-verification request sent when saving the callback config."""
    ctx = _ctx(request)
    if not echostr:
        raise HTTPException(status_code=400, detail="缺少 echostr 参数")
    try:
        if not ctx.crypto.verify_signature(msg_signature, timestamp, nonce, echostr):
            raise HTTPException(status_code=403, detail="签名校验失败")
        plain = ctx.crypto.decrypt(echostr)
    except WeComCryptoError as exc:
        log.warning("URL 验证失败: %s", exc)
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    log.info("URL 验证成功")
    return PlainTextResponse(plain)


@router.post("/wecom/callback")
async def receive_message(request: Request) -> Response:
    """Receive an encrypted message event.

    The handler only validates, decrypts and enqueues the message, then returns
    within milliseconds.  Downloading happens in the background worker because
    WeCom expects a response within 5 seconds.
    """
    ctx = _ctx(request)
    params = request.query_params
    body = (await request.body()).decode("utf-8", errors="replace")

    try:
        encrypt = xmlmsg.extract_encrypt(body)
    except xmlmsg.MessageParseError as exc:
        log.warning("无法解析回调报文: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not ctx.crypto.verify_signature(
        params.get("msg_signature", ""), params.get("timestamp", ""), params.get("nonce", ""), encrypt
    ):
        log.warning("回调签名校验失败（remote=%s）", request.client.host if request.client else "?")
        raise HTTPException(status_code=403, detail="签名校验失败")

    try:
        plain = ctx.crypto.decrypt(encrypt)
        message = xmlmsg.parse_message(plain)
    except (WeComCryptoError, xmlmsg.MessageParseError) as exc:
        log.warning("回调解密/解析失败: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        is_new = ctx.store.add_message(message, plain)
    except Exception as exc:  # noqa: BLE001 - returning 500 makes WeCom retry
        log.exception("写入消息队列失败")
        raise HTTPException(status_code=500, detail="内部错误") from exc

    if not is_new:
        log.info("重复消息已忽略 msg_id=%s", message.msg_id)
        return Response(status_code=200)

    log.info(
        "收到消息 type=%s from=%s msg_id=%s media_id=%s",
        message.msg_type,
        message.from_user,
        message.msg_id,
        message.media_id or "-",
    )

    if message.is_media and ctx.settings.auto_reply.strip():
        try:
            reply_xml = xmlmsg.build_text_reply_xml(
                to_user=message.from_user,
                from_user=message.to_user or ctx.settings.corp_id,
                content=ctx.settings.auto_reply,
                created_at=int(time.time()),
            )
            envelope = ctx.crypto.encrypted_envelope(reply_xml)
            return Response(content=envelope, media_type="application/xml")
        except Exception:  # noqa: BLE001 - the message is already queued
            log.exception("生成自动回复失败（消息已入队）")

    return Response(status_code=200)


@router.get("/healthz")
def healthz(request: Request) -> dict:
    ctx = _ctx(request)
    return {
        "status": "ok",
        "corp_id": ctx.settings.masked_corp_id(),
        "worker_alive": ctx.worker.is_alive(),
        "stats": ctx.store.stats(),
        "time": int(time.time()),
    }


@router.get("/api/messages")
def list_messages(request: Request, token: str = "", limit: int = 20) -> dict:
    """Small read-only debugging endpoint, protected by ``WECOM_ADMIN_TOKEN``."""
    ctx = _ctx(request)
    if not ctx.settings.admin_token or not hmac.compare_digest(token, ctx.settings.admin_token):
        raise HTTPException(status_code=403, detail="forbidden")
    limit = max(1, min(limit, 100))
    return {"items": ctx.store.recent(limit), "stats": ctx.store.stats()}
