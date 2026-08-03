"""Probing must never leave the cart worse than it found it.

Applying a coupon replaces whatever is applied, so a probe that finds nothing
better used to strip a discount the cart arrived with — the user then paid full
price. These tests pin the recovery, the verification, and the arithmetic that
decides whether a top-up is actually worth anything.
"""

from __future__ import annotations

import pytest

from food_cli.cli import app
from food_cli.commands import food as F
from food_cli.offers import coupons as offers
from tests.conftest import FAKE_COUPONS, parse_out, patch_call

pytestmark = pytest.mark.usefixtures("fresh_db")

DISCOUNTED_CART = (
    "Items (2):\n"
    "  - Garden Platter — ₹240 (ID: it_100)\n"
    "  - Truffle Fries — ₹120 (ID: it_101)\n"
    "\nItem total: ₹360\n"
    "Coupon FLAT85OFF-ABOVE249 applied\n"
    "Coupon discount: ₹85\n"
    "Delivery: FREE\nTaxes & charges: ₹70\nTO PAY: ₹345\n"
)

FULL_PRICE_CART = (
    "Items (2):\n"
    "  - Garden Platter — ₹240 (ID: it_100)\n"
    "  - Truffle Fries — ₹120 (ID: it_101)\n"
    "\nItem total: ₹360\nDelivery: FREE\nTaxes & charges: ₹70\nTO PAY: ₹430\n"
)

PAREN_COUPON_CART = (
    "Items (2):\n"
    "  - Garden Bites — ₹68 (ID: fake_item_201)\n"
    "  - Coffee — ₹39 (ID: fake_item_202)\n"
    "\nItem total: ₹107\n"
    "Coupon (SAVE23): -₹23\n"
    "Delivery: FREE\nTaxes & charges: ₹35\nTO PAY: ₹119\n"
)

REVERTED_CART = (
    "Items (1):\n"
    "  - Idli x2 — ₹45.5 (ID: 7729000)\n"
    "\nItem total: ₹91\nDelivery: ₹67\nTaxes & charges: ₹34.84\nTO PAY: ₹192.84\n"
)


# ------------------------------------------------------------------- parsers

def test_reads_the_coupon_already_on_the_cart():
    assert offers.applied_coupon(DISCOUNTED_CART) == "FLAT85OFF-ABOVE249"
    assert offers.applied_coupon(FULL_PRICE_CART) is None


def test_reads_parenthesized_coupon_from_the_real_cart_shape():
    assert offers.applied_coupon(PAREN_COUPON_CART) == "SAVE23"
    assert offers.applied_discount(PAREN_COUPON_CART) == 23.0


def test_reads_what_that_coupon_is_saving():
    assert offers.applied_discount(DISCOUNTED_CART) == 85.0
    assert offers.applied_discount(FULL_PRICE_CART) is None


# ------------------------------------------------------- restore after probing

def carts(mcp, sequence):
    """Serve a different cart on each read, so mutation can be simulated."""
    state = {"i": 0}

    def route(server, tool, args):
        if tool == "get_food_cart":
            i = min(state["i"], len(sequence) - 1)
            state["i"] += 1
            return {"isError": False, "content": sequence[i]}
        return mcp(server, tool, args)
    return route


def test_a_pre_applied_coupon_is_put_back_when_nothing_beats_it(runner, mcp, monkeypatch):
    """The reported bug: the cart went from ₹281 back to ₹369."""
    patch_call(monkeypatch, carts(mcp, [DISCOUNTED_CART]))
    res = F.apply_best_coupon("9001", "addr_test_001", probe=2)
    assert res["status"] == "no_beneficial_coupon"
    assert res["restored"] == "FLAT85OFF-ABOVE249"
    applied = [c[2]["couponCode"] for c in mcp.calls if c[1] == "apply_food_coupon"]
    assert applied[-1] == "FLAT85OFF-ABOVE249", "the last write must restore the original"


def test_nothing_is_restored_when_the_cart_had_no_coupon(runner, mcp, monkeypatch):
    patch_call(monkeypatch, carts(mcp, [FULL_PRICE_CART]))
    res = F.apply_best_coupon("9001", "addr_test_001", probe=2)
    assert "restored" not in res


def test_a_cart_left_worse_is_reported_not_swallowed(runner, mcp, monkeypatch):
    """Restoring can itself fail. The user must not be asked to pay the result."""
    patch_call(monkeypatch, carts(mcp, [DISCOUNTED_CART, FULL_PRICE_CART]))
    res = F.apply_best_coupon("9001", "addr_test_001", probe=1)
    assert res["status"] == "cart_degraded"
    assert res["cart_degraded"]["before"] == 345.0
    assert res["cart_degraded"]["after"] == 430.0
    assert res["cart_degraded"]["lost"] == 85.0


def test_an_unchanged_cart_is_not_flagged(runner, mcp, monkeypatch):
    patch_call(monkeypatch, carts(mcp, [DISCOUNTED_CART, DISCOUNTED_CART]))
    res = F.apply_best_coupon("9001", "addr_test_001", probe=1)
    assert res["status"] == "no_beneficial_coupon"
    assert "cart_degraded" not in res


