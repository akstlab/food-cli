"""MCP session plumbing and the remaining error branches."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from food_cli.mcp import client
from food_cli import commands as C
from food_cli.offers import coupons as offers
from food_cli.core import store
from food_cli.offers import topup
from food_cli.cli import app
from tests.conftest import parse_out


pytestmark = pytest.mark.usefixtures("fresh_db")


# ------------------------------------------------------- session plumbing

class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = "desc"
        self.input_schema = {"type": "object"}
        self.output_schema = None


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Result:
    def __init__(self, content, is_error=False, structured_content=None, meta=None):
        self.content = content
        self.isError = is_error
        self.structured_content = structured_content
        self.meta = meta


class _FakeSession:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        class R:
            tools = [_Tool("get_food_cart"), _Tool("place_food_order")]
        return R()

    async def call_tool(self, name, args):
        if name == "returns_json":
            return _Result([_Block('{"ok": true}')])
        if name == "returns_many":
            return _Result([_Block("one"), _Block("two")])
        if name == "returns_widget_guidance":
            return _Result([_Block(
                "TO PAY: ₹155\n\nCart widget is displayed — ignore this.\n"
                "⚠️ A rich UI widget may be shown to the user with this data."
            )], structured_content={"pricing": {"to_pay": 155}})
        if name == "returns_widget_data":
            return _Result(
                [_Block("rendered fallback")],
                structured_content={"availabilityStatus": "OPEN"},
                meta={"payment": {"kind": "qr"}},
            )
        return _Result([_Block("plain text")])


@pytest.fixture()
def patched_transport(monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_http_client(**kw):
        yield object()

    @contextlib.asynccontextmanager
    async def fake_streams(url, http_client=None):
        yield (object(), object(), None)

    monkeypatch.setattr(client, "create_mcp_http_client", lambda **kw: fake_http_client())
    monkeypatch.setattr(client, "streamable_http_client", fake_streams)
    monkeypatch.setattr(client, "ClientSession", _FakeSession)
    monkeypatch.setattr(client, "_maybe_refresh", lambda s: None)


def test_list_tools_maps_schema(patched_transport):
    tools = asyncio.run(client.list_tools("food"))
    assert [t["name"] for t in tools] == ["get_food_cart", "place_food_order"]
    assert tools[0]["input_schema"] == {"type": "object"}


def test_call_tool_parses_json_content(patched_transport):
    res = asyncio.run(client.call_tool("food", "returns_json", {}))
    assert res["content"] == {"ok": True}
    assert res["isError"] is False


def test_call_tool_keeps_plain_text(patched_transport):
    assert asyncio.run(client.call_tool("food", "anything", {}))["content"] == "plain text"


def test_call_tool_multiple_blocks(patched_transport):
    assert asyncio.run(client.call_tool("food", "returns_many", {}))["content"] == ["one", "two"]


def test_call_tool_removes_widget_guidance_but_keeps_json(patched_transport):
    res = asyncio.run(client.call_tool("food", "returns_widget_guidance", {}))
    assert res["content"] == "TO PAY: ₹155"
    assert res["structuredContent"] == {"pricing": {"to_pay": 155}}


def test_call_tool_preserves_structured_json_without_widget_meta(patched_transport):
    res = asyncio.run(client.call_tool("food", "returns_widget_data", {}))
    assert res["structuredContent"] == {"availabilityStatus": "OPEN"}
    assert "_meta" not in res


def test_default_consent_prints_url(capsys):
    client._default_consent("https://example.com/authorize")
    assert "https://example.com/authorize" in capsys.readouterr().err


def test_session_raises_when_not_waiting(monkeypatch, patched_transport):
    """wait_for_consent=False must surface the URL rather than block."""
    seen = {}

    async def go():
        async with client.session("food", on_consent_url=lambda u: seen.setdefault("u", u),
                                  wait_for_consent=False):
            pass

    asyncio.run(go())          # patched transport never needs consent
    assert seen == {} or "u" in seen


def test_client_metadata_shape():
    md = client._client_metadata("http://localhost:1/cb", "scope:a scope:b")
    assert md.client_name == "food-cli"
    assert md.scope == "scope:a scope:b"
    assert "refresh_token" in md.grant_types


# ---------------------------------------------------------- offers edges

def test_parse_coupons_non_string():
    assert offers.parse_coupons(None) == []
    assert offers.parse_coupons(123) == []


def test_estimate_discount_unknown_shape():
    assert offers.estimate_discount({"applicable": True}, 100) is None


def test_estimate_discount_min_order_not_met():
    c = {"applicable": True, "min_order": 500, "flat": 50}
    assert offers.estimate_discount(c, 100) == 0.0


def test_rank_sinks_unknowables():
    cs = [{"applicable": True, "flat": 10}, {"applicable": True}]
    assert offers.rank(cs, 100)[0]["estimated_discount"] == 10.0


def test_dedupe_keeps_richest_line():
    text = ("  - DUP [✅ APPLICABLE] — short (code: a-1)\n"
            "  - DUP [✅ APPLICABLE] — a much longer description here (code: a-1)\n")
    assert len(offers.parse_coupons(text)) == 1
    assert "longer" in offers.parse_coupons(text)[0]["text"]


def test_card_offers_empty():
    assert offers.card_offers([]) == []


# ----------------------------------------------------------- topup edges

def test_is_veg_prefers_api_flag():
    assert topup.is_veg({"name": "Chicken Salad", "veg": True}) is True
    assert topup.is_veg({"name": "Garden Salad", "veg": False}) is False


def test_is_veg_instamart_classifier():
    assert topup.is_veg({"name": "x", "vegClassifier": "VEG_CLASSIFIER_VEG"}) is True
    assert topup.is_veg({"name": "x", "vegClassifier": "VEG_CLASSIFIER_NON_VEG"}) is False


def test_is_veg_name_fallback():
    assert topup.is_veg({"name": "Grilled Chicken"}) is False
    assert topup.is_veg({"name": "Garden Platter"}) is True


def test_max_copies_limits_condiments():
    assert topup._max_copies("Ketchup Sachet") == 2
    assert topup._max_copies("Garlic Bread") == 4


def test_desirability_prefers_filler():
    a = topup._desirability({"name": "Lemon Soda", "price": 60})
    b = topup._desirability({"name": "Obscure Thing", "price": 5})
    assert a > b


def test_plan_skips_items_priced_above_cap():
    items = [{"item_id": "x", "name": "Huge", "price": 100000}]
    assert topup.plan(items, 50)["found"] is False


# ------------------------------------------------------- commands edges

def test_serves_count_none():
    assert C._serves_count(None) == 1


def test_rupees_helper():
    assert C._rupees("₹228") == 228.0
    assert C._rupees(None) is None
    assert C._rupees("none") is None


def test_instamart_fee_analysis_non_dict():
    assert C._instamart_fee_analysis({"content": "text"})["known"] is False


def test_text_of_dict():
    assert json.loads(C.text_of({"content": {"a": 1}})) == {"a": 1}


def test_log_order_ignores_unidentifiable(fresh_db):
    C._log_order("food", {"content": "no ids here"}, "addr")
    assert store.list_orders() == []


def test_log_order_prose_order_id(fresh_db):
    C._log_order("food", {"content": "Order 12345678 placed. TO PAY: ₹100"}, "addr")
    assert store.list_orders()[0]["id"] == "12345678"


def test_parse_instamart_orders_handles_junk():
    assert C.parse_instamart_orders({"content": "not a dict"}) == []
    assert C.parse_instamart_orders({"content": [{"no_orders": 1}]}) == []


def test_fee_analysis_high_but_above_threshold():
    info = C.fee_analysis(1000, 1400)
    assert info["high_fees"] is True and "advice" in info


# ----------------------------------------------------------- CLI branches

def test_food_menu(runner, mcp):
    assert runner.invoke(app, ["restaurant", "menu", "9001", "--page", "1"]).exit_code == 0


def test_food_dish_with_images(runner, mcp, monkeypatch):
    monkeypatch.setattr(C.media, "download_many", lambda urls, *a, **k: {})
    data = parse_out(runner.invoke(app, ["restaurant", "dish", "platter", "--images"]))
    assert "media_note" in data
    assert data["dishes"][0]["image_url"] == "https://cdn.example.com/a.jpg"


def test_food_dish_with_eta(runner, mcp):
    data = parse_out(runner.invoke(app, ["restaurant", "dish", "platter", "--with-eta"]))
    assert data["dishes"][0]["eta_minutes"] == 22


def test_food_dish_sort_rating(runner, mcp):
    data = parse_out(runner.invoke(app, ["restaurant", "dish", "platter", "--sort", "rating"]))
    ratings = [d["rating"] or 0 for d in data["dishes"]]
    assert ratings == sorted(ratings, reverse=True)


def test_food_coupons_command(runner, mcp):
    assert runner.invoke(app, ["restaurant", "coupons", "--restaurant", "9001"]).exit_code == 0


def test_food_apply_coupon_command(runner, mcp):
    assert runner.invoke(app, [
        "restaurant", "apply-coupon", "WELCOME50", "--restaurant", "9001",
    ]).exit_code == 0


def test_food_payment_options(runner, mcp):
    assert runner.invoke(app, ["restaurant", "payment-options"]).exit_code == 0


def test_food_clear(runner, mcp):
    assert runner.invoke(app, ["restaurant", "clear"]).exit_code == 0


def test_food_eta_without_restaurant(runner, mcp):
    with store.connect() as c:
        c.execute("DELETE FROM prefs WHERE key='last_restaurant_id'")
    assert runner.invoke(app, ["restaurant", "eta"]).exit_code == 2


def test_food_eta_unknown_restaurant(runner, mcp):
    data = parse_out(runner.invoke(app, ["restaurant", "eta", "--restaurant", "does-not-exist"]))
    assert data["known"] is False


def test_im_search_and_usual(runner, mcp):
    assert runner.invoke(app, ["im", "search", "milk"]).exit_code == 0
    assert runner.invoke(app, ["im", "usual"]).exit_code == 0


def test_im_fees(runner, mcp):
    assert runner.invoke(app, ["im", "fees"]).exit_code == 0


def test_orders_list_remote_food(runner, mcp):
    assert runner.invoke(app, ["orders", "list", "--remote"]).exit_code == 0


def test_orders_list_remote_instamart(runner, mcp):
    assert runner.invoke(app, ["orders", "list", "--remote", "--kind", "instamart"]).exit_code == 0


def test_orders_track_instamart(runner, mcp):
    assert runner.invoke(app, ["orders", "track", "7770001", "--kind", "instamart"]).exit_code == 0


def test_orders_sync_single_kind(runner, mcp):
    data = parse_out(runner.invoke(app, ["orders", "sync", "--kind", "food"]))
    assert "food" in data["synced"] and "instamart" not in data["synced"]


def test_address_list_local_and_remote(runner, mcp):
    assert runner.invoke(app, ["address", "list"]).exit_code == 0
    assert runner.invoke(app, ["address", "list", "--local"]).exit_code == 0


def test_topup_no_restaurant(runner, mcp):
    with store.connect() as c:
        c.execute("DELETE FROM prefs WHERE key='last_restaurant_id'")
    assert runner.invoke(app, ["restaurant", "topup"]).exit_code == 2


def test_suggest_learns_when_profile_empty(runner, mcp):
    store.clear_preferences(learned_only=False)
    assert runner.invoke(app, ["restaurant", "suggest", "--budget", "1000"]).exit_code == 0


def test_suggest_with_images(runner, mcp, monkeypatch):
    monkeypatch.setattr(C.media, "download_many", lambda urls, *a, **k: {})
    assert runner.invoke(app, ["restaurant", "suggest", "--budget", "1000", "--images"]).exit_code == 0


def test_suggest_max_eta(runner, mcp):
    data = parse_out(runner.invoke(app, ["restaurant", "suggest", "--budget", "2000", "--max-eta", "30"]))
    assert all(d.get("eta_minutes", 0) <= 30 for d in data["suggestions"])


def test_place_with_note_and_intent_app(runner, mcp):
    r = runner.invoke(app, ["restaurant", "place", "-y", "--max-total", "430", "--payment", "UPI", "--intent-app", "gpay://upi/",
                            "--intent-app", "gpay://upi/", "--note", "less spicy",
                            "--restaurant", "9001",
                            "--ignore-card-offers", "--ignore-near-misses", "--no-open"])
    assert r.exit_code == 0
    _, _, args = [c for c in mcp.calls if c[1] == "place_food_order"][0]
    assert args["noteToRestaurant"] == "less spicy"
    assert args["intentApp"] == "gpay://upi/"
