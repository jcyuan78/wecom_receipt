"""Transparent HTTP forwarding used by the debug proxy mode.

The public server keeps the WeCom callback URL and forwards the original
request to the developer machine.  API calls from the developer machine are
forwarded back to WeCom through the same server, so the developer never needs
a public IP or the production egress IP.
"""

from __future__ import annotations

from loguru import logger as log
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, Request, Response

from .config import Settings


_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",
}

_RESPONSE_DROP = {
    "connection",
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "keep-alive",
}


@dataclass(slots=True)
class ProxyContext:
    settings: Settings
    client: httpx.AsyncClient


def _forward_headers(request: Request, drop: tuple[str, ...]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        lower = key.lower()
        if lower in _HOP_BY_HOP or lower in drop:
            continue
        headers[key] = value
    return headers


async def forward_to(
    ctx: ProxyContext,
    request: Request,
    *,
    target_base: str | None = None,
    path: str | None = None,
    drop_headers: tuple[str, ...] = (),
) -> Response:
    base = (target_base or ctx.settings.debug_proxy_target).rstrip("/")
    target_path = path if path is not None else request.url.path
    if not target_path.startswith("/"):
        target_path = "/" + target_path

    body = await request.body()
    headers = _forward_headers(request, drop_headers)

    try:
        response = await ctx.client.request(
            request.method,
            base + target_path,
            params=request.query_params,
            content=body,
            headers=headers,
        )
    except httpx.HTTPError as exc:
        log.warning("调试转发失败 {} {}: {}", request.method, target_path, exc)
        raise HTTPException(status_code=502, detail=f"调试目标不可达: {exc}") from exc

    response_headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _RESPONSE_DROP
    }
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=response_headers,
    )
