"""Debug proxy mode tests."""

from __future__ import annotations

import shutil

import httpx
import pytest
from fastapi.testclient import TestClient

from app.client import WeComClient
from app.config import ConfigError, Settings
from app.main import create_app
from tests.helpers import ROOT


def _settings(name: str, **overrides) -> Settings:
    data_dir = ROOT / "data" / "tests" / name
    shutil.rmtree(data_dir, ignore_errors=True)
    params = {
        "mode": "proxy",
        "debug_proxy_target": "http://dev.local:8081",
        "debug_proxy_token": "debug-secret",
        "wecom_api_base": "https://qyapi.example",
        "data_dir": data_dir,
        "log_level": "WARNING",
    }
    params.update(overrides)
    return Settings(**params)


def test_callback_get_and_post_are_forwarded_to_dev():
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(
            200, content=b"DEV-REPLY", headers={"Content-Type": "text/plain"}
        )

    app = create_app(_settings("proxy_callback"), proxy_transport=httpx.MockTransport(handler))

    with TestClient(app) as client:
        get_response = client.get(
            "/wecom/callback",
            params={"msg_signature": "s", "timestamp": "1", "nonce": "n", "echostr": "e"},
        )
        assert get_response.status_code == 200
        assert get_response.text == "DEV-REPLY"
        assert captured["method"] == "GET"
        assert str(captured["url"]).startswith("http://dev.local:8081/wecom/callback?")
        assert "echostr=e" in str(captured["url"])

        post_response = client.post(
            "/wecom/callback?msg_signature=s&timestamp=1&nonce=n",
            content=b"<xml><Encrypt>x</Encrypt></xml>",
            headers={"Content-Type": "text/xml"},
        )
        assert post_response.status_code == 200
        assert post_response.text == "DEV-REPLY"
        assert captured["method"] == "POST"
        assert captured["body"] == b"<xml><Encrypt>x</Encrypt></xml>"


def test_api_proxy_forwards_to_qyapi_with_token():
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        captured["token"] = request.headers.get("x-debug-token")
        return httpx.Response(200, json={"errcode": 0, "access_token": "T"})

    app = create_app(_settings("proxy_api"), proxy_transport=httpx.MockTransport(handler))

    with TestClient(app) as client:
        response = client.post(
            "/debug/wecom-api/cgi-bin/gettoken?corpid=corp&corpsecret=secret",
            headers={"X-Debug-Token": "debug-secret"},
        )
        assert response.status_code == 200
        assert response.json()["access_token"] == "T"
        assert str(captured["url"]).startswith("https://qyapi.example/cgi-bin/gettoken?")
        assert "corpsecret=secret" in str(captured["url"])
        assert captured["token"] is None  # never forwarded upstream

        unauthorized = client.post(
            "/debug/wecom-api/cgi-bin/gettoken?corpid=corp&corpsecret=secret",
            headers={"X-Debug-Token": "wrong"},
        )
        assert unauthorized.status_code == 401


def test_proxy_health_exposes_mode_only():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    app = create_app(_settings("proxy_health"), proxy_transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        body = client.get("/healthz").json()
    assert body["mode"] == "proxy"
    assert body["target"] == "http://dev.local:8081"
    assert "stats" not in body


def test_proxy_mode_requires_target_and_token():
    with pytest.raises(ConfigError):
        with TestClient(create_app(Settings(mode="proxy", debug_proxy_token="x"))):
            pass

    with pytest.raises(ConfigError):
        with TestClient(
            create_app(Settings(mode="proxy", debug_proxy_target="http://dev.local:8081"))
        ):
            pass


def test_debug_api_is_absent_in_normal_mode():
    settings = Settings(
        corp_id="wwtest",
        agent_id="1",
        app_secret="s",
        callback_token="t",
        callback_aes_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY",
        data_dir=ROOT / "data" / "tests" / "proxy_normal",
        worker_enabled=False,
        log_level="WARNING",
    )
    app = create_app(settings, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with TestClient(app) as client:
        assert client.get("/debug/wecom-api/cgi-bin/gettoken").status_code == 404


def test_relay_token_header_is_sent_to_relay():
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("x-debug-token")
        return httpx.Response(
            200,
            json={"errcode": 0, "access_token": "T", "expires_in": 7200},
        )

    client = WeComClient(
        corp_id="c",
        secret="s",
        api_base="http://server:8080/debug/wecom-api",
        transport=httpx.MockTransport(handler),
        extra_headers={"X-Debug-Token": "debug-secret"},
    )
    assert client.access_token() == "T"
    assert seen["token"] == "debug-secret"
    client.close()
