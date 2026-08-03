"""Pure-function tests: parsing, scoring, planning. No network, no DB."""

from __future__ import annotations

import pytest

from food_cli import commands as C
from food_cli.core import media
from food_cli.offers import coupons as offers
from food_cli.core import profile
from food_cli.core import qr
from food_cli.offers import topup
from tests.conftest import FAKE_CART, FAKE_COUPONS, FAKE_DISHES, FAKE_MENU, FAKE_ORDERS, FAKE_RESTAURANTS


def food_cart(*, item_total=100, coupon_discount=0, delivery=0, taxes=35,
              payable=135, coupon=None):
    """Provider-shaped Food cart for bill guard tests."""
    pricing = {"item_total": item_total, "delivery_charge": delivery, "to_pay": payable}
    if taxes is not None:
        pricing["taxes_and_charges"] = taxes
    return {"structuredContent": {"data": {"pricing": pricing, "offers": {
        "coupon_applied": coupon, "coupon_discount": coupon_discount,
    }}}}


# ------------------------------------------------------------- restaurants

def test_parse_restaurants_extracts_fields():
    rows = C.parse_restaurants(FAKE_RESTAURANTS)
    real = [r for r in rows if not r["is_dish"]]
    assert len(real) == 3
    top = real[0]
    assert top["id"] == "9001"
    assert top["rating"] == 4.6
    assert top["eta_minutes"] == 22
    assert top["cost_for_two"] == 500
    assert top["promoted"] is True
    assert top["availability_status"] == "OPEN"
    assert "(Ad)" not in top["name"]


def test_restaurant_availability_requires_an_exact_id_match():
    response = {
        "content": {
            "restaurants": [
                {"id": "90010", "name": "Similar Outlet", "availabilityStatus": "OPEN"},
                {"id": "9001", "name": "Exact Outlet", "availabilityStatus": "CLOSED"},
            ]
        }
    }
    result = C.restaurant_availability_in(response, "9001")
    assert result["status"] == "CLOSED"
    assert result["verified"] is True


def test_restaurant_availability_reads_structured_content():
    response = {
        "content": "1. Exact Outlet — Indian | 4.5★ | 18 min | ₹300 for two (ID: 9001)",
        "structuredContent": {
            "restaurants": [
                {"id": "9001", "name": "Exact Outlet", "availabilityStatus": "OPEN"},
            ]
        },
    }
    result = C.restaurant_availability_in(response, "9001")
    assert result["status"] == "OPEN"
    assert result["verified"] is True


def test_parse_restaurants_flags_dish_rows():
    rows = C.parse_restaurants(FAKE_RESTAURANTS)
    assert [r["id"] for r in rows if r["is_dish"]] == ["9004"]


def test_restaurant_name_does_not_fake_an_open_status():
    rows = C.parse_restaurants(
        "1. Open Kitchen — Indian | 4.2★ | 20 min | ₹300 for two (ID: 9010)"
    )
    assert rows[0]["availability_status"] is None


def test_parse_restaurants_empty():
    assert C.parse_restaurants("") == []
    assert C.parse_restaurants(None) == []


# ------------------------------------------------------------------ dishes

def test_parse_dishes():
    rows = C.parse_dishes(FAKE_DISHES)
    assert len(rows) == 3
    assert rows[0]["item_id"] == "it_100"
    assert rows[0]["restaurant_id"] == "9001"
    assert rows[0]["price"] == 240.0
    assert rows[0]["veg"] is True
    assert rows[1]["veg"] is False
    assert "add --restaurant 9001 --item it_100:1" in rows[0]["add_command"]


# ------------------------------------------------------------------- carts

def test_cart_items_roundtrip():
    items = C._cart_items(FAKE_CART)
    assert [i["menu_item_id"] for i in items] == ["it_100", "it_101"]
    assert items[0]["price"] == 240.0
    assert items[0]["quantity"] == 1


def test_cart_items_reads_quantity():
    items = C._cart_items("  - Thing x3 — ₹90 (ID: z1)")
    assert items[0]["quantity"] == 3


@pytest.mark.parametrize("text,want", [
    ("Item total: ₹378\nTO PAY: ₹450", 378.0),
    ("TO PAY: ₹99", 99.0),
    ("nothing here", None),
])
def test_extract_total(text, want):
    assert C.extract_total(text) == want


