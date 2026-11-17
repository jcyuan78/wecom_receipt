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
