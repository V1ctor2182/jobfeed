"""Require the access code for dashboard requests that arrive through a tunnel.

A tunnel such as Cloudflare Tunnel or Tailscale Funnel connects to the
dashboard from the server itself, so the socket alone cannot tell its requests
from local ones. The headers can: the tunnel's edge adds forwarding headers that
a visitor cannot remove. Requests without them, from this machine, are unchanged.
"""

from __future__ import annotations

import asyncio
import hmac
import html
from http.cookies import SimpleCookie
from typing import cast
from urllib.parse import parse_qs

from fastapi import Request
from starlette.requests import HTTPConnection
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.types import ASGIApp, Receive, Scope, Send

from jobfeed.remote_access import RemoteAccessStore
from jobfeed.web.dashboard_origin import dashboard_loopback_origin

COOKIE = "jobfeed_access"
SIGN_IN_PATH = "/access"
_SESSION_SECONDS = 30 * 24 * 3600
_MAX_FORM_BYTES = 1024
_WRONG_CODE_DELAY_S = 1.0
# Added by Cloudflare's edge or by Tailscale for every request they forward.
_TUNNEL_HEADERS = (
    "cf-connecting-ip",
    "cf-ray",
    "tailscale-funnel-request",
    "tailscale-user-login",
)


def through_tunnel(connection: HTTPConnection) -> bool:
    """Return whether a request came from outside this machine through a proxy.

    Args:
        connection: Incoming HTTP or WebSocket connection.

    Returns:
        True for tunnel traffic, and for any forwarded request other than the
        verified local ``jobfeed.localhost`` proxy.
    """
    headers = connection.headers
    if any(name in headers for name in _TUNNEL_HEADERS):
        return True
    if "x-forwarded-for" in headers or "forwarded" in headers:
        return dashboard_loopback_origin(cast(Request, connection)) is None
    return False


class RemoteAccessGate:
    """ASGI middleware that asks tunnel visitors for the access code.

    Args:
        app: The dashboard application.
        store: Where the access code hash lives.
    """

    def __init__(self, app: ASGIApp, store: RemoteAccessStore) -> None:
        self.app = app
        self.store = store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass local requests through and check the rest.

        Args:
            scope: ASGI connection scope.
            receive: ASGI receive channel.
            send: ASGI send channel.
        """
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        if not through_tunnel(connection):
            await self.app(scope, receive, send)
            return
        access = self.store.load()
        if access is None:
            await _refuse(
                scope,
                receive,
                send,
                PlainTextResponse(
                    "Remote access to this Jobfeed dashboard is turned off.", 403
                ),
            )
            return
        if scope["type"] == "http" and scope["path"] == SIGN_IN_PATH:
            response = await self._sign_in(connection, receive)
            await response(scope, receive, send)
            return
        cookie = SimpleCookie(connection.headers.get("cookie", ""))
        token = cookie[COOKIE].value if COOKIE in cookie else ""
        if token and _same(token, access.session_token()):
            await self.app(scope, receive, send)
            return
        if scope["path"].startswith("/api/"):
            response = JSONResponse(
                {
                    "error": {
                        "code": "remote_access_required",
                        "message": "Sign in with your Jobfeed access code.",
                    }
                },
                status_code=401,
            )
        else:
            response = RedirectResponse(SIGN_IN_PATH, status_code=303)
        await _refuse(scope, receive, send, response)

    async def _sign_in(self, connection: HTTPConnection, receive: Receive) -> Response:
        access = self.store.load()
        if access is None:
            return PlainTextResponse("Remote access is turned off.", 403)
        if connection.scope["method"] != "POST":
            return _page()
        origin = connection.headers.get("origin")
        if origin is not None and origin.split("://", 1)[-1] != connection.headers.get(
            "host"
        ):
            return _page("Open the sign-in page from your Jobfeed link.", 403)
        body = await _read_body(receive)
        if body is None:
            return _page("That code didn't work. Check it and try again.", 400)
        code = parse_qs(body.decode("utf-8", "replace")).get("code", [""])[0]
        if not access.accepts(code):
            await asyncio.sleep(_WRONG_CODE_DELAY_S)
            return _page("That code didn't work. Check it and try again.", 401)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE,
            access.session_token(),
            max_age=_SESSION_SECONDS,
            path="/",
            secure=connection.url.scheme == "https"
            or connection.headers.get("x-forwarded-proto") == "https",
            httponly=True,
            samesite="lax",
        )
        return response


def _same(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())


async def _read_body(receive: Receive) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return None
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > _MAX_FORM_BYTES:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


async def _refuse(
    scope: Scope, receive: Receive, send: Send, response: Response
) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    await response(scope, receive, send)


def _page(message: str = "", status_code: int = 200) -> HTMLResponse:
    notice = f'<p class="notice">{html.escape(message)}</p>' if message else ""
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Jobfeed sign-in</title>
<style>
:root {{ color-scheme: light dark; --ink: #1d2421; --muted: #5a645e; --line: #c9d0cb;
  --paper: #f7f8f6; --accent: #1f6f4a; }}
@media (prefers-color-scheme: dark) {{ :root {{ --ink: #e7ece9; --muted: #a3ada7;
  --line: #3a423e; --paper: #141816; --accent: #6cc39a; }} }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
  background: var(--paper); color: var(--ink);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ width: min(360px, calc(100vw - 32px)); }}
h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
p {{ color: var(--muted); margin: 0 0 1.25rem; }}
.notice {{ color: #b3261e; }}
label {{ display: block; font-weight: 600; margin-bottom: .4rem; }}
input {{ box-sizing: border-box; width: 100%; padding: .7rem .8rem; font: inherit;
  letter-spacing: .08em; text-transform: uppercase; border: 1px solid var(--line);
  border-radius: 8px; background: transparent; color: var(--ink); }}
button {{ margin-top: 1rem; width: 100%; padding: .7rem; font: inherit;
  font-weight: 600;
  border: 0; border-radius: 8px; background: var(--accent); color: var(--paper); }}
</style></head>
<body><main>
<h1>Jobfeed</h1>
<p>Enter the access code you were given during setup.</p>
{notice}
<form method="post" action="{SIGN_IN_PATH}">
<label for="code">Access code</label>
<input id="code" name="code" autocomplete="one-time-code" autocapitalize="characters"
  spellcheck="false" required autofocus placeholder="XXXX-XXXX-XXXX-XXXX">
<button type="submit">Open dashboard</button>
</form>
</main></body></html>""",
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["COOKIE", "SIGN_IN_PATH", "RemoteAccessGate", "through_tunnel"]