@pytest.mark.parametrize("text,want", [
    ("Item total: ₹378\nTO PAY: ₹450", 450.0),
    ('{"cartTotal": 204, "status": "PENDING_PAYMENT"}', 204.0),
    ('{"cart_total": "₹204"}', 204.0),
    ('{"cartTotalAmount": "204.50"}', 204.5),
    ('{"totalAmount": 99}', 99.0),
    ("Cart total: INR 125.25", 125.25),
    ("Total paid: ₹180", 180.0),
    ("Item total: ₹100\nDelivery: ₹69", None),
    ("nothing here", None),
])
def test_extract_payable_only_returns_a_final_charge(text, want):
    assert C.extract_payable(text) == want


@pytest.mark.parametrize("text,want", [
    ("Delivery: FREE", 0.0),
    ("Delivery: ₹69", 69.0),
    ("Delivery fee: INR 12.50", 12.5),
    ("No bill breakdown", None),
])
def test_extract_delivery_fee(text, want):
    assert C.extract_delivery_fee(text) == want


@pytest.mark.parametrize("text,want", [
    ("Taxes & charges: ₹35.43", 35.43),
    ("Tax and charge: INR 12", 12.0),
    ('{"taxAmount": 18.5}', 18.5),
    ("No tax breakdown", None),
])
def test_extract_taxes_and_charges(text, want):
    assert C.extract_taxes_and_charges(text) == want


def test_bill_breakdown_reconciles_get_food_cart():
    bill = C.parse_bill_breakdown(food_cart(item_total=360, taxes=70, payable=430))
    assert bill == {
        "source_tool": "get_food_cart.structuredContent.data.pricing",
        "item_total": 360.0,
        "coupon_discount": 0.0,
        "delivery_fee": 0.0,
        "taxes_and_charges": 70.0,
        "payable_total": 430.0,
        "coupon_code": None,
        "calculated_total": 430.0,
        "difference": 0.0,
        "reconciles": True,
        "complete": True,
        "missing_fields": [],
        "note": "Authoritative pre-order bill from get_food_cart.",
    }


def test_bill_breakdown_rejects_missing_or_unexplained_charges():
    missing = C.parse_bill_breakdown(food_cart(taxes=None))
    assert missing["complete"] is False
    assert missing["missing_fields"] == ["taxes_and_charges"]

    drift = C.parse_bill_breakdown(food_cart(payable=226))
    assert drift["complete"] is False
    assert drift["difference"] == 91


@pytest.mark.parametrize(("cart", "difference"), [
    (
        food_cart(item_total=245, coupon_discount=69, coupon="SAMPLE", taxes=46.86, payable=222),
        -0.86,
    ),
    (
        food_cart(payable=136),
        1.00,
    ),
])
def test_bill_breakdown_accepts_provider_rounding_drift(cart, difference):
    bill = C.parse_bill_breakdown(cart)
    assert bill["complete"] is True
    assert bill["reconciles"] is True
    assert bill["difference"] == difference
    assert "rounding tolerance" in bill["note"]


def test_bill_breakdown_rejects_two_rupee_drift():
    bill = C.parse_bill_breakdown(food_cart(payable=137))
    assert bill["difference"] == 2
    assert bill["complete"] is False
    assert bill["reconciles"] is False


def test_bill_breakdown_still_requires_taxes():
    bill = C.parse_bill_breakdown(food_cart(taxes=None, payable=100))
    assert bill["complete"] is False
    assert bill["missing_fields"] == ["taxes_and_charges"]


# ----------------------------------------------------------------- serves

@pytest.mark.parametrize("name,want", [
    ("Sharing Platter (For 2-3 People)", 3),
    ("Bento Box (serves 1)", 1),
    ("Meal For Two", 2),
    ("Family Feast", 3),
    ("Plain Dosa", 1),
])
def test_serves_count(name, want):
    assert C._serves_count(name) == want


# ------------------------------------------------------------------- diet

@pytest.mark.parametrize("name,want", [
    ("Grilled Chicken Wings", True),
    ("Smoked Salmon Plate", True),
    ("Dhaba Egg Curry", True),
    ("Garden Platter", False),
    ("Paneer Tikka", False),
    ("Mushroom Risotto", False),
    ("Veg Protein Burger", False),
    ("Soya Chaap", False),
])
def test_non_veg_detection(name, want):
    assert profile._is_non_veg(name) is want


# ----------------------------------------------------------------- coupons

def test_parse_coupons_shortfall_is_not_a_discount():
    by = {c["code"]: c for c in offers.parse_coupons(FAKE_COUPONS)}
    near = by["NEARLY"]
    assert near["shortfall"] == 60.0
    assert near["cap"] == 90.0
    assert near["flat"] is None          # "Add ₹60" must not become a discount
    assert near["applicable"] is False


