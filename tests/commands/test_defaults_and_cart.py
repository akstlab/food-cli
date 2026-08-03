"""Defaults, cart customisation, and the coupon-maximising path.

All against the mocked MCP layer - no network, no account.
"""

from __future__ import annotations

import pytest

from food_cli import commands as C
from food_cli.commands import checkout as checkout_mod
from food_cli.core import store
from food_cli.cli import app
from tests.conftest import parse_out


pytestmark = pytest.mark.usefixtures("fresh_db")


def run(runner, *args):
    return runner.invoke(app, list(args))


# ----------------------------------------------------------------- defaults

def test_dish_results_are_capped_by_default(runner, mcp):
    """A long list is unusable aloud, so the default must be small."""
    data = parse_out(run(runner, "restaurant", "dish", "platter"))
    assert len(data["dishes"]) <= 10


def test_place_never_mutates_an_already_approved_cart(runner, mcp):
    """Coupon selection happens during add, never after the final approval."""
    mcp.set("food", "apply_food_coupon", "Coupon applied.\nNew total: ₹300")
    r = run(runner, "restaurant", "place", "-y", "--max-total", "430", "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001",
            "--ignore-card-offers", "--ignore-near-misses", "--no-open")
    assert r.exit_code == 0
    assert "apply_food_coupon" not in mcp.tools_called()
    assert parse_out(r)["coupon"]["status"] == "not_mutated_at_placement"


def test_auto_coupon_can_be_turned_off(runner, mcp):
    r = run(runner, "restaurant", "place", "-y", "--max-total", "430", "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001", "--no-auto-coupon",
            "--ignore-card-offers", "--ignore-near-misses", "--no-open")
    assert r.exit_code == 0
    assert "apply_food_coupon" not in mcp.tools_called()


def test_coupon_failure_never_blocks_a_confirmed_order(runner, mcp):
    def flaky(server, tool, args):
        if tool == "fetch_food_coupons":
            raise RuntimeError("coupons down")
        return mcp.responses[(server, tool)]

    import food_cli.commands as mod
    orig = mod.call
    mod.call = flaky
    try:
        r = run(runner, "restaurant", "place", "-y", "--max-total", "430", "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001",
                "--ignore-card-offers", "--ignore-near-misses", "--no-open")
    finally:
        mod.call = orig
    assert r.exit_code == 0


def test_place_requires_the_total_the_user_approved(runner, mcp):
    r = run(
        runner, "restaurant", "place", "-y", "--payment", "UPI", "--intent-app", "gpay://upi/",
        "--no-auto-coupon", "--ignore-card-offers", "--ignore-near-misses",
    )
    assert r.exit_code == 2
    assert "--max-total" in r.stderr
    assert "place_food_order" not in mcp.tools_called()


def test_place_stops_when_live_cart_exceeds_approved_total(runner, mcp):
    mcp.set(
        "food", "get_food_cart",
        "Item total: ₹100\nDelivery: ₹69\nTaxes & charges: ₹35\nTO PAY: ₹204",
    )
    r = run(
        runner, "restaurant", "place", "-y", "--max-total", "135",
        "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001", "--no-auto-coupon", "--ignore-card-offers",
        "--ignore-near-misses", "--no-open",
    )
    data = parse_out(r)
    assert r.exit_code == 5
    assert data["status"] == "blocked_total_changed"
    assert data["stage"] == "preflight"
    assert data["increase"] == 69
    assert data["delivery_fee"] == 69
    assert "place_food_order" not in mcp.tools_called()


def test_place_suppresses_payment_when_provider_adds_fee_during_placement(
    runner, mcp, monkeypatch,
):
    """Regression: preview was ₹135, but placement introduced a ₹69 fee."""
    mcp.set(
        "food", "get_food_cart",
        "Item total: ₹100\nDelivery: FREE\nTaxes & charges: ₹35\nTO PAY: ₹135",
    )
    mcp.set("food", "place_food_order", [
        "UPI payment initiated. Delivery: ₹69",
        {
            "orderId": "888000100000320",
            "cartTotal": 204,
            "status": "PENDING_PAYMENT",
            "bridgeUrl": "https://mcp.example.com/pay/changed-total",
        },
    ])

    def must_not_resolve(_url):
        raise AssertionError("changed-price payment must not be rendered or opened")

    monkeypatch.setattr(C.qr, "resolve_payment_page", must_not_resolve)
    r = run(
        runner, "restaurant", "place", "-y", "--max-total", "135",
        "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001", "--no-auto-coupon", "--ignore-card-offers",
        "--ignore-near-misses", "--no-open", "--wait",
    )
    data = parse_out(r)
    assert r.exit_code == 5
    assert data["status"] == "blocked_total_changed"
    assert data["stage"] == "post_placement"
    assert data["preflight_total"] == 135
    assert data["placed_total"] == 204
    assert data["increase"] == 69
    assert data["delivery_fee"] == {
        "preflight": 0.0, "placed": None, "verified": False,
        "changed": None,
        "explanation": (
            "Post-placement delivery fee was not returned; the cause of any "
            "total change is unknown."
        ),
    }
    assert "cause is unknown" in r.stderr
    assert data["payment_suppressed"] is True
    assert data["provider_response_suppressed"] is True
    assert "DO NOT PAY" in r.stderr
    assert "changed-total" not in r.stdout
    assert "check_payment_status" not in mcp.tools_called()
    assert store.list_orders() == []


