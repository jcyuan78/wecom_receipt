"""Unit tests for crypto, XML parsing, filenames and the store."""

from __future__ import annotations

import base64
import hashlib
import shutil

import pytest

from app import xmlmsg
from app.client import parse_content_disposition
from app.crypto import WeComCrypto, WeComCryptoError
from app.models import Message
from app.storage import safe_filename
from app.store import MessageStore

from tests.helpers import AES_KEY, CORP_ID, ROOT, TOKEN


class TestCrypto:
    def test_roundtrip(self):
        crypto = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
        plain = "<xml><Content><![CDATA[你好 WeCom]]></Content></xml>"
        encrypted = crypto.encrypt(plain)
        assert encrypted != plain
        assert crypto.decrypt(encrypted) == plain

    def test_roundtrip_unicode_and_empty(self):
        crypto = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
        for plain in ("", "a", "x" * 1000, "emoji 🚀 文件"):
            assert crypto.decrypt(crypto.encrypt(plain)) == plain

    def test_wrong_receive_id_is_rejected(self):
        sender = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
        receiver = WeComCrypto(TOKEN, AES_KEY, "wwothercorp")
        with pytest.raises(WeComCryptoError):
            receiver.decrypt(sender.encrypt("<xml/>"))

    def test_tampered_ciphertext_is_rejected(self):
        crypto = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
        blob = bytearray(base64.b64decode(crypto.encrypt("<xml/>") + "="))
        blob[0] ^= 0xFF
        with pytest.raises(WeComCryptoError):
            crypto.decrypt(base64.b64encode(bytes(blob)).decode())

    def test_signature(self):
        crypto = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
        sig = crypto.signature("1700000000", "abc", "cipher")
        assert sig == hashlib.sha1(
            "".join(sorted([TOKEN, "1700000000", "abc", "cipher"])).encode()
        ).hexdigest()
        assert crypto.verify_signature(sig.upper(), "1700000000", "abc", "cipher")
        assert not crypto.verify_signature("0" * 40, "1700000000", "abc", "cipher")

    def test_envelope_roundtrip(self):
        crypto = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
        envelope = crypto.encrypted_envelope("<xml><Content>hi</Content></xml>", timestamp=1700000000, nonce="n1")
        fields = xmlmsg._parse_fields(envelope)
        assert fields["TimeStamp"] == "1700000000"
        assert crypto.verify_signature(fields["MsgSignature"], fields["TimeStamp"], fields["Nonce"], fields["Encrypt"])
        assert crypto.decrypt(fields["Encrypt"]) == "<xml><Content>hi</Content></xml>"

    def test_invalid_aes_key(self):
        with pytest.raises(WeComCryptoError):
            WeComCrypto(TOKEN, "too-short", CORP_ID)
        with pytest.raises(WeComCryptoError):
            WeComCrypto(TOKEN, "!" * 43, CORP_ID)


class TestXml:
    def test_parse_image(self):
        message = xmlmsg.parse_message(
            "<xml><ToUserName><![CDATA[corp]]></ToUserName>"
            "<FromUserName><![CDATA[alice]]></FromUserName>"
            "<CreateTime>1700000000</CreateTime><MsgType><![CDATA[image]]></MsgType>"
            "<PicUrl><![CDATA[https://x/y.png]]></PicUrl>"
            "<MediaId><![CDATA[MEDIA]]></MediaId><MsgId>42</MsgId><AgentID>7</AgentID></xml>"
        )
        assert message.msg_id == "42"
        assert message.msg_type == "image"
        assert message.is_media
        assert message.fallback_filename == "image-42"

    def test_parse_file(self):
        message = xmlmsg.parse_message(
            "<xml><FromUserName>alice</FromUserName><MsgType>file</MsgType>"
            "<MediaId>M</MediaId><FileName>报表.xlsx</FileName><FileSize>1024</FileSize>"
            "<MsgId>43</MsgId></xml>"
        )
        assert message.file_name == "报表.xlsx"
        assert message.file_size == 1024
        assert message.is_media

    def test_parse_requires_msg_type(self):
        with pytest.raises(xmlmsg.MessageParseError):
            xmlmsg.parse_message("<xml><FromUserName>a</FromUserName></xml>")

    def test_dtd_is_rejected(self):
        with pytest.raises(xmlmsg.MessageParseError):
            xmlmsg.parse_message(
                '<?xml version="1.0"?><!DOCTYPE xml [<!ENTITY x SYSTEM "file:///c:/windows/win.ini">]>'
                "<xml><FromUserName>&x;</FromUserName><MsgType>text</MsgType></xml>"
            )

    def test_event_without_msgid_gets_unique_id(self):
        a = xmlmsg.parse_message("<xml><FromUserName>a</FromUserName><MsgType>event</MsgType><Event>enter_agent</Event></xml>")
        b = xmlmsg.parse_message("<xml><FromUserName>a</FromUserName><MsgType>event</MsgType><Event>enter_agent</Event></xml>")
        assert a.msg_id != b.msg_id
        assert not a.is_media

    def test_reply_xml_escapes_cdata_terminator(self):
        reply = xmlmsg.build_text_reply_xml("u", "corp", "a]]>b", 1)
        assert "]]]]><![CDATA[>" in reply