def test_a_dry_run_never_touches_the_cart(runner, mcp, monkeypatch):
    patch_call(monkeypatch, carts(mcp, [DISCOUNTED_CART]))
    F.apply_best_coupon("9001", "addr_test_001", apply=False)
    assert not [c for c in mcp.calls if c[1] == "apply_food_coupon"]


def test_coupon_api_cart_revert_is_restored_and_probing_stops(mcp, monkeypatch):
    """The coupon endpoint resurrected an old Idli cart in production."""
    patch_call(monkeypatch, carts(mcp, [FULL_PRICE_CART, REVERTED_CART, FULL_PRICE_CART]))
    res = F.apply_best_coupon("9001", "addr_test_001", probe=1)
    assert res["status"] == "cart_restored_after_coupon_revert"
    assert res["cart_integrity"]["restored"] is True
    applies = [c for c in mcp.calls if c[1] == "apply_food_coupon"]
    assert len(applies) == 1, "must not retry into another server-side revert"
    restore = [c for c in mcp.calls if c[1] == "update_food_cart"][-1][2]
    assert restore["cartItems"] == [
        {"menu_item_id": "it_100", "quantity": 1},
        {"menu_item_id": "it_101", "quantity": 1},
    ]


# ------------------------------------------------- netting off what is applied

def test_a_topup_is_costed_against_the_discount_already_held(mcp):
    """"Save ₹90" on a cart already saving ₹85 is worth ₹5, not ₹90."""
    result = {
        "already_applied": "FLAT85OFF-ABOVE249",
        "already_saving": 85.0,
        "near_misses": [{"code": "BIG90", "spend_more": 60.0, "would_save": 90.0}],
    }
    up = F.topup_upside(result, "9001", "addr_test_001")
    assert up is not None
    assert up["discount_gain"] == 5.0, "must not claim the whole ₹90"
    assert up["extra_cash"] > 0, "adding ₹60 of food to gain ₹5 costs money"


def test_an_unstated_discount_is_never_called_cheaper(mcp):
    result = {
        "already_applied": "MYSTERY",
        "already_saving": None,
        "near_misses": [{"code": "BIG90", "spend_more": 10.0, "would_save": 90.0}],
    }
    up = F.topup_upside(result, "9001", "addr_test_001")
    assert up["baseline_uncertain"] is True
    assert "may cost more" in up["verdict"]
    assert "cheaper" not in up["verdict"]


# ------------------------------------------------------- the placement gates

def test_place_does_not_block_on_a_near_miss_it_already_beats(runner, mcp, monkeypatch):
    """The false block: the order was refused over a coupon worth less than the
    one already applied."""
    mcp.set("food", "fetch_food_coupons", FAKE_COUPONS)
    patch_call(monkeypatch, carts(mcp, [DISCOUNTED_CART]))
    r = runner.invoke(app, ["restaurant", "place", "-y", "--max-total", "430", "--restaurant", "9001",
                            "--payment", "UPI", "--intent-app", "gpay://upi/", "--ignore-card-offers", "--no-open"])
    assert r.exit_code != 4, "must not block: the applied coupon already beats it"


def test_place_never_reprobes_a_coupon_after_approval(runner, mcp, monkeypatch):
    """Placement must not mutate either a discounted or a full-price cart."""
    patch_call(monkeypatch, carts(mcp, [DISCOUNTED_CART]))
    r = runner.invoke(app, ["restaurant", "place", "-y", "--max-total", "430", "--restaurant", "9001",
                            "--payment", "UPI", "--intent-app", "gpay://upi/", "--ignore-card-offers",
                            "--ignore-near-misses", "--no-open"])
    data = parse_out(r)
    assert data["coupon"]["status"] == "not_mutated_at_placement"
    assert not [c for c in mcp.calls if c[1] == "apply_food_coupon"]


def test_place_does_not_probe_parenthesized_coupon_after_approval(runner, mcp, monkeypatch):
    patch_call(monkeypatch, carts(mcp, [PAREN_COUPON_CART]))
    r = runner.invoke(app, [
        "restaurant", "place", "-y", "--max-total", "430", "--restaurant", "9001",
        "--payment", "UPI", "--intent-app", "gpay://upi/", "--ignore-card-offers",
        "--ignore-near-misses", "--no-open",
    ])
    assert not [c for c in mcp.calls if c[1] == "apply_food_coupon"]


def test_place_does_not_probe_when_no_coupon_is_applied(runner, mcp, monkeypatch):
    patch_call(monkeypatch, carts(mcp, [FULL_PRICE_CART]))
    r = runner.invoke(app, ["restaurant", "place", "-y", "--max-total", "430", "--restaurant", "9001",
                            "--payment", "UPI", "--intent-app", "gpay://upi/", "--ignore-card-offers",
                            "--ignore-near-misses", "--no-open"])
    assert not [c for c in mcp.calls if c[1] == "apply_food_coupon"]
