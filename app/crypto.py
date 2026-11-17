"""Message encryption/decryption for the WeCom callback protocol.

WeCom (企业微信) encrypts callback payloads with AES-256-CBC:

* key      = base64_decode(EncodingAESKey + "=")   (32 bytes)
* iv       = first 16 bytes of the key
* plain    = random(16) | len(msg) as uint32 BE | msg | receive_id
* padding  = PKCS#7, block size 32
* signature= sha1(sort(token, timestamp, nonce, encrypt))

The same scheme is used for the ``echostr`` URL verification and for
encrypted passive replies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BLOCK_SIZE = 32


class WeComCryptoError(ValueError):
    """Raised when a payload cannot be decrypted or verified."""


def parse_encoding_aes_key(encoding_aes_key: str) -> bytes:
    key = (encoding_aes_key or "").strip()
    if len(key) != 43:
        raise WeComCryptoError("EncodingAESKey 必须是 43 位字符")
    try:
        raw = base64.b64decode(key + "=", validate=True)
    except Exception as exc:  # noqa: BLE001 - normalize to our own error
        raise WeComCryptoError("EncodingAESKey 不是合法的 Base64") from exc
    if len(raw) != 32:
        raise WeComCryptoError("EncodingAESKey 解码后必须是 32 字节")
    return raw


def _pkcs7_pad(data: bytes) -> bytes:
    pad = BLOCK_SIZE - len(data) % BLOCK_SIZE
    return data + bytes((pad,)) * pad


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise WeComCryptoError("密文为空")
    pad = data[-1]
    if pad < 1 or pad > BLOCK_SIZE or pad > len(data):
        raise WeComCryptoError("无效的填充")
    if data[-pad:] != bytes((pad,)) * pad:
        raise WeComCryptoError("无效的填充")
    return data[:-pad]


def cdata(text: str) -> str:
    """Wrap text in a CDATA section, splitting any ``]]>`` sequence."""
    return "<![CDATA[" + str(text).replace("]]>", "]]]]><![CDATA[>") + "]]>"


class WeComCrypto:
    def __init__(self, token: str, encoding_aes_key: str, receive_id: str) -> None:
        if not token:
            raise WeComCryptoError("Token 不能为空")
        if not receive_id:
            raise WeComCryptoError("receive_id (企业 CorpID) 不能为空")
        self.token = token
        self.receive_id = receive_id
        self._key = parse_encoding_aes_key(encoding_aes_key)
        self._iv = self._key[:16]

    # ------------------------------------------------------------- signature
    def signature(self, timestamp: str | int, nonce: str, encrypt: str) -> str:
        parts = sorted([self.token, str(timestamp), str(nonce), encrypt])
        return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

    def verify_signature(
        self, msg_signature: str, timestamp: str | int, nonce: str, encrypt: str
    ) -> bool:
        if not msg_signature:
            return False
        expected = self.signature(timestamp, nonce, encrypt)
        return hmac.compare_digest(expected, msg_signature.lower())

    # --------------------------------------------------------------- ciphers
    def _cipher(self) -> Cipher:
        return Cipher(algorithms.AES(self._key), modes.CBC(self._iv))

    def decrypt(self, encrypted: str) -> str:
        try:
            blob = base64.b64decode(encrypted, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise WeComCryptoError("密文不是合法的 Base64") from exc
        if not blob or len(blob) % 16:
            raise WeComCryptoError("密文长度非法")

        decryptor = self._cipher().decryptor()
        plain = _pkcs7_unpad(decryptor.update(blob) + decryptor.finalize())
        if len(plain) < 20:
            raise WeComCryptoError("明文长度非法")

        msg_len = struct.unpack(">I", plain[16:20])[0]
        if msg_len > len(plain) - 20:
            raise WeComCryptoError("明文中的消息长度非法")

        message = plain[20 : 20 + msg_len]
        receive_id = plain[20 + msg_len :].decode("utf-8", errors="replace")
        if receive_id != self.receive_id:
            raise WeComCryptoError(
                f"receive_id 不匹配（收到 {receive_id!r}，期望 {self.receive_id!r}）"
            )
        try:
            return message.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WeComCryptoError("明文不是合法的 UTF-8") from exc

    def encrypt(self, message: str) -> str:
        payload = message.encode("utf-8")
        plain = (
            os.urandom(16)
            + struct.pack(">I", len(payload))
            + payload
            + self.receive_id.encode("utf-8")
        )
        encryptor = self._cipher().encryptor()
        blob = encryptor.update(_pkcs7_pad(plain)) + encryptor.finalize()
        return base64.b64encode(blob).decode("ascii")

    # ---------------------------------------------------------------- helper
    def encrypted_envelope(
        self, reply_xml: str, timestamp: int | None = None, nonce: str | None = None
    ) -> str:
        """Encrypt a passive reply and wrap it in the envelope WeCom expects."""
        ts = int(time.time()) if timestamp is None else int(timestamp)
        nc = nonce or os.urandom(5).hex()
        encrypt = self.encrypt(reply_xml)
        signature = self.signature(ts, nc, encrypt)
        return (
            "<xml>"
            f"<Encrypt>{cdata(encrypt)}</Encrypt>"
            f"<MsgSignature>{cdata(signature)}</MsgSignature>"
            f"<TimeStamp>{ts}</TimeStamp>"
            f"<Nonce>{cdata(nc)}</Nonce>"
            "</xml>"
        )


__all__ = [
    "WeComCrypto",
    "WeComCryptoError",
    "cdata",
    "parse_encoding_aes_key",
]