def test_cart_exposes_delivery_and_exact_total_for_confirmation(runner, mcp):
    data = parse_out(run(runner, "restaurant", "cart"))
    assert data["checkout_preview"]["payable_total"] == 430
    assert data["checkout_preview"]["delivery_fee"] == 0
    assert data["checkout_preview"]["delivery_is_free"] is True
    assert data["checkout_preview"]["bill_breakdown"]["complete"] is True
    assert data["checkout_preview"]["bill_breakdown"]["source_tool"] == "get_food_cart.structuredContent.data.pricing"
    assert "bill_breakdown.complete" in data["checkout_preview"]["approval_note"]


def test_place_blocks_when_cart_has_no_reconciled_bill_breakdown(runner, mcp):
    mcp.set(
        "food", "get_food_cart",
        "Item total: ₹100\nDelivery: FREE\nTO PAY: ₹135",
    )
    r = run(
        runner, "restaurant", "place", "-y", "--max-total", "135",
        "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001", "--no-auto-coupon",
        "--ignore-card-offers", "--ignore-near-misses", "--no-open",
    )
    data = parse_out(r)
    assert r.exit_code == 5
    assert data["status"] == "blocked_incomplete_bill_breakdown"
    assert data["bill_breakdown"]["missing_fields"] == ["taxes_and_charges"]
    assert "place_food_order" not in mcp.tools_called()


def test_unparseable_placement_total_is_reconciled_from_order_details(runner, mcp):
    mcp.set(
        "food", "get_food_cart",
        "Item total: ₹145\nDelivery: FREE\nTaxes & charges: ₹35\nTO PAY: ₹180",
    )
    mcp.set("food", "place_food_order", [
        "UPI payment initiated.",
        {
            "orderId": "888000100000320",
            "status": "PENDING_PAYMENT",
            "appIntent": "gpay://upi/pay?pa=merchant@example&am=180",
        },
    ])
    mcp.set(
        "food", "get_food_order_details",
        "Order 888000100000320\nTotal paid: ₹180\nDelivery: FREE\nStatus: PENDING_PAYMENT",
    )
    r = run(
        runner, "restaurant", "place", "-y", "--max-total", "180",
        "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001", "--no-auto-coupon",
        "--ignore-card-offers", "--ignore-near-misses", "--no-open",
    )
    data = parse_out(r)
    assert r.exit_code == 0
    assert "get_food_order_details" in mcp.tools_called()
    assert data["price_guard"]["placed_total"] == 180
    assert data["price_guard"]["order_details_reconciliation"]["succeeded"] is True
    assert data["payment"]["order_id"] == "888000100000320"


def test_unverified_placed_total_still_blocks_after_reconciliation_fails(runner, mcp):
    mcp.set("food", "place_food_order", [
        "UPI payment initiated.",
        {"orderId": "888000100000321", "status": "PENDING_PAYMENT"},
    ])
    mcp.set(
        "food", "get_food_order_details",
        "Order 888000100000321\nStatus: PENDING_PAYMENT\nAmount unavailable",
    )
    r = run(
        runner, "restaurant", "place", "-y", "--max-total", "430",
        "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001", "--no-auto-coupon",
        "--ignore-card-offers", "--ignore-near-misses", "--no-open",
    )
    data = parse_out(r)
    assert r.exit_code == 5
    assert data["status"] == "blocked_unverified_placed_total"
    assert data["order_details_reconciliation"]["attempted"] is True
    assert data["order_details_reconciliation"]["succeeded"] is False
    assert data["payment_suppressed"] is True


