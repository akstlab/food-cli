"""One-shot OAuth callback server."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from food_cli.mcp import callback
from food_cli.mcp import oauth
from tests.conftest import loopback_get


def test_redirect_uri_is_loopback():
    # Host is `localhost`, and the path is whatever that provider whitelists.
    assert oauth.redirect_uri_for("food", 99) == "http://localhost:99/oauth/callback"
    assert oauth.redirect_uri_for("zepto", 99) == "http://localhost:99/callback"


# ------------------------------------------------------------- the server

def test_captures_code_and_shuts_down():
    with callback.CallbackServer(port=0) as srv:
        got = {}
        t = threading.Thread(target=lambda: got.update(srv.wait(timeout=10)))
        t.start()
        time.sleep(0.1)
        body = loopback_get(f"{srv.redirect_uri}?code=ABC&state=S")
        t.join(timeout=10)
    assert got == {"code": "ABC", "state": "S"}
    assert "Signed in" in body
    # port released
    s = socket.socket(); s.settimeout(1)
    assert s.connect_ex(("127.0.0.1", srv.port)) != 0
    s.close()


def test_error_page_escapes_and_reports():
    with callback.CallbackServer(port=0) as srv:
        t = threading.Thread(target=lambda: srv.wait(timeout=10))
        t.start()
        time.sleep(0.1)
        body = loopback_get(
            f"{srv.redirect_uri}?error=access_denied"
            "&error_description=%3Cscript%3Ealert(1)%3C/script%3E")
        t.join(timeout=10)
    assert "<script>" not in body          # escaped
    assert "Authorization failed" in body


def test_unknown_path_404s():
    with callback.CallbackServer(port=0) as srv:
        t = threading.Thread(target=lambda: srv.wait(timeout=2))
        t.start()
        body = loopback_get(f"http://127.0.0.1:{srv.port}/nope")
        t.join(timeout=3)
    assert "Signed in" not in body


def test_wait_times_out():
    with callback.CallbackServer(port=0) as srv:
        with pytest.raises(TimeoutError, match="No redirect received"):
            srv.wait(timeout=0.2)


# ------------------------------------------------- redirect_uri round-trip

def test_exchange_uses_the_same_redirect_uri(monkeypatch, fresh_db):
    """The token request must echo the exact URI sent to /authorize."""
    from food_cli.core import store

    sent = {}

    class R:
        status_code = 200
        text = "{}"

        @staticmethod
        def json():
            return {"access_token": "t", "token_type": "Bearer", "expires_in": 60}

    def fake_post(url, data=None, **kw):
        sent.update(data)
        return R()

    monkeypatch.setattr(oauth.httpx, "post", fake_post)

    uri = "http://box.local:4321/oauth/callback"
    oauth.build_authorize_url("food", redirect_uri=uri)
    state = store.get_pref("oauth_pending")["state"]
    oauth.exchange(f"{uri}?code=C&state={state}")
    assert sent["redirect_uri"] == uri
