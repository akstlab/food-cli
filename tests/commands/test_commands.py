"""End-to-end CLI tests against a fully mocked MCP layer.

Nothing here touches the network or a real Swiggy account.
"""

from __future__ import annotations


from food_cli.core import store
from food_cli.cli import app
from tests.conftest import parse_out


def run(runner, *args):
    return runner.invoke(app, list(args))


# --------------------------------------------------------------------- food

def test_food_search_returns_structured_rows(runner, mcp):
    r = run(runner, "restaurant", "search", "pizza")
    assert r.exit_code == 0
    data = parse_out(r)
    assert len(data["restaurants"]) == 3
    assert data["eta_summary"]["fastest_minutes"] == 22
    assert "dish_results" in data and "note" in data


def test_food_search_max_eta_reports_what_it_hid(runner, mcp):
    data = parse_out(run(runner, "restaurant", "search", "pizza", "--max-eta", "30"))
    assert [r["id"] for r in data["restaurants"]] == ["9001"]
    assert "filtered_out" in data          # never silently truncate


def test_food_search_fastest_sorts(runner, mcp):
    data = parse_out(run(runner, "restaurant", "search", "pizza", "--fastest"))
    etas = [r["eta_minutes"] for r in data["restaurants"]]
    assert etas == sorted(etas)


def test_food_dish_sorted_and_limited(runner, mcp):
    data = parse_out(run(runner, "restaurant", "dish", "platter", "--sort", "price", "--limit", "2"))
    prices = [d["price"] for d in data["dishes"]]
    assert prices == sorted(prices) and len(prices) == 2
    assert "truncated" in data


def test_food_dish_max_price_reports_filtering(runner, mcp):
    data = parse_out(run(runner, "restaurant", "dish", "platter", "--max-price", "300"))
    assert all(d["price"] <= 300 for d in data["dishes"])
    assert "filtered_out" in data


def test_food_cart_includes_eta(runner, mcp):
    store.set_pref("last_restaurant_id", "9001")
    store.set_pref("cart_items:9001", {
        "it_100": {"menu_item_id": "it_100", "quantity": 1},
        "it_101": {"menu_item_id": "it_101", "quantity": 1},
    })
    data = parse_out(run(runner, "restaurant", "cart"))
    assert data["eta"]["eta_minutes"] == 22
    assert "9:" in data["eta"]["spoken"] or "pm" in data["eta"]["spoken"] or "am" in data["eta"]["spoken"]


def test_food_cart_suppresses_eta_for_stale_restaurant_context(runner, mcp):
    store.set_pref("last_restaurant_id", "9001")
    store.set_pref("cart_items:9001", {
        "different_item": {"menu_item_id": "different_item", "quantity": 1},
    })
    data = parse_out(run(runner, "restaurant", "cart"))
    context = data["checkout_preview"]["restaurant_context"]
    assert context["conflict"] is True
    assert context["verified"] is False
    assert "eta" not in data


def test_food_add_records_restaurant(runner, mcp):
    r = run(runner, "restaurant", "add", "--restaurant", "9001", "--item", "it_100:2")
    assert r.exit_code == 0
    assert store.get_pref("last_restaurant_id") == "9001"
    _, _, args = [c for c in mcp.calls if c[1] == "update_food_cart"][0]
    assert args["cartItems"] == [{"menu_item_id": "it_100", "quantity": 2}]


def test_food_remove_rewrites_cart(runner, mcp):
    store.set_pref("last_restaurant_id", "9001")
    data = parse_out(run(runner, "restaurant", "remove", "--item", "it_101"))
    assert data["status"] == "removed"
    assert [i["menu_item_id"] for i in data["remaining"]] == ["it_100"]
    assert "flush_food_cart" in mcp.tools_called()


def test_food_remove_reports_missing_item(runner, mcp):
    store.set_pref("last_restaurant_id", "9001")
    data = parse_out(run(runner, "restaurant", "remove", "--item", "nope"))
    assert data["not_in_cart"] == ["nope"]


def test_food_remove_all_empties_cart(runner, mcp):
    store.set_pref("last_restaurant_id", "9001")
    data = parse_out(run(runner, "restaurant", "remove", "--item", "it_100", "--item", "it_101"))
    assert data["status"] == "emptied"


def test_food_set_qty_zero_removes(runner, mcp):
    store.set_pref("last_restaurant_id", "9001")
    data = parse_out(run(runner, "restaurant", "set-qty", "--item", "it_100", "--qty", "0"))
    assert data["status"] == "updated"


