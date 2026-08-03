"""MCP client for the providers' official servers, with SQLite-backed OAuth.

Auth is OAuth 2.0 + PKCE. We never handle the user's password or OTP: the
consent URL is surfaced to the caller (so a voice agent can read it out) and the
user completes sign-in in their own browser.
"""

from __future__ import annotations

import asyncio
import html
import json
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

from ..core import store
from ..providers import SERVERS, provider_for, server_url

__all__ = ["SERVERS", "SqliteTokenStorage", "session", "list_tools", "call_tool"]


_WIDGET_GUIDANCE_PREFIXES = (
    "cart widget is displayed",
    "note: the cart widget",
    "a rich ui widget may be shown",
    "⚠️ a rich ui widget may be shown",
    "a payment picker is shown",
    "👉 a payment picker is shown",
)


def _without_widget_guidance(text: str) -> str:
    """Remove provider instructions meant for graphical MCP hosts, not this CLI."""
    kept = [
        line for line in (text or "").splitlines()
        if not line.strip().casefold().startswith(_WIDGET_GUIDANCE_PREFIXES)
    ]
    # Removing whole UI-only paragraphs can leave several trailing blank lines.
    return "\n".join(kept).rstrip()


class SqliteTokenStorage(TokenStorage):
    """Persists OAuth tokens per-server in the local SQLite DB."""

    def __init__(self, server: str):
        self.server = server

    async def get_tokens(self) -> OAuthToken | None:
        with store.connect() as c:
            row = c.execute("SELECT tokens FROM oauth WHERE server=?", (self.server,)).fetchone()
        if not row or not row["tokens"]:
            return None
        return OAuthToken.model_validate_json(row["tokens"])

    async def set_tokens(self, tokens: OAuthToken) -> None:
        with store.connect() as c:
            c.execute(
                "INSERT INTO oauth(server,tokens,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(server) DO UPDATE SET tokens=excluded.tokens, "
                "updated_at=excluded.updated_at",
                (self.server, tokens.model_dump_json(), time.time()),
            )

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        with store.connect() as c:
            row = c.execute(
                "SELECT client_info FROM oauth WHERE server=?", (self.server,)
            ).fetchone()
        if not row or not row["client_info"]:
            return None
        return OAuthClientInformationFull.model_validate_json(row["client_info"])

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        with store.connect() as c:
            c.execute(
                "INSERT INTO oauth(server,client_info,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(server) DO UPDATE SET client_info=excluded.client_info, "
                "updated_at=excluded.updated_at",
                (self.server, info.model_dump_json(), time.time()),
            )


class _CallbackServer:
    """One-shot localhost listener that captures the OAuth redirect.

    The path is per-provider: providers whitelist redirect URIs by exact path
    and they do not agree on one.
    """

    def __init__(self, path: str = "/oauth/callback"):
        self.path = path
        self.result: dict[str, str] = {}
        self._event = threading.Event()
        self._serving = False
        self._closed = False
        self.port = self._free_port()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                qs = parse_qs(urlparse(self.path).query)
                outer.result = {k: v[0] for k, v in qs.items()}
                ok = "code" in outer.result
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                # Escape: these are query parameters from the browser, so
                # reflecting them raw is a cross-site-scripting hole even on
                # a loopback listener.
                detail = html.escape(", ".join(sorted(outer.result)))
                msg = (
                    "<h2>food-cli connected.</h2><p>You can close this tab.</p>"
                    if ok else
                    f"<h2>Authorization failed</h2><p>Parameters received: {detail}</p>"
                )
                self.write_done = self.wfile.write(msg.encode())
                outer._event.set()

            def log_message(self, *a):  # silence
                pass

        self._httpd = HTTPServer(("127.0.0.1", self.port), Handler)

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @property
    def redirect_uri(self) -> str:
        # See mcp/oauth.redirect_uri_for: the name must be `localhost` even
        # though the socket is bound to 127.0.0.1.
        return f"http://localhost:{self.port}{self.path}"

    def start(self):
        self._serving = True
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self):
        """Release the port. Idempotent - the session always calls this."""
        if self._closed:
            return
        self._closed = True
        try:
            # shutdown() blocks until serve_forever's loop acknowledges it, so
            # calling it on a server that was never started hangs for good.
            if self._serving:
                self._httpd.shutdown()
                self._serving = False
            self._httpd.server_close()
        except Exception:  # noqa: BLE001  - nothing useful to do while tearing down
            pass

    def wait(self, timeout: float = 300) -> dict[str, str]:
        if not self._event.wait(timeout):
            self.stop()
            raise TimeoutError("Timed out waiting for authorization.")
        self.stop()
        return self.result


