"""OAuth, token storage and refresh — all HTTP mocked, no network."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from food_cli.mcp import client
from food_cli import providers
from food_cli.mcp import oauth
from food_cli.core import store
from food_cli.cli import app
from tests.conftest import parse_out


pytestmark = pytest.mark.usefixtures("fresh_db")


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture()
def token_post(monkeypatch):
    """Capture the token-endpoint POST and control its reply."""
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append({"url": url, "data": data})
        return fake_post.response

    fake_post.response = FakeResponse(200, {
        "access_token": "tok-abc", "token_type": "Bearer",
        "expires_in": 432000, "refresh_token": "ref-xyz",
    })
    fake_post.calls = calls
    monkeypatch.setattr(oauth.httpx, "post", fake_post)
    return fake_post


# ----------------------------------------------------------- authorize URL

def test_build_authorize_url_has_pkce_and_offline_access():
    url = oauth.build_authorize_url("food")
    q = parse_qs(urlparse(url).query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == [providers.provider_for("food").oauth.client_id]
    assert q["code_challenge_method"] == ["S256"]
    assert len(q["code_challenge"][0]) > 20
    assert "offline_access" in q["scope"][0]
    pending = store.get_pref("oauth_pending")
    assert pending["state"] == q["state"][0]
    assert pending["verifier"]


def test_authorize_url_state_is_fresh_each_time():
    a = parse_qs(urlparse(oauth.build_authorize_url("food")).query)["state"][0]
    b = parse_qs(urlparse(oauth.build_authorize_url("food")).query)["state"][0]
    assert a != b


# --------------------------------------------------------------- code swap

def test_exchange_full_redirect_url(token_post):
    oauth.build_authorize_url("food")
    state = store.get_pref("oauth_pending")["state"]
    res = oauth.exchange(f"http://127.0.0.1:21621/oauth/callback?code=THECODE&state={state}")
    assert res["status"] == "authorized"
    # One Swiggy sign-in authorises both Swiggy servers - and must not
    # write the token into another provider's row.
    assert set(res["servers"]) == {"food", "instamart"}
    assert "zepto" not in res["servers"]
    assert res["has_refresh_token"] is True
    assert token_post.calls[0]["data"]["code"] == "THECODE"
    assert token_post.calls[0]["data"]["grant_type"] == "authorization_code"
    assert store.get_pref("oauth_pending") is None      # consumed


def test_exchange_accepts_bare_code(token_post):
    oauth.build_authorize_url("food")
    assert oauth.exchange("BARECODE")["status"] == "authorized"


def test_exchange_rejects_state_mismatch(token_post):
    oauth.build_authorize_url("food")
    with pytest.raises(ValueError, match="State mismatch"):
        oauth.exchange("http://127.0.0.1/cb?code=X&state=WRONG")


def test_exchange_surfaces_provider_error(token_post):
    oauth.build_authorize_url("food")
    with pytest.raises(ValueError, match="Authorization failed"):
        oauth.exchange("http://127.0.0.1/cb?error=access_denied&error_description=nope")


def test_exchange_without_code_in_url(token_post):
    oauth.build_authorize_url("food")
    with pytest.raises(ValueError, match="No .code="):
        oauth.exchange("http://127.0.0.1/cb?foo=bar")


def test_exchange_without_pending_state(token_post):
    with pytest.raises(RuntimeError, match="No pending authorization"):
        oauth.exchange("SOMECODE")


def test_exchange_http_failure(token_post):
    oauth.build_authorize_url("food")
    token_post.response = FakeResponse(400, text="bad request")
    with pytest.raises(RuntimeError, match="Token exchange failed"):
        oauth.exchange("CODE")


def test_exchange_only_one_server(token_post):
    oauth.build_authorize_url("food")
    res = oauth.exchange("CODE", apply_to_all=False)
    assert res["servers"] == ["food"]


# ---------------------------------------------------------------- refresh

def test_refresh_uses_stored_refresh_token(token_post):
    oauth.build_authorize_url("food")
    oauth.exchange("CODE")
    token_post.response = FakeResponse(200, {
        "access_token": "tok-2", "token_type": "Bearer", "expires_in": 100,
    })
    res = oauth.refresh("food")
    assert res["status"] == "refreshed"
    last = token_post.calls[-1]["data"]
    assert last["grant_type"] == "refresh_token"
    assert last["refresh_token"] == "ref-xyz"


def test_refresh_keeps_old_token_when_not_rotated(token_post):
    oauth.build_authorize_url("food")
    oauth.exchange("CODE")
    token_post.response = FakeResponse(200, {"access_token": "t2", "expires_in": 5})
    assert oauth.refresh("food")["rotated_refresh_token"] is False
    assert oauth.token_info("food")["has_refresh_token"] is True


def test_refresh_without_token_is_explicit(token_post):
    oauth.build_authorize_url("food")
    token_post.response = FakeResponse(200, {"access_token": "t", "expires_in": 10})
    oauth.exchange("CODE")                      # no refresh_token in reply
    with pytest.raises(RuntimeError, match="No refresh token stored"):
        oauth.refresh("food")


def test_refresh_without_any_token():
    with pytest.raises(RuntimeError, match="No stored token"):
        oauth.refresh("food")


def test_refresh_http_failure(token_post):
    oauth.build_authorize_url("food")
    oauth.exchange("CODE")
    token_post.response = FakeResponse(500, text="boom")
    with pytest.raises(RuntimeError, match="Refresh failed"):
        oauth.refresh("food")


# ------------------------------------------------------------- token_info

def test_token_info_unauthorized():
    assert oauth.token_info("food") == {"authorized": False, "provider": "swiggy"}


def test_token_info_reports_expiry(token_post):
    oauth.build_authorize_url("food")
    oauth.exchange("CODE")
    info = oauth.token_info("food")
    assert info["authorized"] and info["has_refresh_token"]
    assert info["seconds_remaining"] > 0 and info["expired"] is False


def test_token_info_detects_expired(token_post):
    oauth.build_authorize_url("food")
    token_post.response = FakeResponse(200, {"access_token": "t", "expires_in": 1})
    oauth.exchange("CODE")
    with store.connect() as c:
        c.execute("UPDATE oauth SET updated_at=? WHERE server='food'", (time.time() - 100,))
    assert oauth.token_info("food")["expired"] is True


# ------------------------------------------------- proactive refresh hook

def test_maybe_refresh_is_never_fatal(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(oauth, "refresh", boom)
    monkeypatch.setattr(oauth, "token_info", lambda s: {
        "authorized": True, "has_refresh_token": True, "seconds_remaining": 1})
    client._maybe_refresh("food")     # must not raise


def test_maybe_refresh_warns_without_refresh_token(monkeypatch, capsys):
    monkeypatch.setattr(oauth, "token_info", lambda s: {
        "authorized": True, "has_refresh_token": False, "seconds_remaining": 60})
    client._maybe_refresh("food")
    assert "no refresh token" in capsys.readouterr().err.lower()


def test_maybe_refresh_noop_when_fresh(monkeypatch):
    called = []
    monkeypatch.setattr(oauth, "refresh", lambda *a, **k: called.append(1))
    monkeypatch.setattr(oauth, "token_info", lambda s: {
        "authorized": True, "has_refresh_token": True, "seconds_remaining": 99999})
    client._maybe_refresh("food")
    assert not called


def test_maybe_refresh_noop_when_unauthorized(monkeypatch):
    monkeypatch.setattr(oauth, "token_info", lambda s: {"authorized": False})
    client._maybe_refresh("food")


# --------------------------------------------------------- token storage

@pytest.mark.anyio
async def _unused():
    pass


def test_sqlite_token_storage_roundtrip():
    import asyncio
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    st = client.SqliteTokenStorage("food")

    async def go():
        assert await st.get_tokens() is None
        assert await st.get_client_info() is None
        await st.set_tokens(OAuthToken(access_token="a", token_type="Bearer"))
        assert (await st.get_tokens()).access_token == "a"
        info = OAuthClientInformationFull(
            client_id="cid", redirect_uris=["http://127.0.0.1/cb"])
        await st.set_client_info(info)
        assert (await st.get_client_info()).client_id == "cid"

    asyncio.run(go())


def test_callback_server_captures_code():
    import threading
    from tests.conftest import loopback_get

    cb = client._CallbackServer()
    cb.start()
    result = {}

    def wait():
        result.update(cb.wait(timeout=10))

    t = threading.Thread(target=wait)
    t.start()
    loopback_get(f"{cb.redirect_uri}?code=ABC&state=S")
    t.join(timeout=10)
    assert result["code"] == "ABC" and result["state"] == "S"


def test_callback_server_redirect_uri_shape():
    cb = client._CallbackServer()
    # `localhost`, not the literal address: Zepto's edge 403s on a loopback
    # literal anywhere in the request. The socket is still bound to 127.0.0.1.
    assert cb.redirect_uri.startswith("http://localhost:")
    assert cb.redirect_uri.endswith("/oauth/callback")


# --------------------------------------------------------------- CLI auth

def test_auth_url_command(runner, mcp):
    data = parse_out(runner.invoke(app, ["auth", "url"]))
    assert data["consent_url"].startswith(
        providers.provider_for("food").oauth.authorize_url)


def test_auth_paste_command(runner, mcp, token_post):
    runner.invoke(app, ["auth", "url"])
    state = store.get_pref("oauth_pending")["state"]
    r = runner.invoke(app, ["auth", "paste", f"http://127.0.0.1/cb?code=C&state={state}"])
    assert r.exit_code == 0
    assert parse_out(r)["status"] == "authorized"


def test_auth_paste_bad_input_exits_nonzero(runner, mcp, token_post):
    assert runner.invoke(app, ["auth", "paste", "junk"]).exit_code == 1


def test_auth_refresh_command_reports_error(runner, mcp):
    assert runner.invoke(app, ["auth", "refresh"]).exit_code == 1


def test_auth_refresh_command_succeeds(runner, mcp, token_post):
    runner.invoke(app, ["auth", "url"])
    state = store.get_pref("oauth_pending")["state"]
    runner.invoke(app, ["auth", "paste", f"http://127.0.0.1/cb?code=C&state={state}"])
    r = runner.invoke(app, ["auth", "refresh"])
    assert r.exit_code == 0 and parse_out(r)["status"] == "refreshed"


def test_auth_login_command(runner, mcp):
    r = runner.invoke(app, ["auth", "login", "--server", "food"])
    assert r.exit_code == 0
    assert parse_out(r)["food"]["status"] == "authorized"


def test_auth_wait_succeeds_when_token_valid(runner, mcp, token_post):
    runner.invoke(app, ["auth", "url"])
    state = store.get_pref("oauth_pending")["state"]
    runner.invoke(app, ["auth", "paste", f"http://127.0.0.1/cb?code=C&state={state}"])
    r = runner.invoke(app, ["auth", "wait", "--server", "food", "--timeout", "5"])
    assert r.exit_code == 0 and parse_out(r)["status"] == "authorized"


def test_auth_wait_times_out(runner, mcp):
    r = runner.invoke(app, ["auth", "wait", "--server", "food",
                            "--timeout", "1", "--interval", "0.2"])
    assert r.exit_code == 2 and parse_out(r)["status"] == "timeout"


def test_auth_logout_single_server(runner, mcp, token_post):
    runner.invoke(app, ["auth", "url"])
    state = store.get_pref("oauth_pending")["state"]
    runner.invoke(app, ["auth", "paste", f"http://127.0.0.1/cb?code=C&state={state}"])
    runner.invoke(app, ["auth", "logout", "--server", "food"])
    assert oauth.token_info("food")["authorized"] is False


def test_auth_url_tells_an_agent_to_spend_the_code_not_refuse_it(runner, mcp):
    """An authorization code is not an OTP.

    PKCE binds it to a verifier that never leaves this machine, and it is
    single-use, so the safe move on being handed the redirect URL is to spend
    it at once. An assistant that refuses just strands the user mid-sign-in.
    """
    data = parse_out(runner.invoke(app, ["auth", "url"]))
    note = data["note"]
    assert "PKCE" in note and "single-use" in note
    assert "immediately" in note and "refuse" in note
    assert "auth paste" in data["next"]