def test_food_remove_without_restaurant_fails_clearly(runner, mcp):
    r = run(runner, "restaurant", "remove", "--item", "x")
    assert r.exit_code == 2


# ------------------------------------------------------------------ offers

def test_best_offer_dry_run_changes_nothing(runner, mcp):
    data = parse_out(run(runner, "restaurant", "best-offer", "--restaurant", "9001", "--dry-run"))
    assert data["status"] == "dry_run"
    assert "apply_food_coupon" not in mcp.tools_called()
    assert data["best"]["code"] == "WELCOME50"


def test_best_offer_surfaces_card_offers_and_near_misses(runner, mcp):
    data = parse_out(run(runner, "restaurant", "best-offer", "--restaurant", "9001", "--dry-run"))
    assert [c["code"] for c in data["card_offers"]] == ["BANKX"]
    assert data["near_misses"][0]["code"] == "NEARLY"


def test_best_offer_probe_reports_failure_honestly(runner, mcp):
    # every apply attempt fails in the fixture
    data = parse_out(run(runner, "restaurant", "best-offer", "--restaurant", "9001", "--probe", "1"))
    assert data["status"] == "no_beneficial_coupon"
    assert all(a["worked"] is False for a in data["attempts"])


def test_best_offer_applies_when_total_drops(runner, mcp):
    mcp.set("food", "apply_food_coupon", "Coupon applied.\nNew total: ₹300")
    data = parse_out(run(runner, "restaurant", "best-offer", "--restaurant", "9001"))
    # Coupon success text is not a bill. Only the authoritative cart read-back
    # can establish a lower payable amount.
    assert data["status"] == "no_beneficial_coupon"


def test_best_offer_unknown_total(runner, mcp):
    mcp.set("food", "get_food_cart", "cart is empty")
    r = run(runner, "restaurant", "best-offer", "--restaurant", "9001")
    assert r.exit_code == 1
    assert parse_out(r)["status"] == "cart_total_unknown"


# ------------------------------------------------------------------- topup

def test_topup_suggests_cheapest_additions(runner, mcp):
    data = parse_out(run(runner, "restaurant", "topup", "--restaurant", "9001"))
    best = data["best"]
    assert best["coupon"] == "NEARLY"
    assert best["added_cost"] >= 60
    assert best["add_items"]
    assert "food restaurant add --restaurant 9001" in best["add_command"]


def test_topup_apply_updates_cart(runner, mcp):
    run(runner, "restaurant", "topup", "--restaurant", "9001", "--apply")
    assert "update_food_cart" in mcp.tools_called()


def test_topup_without_actionable_threshold(runner, mcp):
    mcp.set("food", "fetch_food_coupons", "Found 0 coupons (0 applicable):")
    data = parse_out(run(runner, "restaurant", "topup", "--restaurant", "9001"))
    assert data["status"] == "no_actionable_threshold"


# ------------------------------------------------------------- place / gates

def test_place_refuses_without_yes(runner, mcp):
    r = run(runner, "restaurant", "place")
    assert r.exit_code == 2
    assert "place_food_order" not in mcp.tools_called()


def test_place_blocks_on_card_offers(runner, mcp):
    run(runner, "restaurant", "best-offer", "--restaurant", "9001", "--dry-run")
    r = run(runner, "restaurant", "place", "-y", "--max-total", "430", "--restaurant", "9001", "--payment", "UPI", "--intent-app", "gpay://upi/")
    assert r.exit_code == 3
    assert parse_out(r)["status"] == "blocked_card_offers"
    assert "place_food_order" not in mcp.tools_called()


def test_place_blocks_on_worthwhile_near_miss(runner, mcp):
    run(runner, "restaurant", "best-offer", "--restaurant", "9001", "--dry-run")
    r = run(runner, "restaurant", "place", "-y", "--max-total", "430", "--restaurant", "9001", "--payment", "UPI", "--intent-app", "gpay://upi/",
            "--ignore-card-offers")
    assert r.exit_code == 4
    data = parse_out(r)
    assert data["status"] == "blocked_near_misses"
    assert data["closest"]["code"] == "NEARLY"


