"""Thin Starlette wrapper in front of hermes-webui.

Adds one new surface — `/tui` — that exposes an in-browser xterm with two modes:
  - OAuth one-shots: `hermes auth add <X> --type oauth --no-browser` for Codex /
    Nous Portal device-code flows (`/tui/ws/auth/<provider>`).
  - Free-form shell: `/bin/bash -i` for users without SSH access who need to
    run other `hermes` CLI commands or peek at `/data` (`/tui/ws/shell`).

Hermes CLI ``hermes dashboard`` is reverse-proxied under ``/dashboard`` by default
(override with ``HERMES_DASHBOARD_MOUNT_PATH`` — see ``admin/dashboard_proxy.py``): loopback port
9119 (``HERMES_DASHBOARD_HOST`` / ``HERMES_DASHBOARD_PORT``) with ``X-Forwarded-Prefix`` so
upstream rewrites SPA asset URLs correctly.

Every other path is reverse-proxied to hermes-webui on loopback
(``HERMES_WEBUI_HOST`` / ``HERMES_WEBUI_PORT``, default ``127.0.0.1:9120``),
including WebSockets and SSE chat streams.

This wrapper does NOT enforce separate auth on traffic proxied to hermes-webui; that app
handles its password gate via session cookies / `/login`.

The **`/tui`** page probes hermes-webui's API cookies before responding; **`/dashboard`** runs
Hermes upstream's CLI dashboard and does **not** use **`ADMIN_PASSWORD`**. Whenever **`hermes dashboard`** is
listening it can expose **`.env`** — minimize uptime on public Railway URLs unless you acknowledge that risk (see upstream [**Web Dashboard**](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard) docs).
"""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Mount, Route, WebSocketRoute

from . import proxy as hermes_proxy
from . import terminal as hermes_terminal
from .dashboard_proxy import DASHBOARD_MOUNT_PREFIX, build_dashboard_starlette_app


TEMPLATE_PATH = Path(__file__).parent / "templates" / "tui.html"


async def _is_authenticated(request: Request) -> bool:
    """Cheap auth probe: hit hermes-webui's /api/onboarding/status with the user's cookies.

    First call after boot can be slow (5+s) due to hermes_cli imports inside the
    webui server. Use a generous timeout — auth checks are infrequent.
    """
    import httpx

    cookie = request.headers.get("cookie", "")
    if not cookie:
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{hermes_proxy.WEBUI_BASE_URL}/api/onboarding/status",
                headers={"cookie": cookie, "host": f"{hermes_proxy.WEBUI_HOST}:{hermes_proxy.WEBUI_PORT}"},
            )
        return r.status_code == 200
    except (httpx.ConnectError, httpx.ReadTimeout):
        return False


async def tui_page(request: Request):
    if not await _is_authenticated(request):
        return RedirectResponse("/login?next=/tui", status_code=303)
    return HTMLResponse(TEMPLATE_PATH.read_text(encoding="utf-8"))


routes = [
    Route("/tui", tui_page, methods=["GET"]),
    WebSocketRoute("/tui/ws/auth/{provider}", hermes_terminal.login_ws),
    WebSocketRoute("/tui/ws/shell", hermes_terminal.shell_ws),
    Mount(DASHBOARD_MOUNT_PREFIX, build_dashboard_starlette_app()),
    # Catch-all proxy for everything else (HTTP + WebSocket).
    WebSocketRoute("/{path:path}", hermes_proxy.ws_proxy),
    Route("/{path:path}", hermes_proxy.http_proxy, methods=hermes_proxy.PROXY_METHODS),
    # Root path needs its own route — Starlette's path converter requires at least one segment.
    Route("/", hermes_proxy.http_proxy, methods=hermes_proxy.PROXY_METHODS),
]


app = Starlette(debug=False, routes=routes)
