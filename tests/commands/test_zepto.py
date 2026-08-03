"""The Zepto group.

Zepto is the one provider where a fully hands-free order is possible, because
it takes cash on delivery. That makes the gates in front of `place` the most
important thing in this file.
"""

from __future__ import annotations

import pytest

from food_cli.cli import app
from food_cli.commands import zepto as Z
from food_cli.core import store
from tests.conftest import FAKE_ZEPTO_ADDRESSES, ZEPTO_ADDRESS_ID, parse_out

pytestmark = pytest.mark.usefixtures("fresh_db")


def select(mcp):
    """Give Zepto a store context, as a real session would."""
    store.set_pref("zepto_address_id", ZEPTO_ADDRESS_ID)
    store.set_pref("zepto_store_ready", Z.store_marker(ZEPTO_ADDRESS_ID))


# ---------------------------------------------------------------- addresses

def test_parse_addresses_pairs_labels_with_ids():
    rows = Z.parse_addresses(FAKE_ZEPTO_ADDRESSES)
    assert [r["label"] for r in rows] == ["Home", "Office"]
    assert rows[0]["id"] == ZEPTO_ADDRESS_ID
    assert "Baker Street" in rows[0]["text"]


def test_the_id_block_is_not_mistaken_for_an_address():
    """`1. "Home" → ID: ...` matches the prose shape too; it must not win."""
    rows = Z.parse_addresses(FAKE_ZEPTO_ADDRESSES)
    assert all("→" not in r["label"] for r in rows)
    assert len(rows) == 2


def test_parse_addresses_survives_junk():
    assert Z.parse_addresses("") == []
    assert Z.parse_addresses("no addresses here") == []


def test_addresses_command_exposes_ids_but_warns_against_speaking_them(runner, mcp):
    data = parse_out(runner.invoke(app, ["zepto", "addresses"]))
    assert data["addresses"][0]["id"] == ZEPTO_ADDRESS_ID
    assert "label or number" in data["note"]


def test_use_address_selects_and_remembers(runner, mcp):
    r = runner.invoke(app, ["zepto", "use-address", ZEPTO_ADDRESS_ID])
    assert r.exit_code == 0
    assert store.get_pref("zepto_address_id") == ZEPTO_ADDRESS_ID
    assert ("zepto", "select_saved_address", {"addressId": ZEPTO_ADDRESS_ID}) in mcp.calls


def test_no_address_is_explained_not_passed_upstream(runner, mcp):
    """Without a store, Zepto fails every call with 'Store not selected'.
    Saying so here is more use than relaying that."""
    r = runner.invoke(app, ["zepto", "search", "milk"])
    assert r.exit_code == 2
    assert "use-address" in r.stderr


# ------------------------------------------------------------------- search

def test_search_sets_store_context_first(runner, mcp):
    store.set_pref("zepto_address_id", ZEPTO_ADDRESS_ID)
    runner.invoke(app, ["zepto", "search", "oat milk"])
    assert mcp.tools_called()[0] == "select_saved_address"
    assert ("zepto", "search_products", {"query": "oat milk"}) in mcp.calls


def test_store_context_is_established_once(runner, mcp):
    select(mcp)
    runner.invoke(app, ["zepto", "search", "oat milk"])
    assert "select_saved_address" not in mcp.tools_called()


def test_search_many_sends_a_query_array(runner, mcp):
    select(mcp)
    runner.invoke(app, ["zepto", "search-many", "milk", "bread"])
    assert ("zepto", "search_multiple_products", {"queries": ["milk", "bread"]}) in mcp.calls


def test_usual_needs_no_store(runner, mcp):
    data = parse_out(runner.invoke(app, ["zepto", "usual"]))
    assert "Barista Oat Drink" in data["content"]


# --------------------------------------------------------------------- cart

def test_add_parses_the_two_ids_and_quantity(runner, mcp):
    select(mcp)
    runner.invoke(app, ["zepto", "add", "--item", "pv_001:sp_001:2"])
    args = dict(mcp.calls[-1][2])
    assert args["cartItems"] == [
        {"productVariantId": "pv_001", "storeProductId": "sp_001", "quantity": 2}
    ]
    assert args["deviceId"]


def test_quantity_defaults_to_one(runner, mcp):
    select(mcp)
    runner.invoke(app, ["zepto", "add", "--item", "pv_001:sp_001"])
    assert mcp.calls[-1][2]["cartItems"][0]["quantity"] == 1


def test_a_malformed_item_is_rejected_before_any_call(runner, mcp):
    select(mcp)
    r = runner.invoke(app, ["zepto", "add", "--item", "pv_001"])
    assert r.exit_code != 0
    assert "update_cart" not in mcp.tools_called()


def test_remove_is_quantity_zero(runner, mcp):
    select(mcp)
    runner.invoke(app, ["zepto", "remove", "--item", "pv_001:sp_001"])
    assert mcp.calls[-1][2]["cartItems"][0]["quantity"] == 0


def test_clear_replaces_the_cart_with_nothing(runner, mcp):
    select(mcp)
    runner.invoke(app, ["zepto", "clear"])
    args = mcp.calls[-1][2]
    assert args["cartItems"] == [] and args["replaceCart"] is True


def test_the_device_id_is_stable_across_commands(runner, mcp):
    select(mcp)
    runner.invoke(app, ["zepto", "add", "--item", "pv_001:sp_001"])
    first = mcp.calls[-1][2]["deviceId"]
    runner.invoke(app, ["zepto", "add", "--item", "pv_002:sp_002"])
    assert mcp.calls[-1][2]["deviceId"] == first