def test_place_succeeds_with_all_overrides(runner, mcp):
    run(runner, "restaurant", "best-offer", "--restaurant", "9001", "--dry-run")
    r = run(runner, "restaurant", "place", "-y", "--max-total", "430", "--restaurant", "9001", "--payment", "UPI", "--intent-app", "gpay://upi/",
            "--ignore-card-offers", "--ignore-near-misses", "--no-open")
    assert r.exit_code == 0
    assert "place_food_order" in mcp.tools_called()
    assert store.list_orders()[0]["id"] == "8880001"


def test_place_refuses_a_restaurant_closed_for_delivery(runner, mcp):
    mcp.set(
        "food", "search_restaurants",
        "1. Vesuvio Pizzeria — Italian | 4.6★ | 22 min | ₹500 for two | CLOSED (ID: 9001)",
    )
    r = run(
        runner, "restaurant", "place", "-y", "--max-total", "430",
        "--restaurant", "9001", "--payment", "UPI",
        "--intent-app", "gpay://upi/", "--no-auto-coupon",
        "--ignore-card-offers", "--ignore-near-misses", "--no-open",
    )
    data = parse_out(r)
    assert r.exit_code == 5
    assert data["status"] == "blocked_restaurant_not_open"
    assert data["restaurant_availability"]["status"] == "CLOSED"
    assert "place_food_order" not in mcp.tools_called()


def test_place_refuses_when_restaurant_status_is_missing(runner, mcp):
    mcp.set(
        "food", "search_restaurants",
        "1. Vesuvio Pizzeria — Italian | 4.6★ | 22 min | ₹500 for two (ID: 9001)",
    )
    r = run(
        runner, "restaurant", "place", "-y", "--max-total", "430",
        "--restaurant", "9001", "--payment", "UPI",
        "--intent-app", "gpay://upi/", "--no-auto-coupon",
        "--ignore-card-offers", "--ignore-near-misses", "--no-open",
    )
    data = parse_out(r)
    assert r.exit_code == 5
    assert data["status"] == "blocked_unverified_restaurant_status"
    assert data["restaurant_availability"]["status"] == "UNKNOWN"
    assert "place_food_order" not in mcp.tools_called()


def test_place_refuses_when_structured_cart_says_unavailable(runner, mcp):
    from tests.conftest import FAKE_CART

    mcp.responses[("food", "get_food_cart")] = {
        "isError": False,
        "content": FAKE_CART,
        "structuredContent": {
            "statusCode": 6,
            "statusMessage": "Restaurant is no longer taking new orders.",
            "data": {
                "items": [
                    {"menu_item_id": "it_100", "name": "Garden Platter", "in_stock": 0},
                ],
            },
        },
    }
    r = run(
        runner, "restaurant", "place", "-y", "--max-total", "430",
        "--restaurant", "9001", "--payment", "UPI",
        "--no-auto-coupon", "--ignore-card-offers", "--ignore-near-misses", "--no-open",
    )
    data = parse_out(r)
    assert r.exit_code == 5
    assert data["status"] == "blocked_cart_unavailable"
    assert data["cart_orderability"]["unavailable_items"][0]["name"] == "Garden Platter"
    assert "place_food_order" not in mcp.tools_called()


def test_place_logs_order_amount(runner, mcp):
    run(runner, "restaurant", "place", "-y", "--max-total", "430", "--payment", "UPI", "--intent-app", "gpay://upi/", "--restaurant", "9001", "--ignore-card-offers",
        "--ignore-near-misses", "--no-open")
    o = store.list_orders()[0]
    assert o["amount"] == 430 and o["status"] == "PENDING_PAYMENT"


# --------------------------------------------------------------- instamart

def test_im_cart_includes_fee_analysis(runner, mcp):
    data = parse_out(run(runner, "im", "cart"))
    fa = data["fee_analysis"]
    assert fa["known"] is True
    assert fa["subtotal"] == 216 and fa["total"] == 228
    preview = data["checkout_preview"]
    assert preview["complete"] is True
    assert preview["delivery_fee"] == 7
    assert preview["payable_total"] == 228


def test_im_cart_prefers_structured_bill_json(runner, mcp):
    mcp.responses[("instamart", "get_cart")] = {
        "isError": False,
        "content": "Instamart cart ready.",
        "structuredContent": {
            "data": {
                "cartTotalAmount": "₹155",
                "items": [{
                    "spinId": "spin_test", "itemName": "Test Rice",
                    "quantity": "1", "discountedFinalPrice": "₹140",
                }],
                "billBreakdown": {
                    "lineItems": [
                        {"label": "Item total", "value": "₹140"},
                        {"label": "Delivery fee", "value": "FREE"},
                        {"label": "Handling fee", "value": "₹15"},
                    ],
                    "toPay": {"value": "₹155"},
                },
            },
        },
    }
    preview = parse_out(run(runner, "im", "cart"))["checkout_preview"]
    assert preview["complete"] is True
    assert preview["delivery_is_free"] is True
    assert preview["payable_total"] == 155


