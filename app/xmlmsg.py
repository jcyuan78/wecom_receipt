"""Parsing and building of WeCom XML message payloads."""

from __future__ import annotations

import uuid

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring
from xml.etree.ElementTree import ParseError

from .crypto import cdata
from .models import Message


class MessageParseError(ValueError):
    """Raised when a callback body is not a valid WeCom message."""


def _parse_fields(xml_text: str) -> dict[str, str]:
    if not xml_text or not xml_text.strip():
        raise MessageParseError("XML 内容为空")
    try:
        root = fromstring(xml_text)
    except (ParseError, DefusedXmlException) as exc:
        raise MessageParseError(f"XML 解析失败: {exc}") from exc
    if root.tag != "xml":
        raise MessageParseError(f"根节点应为 <xml>，实际为 <{root.tag}>")

    fields: dict[str, str] = {}
    for child in root:
        if not isinstance(child.tag, str):
            continue
        fields[child.tag] = (child.text or "").strip()
    return fields


def extract_encrypt(xml_text: str) -> str:
    fields = _parse_fields(xml_text)
    encrypt = fields.get("Encrypt", "")
    if not encrypt:
        raise MessageParseError("回调数据缺少 <Encrypt> 字段")
    return encrypt


def _to_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_message(xml_text: str) -> Message:
    """Convert a decrypted callback XML document into a :class:`Message`."""
    fields = _parse_fields(xml_text)

    msg_type = fields.get("MsgType", "").lower()
    if not msg_type:
        raise MessageParseError("消息缺少 MsgType 字段")

    from_user = fields.get("FromUserName", "")
    if not from_user:
        raise MessageParseError("消息缺少 FromUserName 字段")

    msg_id = fields.get("MsgId", "")
    if not msg_id:
        # Events (and a few legacy payloads) have no MsgId; keep them unique so
        # the dedupe index still works.
        msg_id = f"auto-{uuid.uuid4().hex}"

    return Message(
        msg_id=msg_id,
        msg_type=msg_type,
        from_user=from_user,
        to_user=fields.get("ToUserName", ""),
        agent_id=fields.get("AgentID", ""),
        create_time=_to_int(fields.get("CreateTime")) or 0,
        media_id=fields.get("MediaId") or None,
        file_name=fields.get("FileName") or None,
        file_size=_to_int(fields.get("FileSize")),
        pic_url=fields.get("PicUrl") or None,
        media_format=fields.get("Format") or None,
        content=fields.get("Content"),
        raw=fields,
    )


def build_text_reply_xml(
    to_user: str, from_user: str, content: str, created_at: int
) -> str:
    """Build the *plaintext* passive-reply document (encrypt before sending)."""
    return (
        "<xml>"
        f"<ToUserName>{cdata(to_user)}</ToUserName>"
        f"<FromUserName>{cdata(from_user)}</FromUserName>"
        f"<CreateTime>{int(created_at)}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content>{cdata(content)}</Content>"
        "</xml>"
    )
