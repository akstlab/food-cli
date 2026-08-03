"""The last few branches: consent handlers, sync paths, suggestion text."""

from __future__ import annotations

import asyncio

import pytest

from food_cli.mcp import client
from food_cli import commands as C
from food_cli.core import store
from food_cli.cli import app
from tests.conftest import parse_out, patch_call


pytestmark = pytest.mark.usefixtures("fresh_db")


# ------------------------------------------------- consent/callback wiring

def test_redirect_handler_raises_when_not_waiting(monkeypatch):
    """wait_for_consent=False must surface the URL instead of blocking."""
    seen = {}
    captured = {}

    class FakeProvider:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(client, "OAuthClientProvider", FakeProvider)
    monkeypatch.setattr(client, "_maybe_refresh", lambda s: None)

    import contextlib

    @contextlib.asynccontextmanager
    async def fake_http(**kw):
        yield object()

    @contextlib.asynccontextmanager
    async def fake_streams(url, http_client=None):
        raise RuntimeError("stop-here")
        yield  # pragma: no cover

    monkeypatch.setattr(client, "create_mcp_http_client", lambda **kw: fake_http())
    monkeypatch.setattr(client, "streamable_http_client", fake_streams)

    async def go():
        with pytest.raises(RuntimeError):
            async with client.session("food",
                                      on_consent_url=lambda u: seen.setdefault("u", u),
                                      wait_for_consent=False):
                pass

    asyncio.run(go())

    # Now exercise the handlers the provider was handed.
    redirect = captured["redirect_handler"]
    with pytest.raises(RuntimeError, match="CONSENT_REQUIRED"):
        asyncio.run(redirect("https://example.com/authorize"))
    assert seen["u"] == "https://example.com/authorize"


def test_callback_handler_paths(monkeypatch):
    captured = {}

    class FakeProvider:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(client, "OAuthClientProvider", FakeProvider)
    monkeypatch.setattr(client, "_maybe_refresh", lambda s: None)

    import contextlib

    @contextlib.asynccontextmanager
    async def fake_http(**kw):
        yield object()

    @contextlib.asynccontextmanager
    async def fake_streams(url, http_client=None):
        raise RuntimeError("stop")
        yield  # pragma: no cover

    monkeypatch.setattr(client, "create_mcp_http_client", lambda **kw: fake_http())
    monkeypatch.setattr(client, "streamable_http_client", fake_streams)

    async def go():
        with pytest.raises(RuntimeError):
            async with client.session("food"):
                pass

    asyncio.run(go())

    handler = captured["callback_handler"]

    # success path
    monkeypatch.setattr(client._CallbackServer, "wait",
                        lambda self, timeout=300: {"code": "C1", "state": "S1"})
    res = asyncio.run(handler())
    assert res.code == "C1"

    # failure path
    monkeypatch.setattr(client._CallbackServer, "wait",
                        lambda self, timeout=300: {"error": "denied"})
    with pytest.raises(RuntimeError, match="Authorization failed"):
        asyncio.run(handler())


def test_callback_server_times_out():
    cb = client._CallbackServer()
    with pytest.raises(TimeoutError):
        cb.wait(timeout=0.1)


def test_callback_server_error_page():
    import threading
    from tests.conftest import loopback_get

    cb = client._CallbackServer()
    cb.start()
    got = {}
    t = threading.Thread(target=lambda: got.update(cb.wait(timeout=10)))
    t.start()
    body = loopback_get(f"{cb.redirect_uri}?error=denied")
    t.join(timeout=10)
    assert "Authorization failed" in body


# ------------------------------------------------------- prefs learn sync

def test_prefs_learn_with_sync(runner, mcp):
    data = parse_out(runner.invoke(app, ["prefs", "learn"]))
    assert data["orders_considered"] >= 2


def test_prefs_learn_sync_failure_is_soft(runner, mcp, monkeypatch):
    def boom(server, tool, args):
        raise RuntimeError("upstream down")
    patch_call(monkeypatch, boom)
    r = runner.invoke(app, ["prefs", "learn"])
    assert r.exit_code == 0
    assert "history sync skipped" in r.stderr


# --------------------------------------------- near-miss suggestion text

def test_place_near_miss_names_what_to_add(runner, mcp):
    """The block message should say WHAT to add, not just that you're short."""
    runner.invoke(app, ["restaurant", "best-offer", "--restaurant", "9001", "--dry-run"])
    r = runner.invoke(app, ["restaurant", "place", "-y", "--max-total", "430", "--restaurant", "9001",
                            "--payment", "UPI", "--intent-app", "gpay://upi/", "--ignore-card-offers"])
    assert r.exit_code == 4
    assert "Cheapest way there" in r.stderr
    assert "food restaurant topup" in r.stderr


def test_place_suggestion_survives_menu_failure(runner, mcp, monkeypatch):
    runner.invoke(app, ["restaurant", "best-offer", "--restaurant", "9001", "--dry-run"])
    real = C.call

    def flaky(server, tool, args):
        if tool == "get_restaurant_menu":
            raise RuntimeError("no menu")
        return real(server, tool, args)

    patch_call(monkeypatch, flaky)
    r = runner.invoke(app, ["restaurant", "place", "-y", "--max-total", "430", "--restaurant", "9001",
                            "--payment", "UPI", "--intent-app", "gpay://upi/", "--ignore-card-offers"])
    assert r.exit_code == 4          # still blocks, just without the suggestion


def test_topup_infeasible_option_recorded(runner, mcp):
    mcp.set("food", "fetch_food_coupons",
            "Found 1 coupons (0 applicable):\n"
            "  - FLAT550 [❌ NOT APPLICABLE] — Add ₹99999 more to avail this offer (code: c-1)\n")
    data = parse_out(runner.invoke(app, ["restaurant", "topup", "--restaurant", "9001"]))
    assert data["options"][0]["feasible"] is False
    assert data["best"] is None


def test_topup_veg_flag(runner, mcp):
    data = parse_out(runner.invoke(app, ["restaurant", "topup", "--restaurant", "9001", "--veg"]))
    assert data["veg_only"] is True


# ----------------------------------------------------------- misc guards

def test_set_qty_without_restaurant(runner, mcp):
    with store.connect() as c:
        c.execute("DELETE FROM prefs WHERE key='last_restaurant_id'")
    assert runner.invoke(app, ["restaurant", "set-qty", "--item", "x", "--qty", "1"]).exit_code == 2


def test_remove_from_empty_cart(runner, mcp):
    store.set_pref("last_restaurant_id", "9001")
    mcp.set("food", "get_food_cart", "Your cart is empty")
    data = parse_out(runner.invoke(app, ["restaurant", "remove", "--item", "x"]))
    assert data["status"] == "cart_empty_or_unparsed"


def test_address_default_empty(runner, mcp):
    data = parse_out(runner.invoke(app, ["address", "default"]))
    assert "default_address_id" in data