def test_im_checkout_refuses_without_yes(runner, mcp):
    r = run(runner, "im", "checkout")
    assert r.exit_code == 2
    assert "checkout" not in mcp.tools_called()


def test_im_checkout_blocks_on_high_fees(runner, mcp):
    mcp.set("instamart", "get_cart", {
        "data": {"cartTotalAmount": "₹187",
                 "items": [{"itemName": "Oat Milk", "quantity": 1, "discountedFinalPrice": 104}],
                 "billBreakdown": {
                     "lineItems": [{"label": "Item total", "value": "₹104"},
                                   {"label": "Fees", "value": "₹83"}],
                     "toPay": {"value": "₹187"},
                 }}})
    r = run(runner, "im", "checkout", "-y", "--max-total", "187",
            "--payment", "UPI", "--intent-app", "gpay://upi/")
    assert r.exit_code == 3
    assert parse_out(r)["status"] == "blocked_high_fees"
    assert "checkout" not in mcp.tools_called()


def test_im_checkout_proceeds_with_ignore_fees(runner, mcp):
    mcp.set("instamart", "get_cart", {
        "data": {"cartTotalAmount": "₹187",
                 "items": [{"itemName": "Oat Milk", "quantity": 1, "discountedFinalPrice": 104}],
                 "billBreakdown": {
                     "lineItems": [{"label": "Item total", "value": "₹104"},
                                   {"label": "Fees", "value": "₹83"}],
                     "toPay": {"value": "₹187"},
                 }}})
    mcp.set("instamart", "checkout", {
        "orderId": "8880001", "cartTotal": 187, "status": "PENDING_PAYMENT",
    })
    r = run(runner, "im", "checkout", "-y", "--max-total", "187",
            "--payment", "UPI", "--intent-app", "gpay://upi/",
            "--ignore-fees", "--no-open")
    assert r.exit_code == 0
    assert "checkout" in mcp.tools_called()


def test_im_checkout_blocks_when_live_total_exceeds_approval(runner, mcp):
    r = run(runner, "im", "checkout", "-y", "--max-total", "200",
            "--payment", "UPI", "--ignore-fees", "--no-open")
    assert r.exit_code == 5
    assert parse_out(r)["status"] == "blocked_total_changed"
    assert "checkout" not in mcp.tools_called()


def test_im_payment_options_flags_capabilities(runner, mcp):
    data = parse_out(run(runner, "im", "payment-options"))
    assert data["cod_available"] is True
    assert data["hands_free_possible"] is True
    assert data["wallet_available"] is False


def test_im_add_and_clear(runner, mcp):
    run(runner, "im", "add", "--item", "sp_1:2")
    _, _, args = [c for c in mcp.calls if c[1] == "update_cart"][0]
    assert args["items"] == [{"spinId": "sp_1", "quantity": 2}]
    assert run(runner, "im", "clear").exit_code == 0


# ------------------------------------------------------------------ orders

def test_orders_sync_and_stats(runner, mcp):
    data = parse_out(run(runner, "orders", "sync"))
    assert data["synced"]["food"]["fetched"] == 2
    assert data["synced"]["instamart"]["fetched"] == 1
    stats = parse_out(run(runner, "orders", "stats"))
    assert stats["spend"]["total_spent"] == 430 + 700 + 350
    assert stats["top_items"]


def test_orders_spend_days_filter(runner, mcp):
    run(runner, "orders", "sync")
    assert parse_out(run(runner, "orders", "spend", "--days", "30"))["total_spent"] > 0


def test_orders_track(runner, mcp):
    assert run(runner, "orders", "track", "8880001").exit_code == 0


# -------------------------------------------------------------------- prefs

def test_prefs_learn_and_show(runner, mcp):
    run(runner, "orders", "sync")
    run(runner, "prefs", "learn", "--no-sync")
    prefs = parse_out(run(runner, "prefs", "show"))
    assert "diet" in prefs and prefs["diet"]["source"] == "learned"