def test_applicable_coupon_scores():
    by = {c["code"]: c for c in offers.parse_coupons(FAKE_COUPONS)}
    assert offers.estimate_discount(by["WELCOME50"], 360) == 50.0
    assert offers.estimate_discount(by["NEARLY"], 360) == 0.0


def test_percent_with_cap():
    c = offers.parse_coupons("  - PCT20 [✅ APPLICABLE] — 20% off up to ₹50 (code: c-9)")[0]
    assert offers.estimate_discount(c, 1000) == 50.0     # capped
    assert offers.estimate_discount(c, 100) == 20.0      # uncapped


def test_pick_best_prefers_real_saving():
    best, ranked = offers.pick_best(offers.parse_coupons(FAKE_COUPONS), 360)
    assert best["code"] == "WELCOME50"
    assert ranked[0]["code"] == "WELCOME50"


def test_pick_best_none_when_nothing_usable():
    cs = offers.parse_coupons("  - Z [❌ NOT APPLICABLE] — Add ₹500 more to avail this offer (code: c-1)")
    best, _ = offers.pick_best(cs, 100)
    assert best is None


def test_card_offers_detected_and_isolated():
    cards = offers.card_offers(offers.parse_coupons(FAKE_COUPONS))
    assert [c["code"] for c in cards] == ["BANKX"]
    assert cards[0]["potential_saving"] == 150.0


def test_flat_code_saving_inferred():
    cs = offers.parse_coupons("  - FLAT300 [❌ NOT APPLICABLE] — Add ₹2000 more to avail this offer (code: c-3)")
    assert cs[0]["inferred_flat"] == 300.0
    nm = offers.near_misses(cs)[0]
    assert nm["would_save"] == 300.0
    assert nm["saving_inferred"] is True


def test_near_misses_sorted_by_reachability():
    nm = offers.near_misses(offers.parse_coupons(FAKE_COUPONS))
    assert nm[0]["code"] == "NEARLY"


def test_near_misses_excludes_the_coupon_already_applied():
    nm = offers.near_misses(
        offers.parse_coupons(FAKE_COUPONS), applied_code="NEARLY",
    )
    assert "NEARLY" not in {n["code"] for n in nm}


def test_probe_order_puts_usable_first():
    ranked = offers.rank(offers.parse_coupons(FAKE_COUPONS), 360)
    order = [c["code"] for c in offers.probe_order(ranked, extra=2)]
    assert order[0] == "WELCOME50"


# ------------------------------------------------------------------ topup

MENU_ITEMS = [
    {"item_id": "s", "name": "Ketchup Sachet", "price": 1},
    {"item_id": "d", "name": "Lemon Soda", "price": 60, "bestseller": True},
    {"item_id": "v", "name": "Veg Popcorn", "price": 75, "bestseller": True},
    {"item_id": "c", "name": "Chicken Popcorn", "price": 95},
    {"item_id": "g", "name": "Garlic Bread", "price": 80},
]


def test_topup_reaches_threshold_with_small_overshoot():
    p = topup.plan(MENU_ITEMS, 149)
    assert p["found"]
    assert p["added_cost"] >= 149
    assert p["overshoot"] <= 30


def test_topup_does_not_spam_cheap_sachets():
    p = topup.plan(MENU_ITEMS, 149)
    sach = [i for i in p["items"] if i["item_id"] == "s"]
    assert not sach or sach[0]["quantity"] <= 2


def test_topup_respects_veg_filter():
    p = topup.plan(MENU_ITEMS, 90, veg_only=True)
    assert all("Chicken" not in i["name"] for i in p["items"])


def test_topup_allows_non_veg_when_not_filtered():
    p = topup.plan(MENU_ITEMS, 95, veg_only=False)
    assert p["found"]


def test_topup_zero_shortfall_is_noop():
    p = topup.plan(MENU_ITEMS, 0)
    assert p["found"] and p["added_cost"] == 0 and p["items"] == []


def test_topup_infeasible_is_reported_not_faked():
    p = topup.plan(MENU_ITEMS, 100000, max_total_items=2)
    assert p["found"] is False
    assert "reason" in p


def test_topup_no_candidates():
    assert topup.plan([], 100)["found"] is False


def test_topup_exact_match():
    p = topup.plan(MENU_ITEMS, 60)
    assert p["added_cost"] == 60 and p["overshoot"] == 0


# ------------------------------------------------------------------ orders

