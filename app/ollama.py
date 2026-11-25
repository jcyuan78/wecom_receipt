"""Minimal Ollama client (vision + JSON output)."""

from __future__ import annotations

import base64
import json
from loguru import logger as log
from typing import Any, Sequence

import httpx



class OllamaError(RuntimeError):
    """Raised when Ollama is unreachable or returns something unusable."""


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from a model response, tolerating code fences."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            if isinstance(value, dict):
                return value
        except ValueError:
            pass

    raise OllamaError(f"模型返回的不是合法 JSON: {text[:300]}")


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 300.0,
        keep_alive: str = "10m",
        num_ctx: int = 8192,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def list_models(self) -> list[str]:
        try:
            response = self._http.get("/api/tags")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OllamaError(f"无法连接 Ollama（{self._http.base_url}）: {exc}") from exc
        data = response.json()
        return [str(item.get("name", "")) for item in data.get("models", [])]

    def chat_json(self, prompt: str, images: Sequence[bytes] = (),
        *, system: str | None = None,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        user_message: dict[str, Any] = {"role": "user", "content": prompt}
        if images:
            user_message["images"] = [
                base64.b64encode(image).decode("ascii") for image in images
            ]
        messages.append(user_message)

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
            "keep_alive": self.keep_alive,
        }

        try:
            response = self._http.post("/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise OllamaError(
                f"无法连接 Ollama（{self._http.base_url}），请确认 ollama serve 已启动: {exc}"
            ) from exc

        if response.status_code == 404:
            raise OllamaError(
                f"Ollama 中找不到模型 {self.model!r}，请先执行: ollama pull {self.model}"
            )
        if response.status_code >= 400:
            raise OllamaError(
                f"Ollama 返回 HTTP {response.status_code}: {response.text[:300]}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise OllamaError("Ollama 返回了非 JSON 响应") from exc

        message = data.get("message", {}) or {}
        content = str(message.get("content") or "").strip()
        thinking = str(message.get("thinking") or "").strip()
        if not content and thinking:
            # Thinking models occasionally put everything in "thinking".
            content = thinking
        if not content:
            raise OllamaError(
                "Ollama 返回了空内容（通常是图片过大/上下文不足；"
                "可调大 WECOM_OLLAMA_NUM_CTX，或换更小的图片/模型）"
            )

        log.debug(
            "Ollama 完成: done_reason={} eval_count={}",
            data.get("done_reason"),
            data.get("eval_count"),
        )
        result = extract_json_object(content)
        log.debug("Ollama 响应: {}", result)
        return result

    def generate_json(
        self,
        system_prompt: str,
        prompt: str,
    ) -> dict[str, Any]:
        """Text-only JSON generation via Ollama's ``/api/generate``.

        ``system_prompt`` is the instruction block and ``prompt`` is the user
        text.  No images are accepted here; use :meth:`chat_json` for vision.
        """
        payload = {
            "model": self.model,
            "system": system_prompt,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
            "keep_alive": self.keep_alive,
        }

        try:
            response = self._http.post("/api/generate", json=payload)
        except httpx.HTTPError as exc:
            raise OllamaError(
                f"无法连接 Ollama（{self._http.base_url}），请确认 ollama serve 已启动: {exc}"
            ) from exc

        if response.status_code == 404:
            raise OllamaError(
                f"Ollama 中找不到模型 {self.model!r}，请先执行: ollama pull {self.model}"
            )
        if response.status_code >= 400:
            raise OllamaError(
                f"Ollama 返回 HTTP {response.status_code}: {response.text[:300]}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise OllamaError("Ollama 返回了非 JSON 响应") from exc

        content = str(data.get("response") or "").strip()
        thinking = str(data.get("thinking") or "").strip()
        if not content and thinking:
            # Some thinking models put the whole answer in "thinking".
            content = thinking
        if not content:
            raise OllamaError(
                "Ollama 返回了空内容（可调大 WECOM_OLLAMA_NUM_CTX 或重试）"
            )

        log.debug(
            "Ollama generate 完成: done_reason={} eval_count={}",
            data.get("done_reason"),
            data.get("eval_count"),
        )
        result = extract_json_object(content)
        log.debug("Ollama generate 响应: {}", result)
        return result