def test_unknown_post_placement_fee_is_not_guessed(runner, mcp):
    mcp.set(
        "food", "get_food_cart",
        "Item total: ₹105\nDelivery: FREE\nTaxes & charges: ₹35\nTO PAY: ₹140",
    )
    mcp.set("food", "place_food_order", [
        "UPI payment initiated.",
        {"orderId": "888000100000322", "status": "PENDING_PAYMENT"},
    ])
    mcp.set(
        "food", "get_food_order_details",
        "Order 888000100000322\nTotal paid: ₹230\nStatus: PENDING_PAYMENT",
    )
    r = run(
        runner, "restaurant", "place", "-y", "--max-total", "140",
        "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001", "--no-auto-coupon",
        "--ignore-card-offers", "--ignore-near-misses", "--no-open",
    )
    data = parse_out(r)
    assert r.exit_code == 5
    assert data["status"] == "blocked_total_changed"
    assert data["delivery_fee"] == {
        "preflight": 0.0,
        "placed": None,
        "verified": False,
        "changed": None,
        "explanation": (
            "Post-placement delivery fee was not returned; the cause of any "
            "total change is unknown."
        ),
    }
    assert "cause is unknown" in r.stderr
    assert "Swift" not in r.stderr


def test_food_uses_generic_upi_when_paywithqr_is_advertised(runner, mcp):
    r = run(
        runner, "restaurant", "place", "-y", "--max-total", "430",
        "--payment", "UPI", "--restaurant", "9001",
        "--ignore-card-offers", "--ignore-near-misses", "--no-open",
    )
    data = parse_out(r)
    assert r.exit_code == 0
    _, _, args = [c for c in mcp.calls if c[1] == "place_food_order"][-1]
    assert args["generateUPIQR"] is True
    assert "intentApp" not in args
    assert data["intent_app_choice"]["mode"] == "generic_qr"


def test_food_requires_an_app_when_generic_upi_is_not_advertised(runner, mcp):
    mcp.set("food", "get_payment_options", [
        "UPI apps for this cart.",
        {"allMethods": [
            {"id": "gpay://upi/", "displayName": "Google Pay",
             "groupName": "UPI", "enabled": True},
        ]},
    ])
    r = run(
        runner, "restaurant", "place", "-y", "--max-total", "430",
        "--payment", "UPI", "--restaurant", "9001",
        "--ignore-card-offers", "--ignore-near-misses", "--no-open",
    )
    data = parse_out(r)
    assert r.exit_code == 3
    assert data["status"] == "blocked_upi_app_choice"
    assert data["intent_app_choice"]["available"][0]["name"] == "Google Pay"
    assert "place_food_order" not in mcp.tools_called()


def test_payment_options_exposes_named_apps_and_saved_preference(runner, mcp):
    store.set_pref("preferred_upi_app", {"id": "gpay://upi/", "name": "Google Pay"})
    data = parse_out(run(runner, "restaurant", "payment-options"))
    assert data["generic_upi_qr"]["id"] == "PayWithQR"
    assert data["upi_apps"] == [{"id": "gpay://upi/", "name": "Google Pay"}]
    assert data["preferred_upi_app"]["name"] == "Google Pay"


def test_payment_capabilities_read_structured_data_channel():
    res = {
        "content": "Payment options are ready.",
        "structuredContent": {
            "data": {
                "platforms": {
                    "desktop": {"methods": [{"id": "PayWithQR", "enabled": True}]},
                },
                "allMethods": [
                    {"id": "gpay://upi/", "displayName": "Google Pay",
                     "groupName": "UPI", "enabled": True},
                ],
            },
        },
    }
    assert checkout_mod.generic_upi_qr(res)["id"] == "PayWithQR"
    assert checkout_mod.intent_app_choices(res)[0]["name"] == "Google Pay"


def test_payment_option_lookup_retries_transient_failures(mcp, monkeypatch):
    state = {"calls": 0}

    def flaky(server, tool, args):
        state["calls"] += 1
        if state["calls"] < 3:
            raise RuntimeError("unhandled errors in a TaskGroup")
        return mcp.responses[(server, tool)]

    monkeypatch.setattr(checkout_mod, "call", flaky)
    monkeypatch.setattr(checkout_mod.time, "sleep", lambda _seconds: None)
    chosen, info = checkout_mod.choose_intent_app(
        "food", "addr_test_001", "Google Pay",
    )
    assert state["calls"] == 3
    assert chosen == "gpay://upi/"
    assert info["selected"]["name"] == "Google Pay"


def test_instamart_uses_generic_upi_only_when_provider_advertises_it(runner, mcp):
    r = run(
        runner, "im", "checkout", "-y", "--max-total", "228", "--payment", "UPI",
        "--ignore-fees", "--no-open",
    )
    assert r.exit_code == 0
    args = [call[2] for call in mcp.calls if call[1] == "checkout"][-1]
    assert args["generateUPIQR"] is True
    assert "intentApp" not in args


