"""Remaining branches: failure paths, guards and rarely-hit helpers."""

from __future__ import annotations

import pytest

from food_cli.mcp import client
from food_cli import commands as C
from food_cli.offers import coupons as offers
from food_cli.core import qr
from food_cli.core import store
from food_cli.offers import topup
from food_cli.cli import app
from tests.conftest import parse_out, patch_call


pytestmark = pytest.mark.usefixtures("fresh_db")


def test_store_rolls_back_on_error():
    with pytest.raises(RuntimeError):
        with store.connect() as c:
            c.execute("INSERT INTO prefs(key,value,updated_at) VALUES('x','1',0)")
            raise RuntimeError("boom")
    assert store.get_pref("x") is None


def test_store_chmod_failure_is_survivable(monkeypatch):
    monkeypatch.setattr(store.os, "chmod", lambda *a: (_ for _ in ()).throw(OSError()))
    store.set_pref("k", 1)
    assert store.get_pref("k") == 1


def test_cache_get_missing_row():
    assert store.cache_get("nope") is None


def test_get_default_address_when_none():
    assert store.get_default_address() is None


def test_qr_present_creates_dir(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "dir"
    monkeypatch.setattr(qr, "QR_DIR", target)
    qr.present({"kind": "upi_uri", "value": "upi://pay?pa=a@b&am=1.00"},
               order_ref="x", open_browser=False)
    assert (target / "x.png").exists()


def test_qr_open_browser_path(tmp_path, monkeypatch):
    monkeypatch.setattr(qr, "QR_DIR", tmp_path)
    monkeypatch.setattr(qr, "_open", lambda p: True)
    res = qr.present({"kind": "upi_uri", "value": "upi://pay?pa=a@b&am=1.00"},
                     order_ref="y", open_browser=True)
    assert res["opened"] is True


def test_qr_open_uses_webbrowser_off_darwin(monkeypatch, tmp_path):
    """Non-macOS path still goes through the same allow-list."""
    monkeypatch.setattr(qr, "QR_DIR", tmp_path)
    f = tmp_path / "x.png"
    f.write_bytes(b"x")
    monkeypatch.setattr(qr.sys, "platform", "linux")
    monkeypatch.setattr(qr.webbrowser, "open", lambda u: True)
    assert qr._open(str(f)) is True
    # plain http is refused on every platform
    assert qr._open("http://x") is False


def test_offers_line_without_code():
    assert offers.parse_coupons("   - lowercase only — ₹50 off") == []


def test_offers_applicable_when_no_flag():
    c = offers.parse_coupons("  - PLAIN — Flat ₹20 off (code: a-1)")[0]
    assert c["applicable"] is True


def test_topup_desirability_bestseller():
    assert topup._desirability({"name": "Zed", "price": 50, "bestseller": True}) > \
           topup._desirability({"name": "Zed", "price": 50})


def test_topup_veg_exception_words():
    assert topup._is_non_veg("Veg Chicken Style Nuggets") is False
    assert topup._is_non_veg("Mushroom Duck Sauce") is False   # mushroom => veg exception


def test_commands_resolve_address_explicit():
    assert C.resolve_address("explicit-id") == "explicit-id"


def test_food_suggest_handles_search_failure(runner, mcp, monkeypatch):
    def flaky(server, tool, args):
        if tool == "search_menu":
            raise RuntimeError("upstream down")
        return mcp.responses[(server, tool)]
    patch_call(monkeypatch, flaky)
    r = runner.invoke(app, ["restaurant", "suggest", "--budget", "500"])
    assert r.exit_code == 0
    assert parse_out(r)["suggestions"] == []


def test_attach_images_survives_menu_failure(runner, mcp, monkeypatch):
    def flaky(server, tool, args):
        if tool == "get_restaurant_menu":
            raise RuntimeError("no menu")
        return mcp.responses[(server, tool)]
    patch_call(monkeypatch, flaky)
    data = parse_out(runner.invoke(app, ["restaurant", "dish", "platter", "--images"]))
    assert all(d["image_url"] is None for d in data["dishes"])


def test_menu_items_cached(runner, mcp):
    C._menu_items_for("9001", "addr")
    before = len([c for c in mcp.calls if c[1] == "get_restaurant_menu"])
    C._menu_items_for("9001", "addr")
    after = len([c for c in mcp.calls if c[1] == "get_restaurant_menu"])
    assert after == before          # served from cache


def test_place_advisory_near_miss_does_not_block(runner, mcp):
    """A top-up that costs more than it saves must warn, not halt."""
    mcp.set("food", "fetch_food_coupons",
            "Found 1 coupons (0 applicable):\n"
            "  - FLAT300 [❌ NOT APPLICABLE] — Add ₹5000 more to avail this offer (code: c-9)\n")
    runner.invoke(app, ["restaurant", "best-offer", "--restaurant", "9001", "--dry-run"])
    r = runner.invoke(app, ["restaurant", "place", "-y", "--max-total", "430", "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001",
                            "--ignore-card-offers", "--no-open"])
    assert r.exit_code == 0
    assert "not worth it" in r.stderr or "place_food_order" in mcp.tools_called()


def test_place_qr_rendered_when_present(runner, mcp, tmp_path, monkeypatch):
    monkeypatch.setattr(qr, "QR_DIR", tmp_path)
    monkeypatch.setattr(qr, "resolve_payment_page",
                        lambda url: {"upi_uri": "upi://pay?pa=a@b&am=430.00"})
    r = runner.invoke(app, ["restaurant", "place", "-y", "--max-total", "430", "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001", "--ignore-card-offers",
                            "--ignore-near-misses", "--no-open"])
    assert parse_out(r)["qr"]["kind"] == "upi_uri"


def test_place_without_a_verifiable_upi_artifact_is_blocked(runner, mcp):
    mcp.set("food", "place_food_order", "Order placed. Order 999888 confirmed. TO PAY: ₹430")
    r = runner.invoke(app, ["restaurant", "place", "-y", "--max-total", "430", "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001", "--ignore-card-offers",
                            "--ignore-near-misses", "--no-open"])
    assert r.exit_code == 5
    assert parse_out(r)["status"] == "blocked_unverified_payment_amount"


def test_im_checkout_qr(runner, mcp, tmp_path, monkeypatch):
    monkeypatch.setattr(qr, "QR_DIR", tmp_path)
    monkeypatch.setattr(qr, "resolve_payment_page",
                        lambda url: {"upi_uri": "upi://pay?pa=a@b&am=228.00"})
    r = runner.invoke(app, ["im", "checkout", "-y", "--max-total", "228",
                            "--payment", "UPI", "--intent-app", "gpay://upi/",
                            "--ignore-fees", "--no-open"])
    assert parse_out(r)["qr"]["kind"] == "upi_uri"


def test_pay_qr_from_stored_order(runner, mcp, tmp_path, monkeypatch):
    monkeypatch.setattr(qr, "QR_DIR", tmp_path)
    store.record_order("ord-77", "food", {"upi": "upi://pay?pa=a@b&am=99.00"}, amount=99)
    r = runner.invoke(app, ["pay", "qr", "ord-77", "--no-open"])
    assert r.exit_code == 0


def test_pay_qr_unknown_order(runner, mcp):
    assert runner.invoke(app, ["pay", "qr", "no-such-order"]).exit_code == 2


def test_pay_wait_uses_stored_ids(runner, mcp):
    store.set_pref("last_paas_id_instamart", "ppp-0001")
    store.set_pref("last_order_id_instamart", "8880001")
    r = runner.invoke(app, ["pay", "wait", "--kind", "instamart", "--timeout", "5"])
    assert r.exit_code == 0


def test_pay_wait_auto_confirms_when_needed(runner, mcp):
    mcp.set("instamart", "check_payment_status",
            ["ok", {"status": "success", "isTerminalSuccess": True}])
    r = runner.invoke(app, ["pay", "wait", "ppp-1", "--order-id", "8880001",
                            "--kind", "instamart", "--timeout", "5"])
    assert r.exit_code == 0
    assert "confirm_order" in mcp.tools_called()


def test_orders_list_local_empty(runner, mcp):
    assert parse_out(runner.invoke(app, ["orders", "list"])) == []


def test_config_non_json_value(runner, mcp):
    parse_out(runner.invoke(app, ["config", "address_mode", "ask"]))
    assert store.get_pref("address_mode") == "ask"


def test_auth_login_all_servers(runner, mcp):
    data = parse_out(runner.invoke(app, ["auth", "login"]))
    assert set(data) == set(client.SERVERS)


def test_auth_login_reports_error(runner, mcp, monkeypatch):
    async def boom(server, on_consent_url=None):
        raise RuntimeError("nope")
    monkeypatch.setattr(client, "list_tools", boom)
    from food_cli.commands import auth as cli_mod
    monkeypatch.setattr(cli_mod.client, "list_tools", boom)
    data = parse_out(runner.invoke(app, ["auth", "login", "--server", "food"]))
    assert data["food"]["status"] == "error"


def test_auth_login_print_url_only(runner, mcp, monkeypatch):
    async def needs_consent(server, on_consent_url=None):
        if on_consent_url:
            on_consent_url("https://example.com/authorize?x=1")
        raise RuntimeError("consent required")
    from food_cli.commands import auth as cli_mod
    monkeypatch.setattr(cli_mod.client, "list_tools", needs_consent)
    data = parse_out(runner.invoke(app, ["auth", "login", "--server", "food",
                                         "--print-url-only"]))
    assert data["food"]["status"] == "consent_required"
    assert data["food"]["consent_url"].startswith("https://example.com")


def test_tools_with_schema(runner, mcp):
    data = parse_out(runner.invoke(app, ["mcp", "list", "food", "--schema"]))
    assert "input_schema" in data[0]
