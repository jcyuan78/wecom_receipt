"""Thin WeCom API client: access-token cache + media download."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse

import httpx

from .models import DownloadedMedia

log = logging.getLogger(__name__)

TOKEN_ERROR_CODES = {40014, 42001, 41001, 40001}
_FILENAME_STAR_RE = re.compile(r"filename\*\s*=\s*([^;]+)", re.IGNORECASE)
_FILENAME_RE = re.compile(r'filename\s*=\s*"?([^";]+)"?', re.IGNORECASE)


class WeComAPIError(RuntimeError):
    def __init__(self, errcode: int, errmsg: str, endpoint: str = "") -> None:
        self.errcode = errcode
        self.errmsg = errmsg
        self.endpoint = endpoint
        suffix = f" [{endpoint}]" if endpoint else ""
        super().__init__(f"企业微信接口错误 {errcode}: {errmsg}{suffix}")


class MediaTooLarge(RuntimeError):
    pass


def _repair_mojibake(value: str) -> str:
    """Undo UTF-8-bytes-decoded-as-latin-1 filenames."""
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def parse_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    match = _FILENAME_STAR_RE.search(value)
    if match:
        raw = match.group(1).strip().strip('"')
        if "''" in raw:
            raw = raw.split("''", 1)[1]
        return _repair_mojibake(urllib.parse.unquote(raw)) or None

    match = _FILENAME_RE.search(value)
    if match:
        raw = match.group(1).strip()
        return _repair_mojibake(urllib.parse.unquote(raw)) or None
    return None


class WeComClient:
    def __init__(
        self,
        corp_id: str,
        secret: str,
        api_base: str = "https://qyapi.weixin.qq.com",
        timeout: float = 30.0,
        max_file_mb: int = 100,
        transport: httpx.BaseTransport | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._corp_id = corp_id
        self._secret = secret
        self._max_bytes = max_file_mb * 1024 * 1024
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = threading.Lock()
        self._http = httpx.Client(
            base_url=api_base.rstrip("/"),
            timeout=httpx.Timeout(timeout, read=max(timeout, 60.0)),
            transport=transport,
            follow_redirects=True,
            headers={"User-Agent": "wecom-receiver/0.1", **(extra_headers or {})},
        )

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ token
    def _fetch_token(self) -> str:
        try:
            response = self._http.get(
                "/cgi-bin/gettoken",
                params={"corpid": self._corp_id, "corpsecret": self._secret},
            )
        except httpx.HTTPError as exc:
            raise WeComAPIError(-1, f"无法连接企业微信接口: {exc}", "gettoken") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise WeComAPIError(
                -1, f"gettoken 返回非 JSON（HTTP {response.status_code}）", "gettoken"
            ) from exc

        errcode = int(data.get("errcode", 0) or 0)
        if errcode != 0:
            errmsg = str(data.get("errmsg", ""))
            hint = ""
            if errcode == 60020:
                match = re.search(r"from ip:\s*([0-9a-fA-F.:]+)", errmsg)
                seen_ip = f" {match.group(1)}" if match else ""
                hint = f"（企业微信看到的出口 IP 是{seen_ip}，请把它加入该应用的『企业可信IP』）"
            raise WeComAPIError(errcode, errmsg + hint, "gettoken")

        token = str(data.get("access_token", ""))
        if not token:
            raise WeComAPIError(-1, "gettoken 未返回 access_token", "gettoken")

        expires_in = int(data.get("expires_in", 7200) or 7200)
        self._token = token
        self._token_expires_at = time.monotonic() + max(60, expires_in - 300)
        log.info("已获取 access_token（有效期 %ss）", expires_in)
        return token

    def access_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.monotonic() < self._token_expires_at:
                return self._token
            return self._fetch_token()

    # ------------------------------------------------------------- messaging
    def send_text(self, to_user: str, content: str, *, agent_id: int = 0) -> None:
        """Send an application text message to one member."""
        if not to_user:
            raise WeComAPIError(-1, "touser 为空，无法发送消息", "message/send")

        payload = {
            "touser": to_user,
            "msgtype": "text",
            "agentid": int(agent_id or 0),
            "text": {"content": content[:600]},
            "safe": 0,
        }

        last_error: WeComAPIError | None = None
        for attempt in range(2):
            token = self.access_token(force=attempt > 0)
            try:
                response = self._http.post(
                    "/cgi-bin/message/send",
                    params={"access_token": token},
                    json=payload,
                )
            except httpx.HTTPError as exc:
                raise WeComAPIError(-1, f"发送消息失败: {exc}", "message/send") from exc

            try:
                data = response.json()
            except ValueError as exc:
                raise WeComAPIError(
                    -1, f"message/send 返回非 JSON（HTTP {response.status_code}）", "message/send"
                ) from exc

            errcode = int(data.get("errcode", 0) or 0)
            if errcode == 0:
                return

            last_error = WeComAPIError(
                errcode, str(data.get("errmsg", "")), "message/send"
            )
            if errcode in TOKEN_ERROR_CODES and attempt == 0:
                log.warning("access_token 失效，刷新后重发消息")
                continue
            raise last_error

        raise last_error or WeComAPIError(-1, "发送消息失败", "message/send")

    # ------------------------------------------------------------------ media
    def download_media(self, media_id: str) -> DownloadedMedia:
        """Download a media object (image/file/voice/video) by media_id."""
        last_error: WeComAPIError | None = None

        for attempt in range(2):
            token = self.access_token(force=attempt > 0)
            try:
                return self._download_once(token, media_id)
            except WeComAPIError as exc:
                last_error = exc
                if exc.errcode in TOKEN_ERROR_CODES and attempt == 0:
                    log.warning("access_token 失效（errcode=%s），将刷新后重试", exc.errcode)
                    continue
                raise

        raise last_error or WeComAPIError(-1, "媒体下载失败", "media/get")

    def _download_once(self, token: str, media_id: str) -> DownloadedMedia:
        chunks: list[bytes] = []
        total = 0
        headers: httpx.Headers
        content_type = "application/octet-stream"
        filename: str | None = None

        try:
            with self._http.stream(
                "GET",
                "/cgi-bin/media/get",
                params={"access_token": token, "media_id": media_id},
            ) as response:
                headers = response.headers
                content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
                filename = parse_content_disposition(headers.get("content-disposition"))

                if response.status_code != 200:
                    raise WeComAPIError(
                        -response.status_code,
                        f"media/get HTTP {response.status_code}",
                        "media/get",
                    )

                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self._max_bytes:
                        raise MediaTooLarge(
                            f"文件超过大小上限 {self._max_bytes // (1024 * 1024)} MB"
                        )
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise WeComAPIError(-1, f"下载媒体失败: {exc}", "media/get") from exc

        body = b"".join(chunks)

        # WeCom reports errors as a small JSON document instead of the file.
        if filename is None and body.lstrip()[:1] == b"{":
            try:
                data = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                data = None
            if isinstance(data, dict) and "errcode" in data:
                raise WeComAPIError(
                    int(data.get("errcode", -1) or -1),
                    str(data.get("errmsg", "")),
                    "media/get",
                )

        if not body:
            raise WeComAPIError(-1, "media/get 返回了空文件", "media/get")

        return DownloadedMedia(content=body, filename=filename, content_type=content_type)