def test_instamart_requires_app_choice_when_generic_upi_is_unavailable(runner, mcp):
    mcp.set("instamart", "get_payment_options", [{
        "allMethods": [
            {"id": "gpay://upi/", "displayName": "Google Pay",
             "groupName": "UPI", "enabled": True},
        ],
    }])
    r = run(
        runner, "im", "checkout", "-y", "--max-total", "228", "--payment", "UPI",
        "--ignore-fees", "--no-open",
    )
    assert r.exit_code == 3
    assert parse_out(r)["status"] == "blocked_upi_app_choice"
    assert "checkout" not in mcp.tools_called()


def test_the_widget_qr_path_is_not_reachable(runner, mcp):
    """The generic route is capability-driven, not a forceable CLI flag."""
    r = run(runner, "restaurant", "place", "-y", "--max-total", "430", "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001",
            "--upi-qr", "--ignore-card-offers", "--ignore-near-misses", "--no-open")
    assert r.exit_code != 0, "--upi-qr should no longer be accepted"
    assert not [c for c in mcp.calls if c[1] == "place_food_order"]


def test_explicit_upi_app_is_validated_and_saved(runner, mcp):
    run(runner, "restaurant", "place", "-y", "--max-total", "430",
        "--payment", "UPI", "--intent-app", "Google Pay", "--restaurant", "9001",
        "--ignore-card-offers", "--ignore-near-misses", "--no-open")
    _, _, args = [c for c in mcp.calls if c[1] == "place_food_order"][-1]
    assert args["intentApp"] == "gpay://upi/"
    assert "generateUPIQR" not in args
    assert store.get_pref("preferred_upi_app") == {
        "id": "gpay://upi/", "name": "Google Pay",
    }


def test_saved_upi_app_is_reused_when_still_enabled(runner, mcp):
    store.set_pref("preferred_upi_app", {"id": "gpay://upi/", "name": "Google Pay"})
    mcp.set("food", "get_payment_options", [
        "UPI apps for this cart.",
        {"allMethods": [
            {"id": "gpay://upi/", "displayName": "Google Pay",
             "groupName": "UPI", "enabled": True},
        ]},
    ])
    run(runner, "restaurant", "place", "-y", "--max-total", "430",
        "--payment", "UPI", "--restaurant", "9001",
        "--ignore-card-offers", "--ignore-near-misses", "--no-open")
    _, _, args = [c for c in mcp.calls if c[1] == "place_food_order"][-1]
    assert args["intentApp"] == "gpay://upi/"


def test_saved_upi_app_never_falls_back_when_unavailable(runner, mcp):
    store.set_pref("preferred_upi_app", {"id": "gpay://upi/", "name": "Google Pay"})
    mcp.set("food", "get_payment_options", [
        "UPI options for this cart.",
        {"allMethods": [
            {"id": "phonepe://", "displayName": "PhonePe", "groupName": "UPI", "enabled": True},
            {"id": "gpay://upi/", "groupName": "UPI", "enabled": False},
        ]},
    ])
    r = run(runner, "restaurant", "place", "-y", "--max-total", "430",
            "--payment", "UPI", "--restaurant", "9001",
            "--ignore-card-offers", "--ignore-near-misses", "--no-open")
    data = parse_out(r)
    assert r.exit_code == 3
    assert data["status"] == "blocked_upi_app_choice"
    assert data["intent_app_choice"]["available"] == [
        {"id": "phonepe://", "name": "PhonePe"},
    ]
    assert "place_food_order" not in mcp.tools_called()


def test_no_usable_upi_app_refuses_rather_than_placing(runner, mcp):
    """Without an intent there is nothing payable to hand back, so placing the
    order would only create a pending charge the user cannot settle."""
    mcp.set("food", "get_payment_options", [
        "Cash only.",
        {"allMethods": [{"id": "cod", "groupName": "COD", "enabled": True}]},
    ])
    r = run(runner, "restaurant", "place", "-y", "--max-total", "430",
            "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001",
            "--ignore-card-offers", "--ignore-near-misses", "--no-open")
    assert r.exit_code == 3
    assert parse_out(r)["status"] == "blocked_no_payable_upi"
    assert not [c for c in mcp.calls if c[1] == "place_food_order"]


def test_explicit_intent_app_is_not_overridden(runner, mcp):
    mcp.set("food", "get_payment_options", [
        "UPI options for this cart.",
        {"allMethods": [
            {"id": "phonepe://", "displayName": "PhonePe", "enabled": True},
        ]},
    ])
    run(runner, "restaurant", "place", "-y", "--max-total", "430",
        "--payment", "UPI", "--intent-app", "PhonePe", "--restaurant", "9001",
        "--ignore-card-offers", "--ignore-near-misses", "--no-open")
    _, _, args = [c for c in mcp.calls if c[1] == "place_food_order"][-1]
    assert args["intentApp"] == "phonepe://"