class TestFilenames:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("normal.png", "normal.png"),
            ("../../etc/passwd", "passwd"),
            ("..\\..\\win.ini", "win.ini"),
            ('a<b>c:"d|e?f*g.txt', "a_b_c__d_e_f_g.txt"),
            ("CON.txt", "_CON.txt"),
            ("", "fallback"),
            ("   ", "fallback"),
        ],
    )
    def test_safe_filename(self, raw, expected):
        assert safe_filename(raw, fallback="fallback") == expected

    def test_long_name_is_truncated(self):
        name = safe_filename("x" * 500 + ".png")
        assert len(name) <= 120
        assert name.endswith(".png")

    def test_parse_content_disposition_utf8(self):
        assert parse_content_disposition("attachment; filename*=UTF-8''%E6%8A%A5%E8%A1%A8.txt") == "报表.txt"

    def test_parse_content_disposition_plain(self):
        assert parse_content_disposition('attachment; filename="a b.txt"') == "a b.txt"
        assert parse_content_disposition(None) is None


class TestStore:
    @staticmethod
    def _db(name: str):
        path = ROOT / "data" / "tests" / "store" / name
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True, exist_ok=True)
        return path / "test.sqlite3"

    def test_dedupe_and_retry(self):
        store = MessageStore(self._db("dedupe"), max_attempts=2)
        store.init()
        message = Message(
            msg_id="1", msg_type="image", from_user="alice", media_id="M", create_time=1700000000
        )
        assert store.add_message(message, "<xml/>")
        assert not store.add_message(message, "<xml/>")
        assert store.stats() == {"pending": 1, "total": 1}

        claimed = store.claim_due()
        assert len(claimed) == 1
        assert claimed[0]["status"] == "working"

        store.mark_retry("1", "boom")
        row = store.get("1")
        assert row["status"] == "pending"
        assert row["attempts"] == 1
        assert row["next_attempt_at"]
        assert row["error"] == "boom"

        store.mark_downloaded(
            "1", filename="a.png", file_path="/tmp/a.png", file_size=3,
            content_type="image/png", sha256="abc",
        )
        row = store.get("1")
        assert row["status"] == "downloaded"
        assert row["attempts"] == 0

        # Processor failures retry the existing file, then become terminal.
        store.mark_retry("1", "again")
        row = store.get("1")
        assert row["status"] == "downloaded"
        assert row["attempts"] == 1
        assert row["next_attempt_at"]

        store.mark_retry("1", "again")
        row = store.get("1")
        assert row["status"] == "failed"
        assert row["attempts"] == 2
        store.close()

    def test_text_message_is_ignored(self):
        store = MessageStore(self._db("text"))
        store.init()
        store.add_message(Message(msg_id="t", msg_type="text", from_user="a", content="hi"), "<xml/>")
        assert store.stats() == {"ignored": 1, "total": 1}
        assert store.claim_due() == []
        store.close()