def test_prefs_set_is_explicit_and_sticks(runner, mcp):
    run(runner, "prefs", "set", "diet", '"vegetarian"')
    run(runner, "orders", "sync")
    run(runner, "prefs", "learn", "--no-sync")
    prefs = parse_out(run(runner, "prefs", "show"))
    assert prefs["diet"]["source"] == "explicit"


def test_prefs_forget(runner, mcp):
    run(runner, "prefs", "set", "diet", '"vegetarian"')
    parse_out(run(runner, "prefs", "forget"))
    assert parse_out(run(runner, "prefs", "show"))["diet"]["source"] == "explicit"
    parse_out(run(runner, "prefs", "forget", "--all"))
    assert parse_out(run(runner, "prefs", "show")) == {}


# ------------------------------------------------------------------ suggest

def test_suggest_does_not_filter_on_a_learned_diet(runner, mcp):
    """An inference must never silently hide options."""
    store.set_preference("diet", "vegetarian", "learned", 0.95)
    data = parse_out(run(runner, "restaurant", "suggest", "--people", "2", "--budget", "2000"))
    assert data["based_on"]["veg_only_enforced"] is False
    assert data["based_on"]["diet_note"]
    assert any(not d["veg"] for d in data["suggestions"])


def test_suggest_filters_when_asked(runner, mcp):
    data = parse_out(run(runner, "restaurant", "suggest", "--people", "2",
                         "--budget", "2000", "--veg"))
    assert data["based_on"]["veg_only_enforced"] is True
    assert all(d["veg"] for d in data["suggestions"])


def test_suggest_filters_on_explicit_diet(runner, mcp):
    store.set_preference("diet", "vegetarian", "explicit")
    data = parse_out(run(runner, "restaurant", "suggest", "--people", "2", "--budget", "2000"))
    assert data["based_on"]["veg_only_enforced"] is True


def test_suggest_scales_quantity_by_headcount(runner, mcp):
    data = parse_out(run(runner, "restaurant", "suggest", "--people", "2", "--budget", "2000"))
    single = [d for d in data["suggestions"] if d["serves"] == 1][0]
    assert single["suggested_quantity"] == 2
    assert single["line_total"] == single["price"] * 2


def test_suggest_treats_platter_as_one_unit(runner, mcp):
    data = parse_out(run(runner, "restaurant", "suggest", "--people", "2", "--budget", "2000"))
    platter = [d for d in data["suggestions"] if d["serves"] >= 2]
    assert platter and platter[0]["suggested_quantity"] == 1


def test_suggest_respects_budget(runner, mcp):
    data = parse_out(run(runner, "restaurant", "suggest", "--people", "2", "--budget", "500"))
    assert all(d["line_total"] <= 500 for d in data["suggestions"])


# ----------------------------------------------------------------- address

def test_address_search_filters(runner, mcp):
    data = parse_out(run(runner, "address", "search", "work"))
    assert [m["id"] for m in data["matches"]] == ["addr_test_002"]
    assert data["searched"] == 3


def test_address_search_by_pincode(runner, mcp):
    data = parse_out(run(runner, "address", "search", "400003"))
    assert [m["id"] for m in data["matches"]] == ["addr_test_003"]


def test_address_set_default(runner, mcp):
    run(runner, "address", "set-default", "addr_test_002", "--label", "Work")
    assert parse_out(run(runner, "address", "default"))["default_address_id"] == "addr_test_002"


def test_missing_address_fails_loudly(runner, mcp):
    store.set_pref("default_address_id", None)
    with store.connect() as c:
        c.execute("DELETE FROM prefs WHERE key='default_address_id'")
    r = run(runner, "restaurant", "cart")
    assert r.exit_code == 2


# -------------------------------------------------------------- generic/aux

def test_tools_and_call(runner, mcp):
    assert run(runner, "mcp", "list", "food").exit_code == 0
    r = run(runner, "mcp", "call", "food", "get_food_cart", "--args", '{"addressId":"x"}')
    assert r.exit_code == 0


def test_call_rejects_bad_json(runner, mcp):
    assert run(runner, "mcp", "call", "food", "x", "--args", "{bad").exit_code == 2


def test_config_get_set_list(runner, mcp):
    run(runner, "config", "free_delivery_threshold", "249")
    assert parse_out(run(runner, "config", "free_delivery_threshold"))["free_delivery_threshold"] == 249
    assert "free_delivery_threshold" in parse_out(run(runner, "config"))


def test_auth_status_and_logout(runner, mcp):
    assert run(runner, "auth", "status").exit_code == 0
    assert run(runner, "auth", "logout").exit_code == 0