def test_headless_env_suppresses_auto_open(monkeypatch):
    monkeypatch.setenv("FOOD_CLI_NO_OPEN", "1")
    assert C._no_open_default(False) is True
    monkeypatch.delenv("FOOD_CLI_NO_OPEN")
    assert C._no_open_default(False) is False


def test_poll_floor_protects_the_endpoint(runner, mcp):
    """Swiggy asks not to poll in a tight loop; the auto path must not."""
    assert C.MIN_POLL_INTERVAL >= 15


# ------------------------------------------------------- addons and variants

def test_add_sends_addons_and_variants(runner, mcp):
    run(runner, "restaurant", "add", "--restaurant", "9001", "--item", "it_100:1",
        "--variant", "it_100:g1:v9:Large",
        "--addon", "it_100:g4:c7:Cheese:30")
    _, _, args = [c for c in mcp.calls if c[1] == "update_food_cart"][0]
    item = args["cartItems"][0]
    assert item["variants"] == [{"variation_id": "v9", "group_id": "g1", "name": "Large"}]
    assert item["addons"][0]["choice_id"] == "c7"
    assert item["addons"][0]["price"] == "30"


def test_addon_for_an_item_not_in_the_cart_is_rejected(runner, mcp):
    r = run(runner, "restaurant", "add", "--restaurant", "9001", "--item", "it_100:1",
            "--addon", "it_999:g1:c1")
    assert r.exit_code == 2


def test_malformed_addon_is_rejected(runner, mcp):
    r = run(runner, "restaurant", "add", "--restaurant", "9001", "--item", "it_100:1",
            "--addon", "it_100:only-two")
    assert r.exit_code == 2


def test_items_json_escape_hatch(runner, mcp):
    run(runner, "restaurant", "add", "--restaurant", "9001", "--items-json",
        '[{"menu_item_id":"it_100","quantity":3,"addons":[{"choice_id":"c1"}]}]')
    _, _, args = [c for c in mcp.calls if c[1] == "update_food_cart"][0]
    assert args["cartItems"][0]["quantity"] == 3


def test_items_json_must_be_a_list(runner, mcp):
    assert run(runner, "restaurant", "add", "--restaurant", "9001",
               "--items-json", '{"not":"a list"}').exit_code == 2


def test_add_requires_something_to_add(runner, mcp):
    assert run(runner, "restaurant", "add", "--restaurant", "9001").exit_code == 2


def test_addons_survive_a_cart_rewrite(runner, mcp):
    """Swiggy has no partial update, so a rewrite must replay customisations."""
    run(runner, "restaurant", "add", "--restaurant", "9001", "--item", "it_100:1",
        "--item", "it_101:1", "--addon", "it_100:g4:c7")
    run(runner, "restaurant", "remove", "--item", "it_101")
    _, _, args = [c for c in mcp.calls if c[1] == "update_food_cart"][-1]
    kept = [i for i in args["cartItems"] if i["menu_item_id"] == "it_100"][0]
    assert kept["addons"][0]["choice_id"] == "c7"


# -------------------------------------------------------------- food edit

def test_edit_changes_quantity(runner, mcp):
    store.set_pref("last_restaurant_id", "9001")
    data = parse_out(run(runner, "restaurant", "edit", "--item", "it_100", "--qty", "5"))
    assert data["status"] == "updated"
    row = [i for i in data["cart_items"] if i["menu_item_id"] == "it_100"][0]
    assert row["quantity"] == 5


def test_edit_replaces_addons(runner, mcp):
    run(runner, "restaurant", "add", "--restaurant", "9001", "--item", "it_100:1",
        "--addon", "it_100:g4:c7")
    data = parse_out(run(runner, "restaurant", "edit", "--item", "it_100",
                         "--addon", "it_100:g4:c9"))
    row = [i for i in data["cart_items"] if i["menu_item_id"] == "it_100"][0]
    assert [a["choice_id"] for a in row["addons"]] == ["c9"]


def test_edit_can_clear_addons(runner, mcp):
    run(runner, "restaurant", "add", "--restaurant", "9001", "--item", "it_100:1",
        "--addon", "it_100:g4:c7")
    data = parse_out(run(runner, "restaurant", "edit", "--item", "it_100", "--clear-addons"))
    row = [i for i in data["cart_items"] if i["menu_item_id"] == "it_100"][0]
    assert "addons" not in row


