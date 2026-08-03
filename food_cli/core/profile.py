"""Learn user preferences from order history.

Everything here is *inferred*, so each preference is stored with a confidence
and the evidence behind it. A preference the user stated explicitly is never
overwritten by an inference (enforced in store.set_preference).

Kept deliberately conservative: it is better to record "probably vegetarian,
0.8" than to assert it, because acting on a wrong dietary inference is the one
mistake here with real consequences.
"""

from __future__ import annotations

import re
from collections import Counter

from . import store

NON_VEG = re.compile(
    r"\b(chicken|mutton|lamb|beef|pork|fish|prawn|shrimp|crab|egg|omelette|"
    r"keema|kheema|bacon|ham|seafood|meat|non[- ]?veg|"
    # Named species and cuts are common on menus and were missed at first -
    # getting this wrong means serving meat to a vegetarian.
    r"salmon|tuna|pomfret|basa|surmai|squid|octopus|lobster|oyster|clam|mussel|"
    r"anchovy|turkey|duck|venison|sausage|salami|pepperoni|mince|liver|"
    r"drumstick\s+chicken|wings)\b",
    re.I,
)
# Words that look non-veg but are not.
VEG_EXCEPTIONS = re.compile(r"\b(veg|paneer|soya|mock|jackfruit|mushroom|gobi)\b", re.I)

CUISINE_HINTS = {
    "south indian": ["dosa", "idli", "vada", "sambar", "uttapam", "bhojanam", "meals", "rasam"],
    "andhra": ["andhra", "bhojanam", "gongura", "pesarattu"],
    "kerala": ["parotta", "ishtew", "appam", "kadala", "avial", "kappa", "puttu"],
    "north indian": ["paneer", "roti", "naan", "dal makhani", "rajma", "chole", "kadhi", "tikka"],
    "biryani": ["biryani", "pulao"],
    "chinese": ["manchurian", "noodles", "fried rice", "schezwan"],
    "fast food": ["burger", "fries", "pizza", "wrap", "roll", "sandwich"],
}


def _is_non_veg(name: str) -> bool:
    if VEG_EXCEPTIONS.search(name or ""):
        # "Veg Chicken" style mock items, or paneer dishes named after meat dishes.
        return False
    return bool(NON_VEG.search(name or ""))


def learn(min_orders: int = 2) -> dict:
    """Derive preferences from stored orders. Returns what was learned."""
    items = store.top_items(limit=500)
    orders = store.list_orders(limit=500)
    learned: dict[str, dict] = {}

    if not items and not orders:
        return {"learned": {}, "note": "No order history yet. Run `food orders sync` first."}

    # --- diet -------------------------------------------------------------
    total_units = sum(i["units"] or 0 for i in items)
    nv_units = sum(i["units"] or 0 for i in items if _is_non_veg(i["name"]))
    if total_units:
        nv_ratio = nv_units / total_units
        if nv_ratio == 0:
            diet, conf = "vegetarian", min(0.95, 0.5 + 0.05 * total_units)
        elif nv_ratio < 0.15:
            diet, conf = "mostly_vegetarian", 0.7
        else:
            diet, conf = "eats_non_veg", 0.8
        store.set_preference(
            "diet", diet, "learned", round(conf, 2),
            f"{nv_units}/{total_units} items ordered were non-veg",
        )
        learned["diet"] = {"value": diet, "confidence": round(conf, 2)}

    # --- favourite dishes -------------------------------------------------
    repeats = [i for i in items if (i["orders"] or 0) >= min_orders]
    if repeats:
        favs = [i["name"] for i in repeats[:10]]
        store.set_preference(
            "favourite_dishes", favs, "learned", 0.9,
            f"ordered at least {min_orders} times each",
        )
        learned["favourite_dishes"] = {"value": favs}

    # --- favourite vendors ------------------------------------------------
    vendors = store.vendor_summary(limit=10)
    food_vendors = [v for v in vendors if v["kind"] == "food" and v["n"] >= min_orders]
    if food_vendors:
        names = [v["vendor"] for v in food_vendors]
        store.set_preference(
            "favourite_restaurants", names, "learned", 0.85,
            f"{food_vendors[0]['n']} orders from {names[0]}",
        )
        learned["favourite_restaurants"] = {"value": names}

    # --- typical spend ----------------------------------------------------
    amounts = [o["amount"] for o in orders if o.get("amount") and o.get("kind") == "food"]
    if amounts:
        amounts.sort()
        typical = round(sum(amounts) / len(amounts))
        budget = {
            "typical_order": typical,
            "min": round(amounts[0]),
            "max": round(amounts[-1]),
            "median": round(amounts[len(amounts) // 2]),
        }
        store.set_preference(
            "food_budget", budget, "learned", 0.8,
            f"average of {len(amounts)} food orders",
        )
        learned["food_budget"] = {"value": budget}

    # --- cuisines ---------------------------------------------------------
    counts: Counter = Counter()
    for i in items:
        low = (i["name"] or "").lower()
        for cuisine, words in CUISINE_HINTS.items():
            if any(w in low for w in words):
                counts[cuisine] += i["orders"] or 1
    if counts:
        ranked = [c for c, _ in counts.most_common(5)]
        store.set_preference(
            "favourite_cuisines", ranked, "learned", 0.7,
            f"inferred from {sum(counts.values())} item matches",
        )
        learned["favourite_cuisines"] = {"value": ranked}

    return {"learned": learned, "orders_considered": len(orders), "items_considered": len(items)}
