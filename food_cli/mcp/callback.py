"""One-shot local server that receives the OAuth redirect.

Pasting a URL back into the terminal works everywhere, but it is clumsy: the
browser shows a connection error, and the user has to notice that this is
expected and copy an opaque string. This serves the redirect instead, so the
browser lands on a real page and the flow completes on its own.

It binds loopback only, answers exactly one callback, and shuts down. Both
sign-in routes race each other: whichever finishes first - the browser hitting
the callback, or the user pasting the code into another terminal - stops the
listener, so a socket is never left open on a port nobody is watching.

The callback path is per-provider. Providers whitelist redirect URIs by exact
path and they do not agree on one (Swiggy `/oauth/callback`, Zepto `/callback`),
so the path is supplied by the caller and validated here.
"""

from __future__ import annotations

import html
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_PATH = "/oauth/callback"

_PAGE = """<!doctype html>
<meta charset="utf-8"><title>{title}</title>
<style>
 body{{font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      display:flex;align-items:center;justify-content:center;height:100vh;
      margin:0;background:{bg};color:#fff}}
 .card{{text-align:center;padding:2.5rem 3rem}}
 h1{{font-size:1.5rem;margin:0 0 .5rem}}
 p{{margin:.25rem 0;opacity:.9}}
 code{{background:rgba(0,0,0,.25);padding:.15rem .4rem;border-radius:4px}}
</style>
<div class="card"><h1>{title}</h1>{body}</div>
"""


class ListenerCancelled(RuntimeError):
    """The listener stopped because sign-in completed by another route."""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        expected = self.server.callback_path.rstrip("/")  # type: ignore[attr-defined]
        if parsed.path.rstrip("/") not in (expected, ""):
            self.send_response(404)
            self.end_headers()
            return

        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.server.captured = params  # type: ignore[attr-defined]
        ok = "code" in params
        label = self.server.provider_label  # type: ignore[attr-defined]

        if ok:
            page = _PAGE.format(
                title=f"Signed in to {html.escape(label)}", bg="#1f6f3f",
                body="<p>You can close this tab and return to the terminal.</p>")
        else:
            # Escape: everything here came from the query string.
            detail = html.escape(params.get("error_description")
                                 or params.get("error") or "no authorization code")
            page = _PAGE.format(
                title="Authorization failed", bg="#b00020",
                body=f"<p>{detail}</p><p>Run <code>food auth serve</code> again.</p>")

        body = page.encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # This page is a terminal state; never let it be cached or embedded.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)
        self.server.done.set()  # type: ignore[attr-defined]

    def log_message(self, *a):  # keep stdout clean for JSON
        pass


class CallbackServer:
    """Serves exactly one OAuth redirect, then stops."""

    def __init__(self, port: int = 0, path: str = DEFAULT_PATH,
                 provider_label: str = "your account"):
        # Loopback only: the callback carries a one-time code and has no reason
        # to be reachable from anywhere but this machine. Binding 127.0.0.1
        # (never 0.0.0.0) is what keeps it off the network.
        self._httpd = HTTPServer(("127.0.0.1", port), _Handler)
        self._httpd.done = threading.Event()
        self._httpd.captured = {}
        self._httpd.callback_path = path
        self._httpd.provider_label = provider_label
        self.path = path
        self.port = self._httpd.server_address[1]
        self._closed = False
        self._thread: threading.Thread | None = None

    @property
    def redirect_uri(self) -> str:
        # `localhost`, not `127.0.0.1`: Zepto's edge 403s on a literal loopback
        # address anywhere in the request. The socket above is still bound to
        # 127.0.0.1 - this is only how the address is spelled to the provider.
        return f"http://localhost:{self.port}{self.path}"

    def start(self) -> CallbackServer:
        if self._thread is None:
            self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        """Shut the listener down. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        # shutdown() waits for serve_forever's loop to acknowledge it, so it
        # deadlocks on a server that was never started.
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread = None
        self._httpd.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

    def wait(self, timeout: float = 300, until=None, poll: float = 0.5) -> dict[str, str]:
        """Block until the browser hits the callback.

        `until` is an optional predicate checked while waiting; when it returns
        true the listener stops and `ListenerCancelled` is raised. That is how
        pasting the code in another terminal releases this socket instead of
        leaving it bound until the timeout expires.
        """
        deadline = time.time() + timeout
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                if self._httpd.done.wait(min(poll, remaining)):
                    return dict(self._httpd.captured)
                if until is not None and until():
                    raise ListenerCancelled(
                        "Sign-in completed elsewhere; stopped listening."
                    )
        finally:
            self.stop()

        raise TimeoutError(
            f"No redirect received within {timeout:.0f}s. "
            "Use `food auth url` and paste the URL instead."
        )