def test_edit_zero_quantity_drops_the_item(runner, mcp):
    store.set_pref("last_restaurant_id", "9001")
    data = parse_out(run(runner, "restaurant", "edit", "--item", "it_100", "--qty", "0"))
    assert all(i["menu_item_id"] != "it_100" for i in data.get("cart_items", []))


def test_edit_rejects_an_item_not_in_the_cart(runner, mcp):
    store.set_pref("last_restaurant_id", "9001")
    assert run(runner, "restaurant", "edit", "--item", "nope", "--qty", "1").exit_code == 2


def test_edit_rejects_addon_for_a_different_item(runner, mcp):
    store.set_pref("last_restaurant_id", "9001")
    r = run(runner, "restaurant", "edit", "--item", "it_100", "--addon", "it_101:g1:c1")
    assert r.exit_code == 2


# --------------------------------------------------------------- maximize

def test_maximize_reports_without_applying(runner, mcp):
    mcp.set("food", "apply_food_coupon", "Coupon applied.\nNew total: ₹300")
    data = parse_out(run(runner, "restaurant", "maximize", "--restaurant", "9001"))
    assert data["status"] in ("would_improve", "not_worth_it", "already_optimal")
    # nothing was added to the cart
    assert not [c for c in mcp.calls
                if c[1] == "update_food_cart" and len(c[2].get("cartItems", [])) > 2]


def test_maximize_nets_off_the_current_discount(runner, mcp):
    """A bigger headline coupon is not a bigger saving if it replaces one."""
    before = {"best": {"saving": 125.0},
              "near_misses": [{"code": "BIG", "spend_more": 89.0, "would_save": 200.0}]}
    up = C.topup_upside(before, "9001", "addr")
    assert up is not None
    # gain is 200 - 125 = 75, not 200
    assert up["discount_gain"] == 75.0
    assert up["extra_cash"] == pytest.approx(up["extra_food_value"] - 75.0)


def test_maximize_declines_when_it_costs_more(runner, mcp):
    mcp.set("food", "fetch_food_coupons",
            "Found 1 coupons (0 applicable):\n"
            "  - BIG [❌ NOT APPLICABLE] — Add ₹500 more to get a Flat ₹60 off (code: c-1)\n")
    data = parse_out(run(runner, "restaurant", "maximize", "--restaurant", "9001"))
    assert data["status"] in ("not_worth_it", "already_optimal")


def test_maximize_without_restaurant(runner, mcp):
    with store.connect() as c:
        c.execute("DELETE FROM prefs WHERE key='last_restaurant_id'")
    assert run(runner, "restaurant", "maximize").exit_code == 2


# ------------------------------------------------------------ payment block

def test_payment_block_is_flat_and_complete(runner, mcp, tmp_path, monkeypatch):
    from food_cli.core import qr
    monkeypatch.setattr(C.qr, "QR_DIR", tmp_path)
    monkeypatch.setattr(qr, "resolve_payment_page",
                        lambda url: {"upi_uri": "gpay://upi/pay?pa=x@y&am=430.00"})
    mcp.set("food", "place_food_order", [
        "UPI payment initiated.",
        {"orderId": "8880001", "paasId": "ppp-0001", "cartTotal": 430,
         "status": "PENDING_PAYMENT",
         "bridgeUrl": "https://mcp.example.com/deeplink-redirect?mode=qr"},
    ])
    r = run(runner, "restaurant", "place", "-y", "--max-total", "430", "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001",
            "--ignore-card-offers", "--ignore-near-misses", "--no-open")
    pay = parse_out(r)["payment"]
    assert pay["upi_id"] == "x@y"
    assert pay["amount"] == 430.0
    assert pay["app_intent"].startswith("gpay://")
    assert pay["qr_png"].endswith(".png")
    assert pay["order_id"] == "8880001"


def test_structured_payment_returns_qr_paths_and_https_link(runner, mcp, tmp_path, monkeypatch):
    from tests.conftest import FAKE_PLACE_ORDER

    monkeypatch.setattr(C.qr, "QR_DIR", tmp_path)
    mcp.responses[("food", "place_food_order")] = {
        "isError": False,
        "content": FAKE_PLACE_ORDER[0],
        "structuredContent": {
            "data": {
                "orderId": "8880001",
                "paasId": "ppp-0001",
                "cartTotal": 430,
                "status": "PENDING_PAYMENT",
                "isQrFlow": True,
                "upiIntentUrl": "upi://pay?pa=merchant@bank&am=430.00&cu=INR",
                "bridgeUrl": "https://mcp.example.com/pay/order-8880001",
            },
        },
    }
    r = run(
        runner, "restaurant", "place", "-y", "--max-total", "430",
        "--payment", "UPI", "--restaurant", "9001", "--no-auto-coupon",
        "--ignore-card-offers", "--ignore-near-misses", "--no-open",
    )
    data = parse_out(r)
    assert r.exit_code == 0
    assert data["payment"]["qr_png"].endswith("8880001.png")
    assert data["payment"]["qr_svg"].endswith("8880001.svg")
    assert data["payment"]["payment_link"] == "https://mcp.example.com/pay/order-8880001"
    assert data["payment_link"] == data["payment"]["payment_link"]
    assert (tmp_path / "8880001.png").is_file()