def _client_metadata(redirect_uri: str, scope: str) -> OAuthClientMetadata:
    return OAuthClientMetadata(
        client_name="food-cli",
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scope,
        token_endpoint_auth_method="none",
    )


# Refresh when this little time is left on the access token.
REFRESH_MARGIN_SECONDS = 600
# With no refresh token available, start nagging a day out.
EXPIRY_WARN_SECONDS = 86400


def _maybe_refresh(server: str) -> None:
    """Silently renew a soon-to-expire token. Never fatal - if refresh fails we
    fall through to the normal interactive flow."""
    try:
        from . import oauth

        info = oauth.token_info(server)
        if not info.get("authorized"):
            return
        remaining = info.get("seconds_remaining")
        label = provider_for(server).label

        if not info.get("has_refresh_token"):
            # Some providers do not issue refresh tokens to a public client, so
            # warn early rather than dying on a 401 mid-order.
            if remaining is not None and remaining < EXPIRY_WARN_SECONDS:
                hrs = remaining / 3600
                print(
                    f"[food-cli] WARNING: {server} access expires in {hrs:.1f}h "
                    f"and there is no refresh token. Run `food auth url --server {server}` "
                    f"to sign in to {label} again.",
                    file=sys.stderr,
                )
            return

        if remaining is None or remaining > REFRESH_MARGIN_SECONDS:
            return
        oauth.refresh(server)
        print(f"[food-cli] refreshed {server} access token", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[food-cli] token refresh skipped: {e}", file=sys.stderr)


@asynccontextmanager
async def session(server: str, on_consent_url=None, wait_for_consent: bool = True):
    """Open an authenticated MCP session against one server.

    on_consent_url: callback invoked with the authorization URL when interactive
    sign-in is required. If wait_for_consent is False we raise instead of
    blocking, so callers can surface the URL and return immediately.
    """
    url = server_url(server)
    cfg = provider_for(server).oauth

    # Refresh proactively when the stored token is near expiry, so a long
    # session (or a voice agent mid-order) never dies on a 401.
    _maybe_refresh(server)

    cb = _CallbackServer(path=cfg.callback_path)
    cb.start()

    async def redirect_handler(authorization_url: str) -> None:
        if on_consent_url:
            on_consent_url(authorization_url)
        if not wait_for_consent:
            raise RuntimeError(f"CONSENT_REQUIRED:{authorization_url}")

    async def callback_handler() -> AuthorizationCodeResult:
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, cb.wait)
        if "code" not in res:
            raise RuntimeError(f"Authorization failed: {res}")
        return AuthorizationCodeResult(code=res["code"], state=res.get("state"))

    auth = OAuthClientProvider(
        server_url=url,
        client_metadata=_client_metadata(cb.redirect_uri, cfg.scope),
        storage=SqliteTokenStorage(server),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    # In MCP SDK 2.x auth is an httpx auth flow attached to the HTTP client.
    try:
        async with create_mcp_http_client(auth=auth) as http_client:
            async with streamable_http_client(url, http_client=http_client) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as sess:
                    await sess.initialize()
                    yield sess
    finally:
        # The common case is a still-valid token, where no redirect ever
        # arrives. Without this the listener would hold a loopback port for the
        # life of the process.
        cb.stop()


def _default_consent(url: str) -> None:
    """Surface the consent URL loudly on stderr so stdout stays clean JSON."""
    print(
        "\n[food-cli] Authorization required.\n"
        "Open this URL in your browser and sign in:\n\n"
        f"{url}\n",
        file=sys.stderr,
        flush=True,
    )


async def list_tools(server: str, on_consent_url=_default_consent) -> list[dict]:
    async with session(server, on_consent_url=on_consent_url) as sess:
        res = await sess.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "output_schema": t.output_schema,
            }
            for t in res.tools
        ]


async def call_tool(server: str, name: str, args: dict, on_consent_url=_default_consent) -> dict:
    async with session(server, on_consent_url=on_consent_url) as sess:
        res = await sess.call_tool(name, args)
        out = []
        for block in res.content:
            if getattr(block, "type", None) == "text":
                try:
                    out.append(json.loads(block.text))
                except (json.JSONDecodeError, TypeError):
                    out.append(_without_widget_guidance(block.text))
            else:
                out.append(block.model_dump() if hasattr(block, "model_dump") else str(block))
        result = {
            "isError": bool(getattr(res, "isError", False)),
            "content": out[0] if len(out) == 1 else out,
        }
        structured = getattr(res, "structured_content", None)
        if structured is not None:
            result["structuredContent"] = structured
        return result
