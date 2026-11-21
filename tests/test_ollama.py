"""Ollama client resilience tests."""

from __future__ import annotations

import json

import httpx
import pytest

from app.ollama import OllamaClient, OllamaError


def _client(handler, num_ctx: int = 4096) -> OllamaClient:
    return OllamaClient(
        base_url="http://localhost:11434",
        model="test-model",
        num_ctx=num_ctx,
        transport=httpx.MockTransport(handler),
    )


def test_thinking_field_is_used_when_content_is_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["options"]["num_ctx"] == 4096
        return httpx.Response(
            200,
            json={
                "message": {"content": "", "thinking": '{"invoice_no": "A-1"}'},
                "done_reason": "stop",
            },
        )

    assert _client(handler).chat_json("read") == {"invoice_no": "A-1"}


def test_empty_response_has_actionable_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": ""}})

    with pytest.raises(OllamaError) as exc:
        _client(handler).chat_json("read")
    assert "NUM_CTX" in str(exc.value)


def test_generate_json_uses_generate_endpoint():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/generate"
        body = json.loads(request.content)
        seen.update(body)
        return httpx.Response(
            200,
            json={"response": '{"action": "query_invoices"}', "done_reason": "stop"},
        )

    result = _client(handler).generate_json("SYSTEM RULES", "本月发票总额")
    assert result == {"action": "query_invoices"}
    assert seen["system"] == "SYSTEM RULES"
    assert seen["prompt"] == "本月发票总额"
    assert seen["format"] == "json"
    assert seen["stream"] is False
    assert seen["options"]["num_ctx"] == 4096


def test_generate_json_keyword_arguments_and_thinking_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"response": "", "thinking": '{"ok": true}'},
        )

    result = _client(handler).generate_json(
        system_prompt="SYSTEM", prompt="USER"
    )
    assert result == {"ok": True}


def test_generate_json_empty_response_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": ""})

    with pytest.raises(OllamaError) as exc:
        _client(handler).generate_json("SYSTEM", "USER")
    assert "NUM_CTX" in str(exc.value)
