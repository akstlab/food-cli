"""SQLite store: prefs, addresses, cache, orders, learned preferences."""

from __future__ import annotations

import time

import pytest

from food_cli.core import profile
from food_cli.core import store


pytestmark = pytest.mark.usefixtures("fresh_db")


def test_pref_roundtrip():
    store.set_pref("k", {"a": 1})
    assert store.get_pref("k") == {"a": 1}
    assert store.get_pref("missing", "fallback") == "fallback"


def test_address_default_is_exclusive():
    store.save_address("a1", "Home", {"x": 1}, is_default=True)
    store.save_address("a2", "Work", {"x": 2}, is_default=True)
    assert store.get_default_address() == {"x": 2}
    assert [a["id"] for a in store.list_addresses() if a["is_default"]] == ["a2"]


def test_cache_expiry():
    store.cache_set("c", [1, 2], ttl=60)
    assert store.cache_get("c") == [1, 2]
    store.cache_set("c2", "v", ttl=-1)
    assert store.cache_get("c2") is None
    assert store.cache_get("never-set") is None


def test_record_order_and_spend():
    store.record_order("o1", "food", {"raw": 1}, vendor="Vesuvio Pizzeria",
                       amount=430, discount=50, status="CONFIRMED")
    store.record_order("o2", "instamart", {"raw": 2}, vendor="Instamart",
                       amount=228, discount=0)
    s = store.spend_summary()
    assert s["total_spent"] == 658
    assert s["total_saved"] == 50
    assert s["by_kind"]["food"]["orders"] == 1


def test_spend_summary_since_filter():
    store.record_order("old", "food", {}, amount=100)
    assert store.spend_summary(since=time.time() + 60)["total_spent"] == 0


def test_list_orders_filters_by_kind():
    store.record_order("f1", "food", {}, amount=100)
    store.record_order("i1", "instamart", {}, amount=50)
    assert [o["id"] for o in store.list_orders(kind="food")] == ["f1"]


def test_history_never_overwrites_a_placed_order():
    store.record_order("o9", "food", {"src": "placed"}, amount=500, vendor="Real")
    with store.connect() as c:
        c.execute("UPDATE orders SET source='placed' WHERE id='o9'")
    changed = store.upsert_history_order("o9", "food", "Other", 1, "date", "X", [])
    assert changed is False
    assert store.list_orders()[0]["amount"] == 500


def test_upsert_history_reports_new():
    assert store.upsert_history_order("h1", "food", "V", 100, "d", "Delivered",
                                      [{"name": "Item A", "quantity": 2}]) is True
    assert store.upsert_history_order("h1", "food", "V", 100, "d", "Delivered", []) is False


def test_top_items_and_vendor_summary():
    store.upsert_history_order("h1", "food", "Nordic Kitchen", 300, "d", "Delivered",
                               [{"name": "Rye Bread", "quantity": 2}])
    store.upsert_history_order("h2", "food", "Nordic Kitchen", 200, "d", "Delivered",
                               [{"name": "Rye Bread", "quantity": 1}])
    top = store.top_items()
    assert top[0]["name"] == "Rye Bread"
    assert top[0]["orders"] == 2
    assert store.vendor_summary()[0]["vendor"] == "Nordic Kitchen"


# ------------------------------------------------------------- preferences

def test_explicit_preference_beats_learned():
    assert store.set_preference("diet", "vegetarian", "learned", 0.9) is True
    assert store.set_preference("diet", "eats_non_veg", "explicit") is True
    # a later inference must NOT overwrite what the user stated
    assert store.set_preference("diet", "vegetarian", "learned", 0.99) is False
    assert store.get_preference("diet") == "eats_non_veg"


def test_all_preferences_shape():
    store.set_preference("k", [1], "learned", 0.5, "because")
    p = store.all_preferences()["k"]
    assert p["value"] == [1] and p["source"] == "learned"
    assert p["confidence"] == 0.5 and p["evidence"] == "because"


def test_clear_preferences_keeps_explicit_by_default():
    store.set_preference("a", 1, "learned")
    store.set_preference("b", 2, "explicit")
    store.clear_preferences(learned_only=True)
    assert store.get_preference("a") is None
    assert store.get_preference("b") == 2
    store.clear_preferences(learned_only=False)
    assert store.get_preference("b") is None


# ------------------------------------------------------------------ profile

def test_learn_with_no_history():
    out = profile.learn()
    assert out["learned"] == {}
    assert "note" in out


def test_learn_infers_vegetarian():
    store.upsert_history_order("o1", "food", "Green Cafe", 300, "d", "Delivered",
                               [{"name": "Garden Platter", "quantity": 2},
                                {"name": "Truffle Fries", "quantity": 1}])
    store.upsert_history_order("o2", "food", "Green Cafe", 200, "d", "Delivered",
                               [{"name": "Garden Platter", "quantity": 1}])
    out = profile.learn()
    assert out["learned"]["diet"]["value"] == "vegetarian"
    assert store.get_preference("diet") == "vegetarian"


def test_learn_detects_non_veg():
    store.upsert_history_order("o1", "food", "Grill Bar", 500, "d", "Delivered",
                               [{"name": "Grilled Chicken Wings", "quantity": 3},
                                {"name": "Garden Platter", "quantity": 1}])
    out = profile.learn()
    assert out["learned"]["diet"]["value"] == "eats_non_veg"


def test_learn_budget_and_vendors():
    for i, amt in enumerate((200, 400, 600)):
        store.upsert_history_order(f"o{i}", "food", "Nordic Kitchen", amt, "d", "Delivered",
                                   [{"name": "Rye Bread", "quantity": 1}])
    out = profile.learn()
    assert out["learned"]["food_budget"]["value"]["typical_order"] == 400
    assert "Nordic Kitchen" in out["learned"]["favourite_restaurants"]["value"]
    assert "Rye Bread" in out["learned"]["favourite_dishes"]["value"]