# ------------------------------------------------------------------ payment

def test_payment_options_flags_cod_as_hands_free(runner, mcp):
    data = parse_out(runner.invoke(app, ["zepto", "payment-options"]))
    assert data["cod_available"] is True
    assert data["hands_free_possible"] is True


def test_place_refuses_without_a_payment_method(runner, mcp):
    select(mcp)
    r = runner.invoke(app, ["zepto", "place", "-y"])
    assert r.exit_code == 2
    assert "payment-options" in r.stderr
    assert not [c for c in mcp.calls if c[1].startswith("create_")]


def test_place_refuses_without_confirmation(runner, mcp):
    select(mcp)
    r = runner.invoke(app, ["zepto", "place", "--payment", "cod"])
    assert r.exit_code == 2
    assert not [c for c in mcp.calls if c[1].startswith("create_")]


def test_place_rejects_an_unknown_payment_method(runner, mcp):
    select(mcp)
    r = runner.invoke(app, ["zepto", "place", "-y", "--payment", "bitcoin"])
    assert r.exit_code == 2
    assert not [c for c in mcp.calls if c[1].startswith("create_")]


@pytest.mark.parametrize("method,tool", [
    ("cod", "create_order"),
    ("online", "create_online_payment_order"),
    ("wallet", "create_wallet_order"),
    ("reserve", "create_upi_reserve_pay_order"),
])
def test_the_payment_method_picks_the_tool(runner, mcp, method, tool):
    select(mcp)
    data = parse_out(runner.invoke(app, ["zepto", "place", "-y", "--payment", method]))
    assert data["tool"] == tool
    assert tool in mcp.tools_called()


def test_cash_is_accepted_as_a_spelling_of_cod(runner, mcp):
    select(mcp)
    data = parse_out(runner.invoke(app, ["zepto", "place", "-y", "--payment", "Cash"]))
    assert data["payment_method"] == "cod"


def test_cod_says_there_is_nothing_to_pay_now(runner, mcp):
    select(mcp)
    r = runner.invoke(app, ["zepto", "place", "-y", "--payment", "cod"])
    assert "Cash on delivery" in r.stderr
    assert "credential" not in r.stderr


def test_an_online_order_hands_payment_back_to_the_user(runner, mcp):
    select(mcp)
    r = runner.invoke(app, ["zepto", "place", "-y", "--payment", "online"])
    assert "must be completed by the USER" in r.stderr


def test_placing_records_the_order_locally(runner, mcp):
    select(mcp)
    parse_out(runner.invoke(app, ["zepto", "place", "-y", "--payment", "cod"]))
    assert store.get_pref("last_order_id_zepto") == "99900011122"
    assert [o for o in store.list_orders() if o["kind"] == "zepto"]


def test_tip_and_wallet_are_passed_through(runner, mcp):
    select(mcp)
    runner.invoke(app, ["zepto", "place", "-y", "--payment", "cod",
                        "--tip", "20", "--zepto-cash"])
    args = mcp.calls[-1][2]
    assert args["riderTip"] == 20 and args["useZeptoCash"] is True
    assert args["confirmOrder"] is True


def test_pay_status_defaults_to_the_last_order(runner, mcp):
    select(mcp)
    runner.invoke(app, ["zepto", "place", "-y", "--payment", "online"])
    runner.invoke(app, ["zepto", "pay-status"])
    assert ("zepto", "check_payment_status", {"orderId": "99900011122"}) in mcp.calls


def test_pay_status_without_an_order_is_an_error(runner, mcp):
    assert runner.invoke(app, ["zepto", "pay-status"]).exit_code == 2


# ------------------------------------------------------------------- orders

def test_orders_and_detail(runner, mcp):
    runner.invoke(app, ["zepto", "orders", "--limit", "5"])
    assert ("zepto", "list_order_history", {"limit": 5}) in mcp.calls
    runner.invoke(app, ["zepto", "order", "99900011122"])
    assert ("zepto", "get_order_detail", {"orderId": "99900011122"}) in mcp.calls


def test_escape_hatch_passes_arguments_through(runner, mcp):
    runner.invoke(app, ["zepto", "call", "get_user_details", "--args", "{}"])
    assert ("zepto", "get_user_details", {}) in mcp.calls


def test_escape_hatch_rejects_bad_json(runner, mcp):
    assert runner.invoke(app, ["zepto", "call", "x", "--args", "{bad"]).exit_code == 2


def test_store_context_is_re_established_after_signing_in_again(runner, mcp):
    """Store context lives in the session behind the token, not on this machine.

    Caching it across a re-auth leaves every later call running without a store,
    which Zepto answers with empty results rather than an error.
    """
    select(mcp)
    runner.invoke(app, ["zepto", "search", "milk"])
    assert "select_saved_address" not in mcp.tools_called()

    # A new token: same address, different session.
    with store.connect() as c:
        c.execute(
            "INSERT INTO oauth(server,tokens,updated_at) VALUES('zepto',?,?) "
            "ON CONFLICT(server) DO UPDATE SET tokens=excluded.tokens, "
            "updated_at=excluded.updated_at",
            ('{"access_token":"new","expires_in":600}', 9999999999.0),
        )

    mcp.calls.clear()
    runner.invoke(app, ["zepto", "search", "milk"])
    assert "select_saved_address" in mcp.tools_called()