def test_parse_order_history():
    rows = C.parse_order_history(FAKE_ORDERS, "food")
    assert len(rows) == 2
    assert rows[0]["id"] == "5550001"
    assert rows[0]["amount"] == 430.0
    assert rows[0]["vendor"] == "Vesuvio Pizzeria"
    assert {i["name"]: i["quantity"] for i in rows[0]["items"]}["Truffle Fries"] == 2


def test_parse_instamart_orders():
    from tests.conftest import FAKE_IM_ORDERS
    rows = C.parse_instamart_orders({"content": [FAKE_IM_ORDERS]})
    assert rows[0]["id"] == "7770001"
    assert rows[0]["amount"] == 350
    assert len(rows[0]["items"]) == 2


# -------------------------------------------------------------------- fees

def test_fee_analysis_flags_bad_ratio():
    f = C.fee_analysis(104, 187)
    assert f["high_fees"] is True
    assert f["spend_more_to_save"] == pytest.approx(95.0)
    assert "advice" in f


def test_fee_analysis_ok_above_threshold():
    f = C.fee_analysis(216, 228)
    assert f["high_fees"] is False


def test_fee_analysis_unknown():
    assert C.fee_analysis(None, 100)["known"] is False
    assert C.fee_analysis(0, 100)["known"] is False


# ---------------------------------------------------------------- qr/media

def test_qr_finds_upi_intent():
    found = qr.find_qr({"x": "upi://pay?pa=merchant@bank&am=430.00&cu=INR"})
    assert found["kind"] == "upi_uri"
    assert qr._amount_of(found["value"]) == 430.0
    assert qr._payee_of(found["value"]) == "merchant@bank"


def test_qr_extract_order_id():
    assert qr.extract_order_id('{"orderId": "8880001"}') == "8880001"
    assert qr.extract_order_id("nothing") is None


def test_qr_none_when_absent():
    assert qr.find_qr({"a": "b"}) is None


def test_media_parses_menu_images():
    items = media.parse_menu_items(FAKE_MENU)
    assert items["it_100"]["image_url"] == "https://cdn.example.com/a.jpg"
    assert items["it_100"]["bestseller"] is True
    assert items["it_102"]["image_url"] is None
    assert items["it_105"]["veg"] is False


def test_media_filename_is_stable():
    a = media._filename("https://cdn.example.com/a.jpg")
    assert a == media._filename("https://cdn.example.com/a.jpg")
    assert a.endswith(".jpg")


# ---------------------------------------------- coupon codes with punctuation

REAL_WORLD = (
    "Found 3 coupons (2 applicable):\n"
    "  - FLAT85OFF-ABOVE249 [✅ APPLICABLE] — Flat ₹85 off (code: c-9001)\n"
    "  - SAVE50 [✅ APPLICABLE] — Flat ₹50 off (code: c-9002)\n"
    "  - TRY_NEW.20+X [❌ NOT APPLICABLE] — Add ₹99 more (code: c-9003)\n"
)


def test_a_hyphenated_code_is_not_truncated():
    """The bug: FLAT85OFF-ABOVE249 was cut to FLAT85OFF and then rejected,
    silently costing the user the discount."""
    codes = [c["code"] for c in offers.parse_coupons(REAL_WORLD)]
    assert "FLAT85OFF-ABOVE249" in codes
    assert "FLAT85OFF" not in codes


def test_dots_plus_and_underscores_survive_too():
    codes = [c["code"] for c in offers.parse_coupons(REAL_WORLD)]
    assert "TRY_NEW.20+X" in codes


def test_a_plain_code_is_unchanged():
    codes = [c["code"] for c in offers.parse_coupons(REAL_WORLD)]
    assert "SAVE50" in codes


def test_the_code_does_not_swallow_the_description():
    rows = offers.parse_coupons(REAL_WORLD)
    first = next(c for c in rows if c["code"] == "FLAT85OFF-ABOVE249")
    assert first["applicable"] is True
    assert first["flat"] == 85.0


def test_a_trailing_qualifier_is_not_read_as_the_discount():
    """ABOVE249 is a minimum order, not an ₹249 saving."""
    rows = offers.parse_coupons(REAL_WORLD)
    first = next(c for c in rows if c["code"] == "FLAT85OFF-ABOVE249")
    assert first["inferred_flat"] == 85.0


def test_trailing_punctuation_is_trimmed_off_a_code():
    rows = offers.parse_coupons("  - SAVE20- [✅ APPLICABLE] — Flat ₹20 off\n")
    assert rows[0]["code"] == "SAVE20"
