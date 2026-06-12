from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}
DROPPED_RESPONSE_HEADERS = HOP_BY_HOP | {"content-length"}
PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

WEBHOOK_HOST = os.environ.get("WEBHOOK_HOST", "127.0.0.1")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8644"))
WEBHOOK_BASE_URL = f"http://{WEBHOOK_HOST}:{WEBHOOK_PORT}"

_client: httpx.AsyncClient | None = None


def _filter_request_headers(headers: Mapping[str, str] | Iterable[tuple[str, str]] | Any) -> dict[str, str]:
    items = headers.items() if isinstance(headers, Mapping) else headers
    return {str(k): str(v) for k, v in items if str(k).lower() not in HOP_BY_HOP}


def _filter_response_headers(headers: httpx.Headers) -> list[tuple[str, str]]:
    return [(k, v) for k, v in headers.items() if k.lower() not in DROPPED_RESPONSE_HEADERS]


async def _ensure_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=WEBHOOK_BASE_URL,
            timeout=httpx.Timeout(connect=5.0, read=None, write=None, pool=5.0),
            follow_redirects=False,
        )
    return _client


async def webhook_http_proxy(request: Request) -> Response:
    raw_path = request.path_params.get("path", "")
    path = f"/webhooks/{raw_path}" if raw_path else "/webhooks"
    if request.url.query:
        path = f"{path}?{request.url.query}"

    client = await _ensure_client()
    upstream_headers = _filter_request_headers(request.headers)
    body = await request.body()

    try:
        req = client.build_request(
            request.method,
            path,
            headers=upstream_headers,
            content=body if body else None,
        )
        upstream = await client.send(req, stream=True)
    except httpx.ConnectError:
        return Response(
            "Hermes webhook listener unavailable.",
            status_code=502,
            media_type="text/plain",
        )

    async def body_iter():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers=dict(_filter_response_headers(upstream.headers)),
        media_type=upstream.headers.get("content-type"),
    )