def test_food_suppresses_mismatched_payment_qr_and_link(runner, mcp, tmp_path, monkeypatch):
    monkeypatch.setattr(C.qr, "QR_DIR", tmp_path)
    mcp.responses[("food", "place_food_order")] = {
        "isError": False,
        "content": "PENDING_PAYMENT",
        "structuredContent": {
            "orderId": "8880001",
            "paasId": "ppp-0001",
            "totalAmount": 430,
            "status": "PENDING_PAYMENT",
            "upiIntentUrl": "upi://pay?pa=merchant@bank&am=492.00&cu=INR",
            "bridgeUrl": "https://mcp.example.com/pay/unsafe-order",
        },
    }
    mcp.set(
        "food", "get_food_order_details",
        "Order 8880001\nTotal paid: ₹430\nStatus: PENDING_PAYMENT",
    )
    r = run(
        runner, "restaurant", "place", "-y", "--max-total", "430",
        "--payment", "UPI", "--restaurant", "9001", "--no-auto-coupon",
        "--ignore-card-offers", "--ignore-near-misses", "--no-open",
    )
    data = parse_out(r)
    assert r.exit_code == 5
    assert data["status"] == "blocked_payment_amount_mismatch"
    assert data["payment_artifact"]["amount"] == 492
    assert data["payment_artifact"]["expected_total"] == 430
    assert data["payment_suppressed"] is True
    assert "payment_link" not in data and "qr" not in data
    assert not (tmp_path / "8880001.png").exists()


def test_food_suppresses_created_order_from_wrong_restaurant(runner, mcp):
    mcp.responses[("food", "get_food_order_details")] = {
        "isError": False,
        "content": "Order 8880001\nTotal paid: ₹430\nStatus: PENDING_PAYMENT",
        "structuredContent": {
            "order": {
                "orderId": "8880001",
                "restaurant": {"id": "wrong-restaurant", "name": "Wrong Place"},
                "items": [
                    {"name": "Garden Platter", "quantity": 1},
                    {"name": "Truffle Fries", "quantity": 1},
                ],
            },
        },
    }
    r = run(
        runner, "restaurant", "place", "-y", "--max-total", "430",
        "--payment", "UPI", "--restaurant", "9001", "--no-auto-coupon",
        "--ignore-card-offers", "--ignore-near-misses", "--no-open",
    )
    data = parse_out(r)
    assert r.exit_code == 5
    assert data["status"] == "blocked_created_order_mismatch"
    assert data["order_context"]["restaurant_matches"] is False
    assert data["payment_suppressed"] is True
    assert "payment_link" not in data and "qr" not in data


def test_instamart_structured_payment_returns_qr_path_and_link(runner, mcp, tmp_path, monkeypatch):
    monkeypatch.setattr(C.qr, "QR_DIR", tmp_path)
    mcp.responses[("instamart", "checkout")] = {
        "isError": False,
        "content": "Instamart payment initiated.",
        "structuredContent": {
            "data": {
                "orderId": "8880001",
                "paasId": "ppp-0001",
                "cartTotal": 228,
                "status": "PENDING_PAYMENT",
                "upiIntentUrl": "upi://pay?pa=merchant@bank&am=228.00&cu=INR",
                "bridgeUrl": "https://mcp.example.com/pay/im-order-8880001",
            },
        },
    }
    r = run(
        runner, "im", "checkout", "-y", "--max-total", "228",
        "--payment", "UPI", "--ignore-fees", "--no-open",
    )
    data = parse_out(r)
    assert r.exit_code == 0
    assert data["payment"]["qr_png"].endswith("8880001.png")
    assert data["payment"]["payment_link"] == (
        "https://mcp.example.com/pay/im-order-8880001"
    )
    assert data["payment_link"] == data["payment"]["payment_link"]
    assert data["intent_app_choice"]["mode"] == "generic_qr"


def test_instamart_suppresses_payment_when_qr_amount_changes(runner, mcp):
    mcp.set("instamart", "checkout", {
        "orderId": "8880001", "cartTotal": 228, "status": "PENDING_PAYMENT",
        "upiIntentUrl": "upi://pay?pa=merchant@bank&am=260.00&cu=INR",
    })
    r = run(
        runner, "im", "checkout", "-y", "--max-total", "228",
        "--payment", "UPI", "--ignore-fees", "--no-open",
    )
    data = parse_out(r)
    assert r.exit_code == 5
    assert data["status"] == "blocked_payment_amount_mismatch"
    assert data["payment_suppressed"] is True
    assert "payment" not in data and "qr" not in data


def test_pay_qr_refuses_legacy_unverified_pending_record(runner):
    store.set_pref("pending_payment_last", {
        "order_id": "legacy-order",
        "upi_uri": "upi://pay?pa=merchant@bank&am=492.00&cu=INR",
        "amount": 492,
    })
    r = runner.invoke(app, ["pay", "qr", "--no-open"])
    assert r.exit_code == 5
    assert parse_out(r)["status"] == "blocked_unverified_payment_amount"


# ------------------------------------------------- payment lifecycle (widget)

def test_wait_confirms_when_swiggy_has_not(runner, mcp):
    """The widget calls confirm_order after payment; with no widget, we must."""
    mcp.set("instamart", "check_payment_status",
            ["ok", {"status": "success", "isTerminalSuccess": True}])
    res = C.wait_for_payment("instamart", "ppp-1", "8880001", "addr", timeout=5)
    assert res["status"] == "paid"
    assert res["already_confirmed"] is True
    assert "confirm_order" in mcp.tools_called()


def test_wait_skips_confirm_when_already_confirmed(runner, mcp):
    res = C.wait_for_payment("instamart", "ppp-1", "8880001", "addr", timeout=5)
    assert res["status"] == "paid"
    assert "confirm_order" not in mcp.tools_called()


def test_wait_reports_failure_and_marks_the_order(runner, mcp):
    store.record_order("8880001", "instamart", {}, amount=100, status="PENDING_PAYMENT")
    mcp.set("instamart", "check_payment_status",
            ["no", {"status": "failed", "isTerminalFailure": True}])
    res = C.wait_for_payment("instamart", "ppp-1", "8880001", "addr", timeout=5)
    assert res["status"] == "failed"
    assert store.list_orders()[0]["status"] == "PAYMENT_FAILED"


def test_wait_follows_the_servers_cadence(runner, mcp):
    """A pollIntervalSec hint should be honoured, never undercut."""
    mcp.set("instamart", "check_payment_status",
            ["pending", {"status": "pending", "pollIntervalSec": 45}])
    seen = []
    C.wait_for_payment("instamart", "ppp-1", "8880001", "addr",
                       timeout=0.1, on_tick=lambda n, r, every: seen.append(every))
    assert all(v >= C.MIN_POLL_INTERVAL for v in seen)


def test_place_can_drive_payment_to_completion(runner, mcp):
    r = run(runner, "restaurant", "place", "-y", "--max-total", "430", "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001", "--wait",
            "--wait-timeout", "5", "--ignore-card-offers", "--ignore-near-misses",
            "--no-open")
    assert r.exit_code == 0
    assert parse_out(r)["wait"]["status"] == "paid"


def test_place_without_wait_does_not_poll(runner, mcp):
    run(runner, "restaurant", "place", "-y", "--max-total", "430", "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001",
        "--ignore-card-offers", "--ignore-near-misses", "--no-open")
    assert "check_payment_status" not in mcp.tools_called()


# --------------------------------------------------- payment must be chosen

def test_place_refuses_without_an_explicit_payment_method(runner, mcp):
    """Swiggy picks silently if omitted - possibly Cash on delivery."""
    r = run(runner, "restaurant", "place", "-y", "--max-total", "430", "--restaurant", "9001",
            "--ignore-card-offers", "--ignore-near-misses", "--no-open")
    assert r.exit_code == 2
    assert "place_food_order" not in mcp.tools_called()
    assert "--payment" in r.stderr


def test_checkout_refuses_without_an_explicit_payment_method(runner, mcp):
    r = run(runner, "im", "checkout", "-y", "--ignore-fees", "--no-open")
    assert r.exit_code == 2
    assert "checkout" not in mcp.tools_called()


def test_cash_is_never_chosen_for_the_user(runner, mcp):
    """Nothing may quietly commit the user to paying a courier."""
    run(runner, "im", "checkout", "-y", "--ignore-fees", "--no-open")
    calls = [c for c in mcp.calls if c[1] == "checkout"]
    assert not calls
