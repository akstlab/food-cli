"""Restaurant food ordering."""

from __future__ import annotations

import json
import math
import re

import typer

from ..core import media, profile, qr as qrmod, store
from ..offers import coupons as offers, topup
from .checkout import (
    _log_order,
    _payment_block,
    _remember_pending_payment,
    _wait_after_order,
    choose_food_upi_route,
    generic_upi_qr,
    intent_app_choices,
    order_id_in,
    payment_artifact_guard,
    payment_link_in,
    saved_intent_app,
)
from .common import (
    NEAR_MISS_MAX_RATIO,
    PAYMENT_REQUIRED_HINT,
    call,
    err,
    extract_delivery_fee,
    extract_payable,
    extract_taxes_and_charges,
    extract_total,
    out,
    resolve_address,
    response_blob,
    text_of,
    _no_open_default,
)

food_app = typer.Typer(no_args_is_help=True, help="Restaurant food ordering.")


# ------------------------------------------------------------------- food

# "1. Sample Diner — Italian, Continental | 4.2★ | 26 min | ₹400 for two (ID: 1234)"
_RESTAURANT_LINE = re.compile(
    r"^\s*\d+\.\s*(?P<name>.+?)\s*—\s*(?P<cuisine>[^|]*)"
    r"(?:\|\s*(?P<rating>[\d.]+)★\s*)?"
    r"(?:\|\s*(?P<eta>\d+)\s*min\s*)?"
    r"(?:\|\s*₹(?P<cost>\d+)\s*for two\s*)?"
    r".*?\(ID:\s*(?P<id>\w+)\)",
    re.I,
)
_AVAILABILITY_STATUS = re.compile(
    r"(?:\bavailability(?:Status|\s+status)?\s*[:=]\s*|\|\s*)"
    r"(OPEN|CLOSED|UNAVAILABLE)(?=\s*(?:\||\(ID:|$))",
    re.I,
)


def parse_restaurants(text: str) -> list[dict]:
    """Turn the prose restaurant list into structured rows.

    Lets a voice agent say "twenty-five minutes" without re-parsing prose, and
    lets us sort by ETA.
    """
    rows = []
    for line in (text or "").splitlines():
        m = _RESTAURANT_LINE.match(line)
        if not m:
            continue
        g = m.groupdict()
        status = _AVAILABILITY_STATUS.search(line)
        rows.append({
            "id": g["id"],
            "name": (g["name"] or "").replace("(Ad)", "").strip(),
            "cuisines": [c.strip() for c in (g["cuisine"] or "").split(",") if c.strip()],
            "rating": float(g["rating"]) if g["rating"] else None,
            "eta_minutes": int(g["eta"]) if g["eta"] else None,
            "cost_for_two": int(g["cost"]) if g["cost"] else None,
            "promoted": "(Ad)" in (g["name"] or ""),
            "availability_status": status.group(1).upper() if status else None,
            # A dish row has no rating, ETA or cost - Swiggy rendered a menu
            # item rather than a restaurant.
            "is_dish": not (g["rating"] or g["eta"] or g["cost"]),
        })
    return rows


def _merge_structured_restaurant_status(rows: list[dict], res) -> None:
    """Prefer provider-owned availability from structured search results."""
    statuses: dict[str, str] = {}

    def walk(value):
        if isinstance(value, dict):
            rid = value.get("id", value.get("restaurantId"))
            raw = value.get("availabilityStatus", value.get("availability_status"))
            if rid is not None and raw is not None:
                statuses[str(rid)] = str(raw).strip().upper()
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(res.get("structuredContent"))
    for row in rows:
        if row["id"] in statuses:
            row["availability_status"] = statuses[row["id"]]


@food_app.command("search")
def food_search(
    query: str = typer.Argument(..., help="e.g. 'pizza' or a restaurant name"),
    address: str = typer.Option(None, "--address"),
    offset: int = typer.Option(None, "--offset"),
    fastest: bool = typer.Option(False, "--fastest", help="Sort by delivery ETA, quickest first."),
    max_eta: int = typer.Option(None, "--max-eta", help="Only restaurants delivering within N minutes."),
):
    """Search restaurants near the delivery address."""
    args = {"addressId": resolve_address(address), "query": query}
    if offset is not None:
        args["offset"] = offset
    res = call("food", "search_restaurants", args)

    rows = parse_restaurants(text_of(res))
    _merge_structured_restaurant_status(rows, res)
    _remember_restaurant_names([(r["id"], r["name"]) for r in rows if not r["is_dish"]])

    # Swiggy sometimes answers a dish-shaped query with DISH rows rather than
    # restaurants; those carry no rating/ETA/cost. Keep them separate instead of
    # letting an --max-eta filter silently delete them.
    dishes = [r for r in rows if r["is_dish"]]
    rows = [r for r in rows if not r["is_dish"]]

    dropped = 0
    if max_eta is not None:
        keep = [r for r in rows if r["eta_minutes"] is not None and r["eta_minutes"] <= max_eta]
        dropped = len(rows) - len(keep)
        rows = keep
    if fastest:
        rows.sort(key=lambda r: (r["eta_minutes"] is None, r["eta_minutes"] or 0))

    # Chains list every nearby outlet. Search already carries each one's ETA,
    # so marking the nearest costs nothing here.
    chains: dict[str, list[dict]] = {}
    for r in rows:
        chains.setdefault(brand_of(r["name"]), []).append(r)
    chain_note = []
    for brand, outlets in chains.items():
        known = [o for o in outlets if o["eta_minutes"] is not None]
        if len(known) < 2:
            continue
        best = min(known, key=lambda o: o["eta_minutes"])
        for o in outlets:
            o["brand"] = brand
            o["nearest_branch"] = o is best
        chain_note.append({
            "brand": brand,
            "chosen": {"name": best["name"], "id": best["id"],
                       "eta_minutes": best["eta_minutes"]},
            "rejected": [{"name": o["name"], "id": o["id"],
                          "eta_minutes": o["eta_minutes"]}
                         for o in outlets if o is not best],
        })

    etas = [r["eta_minutes"] for r in rows if r["eta_minutes"]]
    payload = {
        **res,
        "restaurants": rows,
        "eta_summary": {
            "fastest_minutes": min(etas) if etas else None,
            "slowest_minutes": max(etas) if etas else None,
        },
    }
    if dishes:
        payload["dish_results"] = dishes
        payload["note"] = (
            f"{len(dishes)} result(s) were dishes, not restaurants (no rating/ETA). "
            "Use `food restaurant dish` for dish search, or search a restaurant name."
        )
    if dropped:
        payload["filtered_out"] = f"{dropped} restaurant(s) hidden by --max-eta {max_eta}"
    if chain_note:
        payload["brand_choices"] = chain_note
        payload["branch_note"] = (
            "A chain appears at more than one outlet. Order from the one with "
            "nearest_branch true - a farther outlet of the same chain is the "
            "same food with a longer wait and usually a higher delivery fee."
        )
    out(payload)


# "1. Garden Salad — ₹279 | Veg | 4.3★ | Sample Cafe
#  (restaurantId: 111) (ID: 222)"
_DISH_LINE = re.compile(
    r"^\s*\d+\.\s*(?P<name>.+?)\s*—\s*₹(?P<price>[\d.]+)\s*\|"
    r"\s*(?P<veg>Veg|Non-veg)?\s*\|?"
    r"(?:\s*(?P<rating>[\d.]+)★\s*\|)?"
    r"\s*(?P<restaurant>.+?)\s*\(restaurantId:\s*(?P<rid>\w+)\)"
    r"\s*\(ID:\s*(?P<item>\w+)\)",
    re.I,
)


def _remember_restaurant_names(pairs: list[tuple[str, str]]) -> None:
    """Cache restaurantId -> name.

    The cart response carries no restaurant id or name, so an ETA lookup has
    nothing to search for unless we noted the name when the restaurant was
    first seen.
    """
    if not pairs:
        return
    known = store.cache_get("restaurant_names") or {}
    known.update({str(rid): name for rid, name in pairs if rid and name})
    store.cache_set("restaurant_names", known, ttl=30 * 86400)


def _restaurant_name(restaurant_id: str) -> str:
    return (store.cache_get("restaurant_names") or {}).get(str(restaurant_id), "")


def _walk_values(value):
    """Yield every nested value in an MCP response without assuming its shape."""
    yield value
    if isinstance(value, dict):
        for nested in value.values():
            yield from _walk_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_values(nested)


def restaurant_availability_in(res, restaurant_id: str) -> dict:
    """Read the exact restaurant's provider-owned availability status."""
    wanted = str(restaurant_id)
    valid = {"OPEN", "CLOSED", "UNAVAILABLE"}
    for value in _walk_values({
        "structuredContent": res.get("structuredContent"),
        "content": res.get("content"),
    }):
        if not isinstance(value, dict):
            continue
        found_id = next(
            (value.get(key) for key in ("restaurantId", "restaurant_id", "id")
             if value.get(key) is not None),
            None,
        )
        if str(found_id) != wanted:
            continue
        raw = value.get("availabilityStatus", value.get("availability_status"))
        status = str(raw).strip().upper() if raw is not None else "UNKNOWN"
        return {
            "restaurant_id": wanted,
            "restaurant_name": value.get("name") or value.get("restaurantName"),
            "status": status if status in valid else "UNKNOWN",
            "verified": status in valid,
            "source_tool": "search_restaurants",
        }

    match = next(
        (row for row in parse_restaurants(text_of(res)) if str(row["id"]) == wanted),
        None,
    )
    status = (match or {}).get("availability_status") or "UNKNOWN"
    return {
        "restaurant_id": wanted,
        "restaurant_name": (match or {}).get("name"),
        "status": status,
        "verified": status in valid,
        "source_tool": "search_restaurants",
    }


def live_restaurant_availability(restaurant_id: str, addr: str) -> dict:
    """Re-read availability for one exact outlet immediately before checkout."""
    name = _restaurant_name(restaurant_id)
    if not name and str(store.get_pref("last_restaurant_id")) == str(restaurant_id):
        name = store.get_pref("last_restaurant_name") or ""
    res = call("food", "search_restaurants", {
        "addressId": addr,
        "query": name or str(restaurant_id),
    })
    if res.get("upstream_error") or res.get("isError"):
        return {
            "restaurant_id": str(restaurant_id),
            "restaurant_name": name or None,
            "status": "UNKNOWN",
            "verified": False,
            "source_tool": "search_restaurants",
            "error": str(res.get("upstream_error") or text_of(res))[:160],
        }
    return restaurant_availability_in(res, restaurant_id)


# "McDonald's, Indiranagar" / "Domino's - HAL Road" / "KFC (Koramangala)" all
# name the outlet after the chain, with no consistent separator.
_OUTLET_SUFFIX = re.compile(r"\s*(?:,| - | – | — |\(|\||\[)")


def brand_of(name: str) -> str:
    """The chain a restaurant belongs to, ignoring which outlet it is.

    Compared loosely (case and punctuation folded) so "McDonald's" and
    "Mcdonalds" are one brand.
    """
    base = _OUTLET_SUFFIX.split((name or "").strip(), 1)[0]
    return re.sub(r"[^a-z0-9]+", "", base.lower())


def prefer_nearest_branch(dishes: list[dict], addr: str) -> list[dict]:
    """Same chain, several outlets: mark the closest and flag the rest.

    A farther outlet of the same chain is the same food with a longer wait and
    usually a bigger delivery fee, so ordering from it is a mistake nobody makes
    on purpose. Every outlet here already stocks the dish - a search only
    returns restaurants that have it - so distance is the whole decision.

    ETA is only fetched for chains that actually appear more than once, which
    is what keeps this from costing a call per restaurant.
    """
    by_brand: dict[str, list[dict]] = {}
    for d in dishes:
        by_brand.setdefault(brand_of(d["restaurant"]), []).append(d)

    groups = []
    for brand, rows in by_brand.items():
        if len({r["restaurant_id"] for r in rows}) < 2:
            continue

        etas: dict[str, int | None] = {}
        for r in rows:
            rid = r["restaurant_id"]
            if rid not in etas:
                etas[rid] = r.get("eta_minutes")
                if etas[rid] is None:
                    try:
                        etas[rid] = restaurant_eta(
                            rid, addr, r["restaurant"]).get("eta_minutes")
                    except Exception:  # noqa: BLE001 - a missing ETA is not fatal
                        etas[rid] = None
            r["eta_minutes"] = etas[rid]

        known = {k: v for k, v in etas.items() if v is not None}
        if len(known) < 2:
            # Nothing to compare; leave every outlet in play rather than
            # picking one on no evidence.
            continue

        best = min(known, key=lambda k: known[k])
        chosen = next(r for r in rows if r["restaurant_id"] == best)
        for r in rows:
            r["brand"] = brand
            r["nearest_branch"] = r["restaurant_id"] == best
            if not r["nearest_branch"]:
                r["skip_reason"] = (
                    f"{r['restaurant']} is further than {chosen['restaurant']} "
                    f"({etas[r['restaurant_id']]} vs {known[best]} min) - same "
                    "chain, longer wait, usually a higher delivery fee"
                )
        groups.append({
            "brand": brand,
            "chosen": {"restaurant": chosen["restaurant"],
                       "restaurant_id": best,
                       "eta_minutes": known[best]},
            "rejected": [
                {"restaurant": r["restaurant"], "restaurant_id": r["restaurant_id"],
                 "eta_minutes": etas[r["restaurant_id"]]}
                for r in rows if r["restaurant_id"] != best
            ],
        })
    return groups


def parse_dishes(text: str) -> list[dict]:
    """Structure the dish-search results.

    Dish search is the better entry point than browsing a restaurant menu: one
    call yields the item, its price, the restaurant and both ids needed to add
    it to a cart.
    """
    rows = []
    for line in (text or "").splitlines():
        m = _DISH_LINE.match(line)
        if not m:
            continue
        g = m.groupdict()
        rows.append({
            "item_id": g["item"],
            "name": g["name"].strip(),
            "price": float(g["price"]),
            "veg": (g["veg"] or "").lower() == "veg",
            "rating": float(g["rating"]) if g["rating"] else None,
            "restaurant": g["restaurant"].strip(),
            "restaurant_id": g["rid"],
            "add_command": (
                f"food restaurant add --restaurant {g['rid']} --item {g['item']}:1"
            ),
        })
    return rows


@food_app.command("dish")
def food_dish(
    query: str = typer.Argument(..., help="Dish to search across restaurants."),
    address: str = typer.Option(None, "--address"),
    veg: bool = typer.Option(False, "--veg", help="Vegetarian only."),
    restaurant: str = typer.Option(None, "--restaurant", help="Restrict to a restaurant already in the cart."),
    sort: str = typer.Option(None, "--sort", help="price | rating"),
    max_price: float = typer.Option(None, "--max-price"),
    with_eta: bool = typer.Option(
        False, "--with-eta", help="Look up delivery ETA per restaurant (slower: one call each)."
    ),
    limit: int = typer.Option(
        10, "--limit",
        help="Return at most N dishes. Kept small because a long list is "
             "unusable aloud; pass a bigger number when browsing.",
    ),
    images: bool = typer.Option(
        False, "--images", help="Attach image URLs (cross-referenced from each menu)."
    ),
    download: bool = typer.Option(
        False, "--download", help="Also download images locally and return their paths."
    ),
    all_branches: bool = typer.Option(
        False, "--all-branches",
        help="Keep every outlet of a chain. By default, when the same chain "
             "appears more than once, only the nearest one is kept.",
    ),
):
    """Search a dish across ALL restaurants — the best starting point.

    Returns the item id, restaurant id, price and rating together, so you can
    go straight to `food add` without browsing any menu.
    """
    addr = resolve_address(address)
    args = {"addressId": addr, "query": query}
    if veg:
        args["vegFilter"] = True
    if restaurant:
        args["restaurantIdOfAddedItem"] = restaurant
    res = call("food", "search_menu", args)

    dishes = parse_dishes(text_of(res))
    _remember_restaurant_names([(d["restaurant_id"], d["restaurant"]) for d in dishes])
    hidden = 0
    if max_price is not None:
        keep = [d for d in dishes if d["price"] <= max_price]
        hidden = len(dishes) - len(keep)
        dishes = keep
    if sort == "price":
        dishes.sort(key=lambda d: d["price"])
    elif sort == "rating":
        dishes.sort(key=lambda d: (d["rating"] is None, -(d["rating"] or 0)))

    if with_eta:
        seen: dict[str, dict] = {}
        for d in dishes:
            rid = d["restaurant_id"]
            if rid not in seen:
                seen[rid] = restaurant_eta(rid, addr, d["restaurant"])
            d["eta_minutes"] = seen[rid].get("eta_minutes")

    # Same chain, several outlets: the far one is the same food with a longer
    # wait and usually a bigger delivery fee. Decide it here rather than hoping
    # the caller notices two McDonald's in the list.
    brand_choices = prefer_nearest_branch(dishes, addr)
    dropped_branches = 0
    if brand_choices and not all_branches:
        keep = [d for d in dishes if d.get("nearest_branch") is not False]
        dropped_branches = len(dishes) - len(keep)
        dishes = keep

    if images or download:
        _attach_images(dishes, addr, do_download=download)

    payload = {**res, "dishes": dishes[:limit] if limit else dishes}
    if brand_choices:
        payload["brand_choices"] = brand_choices
        payload["branch_note"] = (
            "A chain appeared at more than one outlet. "
            + (f"Kept the nearest and dropped {dropped_branches} farther one(s); "
               "pass --all-branches to see them."
               if dropped_branches else
               "Every outlet is listed; prefer the one with nearest_branch true.")
        )
    if images or download:
        payload["media_note"] = (
            "Swiggy exposes no dish descriptions - only names and images. "
            "Do not read local_image paths aloud; render or attach the image."
        )
    if hidden:
        payload["filtered_out"] = f"{hidden} dish(es) above --max-price {max_price}"
    if limit and len(dishes) > limit:
        payload["truncated"] = f"showing {limit} of {len(dishes)} dishes"
    out(payload)


@food_app.command("menu")
def food_menu(
    restaurant_id: str = typer.Argument(...),
    address: str = typer.Option(None, "--address"),
    page: int = typer.Option(None, "--page"),
):
    """Fetch a restaurant's menu."""
    args = {"addressId": resolve_address(address), "restaurantId": restaurant_id}
    if page is not None:
        args["page"] = page
    out(call("food", "get_restaurant_menu", args))


def _money_value(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _structured_food_cart(cart) -> dict | None:
    """The provider-owned Food cart object, never a prose reconstruction."""
    if not isinstance(cart, dict):
        return None
    structured = cart.get("structuredContent")
    if not isinstance(structured, dict):
        return None
    data = structured.get("data")
    return data if isinstance(data, dict) else None


def structured_delivery_fee(payload) -> float | None:
    """Read an explicitly structured delivery fee; never infer it from text."""
    values = [payload] if isinstance(payload, dict) else []
    if isinstance(payload, dict) and isinstance(payload.get("structuredContent"), dict):
        values.append(payload["structuredContent"])
    for value in values:
        for node in _walk_values(value):
            if not isinstance(node, dict):
                continue
            for key in ("delivery_charge", "deliveryCharge", "delivery_fee", "deliveryFee"):
                if key in node:
                    amount = _money_value(node[key])
                    if amount is not None:
                        return amount
    return None


def parse_bill_breakdown(cart) -> dict:
    """Reconcile a Food bill from structured MCP fields when they exist.

    The live Food MCP returns ``structuredContent.data.pricing``.  That is the
    only source of truth for spend decisions.  If it is absent, the bill is
    incomplete and payment must stop; provider prose is never a substitute.
    """
    data = _structured_food_cart(cart)
    if data is not None:
        pricing = data.get("pricing") if isinstance(data.get("pricing"), dict) else {}
        offer_data = data.get("offers") if isinstance(data.get("offers"), dict) else {}
        item_total = _money_value(pricing.get("item_total"))
        delivery = structured_delivery_fee({"pricing": pricing})
        taxes = _money_value(pricing.get("taxes_and_charges"))
        payable = _money_value(pricing.get("to_pay"))
        discount = _money_value(offer_data.get("coupon_discount"))
        coupon = offer_data.get("coupon_applied") if (discount or 0) > 0 else None
        source_tool = "get_food_cart.structuredContent.data.pricing"
    else:
        # Some test/older provider transports expose only the rendered cart.
        # Prefer the typed cart above whenever present; this fallback remains
        # deliberately conservative and requires every line item explicitly.
        rendered = text_of(cart)
        item_total = extract_total(rendered)
        delivery = extract_delivery_fee(rendered)
        taxes = extract_taxes_and_charges(rendered)
        payable = extract_payable(rendered)
        coupon = offers.applied_coupon(rendered)
        discount = offers.applied_discount(rendered)
        source_tool = "get_food_cart.legacy_rendered_bill"
    if coupon is None and discount is None:
        discount = 0.0

    fields = {
        "item_total": item_total,
        "coupon_discount": discount,
        "delivery_fee": delivery,
        "taxes_and_charges": taxes,
        "payable_total": payable,
    }
    missing = [name for name, value in fields.items() if value is None]
    calculated = None
    difference = None
    reconciles = False
    tolerance = None
    if not missing:
        calculated = round(item_total - discount + delivery + taxes, 2)
        difference = round(payable - calculated, 2)
        # Swiggy calculates taxes from per-item, coupon-adjusted prices, then
        # independently rounds the displayed bill lines and final payable.
        # Those display values can legitimately drift by sub-rupee amounts.
        # Keep materially unexplained charges blocked while absorbing that
        # provider rounding at the presentation boundary.
        tolerance = max(1.0, payable * 0.01)
        reconciles = abs(difference) <= tolerance

    if not missing and reconciles:
        note = "Authoritative pre-order bill from get_food_cart."
        if difference:
            note = (
                "Authoritative pre-order bill from get_food_cart; reconciled "
                f"within provider rounding tolerance (difference ₹{difference:+.2f}, "
                f"tolerance ₹{tolerance:.2f})."
            )
    else:
        note = (
            "The provider did not return a complete, internally consistent "
            "pre-order bill. Do not place the order."
        )
    return {
        "source_tool": source_tool,
        **fields,
        "coupon_code": coupon,
        "calculated_total": calculated,
        "difference": difference,
        "reconciles": reconciles,
        "complete": not missing and reconciles,
        "missing_fields": missing,
        "note": note,
    }


_CART_UNAVAILABLE = re.compile(
    r"no longer taking|not taking (?:new )?orders|not accepting|closed|"
    r"unavailable|not serviceable|cannot deliver",
    re.I,
)


def cart_orderability(res) -> dict:
    """Read restaurant serviceability and item stock from structured cart JSON."""
    structured = res.get("structuredContent")
    if not isinstance(structured, (dict, list)):
        return {
            "source_tool": "get_food_cart",
            "orderable": None,
            "verified": False,
            "reason": "No structured cart availability data was returned.",
            "unavailable_items": [],
        }

    messages: list[str] = []
    unavailable_items: list[dict] = []
    stock_values: list[bool] = []
    for value in _walk_values(structured):
        if not isinstance(value, dict):
            continue
        for key in ("statusMessage", "status_message"):
            message = value.get(key)
            if isinstance(message, str) and message not in messages:
                messages.append(message)
        if "in_stock" in value or "inStock" in value:
            raw = value.get("in_stock", value.get("inStock"))
            available = raw not in (False, 0, "0", "false", "False", None)
            stock_values.append(available)
            if not available:
                unavailable_items.append({
                    "name": value.get("name") or value.get("itemName"),
                    "item_id": value.get("menu_item_id") or value.get("itemId"),
                })

    blocking_message = next((m for m in messages if _CART_UNAVAILABLE.search(m)), None)
    if blocking_message or unavailable_items:
        return {
            "source_tool": "get_food_cart",
            "orderable": False,
            "verified": True,
            "reason": blocking_message or "One or more cart items are unavailable.",
            "unavailable_items": unavailable_items,
        }
    return {
        "source_tool": "get_food_cart",
        "orderable": True if stock_values and all(stock_values) else None,
        "verified": bool(stock_values),
        "reason": None,
        "unavailable_items": [],
    }


def _live_cart_signature(res) -> dict[str, int]:
    """Live menu-item quantities from structured cart JSON or its prose fallback."""
    signature: dict[str, int] = {}
    structured = res.get("structuredContent")
    if isinstance(structured, (dict, list)):
        for value in _walk_values(structured):
            if not isinstance(value, dict):
                continue
            item_id = value.get("menu_item_id", value.get("menuItemId"))
            if item_id is None:
                continue
            try:
                signature[str(item_id)] = int(value.get("quantity") or 1)
            except (TypeError, ValueError):
                continue
    if not signature:
        signature = {
            str(item["menu_item_id"]): int(item.get("quantity") or 1)
            for item in _cart_items(text_of(res))
        }
    return signature


def cart_restaurant_context(res, restaurant_id: str | None = None) -> dict:
    """Detect when cached restaurant context belongs to a different live cart."""
    rid = str(restaurant_id or store.get_pref("last_restaurant_id") or "")
    live = _live_cart_signature(res)
    remembered = store.get_pref(f"cart_items:{rid}") if rid else None
    expected = {
        str(item_id): int((item or {}).get("quantity") or 1)
        for item_id, item in (remembered or {}).items()
        if isinstance(item, dict)
    }
    verified = bool(rid and live and expected and live == expected)
    conflict = bool(rid and live and expected and live != expected)
    return {
        "restaurant_id": rid or None,
        "restaurant_name": _restaurant_name(rid) if rid else None,
        "verified": verified,
        "conflict": conflict,
        "source": "last successful update_food_cart",
        "reason": (
            None if verified else
            "The live cart items do not match this restaurant's remembered cart."
            if conflict else
            "The live cart has no verified restaurant binding; pass the intended "
            "restaurant explicitly at placement."
        ),
    }


def _cart_named_signature(res) -> dict[str, int]:
    signature: dict[str, int] = {}
    structured = res.get("structuredContent")
    if isinstance(structured, (dict, list)):
        for value in _walk_values(structured):
            if not isinstance(value, dict) or not value.get("name"):
                continue
            if value.get("menu_item_id", value.get("menuItemId")) is None:
                continue
            signature[str(value["name"]).strip().casefold()] = int(
                value.get("quantity") or 1
            )
    if not signature:
        signature = {
            str(item["name"]).strip().casefold(): int(item.get("quantity") or 1)
            for item in _cart_items(text_of(res)) if item.get("name")
        }
    return signature


def _order_details_context(details, expected_restaurant_id: str,
                           expected_items: dict[str, int]) -> dict:
    """Vendor/items actually attached to the newly-created provider order."""
    order = None
    structured = details.get("structuredContent")
    for value in _walk_values(structured):
        if not isinstance(value, dict):
            continue
        if isinstance(value.get("restaurant"), dict) and isinstance(value.get("items"), list):
            order = value
            break
    if not order:
        return {
            "verified": False,
            "restaurant_matches": None,
            "items_match": None,
            "reason": "Order details did not return structured vendor/item data.",
        }

    restaurant = order.get("restaurant") or {}
    actual_restaurant_id = restaurant.get("id", restaurant.get("restaurantId"))
    actual_items = {
        str(item.get("name") or "").strip().casefold(): int(item.get("quantity") or 1)
        for item in order.get("items") or []
        if isinstance(item, dict) and item.get("name")
    }
    restaurant_matches = (
        str(actual_restaurant_id) == str(expected_restaurant_id)
        if actual_restaurant_id is not None else None
    )
    items_match = actual_items == expected_items if actual_items and expected_items else None
    return {
        "verified": restaurant_matches is True and items_match is True,
        "restaurant_id": str(actual_restaurant_id) if actual_restaurant_id is not None else None,
        "restaurant_name": restaurant.get("name"),
        "restaurant_matches": restaurant_matches,
        "items_match": items_match,
        "reason": (
            None if restaurant_matches is True and items_match is True else
            "The created order vendor or items differ from the approved live cart."
            if restaurant_matches is False or items_match is False else
            "Order vendor/items could not be fully verified."
        ),
    }


@food_app.command("cart")
def food_cart(address: str = typer.Option(None, "--address")):
    """Show the authoritative pre-order bill and delivery estimate."""
    addr = resolve_address(address)
    res = call("food", "get_food_cart", {"addressId": addr})
    payload = {**res}
    breakdown = parse_bill_breakdown(res)
    restaurant_context = cart_restaurant_context(res)
    payload["checkout_preview"] = {
        "payable_total": breakdown["payable_total"],
        "delivery_fee": breakdown["delivery_fee"],
        "delivery_is_free": breakdown["delivery_fee"] == 0,
        "applied_coupon": breakdown["coupon_code"],
        "bill_breakdown": breakdown,
        "orderability": cart_orderability(res),
        "restaurant_context": restaurant_context,
        "approval_note": (
            "Read the items, delivery fee, taxes/charges, coupon and exact payable "
            "total to the user. Confirm only when bill_breakdown.complete is true "
            "and orderability.orderable is not false."
        ),
    }
    rid = store.get_pref("last_restaurant_id")
    if rid and restaurant_context["verified"]:
        payload["eta"] = restaurant_eta(rid, addr, _restaurant_name(rid))
    out(payload)


def _parse_choice(spec: str, kind: str) -> tuple[str, dict]:
    """Parse `itemId:groupId:choiceId[:name[:price]]` into (itemId, payload)."""
    parts = spec.split(":")
    if len(parts) < 3:
        raise ValueError(
            f"--{kind} needs itemId:groupId:{'choiceId' if kind == 'addon' else 'variationId'}"
            f" (got {spec!r})"
        )
    item_id, group_id, choice_id = parts[0], parts[1], parts[2]
    key = "choice_id" if kind == "addon" else "variation_id"
    payload: dict = {key: choice_id, "group_id": group_id}
    if len(parts) > 3 and parts[3]:
        payload["name"] = parts[3]
    if len(parts) > 4 and parts[4]:
        payload["price"] = parts[4]
    return item_id, payload


@food_app.command("add")
def food_add(
    restaurant_id: str = typer.Option(..., "--restaurant"),
    item: list[str] = typer.Option(None, "--item", help="menu_item_id:quantity (repeatable)."),
    addon: list[str] = typer.Option(
        None, "--addon",
        help="itemId:groupId:choiceId[:name[:price]] — attaches an addon to that item.",
    ),
    variant: list[str] = typer.Option(
        None, "--variant",
        help="itemId:groupId:variationId[:name[:price]] — required for items with variations.",
    ),
    items_json: str = typer.Option(
        None, "--items-json",
        help="Full cartItems array as JSON, for anything the flags cannot express.",
    ),
    address: str = typer.Option(None, "--address"),
    cutlery: bool = typer.Option(False, "--cutlery"),
    auto_coupon: bool = typer.Option(
        True, "--auto-coupon/--no-auto-coupon",
        help="Apply the best eligible coupon before showing the final cart (default: on).",
    ),
):
    """Add items to the food cart, with variants and addons.

    Simple:   --item 12345:2
    Variant:  --item 12345:1 --variant 12345:g1:v9
    Addon:    --item 12345:1 --addon 12345:g4:c7 --addon 12345:g4:c8

    A menu item flagged "has addons" may have a REQUIRED variation (size, base).
    Adding it without one can fail or silently pick a default, so check
    `food restaurant menu <restaurantId>` when the item lists variations.
    """
    if items_json:
        try:
            cart_items = json.loads(items_json)
        except json.JSONDecodeError as e:
            err(f"--items-json must be valid JSON: {e}")
            raise typer.Exit(2) from e
        if not isinstance(cart_items, list):
            err("--items-json must be a JSON array of cart items.")
            raise typer.Exit(2)
    else:
        if not item:
            err("Pass --item menu_item_id:quantity, or --items-json.")
            raise typer.Exit(2)
        by_id: dict[str, dict] = {}
        order: list[str] = []
        for spec in item:
            mid, _, qty = spec.partition(":")
            by_id[mid] = {"menu_item_id": mid, "quantity": int(qty or 1)}
            order.append(mid)
        for kind, specs in (("addon", addon or []), ("variant", variant or [])):
            for spec in specs:
                try:
                    mid, payload = _parse_choice(spec, kind)
                except ValueError as e:
                    err(str(e))
                    raise typer.Exit(2) from e
                if mid not in by_id:
                    err(f"--{kind} refers to {mid}, which is not in any --item.")
                    raise typer.Exit(2)
                by_id[mid].setdefault("addons" if kind == "addon" else "variants", []).append(payload)
        cart_items = [by_id[m] for m in order]

    addr = resolve_address(address)
    previous_restaurant_id = store.get_pref("last_restaurant_id")
    switched_restaurant = (
        previous_restaurant_id is not None
        and str(previous_restaurant_id) != str(restaurant_id)
    )
    cart_reset = None
    if switched_restaurant:
        # Never leave it to an agent to reason about Swiggy's one-restaurant
        # cart. A restaurant switch is always an explicit fresh-cart boundary.
        cart_reset = call("food", "flush_food_cart", {})
    res = call("food", "update_food_cart", {
        "restaurantId": restaurant_id,
        "cartItems": cart_items,
        "addressId": addr,
        "cutleryOptIn": cutlery,
    })
    # Remember it so `food eta` / `food place` can quote a delivery time without
    # the caller having to pass the id again.
    store.set_pref("last_restaurant_id", restaurant_id)
    # Names are per-restaurant: keeping a previous one would send the ETA
    # lookup after the wrong place.
    store.set_pref("last_restaurant_name", _restaurant_name(restaurant_id))
    # The cart read-back is prose and carries no addon/variant ids, so remember
    # what we sent: `remove` and `set-qty` rewrite the whole cart and would
    # otherwise silently strip customisations.
    remembered = {i["menu_item_id"]: i for i in cart_items if isinstance(i, dict)}
    # update_food_cart replaces the entire cart, so this memory must also be
    # an exact replacement. Keeping older ids here makes later recovery restore
    # items the user no longer has.
    store.set_pref(f"cart_items:{restaurant_id}", remembered)
    coupon_result = None
    if auto_coupon:
        # Coupon application is a cart mutation. Do it here, before returning
        # the bill the user will approve; placement must never rewrite a cart.
        try:
            coupon_result = apply_best_coupon(restaurant_id, addr)
        except Exception as e:  # noqa: BLE001 - cart preparation stays useful
            coupon_result = {
                "status": "unavailable",
                "note": f"Coupon lookup skipped: {e}",
            }
    # update_food_cart only acknowledges the mutation. Read the server-owned
    # cart immediately so this one command returns the real delivery fee,
    # taxes, coupon result, orderability, and final payable amount.
    live_cart = call("food", "get_food_cart", {"addressId": addr})
    bill = parse_bill_breakdown(live_cart)
    payload = {
        **res,
        "cart_reset": {
            "performed": switched_restaurant,
            "previous_restaurant_id": str(previous_restaurant_id)
            if previous_restaurant_id is not None else None,
            "detail": cart_reset,
        },
        "live_cart": live_cart,
        "coupon": coupon_result,
        "checkout_preview": {
            "payable_total": bill["payable_total"],
            "delivery_fee": bill["delivery_fee"],
            "delivery_is_free": bill["delivery_fee"] == 0
            if bill["delivery_fee"] is not None else None,
            "applied_coupon": bill["coupon_code"],
            "bill_breakdown": bill,
            "orderability": cart_orderability(live_cart),
            "approval_note": (
                "This is the authoritative post-add cart. Do not start payment "
                "unless complete is true and the user approves payable_total."
            ),
        },
        "eta": restaurant_eta(restaurant_id, addr, _restaurant_name(restaurant_id)),
    }
    out(payload)


# "  - Sample Combo Bowl — ₹199 (ID: 111222)"
_CART_LINE = re.compile(r"^\s*[-*]\s*(?P<name>.+?)\s*—\s*₹(?P<price>[\d.]+).*?\(ID:\s*(?P<id>\w+)\)")
_CART_QTY = re.compile(r"\bx\s*(\d+)\b|\((\d+)\)\s*$")


def _rebuild(items: list[dict], restaurant_id: str) -> list[dict]:
    """Re-attach remembered variants/addons when rewriting the cart."""
    remembered = store.get_pref(f"cart_items:{restaurant_id}") or {}
    out_items = []
    for i in items:
        mid = i["menu_item_id"]
        row = {"menu_item_id": mid, "quantity": i["quantity"]}
        saved = remembered.get(mid) or {}
        for key in ("variants", "variantsV2", "addons"):
            if saved.get(key):
                row[key] = saved[key]
        out_items.append(row)
    return out_items


def _cart_items(text: str) -> list[dict]:
    """Read the current food cart back into structured rows.

    Needed because Swiggy offers no per-item delete: update_food_cart replaces
    the whole cart, so removing one item means rewriting the rest.
    """
    items = []
    for line in (text or "").splitlines():
        m = _CART_LINE.match(line)
        if not m:
            continue
        g = m.groupdict()
        q = _CART_QTY.search(line)
        qty = int(next((x for x in (q.groups() if q else []) if x), 1))
        items.append({
            "menu_item_id": g["id"],
            "name": g["name"].strip(),
            "price": float(g["price"]),
            "quantity": qty,
        })
    return items


def _cart_signature(items: list[dict]) -> tuple[tuple[str, int], ...]:
    """Order-independent item/quantity identity for cart-integrity checks."""
    return tuple(sorted(
        (str(i["menu_item_id"]), int(i.get("quantity") or 1))
        for i in items
        if isinstance(i, dict) and i.get("menu_item_id")
    ))


def _cart_items_from_response(res) -> list[dict]:
    """Read cart items from structured MCP data, with legacy prose fallback."""
    rows: list[dict] = []
    for value in _walk_values((res or {}).get("structuredContent") if isinstance(res, dict) else None):
        if not isinstance(value, dict):
            continue
        item_id = value.get("menu_item_id", value.get("menuItemId"))
        if item_id is None:
            continue
        rows.append({
            "menu_item_id": str(item_id),
            "name": str(value.get("name") or value.get("itemName") or "").strip(),
            "price": _money_value(value.get("price")),
            "quantity": int(value.get("quantity") or 1),
        })
    return rows or _cart_items(text_of(res))


def _intended_cart_snapshot(cart_res: dict, restaurant_id: str) -> dict:
    """The cart state a coupon API is not allowed to replace server-side."""
    visible = _cart_items_from_response(cart_res)
    payload = _rebuild(visible, restaurant_id)
    return {
        "signature": _cart_signature(visible),
        "items": payload,
    }


def _apply_coupon_preserving_cart(
    code: str,
    addr: str,
    restaurant_id: str,
    intended: dict,
) -> tuple[dict, dict, dict]:
    """Apply one coupon and restore the submitted cart if Swiggy swaps it.

    The coupon endpoint can resurrect an older server-side cart. Every coupon
    mutation therefore gets an immediate item/quantity read-back. A mismatch
    is repaired with the exact cart remembered from update_food_cart and then
    verified before the caller is allowed to continue.
    """
    res = call("food", "apply_food_coupon", {
        "couponCode": code,
        "addressId": addr,
    })
    after_res = call("food", "get_food_cart", {"addressId": addr})
    after_items = _cart_items_from_response(after_res)
    expected = intended["signature"]
    observed = _cart_signature(after_items)
    integrity = {
        "expected": [list(x) for x in expected],
        "observed": [list(x) for x in observed],
        "changed": observed != expected,
        "restored": False,
    }
    if observed == expected:
        return res, after_res, integrity

    restore = call("food", "update_food_cart", {
        "restaurantId": restaurant_id,
        "cartItems": intended["items"],
        "addressId": addr,
    })
    verified_res = call("food", "get_food_cart", {"addressId": addr})
    verified = _cart_signature(_cart_items_from_response(verified_res))
    integrity.update({
        "restored": verified == expected,
        "restored_signature": [list(x) for x in verified],
        "restore_response": text_of(restore)[:200],
    })
    return res, verified_res, integrity


# "Sharing Platter (For 2-3 People)", "Meal For Two", "serves 1"
_SERVES = re.compile(
    r"(?:serves|for)\s*(\d+)\s*(?:-\s*(\d+))?\s*(?:people|persons?|pax)?", re.I
)
_SERVES_WORD = re.compile(r"\bfor\s+(two|three|four)\b", re.I)
_WORD_NUM = {"two": 2, "three": 3, "four": 4}


def _serves_count(name: str) -> int:
    """How many people one unit of this item feeds. Defaults to 1."""
    m = _SERVES_WORD.search(name or "")
    if m:
        return _WORD_NUM[m.group(1).lower()]
    m = _SERVES.search(name or "")
    if m:
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        return max(lo, hi)
    if re.search(r"\bfamily|carrier\b", name or "", re.I):
        return 3
    return 1


def _menu_items_for(restaurant_id: str, address_id: str, max_pages: int = 4) -> dict[str, dict]:
    """itemId -> item detail (incl. image) for one restaurant, cached in SQLite.

    Dish search omits images, so they must come from the restaurant's own menu.
    """
    key = f"menu_items:{restaurant_id}"
    cached = store.cache_get(key)
    if cached:
        return cached

    items: dict[str, dict] = {}
    for page in range(1, max_pages + 1):
        res = call("food", "get_restaurant_menu", {
            "addressId": address_id, "restaurantId": restaurant_id, "page": page,
        })
        text = text_of(res)
        items.update(media.parse_menu_items(text))
        if "Use page" not in text:
            break
    store.cache_set(key, items, ttl=3600)
    return items


def _attach_images(dishes: list[dict], address_id: str, do_download: bool = False) -> None:
    """Enrich dish rows with image URLs, and optionally local file paths."""
    by_restaurant: dict[str, dict] = {}
    for d in dishes:
        rid = d["restaurant_id"]
        if rid not in by_restaurant:
            try:
                by_restaurant[rid] = _menu_items_for(rid, address_id)
            except Exception:  # noqa: BLE001
                by_restaurant[rid] = {}
        hit = by_restaurant[rid].get(d["item_id"])
        d["image_url"] = (hit or {}).get("image_url")
        if hit:
            d["bestseller"] = hit.get("bestseller", False)

    if do_download:
        mapping = media.download_many([d.get("image_url") for d in dishes])
        for d in dishes:
            d["local_image"] = mapping.get(d.get("image_url"))


def restaurant_eta(restaurant_id: str, address_id: str, name_hint: str = "") -> dict:
    """Current delivery ETA for a restaurant, plus the clock time it implies.

    The cart response carries no ETA, so we look the restaurant up by name and
    read the ETA off the search listing.
    """
    import datetime as _dt

    query = name_hint or _restaurant_name(restaurant_id) or restaurant_id
    res = call("food", "search_restaurants", {"addressId": address_id, "query": query})
    rows = parse_restaurants(text_of(res))
    match = next((r for r in rows if r["id"] == str(restaurant_id)), None)
    if match is None or match["eta_minutes"] is None:
        return {"known": False, "restaurant_id": restaurant_id}

    eta = match["eta_minutes"]
    arrival = _dt.datetime.now() + _dt.timedelta(minutes=eta)
    return {
        "known": True,
        "restaurant_id": restaurant_id,
        "restaurant": match["name"],
        "eta_minutes": eta,
        "arrives_by": arrival.strftime("%H:%M"),
        "spoken": f"about {eta} minutes, so around {arrival.strftime('%-I:%M %p').lower()}",
    }


@food_app.command("eta")
def food_eta(
    restaurant_id: str = typer.Option(None, "--restaurant", help="Defaults to the cart's restaurant."),
    address: str = typer.Option(None, "--address"),
):
    """Approximate delivery time for the current cart, before ordering."""
    rid = restaurant_id or store.get_pref("last_restaurant_id")
    if not rid:
        err("No restaurant known. Pass --restaurant <id> or add something to the cart first.")
        raise typer.Exit(2)
    out(restaurant_eta(rid, resolve_address(address), _restaurant_name(rid)))


@food_app.command("remove")
def food_remove(
    item: list[str] = typer.Option(..., "--item", help="menu_item_id to remove (repeatable)."),
    restaurant_id: str = typer.Option(None, "--restaurant", help="Defaults to the cart's restaurant."),
    address: str = typer.Option(None, "--address"),
):
    """Remove specific items from the food cart.

    Swiggy has no delete endpoint - update_food_cart is a full replacement, so
    we read the cart, drop the named items and write the remainder back.
    """
    addr = resolve_address(address)
    rid = restaurant_id or store.get_pref("last_restaurant_id")
    if not rid:
        err("No restaurant known. Pass --restaurant <id>.")
        raise typer.Exit(2)

    current = _cart_items(text_of(call("food", "get_food_cart", {"addressId": addr})))
    if not current:
        out({"status": "cart_empty_or_unparsed", "removed": []})
        return

    drop = set(item)
    keep = [i for i in current if i["menu_item_id"] not in drop]
    missing = [i for i in drop if i not in {c["menu_item_id"] for c in current}]

    if not keep:
        res = call("food", "flush_food_cart", {})
        out({"status": "emptied", "removed": sorted(drop), "not_in_cart": missing, "detail": res})
        return

    call("food", "flush_food_cart", {})
    res = call("food", "update_food_cart", {
        "restaurantId": rid,
        "cartItems": _rebuild(keep, rid),
        "addressId": addr,
    })
    out({
        "status": "removed",
        "removed": sorted(drop & {c["menu_item_id"] for c in current}),
        "not_in_cart": missing,
        "remaining": keep,
        "detail": res,
    })


@food_app.command("set-qty")
def food_set_qty(
    item: str = typer.Option(..., "--item", help="menu_item_id."),
    quantity: int = typer.Option(..., "--qty", help="New quantity; 0 removes it."),
    restaurant_id: str = typer.Option(None, "--restaurant"),
    address: str = typer.Option(None, "--address"),
):
    """Change the quantity of one cart item (0 removes it)."""
    addr = resolve_address(address)
    rid = restaurant_id or store.get_pref("last_restaurant_id")
    if not rid:
        err("No restaurant known. Pass --restaurant <id>.")
        raise typer.Exit(2)

    current = _cart_items(text_of(call("food", "get_food_cart", {"addressId": addr})))
    updated = [i for i in current if i["menu_item_id"] != item]
    if quantity > 0:
        updated.append({"menu_item_id": item, "quantity": quantity})

    if not updated:
        out({"status": "emptied", "detail": call("food", "flush_food_cart", {})})
        return

    call("food", "flush_food_cart", {})
    res = call("food", "update_food_cart", {
        "restaurantId": rid,
        "cartItems": _rebuild(updated, rid),
        "addressId": addr,
    })
    out({"status": "updated", "item": item, "quantity": quantity, "detail": res})


@food_app.command("edit")
def food_edit(
    item: str = typer.Option(..., "--item", help="menu_item_id already in the cart."),
    quantity: int = typer.Option(None, "--qty", help="New quantity (0 removes it)."),
    addon: list[str] = typer.Option(
        None, "--addon", help="itemId:groupId:choiceId[:name[:price]] — replaces existing addons."),
    variant: list[str] = typer.Option(
        None, "--variant", help="itemId:groupId:variationId[:name[:price]] — replaces variants."),
    clear_addons: bool = typer.Option(False, "--clear-addons", help="Drop all addons on this item."),
    clear_variants: bool = typer.Option(False, "--clear-variants"),
    restaurant_id: str = typer.Option(None, "--restaurant"),
    address: str = typer.Option(None, "--address"),
):
    """Edit one cart item: quantity, addons, or variants.

    Swiggy has no partial-update endpoint, so this reads the cart, changes the
    one item and writes the whole thing back. Other items keep the addons they
    were added with.
    """
    addr = resolve_address(address)
    rid = restaurant_id or store.get_pref("last_restaurant_id")
    if not rid:
        err("No restaurant known. Pass --restaurant <id>.")
        raise typer.Exit(2)

    current = _cart_items(text_of(call("food", "get_food_cart", {"addressId": addr})))
    if not any(i["menu_item_id"] == item for i in current):
        err(f"{item} is not in the cart. `food restaurant cart` shows what is.")
        raise typer.Exit(2)

    rebuilt = _rebuild(current, rid)
    remembered = store.get_pref(f"cart_items:{rid}") or {}
    updated = []
    for row in rebuilt:
        if row["menu_item_id"] != item:
            updated.append(row)
            continue
        if quantity is not None:
            if quantity <= 0:
                continue                      # dropping it
            row["quantity"] = quantity
        if clear_addons:
            row.pop("addons", None)
        if clear_variants:
            row.pop("variants", None)
            row.pop("variantsV2", None)
        for kind, specs in (("addon", addon or []), ("variant", variant or [])):
            if not specs:
                continue
            key = "addons" if kind == "addon" else "variants"
            row[key] = []
            for spec in specs:
                try:
                    mid, payload = _parse_choice(spec, kind)
                except ValueError as e:
                    err(str(e))
                    raise typer.Exit(2) from e
                if mid != item:
                    err(f"--{kind} refers to {mid}, but --item is {item}.")
                    raise typer.Exit(2)
                row[key].append(payload)
        updated.append(row)

    if not updated:
        out({"status": "emptied", "detail": call("food", "flush_food_cart", {})})
        return

    call("food", "flush_food_cart", {})
    res = call("food", "update_food_cart", {
        "restaurantId": rid, "cartItems": updated, "addressId": addr,
    })
    # keep the memory in step with what the cart now holds
    store.set_pref(f"cart_items:{rid}",
                   {**remembered, **{i["menu_item_id"]: i for i in updated}})
    out({"status": "updated", "item": item, "cart_items": updated, "detail": res})


@food_app.command("clear")
def food_clear():
    """Empty the food cart."""
    out(call("food", "flush_food_cart", {}))


@food_app.command("coupons")
def food_coupons(
    restaurant_id: str = typer.Option(..., "--restaurant"),
    address: str = typer.Option(None, "--address"),
):
    """List coupons available for this restaurant."""
    out(call("food", "fetch_food_coupons", {
        "restaurantId": restaurant_id,
        "addressId": resolve_address(address),
    }))


def apply_best_coupon(restaurant_id: str, addr: str, probe: int = 4,
                      apply: bool = True) -> dict:
    """Find the coupon that genuinely lowers the bill, and apply it.

    Swiggy's listing cannot be trusted to predict eligibility - coupons do not
    stack on already-discounted items, yet the listing still shows a plain
    value shortfall. So candidates are applied for real and the one that
    actually reduces the payable amount wins.
    """
    cart_res = call("food", "get_food_cart", {"addressId": addr})
    bill = parse_bill_breakdown(cart_res)
    total = bill["payable_total"] if bill["complete"] else None
    items_total = bill["item_total"]
    intended = _intended_cart_snapshot(cart_res, restaurant_id)

    # What the cart already had. Probing REPLACES whatever coupon is applied, so
    # without this a run that finds nothing better leaves the cart stripped of a
    # discount it arrived with - the user then pays full price.
    pre_code = bill["coupon_code"]
    pre_discount = bill["coupon_discount"]

    coupon_res = call("food", "fetch_food_coupons",
                      {"restaurantId": restaurant_id, "addressId": addr})
    parsed = offers.parse_coupons(text_of(coupon_res))
    near = offers.near_misses(parsed, applied_code=pre_code)

    store.set_pref(f"card_offers:{restaurant_id}", offers.card_offers(parsed))
    store.set_pref(f"near_misses:{restaurant_id}", near)

    result: dict = {
        "cart_total": total,
        "items_total": items_total,
        "ranked": offers.rank(parsed, total or 0),
        "near_misses": near,
        "card_offers": offers.card_offers(parsed),
        "already_applied": pre_code,
        "already_saving": pre_discount,
    }

    if total is None:
        result["status"] = "cart_total_unknown"
        result["note"] = "Could not read a payable total; cannot rank coupons."
        return result
    if not intended["signature"]:
        result["status"] = "cart_items_unknown"
        result["note"] = (
            "Could not identify the live cart items; refusing to mutate coupons "
            "because the cart could not be restored safely."
        )
        return result

    best, ranked = offers.pick_best(parsed, total)
    result["ranked"] = ranked
    if not apply:
        result["best"] = best
        result["status"] = "dry_run"
        return result

    attempts, winner = [], None
    for cand in offers.probe_order(ranked, probe):
        res, after_res, integrity = _apply_coupon_preserving_cart(
            cand["code"], addr, restaurant_id, intended,
        )
        body = text_of(res)
        after_bill = parse_bill_breakdown(after_res)
        new_total = after_bill["payable_total"] if after_bill["complete"] else None
        ok = new_total is not None and new_total < total
        attempts.append({
            "code": cand["code"], "worked": ok,
            "saving": round(total - new_total, 2) if ok else 0.0,
            "response": body[:160],
            "cart_integrity": integrity,
        })
        if integrity["changed"]:
            result.update(
                status=("cart_restored_after_coupon_revert"
                        if integrity["restored"] else "cart_restore_failed"),
                attempts=attempts,
                best=None,
                cart_integrity=integrity,
                note=(
                    "Swiggy changed the cart while applying a coupon. The intended "
                    "items were restored; coupon probing stopped. Re-read the cart "
                    "before requesting confirmation."
                    if integrity["restored"] else
                    "Swiggy changed the cart while applying a coupon and the intended "
                    "items could not be verified after restoration. Do not place."
                ),
            )
            return result
        if ok and (winner is None or new_total < winner["new_total"]):
            winner = {"code": cand["code"], "new_total": new_total,
                      "saving": round(total - new_total, 2)}

    result["attempts"] = attempts
    if winner:
        # Applying replaces whichever coupon was applied last, so re-apply the
        # winner to leave the cart in its best state.
        if attempts and attempts[-1]["code"] != winner["code"]:
            _, _, integrity = _apply_coupon_preserving_cart(
                winner["code"], addr, restaurant_id, intended,
            )
            if integrity["changed"]:
                result.update(
                    status=("cart_restored_after_coupon_revert"
                            if integrity["restored"] else "cart_restore_failed"),
                    best=None,
                    cart_integrity=integrity,
                )
                return result
        result.update(status="applied", best=winner,
                      new_total=winner["new_total"], actual_saving=winner["saving"])
    else:
        result.update(status="no_beneficial_coupon", best=None)
        # Nothing beat what was already there - so put back what was already
        # there. Probing has otherwise left the last failed candidate applied,
        # or no coupon at all.
        if attempts and pre_code:
            _, _, integrity = _apply_coupon_preserving_cart(
                pre_code, addr, restaurant_id, intended,
            )
            if integrity["changed"]:
                result.update(
                    status=("cart_restored_after_coupon_revert"
                            if integrity["restored"] else "cart_restore_failed"),
                    best=None,
                    cart_integrity=integrity,
                )
                return result
            result["restored"] = pre_code

    if attempts:
        result.update(_verify_cart_not_worse(addr, total, pre_code))
    return result


def _verify_cart_not_worse(addr: str, baseline: float, pre_code: str | None) -> dict:
    """Confirm probing did not leave the cart costing more than it started.

    Probing mutates a real cart, so "it should be fine" is not good enough: read
    the cart back and compare. A regression here means the user is about to be
    asked to pay more than before the CLI touched anything, which must never
    pass silently into a payment.
    """
    bill = parse_bill_breakdown(call("food", "get_food_cart", {"addressId": addr}))
    after = bill["payable_total"] if bill["complete"] else None
    if after is None or baseline is None:
        return {"verified_total": after}
    if after <= baseline + 0.01:
        return {"verified_total": after}

    err(
        f"\n⚠️  The cart is now ₹{after:.0f}, up from ₹{baseline:.0f} before "
        "coupons were tried.\n"
        + (f"    Re-applying {pre_code} did not restore it.\n" if pre_code else
           "    A discount that was on the cart has been lost.\n")
        + "    Do NOT pay this. Re-apply the coupon and re-check the total.\n"
    )
    return {
        "verified_total": after,
        "status": "cart_degraded",
        "cart_degraded": {
            "before": baseline,
            "after": after,
            "lost": round(after - baseline, 2),
            "was_applied": pre_code,
            "note": "Probing left the cart worse than it started. Do not pay.",
        },
    }


def topup_upside(coupon_result: dict, restaurant_id: str, addr: str,
                 veg_only: bool = False) -> dict | None:
    """Is there a coupon worth topping up for, versus what is already applied?

    The comparison has to be against the CURRENT discount, not against full
    price - otherwise a bigger headline coupon looks like free money when it
    would actually replace a discount already in hand.

    Returns the best opportunity with honest figures: what the extra food
    costs in cash after the swap, and how much food it buys.
    """
    # What the cart is ALREADY saving. Two sources, in order: a coupon this run
    # applied, or one the cart arrived with. Missing the second is what made a
    # top-up look like free money against an already-discounted cart.
    current_saving = (coupon_result.get("best") or {}).get("saving")
    if current_saving is None:
        current_saving = coupon_result.get("already_saving")
    unknown_discount = current_saving is None and bool(coupon_result.get("already_applied"))
    current_saving = current_saving or 0.0

    near = [n for n in (coupon_result.get("near_misses") or []) if n.get("would_save")]
    if not near:
        return None

    menu = list(_menu_items_for(restaurant_id, addr).values())
    best: dict | None = None

    for n in sorted(near, key=lambda x: x["spend_more"]):
        plan = topup.plan(menu, n["spend_more"], veg_only=veg_only)
        if not plan.get("found"):
            continue
        added = plan["added_cost"]
        # Swapping coupons: we gain the new discount but lose the current one.
        discount_gain = n["would_save"] - current_saving
        extra_cash = round(added - discount_gain, 2)
        row = {
            "coupon": n["code"],
            "add_items": plan["items"],
            "extra_food_value": added,
            "discount_gain": round(discount_gain, 2),
            "extra_cash": extra_cash,
            "saving_inferred": n.get("saving_inferred", False),
            # A coupon is applied but its value is unstated, so the swap cannot
            # be costed. Never call that "cheaper" - that is the claim that
            # talked a user into replacing a working discount.
            "baseline_uncertain": unknown_discount,
            "verdict": (
                f"unknown: a coupon ({coupon_result.get('already_applied')}) is "
                "already applied and its value is not stated, so this may cost "
                "more, not less" if unknown_discount else
                "cheaper overall AND more food" if extra_cash < 0 else
                f"₹{extra_cash:.0f} more for ₹{added:.0f} of extra food"
            ),
            "add_command": "food restaurant add --restaurant %s %s" % (
                restaurant_id,
                " ".join(f"--item {i['item_id']}:{i['quantity']}" for i in plan["items"]),
            ),
        }
        if best is None or row["extra_cash"] < best["extra_cash"]:
            best = row
    return best


@food_app.command("best-offer")
def food_best_offer(
    restaurant_id: str = typer.Option(..., "--restaurant"),
    address: str = typer.Option(None, "--address"),
    apply: bool = typer.Option(True, "--apply/--dry-run", help="Apply the winner (default) or just rank."),
    probe: int = typer.Option(
        4, "--probe",
        help="How many text-ineligible coupons to also try (they are often wrong). 0 = trust the text.",
    ),
):
    """Score every available coupon against the cart and apply the best one."""
    addr = resolve_address(address)
    res = apply_best_coupon(restaurant_id, addr, probe=probe, apply=apply)
    if res.get("status") in ("applied", "no_beneficial_coupon"):
        try:
            res["topup_upside"] = topup_upside(res, restaurant_id, addr)
        except Exception:  # noqa: BLE001
            res["topup_upside"] = None
    out(res)
    if res.get("status") == "cart_total_unknown":
        raise typer.Exit(1)


@food_app.command("maximize")
def food_maximize(
    restaurant_id: str = typer.Option(None, "--restaurant", help="Defaults to the cart's."),
    address: str = typer.Option(None, "--address"),
    veg: bool = typer.Option(None, "--veg/--no-veg"),
    apply: bool = typer.Option(
        False, "--apply",
        help="Actually add the items. Without this it only reports what it would do.",
    ),
    free_only: bool = typer.Option(
        True, "--free-only/--any-gain",
        help="Only top up when it costs nothing overall. --any-gain also accepts "
             "spending a little more to get proportionally more food.",
    ),
):
    """Get the best price: apply the best coupon, then top up if that beats it.

    Adding items changes what the user is buying, so this reports by default
    and only acts with --apply. `--free-only` (the default) restricts it to
    top-ups that leave the bill the same or lower, which need no judgement
    call - you end up with more food for no extra money.
    """
    addr = resolve_address(address)
    rid = restaurant_id or store.get_pref("last_restaurant_id")
    if not rid:
        err("No restaurant known. Pass --restaurant <id>.")
        raise typer.Exit(2)

    before = apply_best_coupon(rid, addr)
    baseline = (before.get("best") or {}).get("new_total") or before.get("cart_total")

    diet = store.all_preferences().get("diet") or {}
    veg_only = veg if veg is not None else (
        diet.get("source") == "explicit"
        and diet.get("value") in ("vegetarian", "mostly_vegetarian"))

    up = topup_upside(before, rid, addr, veg_only=veg_only)
    result = {
        "current": {
            "coupon": (before.get("best") or {}).get("code"),
            "payable": baseline,
            "saved": (before.get("best") or {}).get("saving"),
        },
        "opportunity": up,
    }

    if not up:
        result["status"] = "already_optimal"
        result["note"] = "No coupon threshold is close enough to be worth topping up for."
        out(result)
        return

    worth_it = up["extra_cash"] <= 0 if free_only else up["extra_cash"] < up["extra_food_value"]
    result["worth_it"] = worth_it
    if not worth_it:
        result["status"] = "not_worth_it"
        result["note"] = (
            f"{up['coupon']} would need ₹{up['extra_food_value']:.0f} of extra food "
            f"for ₹{up['extra_cash']:.0f} more. Use --any-gain to accept that."
        )
        out(result)
        return

    if not apply:
        result["status"] = "would_improve"
        result["note"] = "Re-run with --apply to add the items and re-price."
        out(result)
        return

    # Add the items, then re-probe: the better coupon only becomes eligible
    # once the cart actually crosses the threshold.
    cur = _cart_items(text_of(call("food", "get_food_cart", {"addressId": addr})))
    cart_items = _rebuild(cur, rid)
    for i in up["add_items"]:
        cart_items.append({"menu_item_id": i["item_id"], "quantity": i["quantity"]})
    call("food", "flush_food_cart", {})
    call("food", "update_food_cart",
         {"restaurantId": rid, "cartItems": cart_items, "addressId": addr})

    after = apply_best_coupon(rid, addr)
    new_payable = (after.get("best") or {}).get("new_total") or after.get("cart_total")
    result.update(
        status="applied",
        after={
            "coupon": (after.get("best") or {}).get("code"),
            "payable": new_payable,
            "saved": (after.get("best") or {}).get("saving"),
        },
        net_change=round((new_payable or 0) - (baseline or 0), 2),
    )
    out(result)


@food_app.command("topup")
def food_topup(
    restaurant_id: str = typer.Option(None, "--restaurant", help="Defaults to the cart's restaurant."),
    address: str = typer.Option(None, "--address"),
    veg: bool = typer.Option(None, "--veg/--no-veg", help="Restrict filler to vegetarian."),
    max_items: int = typer.Option(4, "--max-items", help="Most add-on units to suggest."),
    apply: bool = typer.Option(False, "--apply", help="Actually add the suggested items to the cart."),
):
    """Suggest the cheapest add-ons that unlock the best available coupon.

    Solves 'what is the least I can add to cross the threshold' against the live
    menu, preferring sensible filler (a drink, a side) over a pile of sachets.
    """
    addr = resolve_address(address)
    rid = restaurant_id or store.get_pref("last_restaurant_id")
    if not rid:
        err("No restaurant known. Pass --restaurant <id>.")
        raise typer.Exit(2)

    coupon_res = call("food", "fetch_food_coupons", {"restaurantId": rid, "addressId": addr})
    parsed = offers.parse_coupons(text_of(coupon_res))
    near = [n for n in offers.near_misses(parsed) if n.get("would_save")]
    if not near:
        out({"status": "no_actionable_threshold",
             "note": "No coupon states both a shortfall and a saving."})
        return

    # Diet: explicit flag, else an explicitly stated preference; a learned diet
    # only informs (see the inference-never-filters rule).
    diet_pref = store.all_preferences().get("diet") or {}
    veg_only = veg if veg is not None else (
        diet_pref.get("source") == "explicit"
        and diet_pref.get("value") in ("vegetarian", "mostly_vegetarian")
    )

    menu = list(_menu_items_for(rid, addr).values())
    options = []
    for n in sorted(near, key=lambda x: x["spend_more"]):
        p = topup.plan(menu, n["spend_more"], veg_only=veg_only, max_total_items=max_items)
        if not p.get("found"):
            options.append({"coupon": n["code"], "shortfall": n["spend_more"],
                            "would_save": n["would_save"], "feasible": False,
                            "reason": p.get("reason")})
            continue
        net = round(n["would_save"] - p["added_cost"], 2)
        options.append({
            "coupon": n["code"],
            "shortfall": n["spend_more"],
            "would_save": n["would_save"],
            "saving_inferred": n.get("saving_inferred", False),
            "feasible": True,
            "add_items": p["items"],
            "added_cost": p["added_cost"],
            "overshoot": p["overshoot"],
            # Negative net still buys you food - it is not a loss, just a spend.
            "net_vs_saving": net,
            "worth_it": net >= 0,
            "add_command": "food restaurant add --restaurant %s %s" % (
                rid, " ".join(f"--item {i['item_id']}:{i['quantity']}" for i in p["items"])
            ),
        })

    feasible = [o for o in options if o.get("feasible")]
    best = max(feasible, key=lambda o: o["net_vs_saving"]) if feasible else None

    if best and apply:
        items = [f"{i['item_id']}:{i['quantity']}" for i in best["add_items"]]
        cur = _cart_items(text_of(call("food", "get_food_cart", {"addressId": addr})))
        cart_items = [{"menu_item_id": i["menu_item_id"], "quantity": i["quantity"]} for i in cur]
        for spec in items:
            mid, _, q = spec.partition(":")
            cart_items.append({"menu_item_id": mid, "quantity": int(q)})
        call("food", "flush_food_cart", {})
        applied = call("food", "update_food_cart", {
            "restaurantId": rid, "cartItems": cart_items, "addressId": addr,
        })
        best["applied"] = text_of(applied)[:400]

    out({"veg_only": veg_only, "best": best, "options": options})


@food_app.command("apply-coupon")
def food_apply_coupon(
    code: str = typer.Argument(...),
    address: str = typer.Option(None, "--address"),
    restaurant: str = typer.Option(None, "--restaurant"),
):
    """Apply a coupon while preserving the current item/quantity snapshot."""
    addr = resolve_address(address)
    rid = restaurant or store.get_pref("last_restaurant_id")
    if not rid:
        err("No restaurant known. Pass --restaurant <id> so cart recovery is safe.")
        raise typer.Exit(2)
    cart_res = call("food", "get_food_cart", {"addressId": addr})
    intended = _intended_cart_snapshot(cart_res, rid)
    if not intended["signature"]:
        err("Could not identify the live cart items; refusing to mutate its coupon.")
        raise typer.Exit(5)
    res, live_cart, integrity = _apply_coupon_preserving_cart(code, addr, rid, intended)
    out({**res, "live_cart": live_cart, "cart_integrity": integrity})


@food_app.command("payment-options")
def food_payment_options(address: str = typer.Option(None, "--address")):
    """List the user's available payment methods."""
    res = call("food", "get_payment_options", {"addressId": resolve_address(address)})
    out({
        **res,
        "generic_upi_qr": generic_upi_qr(res),
        "upi_apps": intent_app_choices(res),
        "preferred_upi_app": saved_intent_app(),
        "note": (
            "Use generic UPI QR without asking for an app when generic_upi_qr "
            "is present. Otherwise ask for an enabled app when no preference is saved."
        ),
    })


@food_app.command("place")
def food_place(
    address: str = typer.Option(None, "--address"),
    payment: str = typer.Option(
        None, "--payment",
        help='Required. "UPI" or "Cash". Ask the user - never assume, and never '
             'default to Cash on delivery.',
    ),
    intent_app: str = typer.Option(
        None, "--intent-app",
        help="The UPI app the user chose, by provider id or exact display name. "
             "A valid explicit choice is saved for later orders.",
    ),
    note: str = typer.Option(None, "--note", help="Note to the restaurant."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Required. Confirms you intend to spend money."),
    max_total: float = typer.Option(
        None, "--max-total",
        help="Required. Most rupees the user approved after seeing the final cart. "
             "Placement stops if the live or provider-returned total is higher.",
    ),
    no_open: bool = typer.Option(
        False, "--no-open",
        help="Do not auto-open the QR image. Set FOOD_CLI_NO_OPEN=1 to make "
             "this the default on headless or agent-driven machines.",
    ),
    restaurant: str = typer.Option(
        None, "--restaurant",
        help="Restaurant id. Required unless the current cart already recorded one; "
             "used for live availability and offer checks.",
    ),
    ignore_card_offers: bool = typer.Option(
        False, "--ignore-card-offers",
        help="Proceed even though card-only offers exist that UPI/Cash cannot claim.",
    ),
    ignore_near_misses: bool = typer.Option(
        False, "--ignore-near-misses",
        help="Proceed even though a small top-up would unlock a coupon.",
    ),
    wait: bool = typer.Option(
        False, "--wait",
        help="After placing, watch the payment through to confirmation. A CLI "
             "has no payment widget, so nothing else will.",
    ),
    wait_timeout: int = typer.Option(300, "--wait-timeout"),
    auto_coupon: bool = typer.Option(
        True, "--auto-coupon/--no-auto-coupon",
        help="Deprecated compatibility flag. Coupons are selected during cart "
             "preparation; placement never mutates the approved cart.",
    ),
):
    """Place the food order with payment, confirmation and an approved price ceiling."""
    if not payment:
        err(PAYMENT_REQUIRED_HINT.format(group="food"))
        raise typer.Exit(2)
    if not yes:
        err(
            "Refusing to place an order without --yes/-y.\n"
            "Show the user the cart and total first, get an explicit confirmation, "
            "then re-run with --yes."
        )
        raise typer.Exit(2)
    if max_total is None or not math.isfinite(max_total) or max_total <= 0:
        err(
            "Refusing to place an order without a positive --max-total.\n"
            "Read the final cart total to the user, get explicit confirmation, then "
            "pass that exact amount (for example, --max-total 135). This binds -y to "
            "the price the user actually approved."
        )
        raise typer.Exit(2)

    rid_for_offers = restaurant or store.get_pref("last_restaurant_id")
    if not rid_for_offers:
        err(
            "Refusing to place this order without a restaurant id; its live "
            "delivery status cannot be verified. Pass --restaurant <id>."
        )
        out({
            "status": "blocked_unverified_restaurant_status",
            "restaurant_availability": {
                "restaurant_id": None,
                "status": "UNKNOWN",
                "verified": False,
                "source_tool": "search_restaurants",
            },
        })
        raise typer.Exit(5)
    addr = resolve_address(address)

    # Coupon selection belongs to cart preparation, before the CLI presents
    # the bill for approval. It must never run after --max-total was chosen.
    # Keep the old flag accepted for scripts, but intentionally inert.
    coupon_result = {"status": "not_mutated_at_placement"}
    if False:  # pragma: no cover - retained below only until legacy helpers move
        try:
            # A coupon already on the cart has been verified against the real
            # bill. Re-probing it at placement risks stripping it for nothing -
            # probing replaces whatever is applied - so keep it and only rank.
            addr_now = addr
            existing = offers.applied_coupon(
                text_of(call("food", "get_food_cart", {"addressId": addr_now})))
            if existing:
                err(f"\n\U0001f3f7️  Keeping the coupon already applied "
                    f"({existing}); not re-probing it at placement.\n")
                coupon_result = apply_best_coupon(rid_for_offers, addr_now, apply=False)
                coupon_result["status"] = "kept_existing"
                coupon_result["already_applied"] = existing
            else:
                coupon_result = apply_best_coupon(rid_for_offers, addr_now)
            if coupon_result.get("status") == "applied":
                b = coupon_result["best"]
                err(f"\n\U0001f3f7\uFE0F  Applied {b['code']}: pay \u20b9{b['new_total']:.0f} "
                    f"(saved \u20b9{b['saving']:.0f})\n")
            elif coupon_result.get("status") == "no_beneficial_coupon":
                err("\n(no coupon lowered the bill - often because the items are "
                    "already discounted)\n")

            if coupon_result.get("status") in {
                "cart_restored_after_coupon_revert", "cart_restore_failed",
            }:
                restored = coupon_result.get("status") == "cart_restored_after_coupon_revert"
                err(
                    "\n⛔ Swiggy changed the cart while applying a coupon. "
                    + ("The intended items were restored, but the checkout preview "
                       "must be reviewed again.\n" if restored else
                       "The intended items could not be verified after recovery.\n")
                )
                out({
                    "status": ("blocked_cart_recovered" if restored
                               else "blocked_cart_restore_failed"),
                    "coupon": coupon_result,
                    "action": "Run `food restaurant cart`, show the new final total, and ask again.",
                })
                raise typer.Exit(5)

            up = topup_upside(coupon_result, rid_for_offers, addr)
            if up:
                items = ", ".join(f"{i['quantity']}x {i['name']}" for i in up["add_items"])
                err(
                    f"\U0001f4a1 Better deal available: {up['coupon']} - add {items} "
                    f"(\u20b9{up['extra_food_value']:.0f}) and it is {up['verdict']}.\n"
                    f"   {up['add_command']}\n"
                )
                coupon_result["topup_upside"] = up
        except typer.Exit:
            raise
        except Exception as e:  # noqa: BLE001
            # A coupon failure must never block an order the user confirmed.
            err(f"(auto-coupon skipped: {e})")

    # Card-linked offers are unreachable via UPI/Cash. Warn before spending, so
    # the user can choose to pay by card in the Swiggy app instead and save more.
    pending_card = store.get_pref(f"card_offers:{rid_for_offers}") or [] if rid_for_offers else []
    if pending_card and not ignore_card_offers:
        lines = "\n".join(
            f"    - {c['code']}: {c['text']}" for c in pending_card[:6]
        )
        err(
            "\n\u26a0\ufe0f  CARD OFFERS AVAILABLE - you may be able to save more.\n"
            f"{lines}\n\n"
            "These need a specific bank card, which this CLI cannot use "
            "(UPI and Cash only here). To claim one, pay by card in the Swiggy app "
            "instead.\n"
            "To order anyway on UPI/Cash, re-run with --ignore-card-offers.\n"
        )
        out({
            "status": "blocked_card_offers",
            "card_offers": pending_card,
            "override": "--ignore-card-offers",
        })
        raise typer.Exit(3)

    # Near misses: a small top-up unlocks a real discount. Only gate on the ones
    # whose saving is actually known - "spend Rs 770 more" with no stated saving
    # is noise, not advice.
    near = store.get_pref(f"near_misses:{rid_for_offers}") or [] if rid_for_offers else []
    # Only gate on a top-up that is actually WORTH it. "Spend Rs 499 more to
    # save Rs 150" on a Rs 199 cart is a bad deal; blocking on it would just
    # train the user to always pass the override.
    #
    # And net it off whatever the cart is ALREADY saving. A coupon promising
    # "save Rs 90" against a cart that already has Rs 85 off is worth Rs 5, not
    # Rs 90 - blocking an order over that, and telling the user to add food to
    # get it, is worse than saying nothing. If a coupon is applied but its value
    # is unstated, the comparison cannot be made at all, so do not gate on it.
    applied_saving = 0.0
    baseline_unknown = False
    if coupon_result:
        applied_saving = ((coupon_result.get("best") or {}).get("saving")
                          or coupon_result.get("already_saving") or 0.0)
        baseline_unknown = (
            not applied_saving and bool(coupon_result.get("already_applied"))
        )

    def _net(n: dict) -> float:
        return (n.get("would_save") or 0.0) - applied_saving

    actionable = [] if baseline_unknown else [
        n for n in near
        if n.get("would_save") and _net(n) > 0
        and n["spend_more"] <= _net(n) * NEAR_MISS_MAX_RATIO
    ]
    advisory = [n for n in near if n.get("would_save") and n not in actionable]
    actionable.sort(key=lambda n: n["spend_more"])
    if actionable and not ignore_near_misses:
        best_n = actionable[0]
        # Say WHAT to add, not just that they are short.
        suggestion = ""
        try:
            menu = list(_menu_items_for(rid_for_offers, addr).values())
            dp = store.all_preferences().get("diet") or {}
            vo = (dp.get("source") == "explicit"
                  and dp.get("value") in ("vegetarian", "mostly_vegetarian"))
            plan = topup.plan(menu, best_n["spend_more"], veg_only=vo)
            if plan.get("found"):
                adds = ", ".join(
                    f"{i['quantity']}x {i['name']} (\u20b9{i['line_total']:.0f})"
                    for i in plan["items"]
                )
                suggestion = (
                    f"\n    Cheapest way there: add {adds} "
                    f"= \u20b9{plan['added_cost']:.0f}.\n"
                    f"    Run: food restaurant topup --restaurant {rid_for_offers} --apply\n"
                )
        except (KeyError, ValueError, RuntimeError, TypeError) as e:
            # Never block the gate itself on a suggestion failure, but do not
            # swallow programming errors silently either.
            err(f"(could not compute a top-up suggestion: {e})")

        lines = "\n".join(
            f"    - {n['code']}: spend \u20b9{n['spend_more']:.0f} more \u2192 save "
            f"\u20b9{n['would_save']:.0f}"
            + ("  (saving inferred from the code name)" if n.get("saving_inferred") else "")
            for n in actionable[:5]
        )
        err(
            "\n\U0001f4a1 ALMOST AT A DISCOUNT - adding a little more unlocks a coupon:\n"
            f"{lines}\n"
            f"{suggestion}\n"
            f"    Closest: add \u20b9{best_n['spend_more']:.0f} of food to save "
            f"\u20b9{best_n['would_save']:.0f}.\n"
            "Add items and re-run `food restaurant best-offer`, or order as-is with "
            "--ignore-near-misses.\n"
        )
        out({
            "status": "blocked_near_misses",
            "near_misses": actionable,
            "closest": best_n,
            "not_worth_it": advisory,
            "override": "--ignore-near-misses",
        })
        raise typer.Exit(4)
    if advisory:
        # Mention, but never block - these need more spend than they return.
        best_a = min(advisory, key=lambda n: n["spend_more"])
        err(
            f"(FYI: {best_a['code']} would save \u20b9{best_a['would_save']:.0f} but needs "
            f"\u20b9{best_a['spend_more']:.0f} more spend - not worth it. Ordering anyway.)"
        )

    args: dict = {"addressId": addr}
    intent_choice: dict | None = None
    if payment:
        args["paymentMethod"] = payment
    if (payment or "").upper() == "UPI":
        # Generic UPI is valid only when the live provider response advertises
        # desktop PayWithQR. An explicit app wins; without generic QR, a saved
        # or newly selected enabled app is required.
        route, why = choose_food_upi_route(args["addressId"], intent_app)
        chosen = (route or {}).get("intentApp")
        intent_choice = {"requested": intent_app, "used": chosen, **why}
        if not route:
            err(
                f"\nRefusing to place this order: {why['reason']}.\n"
                "Show the available UPI apps to the user, ask which one they "
                "prefer, then pass it with --intent-app.\n"
                "    food restaurant payment-options\n"
            )
            status = (
                "blocked_upi_app_choice" if why.get("requires_choice")
                else "blocked_no_payable_upi"
            )
            out({"status": status, "intent_app_choice": intent_choice})
            raise typer.Exit(3)
        args.update(route)
        if why.get("mode") == "generic_qr":
            err("Using provider-supported generic UPI QR; no app choice is required.")
        else:
            err(f"Using {why['selected']['name']} for UPI - {why['reason']}.")
    elif intent_app:
        err("--intent-app is only valid together with --payment UPI.")
        raise typer.Exit(2)
    if note:
        args["noteToRestaurant"] = note

    availability = live_restaurant_availability(rid_for_offers, addr)
    if availability["status"] != "OPEN":
        status = availability["status"]
        if status in {"CLOSED", "UNAVAILABLE"}:
            err(
                f"Refusing to place this order: the restaurant is {status.lower()} "
                "for delivery at the selected address."
            )
            blocked_status = "blocked_restaurant_not_open"
        else:
            err(
                "Refusing to place this order: the restaurant's live delivery "
                "status could not be verified as OPEN."
            )
            blocked_status = "blocked_unverified_restaurant_status"
        out({
            "status": blocked_status,
            "restaurant_availability": availability,
            "action": "Search again and choose a restaurant whose availability_status is OPEN.",
        })
        raise typer.Exit(5)

    # Bind the user's `-y` to the amount they actually heard. Swiggy can
    # recompute delivery charges after an earlier cart preview; without this
    # check, consent to (say) Rs 135 silently becomes consent to Rs 204.
    #
    # This first check catches a cart that changed before placement and avoids
    # creating an order at all. A second check below catches a fee introduced
    # atomically by place_food_order itself.
    live_cart = call("food", "get_food_cart", {"addressId": args["addressId"]})
    live_blob = text_of(live_cart)
    live_breakdown = parse_bill_breakdown(live_cart)
    live_total = live_breakdown["payable_total"]
    live_delivery_fee = live_breakdown["delivery_fee"]
    cart_availability = cart_orderability(live_cart)
    restaurant_context = cart_restaurant_context(live_cart, rid_for_offers)
    if restaurant_context["conflict"]:
        err(
            "Refusing to place this order: the live cart items do not match "
            "the remembered cart for the selected restaurant."
        )
        out({
            "status": "blocked_cart_restaurant_mismatch",
            "restaurant_availability": availability,
            "restaurant_context": restaurant_context,
            "stage": "preflight",
            "action": (
                "Re-add the intended items with --restaurant, review the new cart, "
                "and obtain fresh approval."
            ),
        })
        raise typer.Exit(5)
    if cart_availability["orderable"] is False:
        err(
            "Refusing to place this order: the authoritative cart says the "
            f"restaurant or an item is unavailable ({cart_availability['reason']})."
        )
        out({
            "status": "blocked_cart_unavailable",
            "restaurant_availability": availability,
            "cart_orderability": cart_availability,
            "stage": "preflight",
            "action": "Choose an available restaurant/item and review the new cart.",
        })
        raise typer.Exit(5)
    if live_total is None:
        err(
            "Refusing to place this order: the live cart total could not be verified.\n"
            "Re-read the cart and try again; do not guess or increase --max-total."
        )
        out({
            "status": "blocked_unverified_total",
            "approved_max_total": max_total,
            "delivery_fee": live_delivery_fee,
            "bill_breakdown": live_breakdown,
            "stage": "preflight",
        })
        raise typer.Exit(5)
    if live_total > max_total + 0.009:
        err(
            f"Refusing to place this order: the live total is ₹{live_total:.2f}, "
            f"above the user-approved ₹{max_total:.2f}.\n"
            "Show the changed cart and get a new explicit confirmation."
        )
        out({
            "status": "blocked_total_changed",
            "approved_max_total": max_total,
            "live_total": live_total,
            "delivery_fee": live_delivery_fee,
            "bill_breakdown": live_breakdown,
            "increase": round(live_total - max_total, 2),
            "stage": "preflight",
        })
        raise typer.Exit(5)
    if not live_breakdown["complete"]:
        err(
            "Refusing to place this order: get_food_cart did not return a complete, "
            "internally consistent bill breakdown.\n"
            "Do not infer missing fees. Re-read the cart before asking for approval."
        )
        out({
            "status": "blocked_incomplete_bill_breakdown",
            "approved_max_total": max_total,
            "live_total": live_total,
            "bill_breakdown": live_breakdown,
            "stage": "preflight",
        })
        raise typer.Exit(5)
    if str(args.get("paymentMethod") or "").upper() == "UPI":
        payment_quote = (intent_choice or {}).get("payment_amount")
        if payment_quote is not None and round(payment_quote, 2) != round(live_total, 2):
            err(
                "Refusing to place this UPI order: the live payment picker does "
                "not agree with the approved cart total."
            )
            out({
                "status": "blocked_payment_quote_mismatch",
                "approved_max_total": max_total,
                "preflight_total": live_total,
                "payment_option_amount": payment_quote,
                "intent_app_choice": intent_choice,
                "stage": "preflight",
                "action": "Refresh the cart and payment options, then obtain fresh approval.",
            })
            raise typer.Exit(5)

    delivery_label = (
        "FREE" if live_delivery_fee == 0
        else f"₹{live_delivery_fee:.2f}" if live_delivery_fee is not None
        else "not itemised (payable total is verified)"
    )
    err(
        f"Preflight verified: delivery {delivery_label}; "
        f"payable ₹{live_total:.2f} within approved ₹{max_total:.2f}.\n"
    )

    approved_items = _cart_named_signature(live_cart)
    res = call("food", "place_food_order", args)
    blob = response_blob(res)
    placement_total = extract_payable(blob)
    placed_total = placement_total
    effective_res = res
    reconciliation: dict = {
        "attempted": False,
        "succeeded": False,
        "placement_total": placement_total,
    }
    order_id = qrmod.extract_order_id(res) or order_id_in(blob)
    if order_id:
        # Always reconcile against order details. The placement response can
        # contain a current totalAmount beside stale server-side order/payment
        # state; the details endpoint caught a real ₹124-vs-₹186 drift that the
        # placement total alone could not reveal.
        reconciliation["attempted"] = True
        try:
            details = call("food", "get_food_order_details", {"orderId": order_id})
            detail_error = details.get("upstream_error")
            if detail_error or details.get("isError"):
                reconciliation["error"] = str(detail_error or text_of(details))[:160]
            else:
                detail_blob = response_blob(details)
                detail_total = extract_payable(detail_blob)
                order_context = _order_details_context(
                    details, rid_for_offers, approved_items,
                )
                reconciliation.update({
                    "succeeded": detail_total is not None,
                    "details_total": detail_total,
                    "totals_match": (
                        round(detail_total, 2) == round(placement_total, 2)
                        if detail_total is not None and placement_total is not None
                        else None
                    ),
                    "order_context": order_context,
                })
                if detail_total is not None:
                    placed_total = detail_total
                    blob = f"{blob}\n{detail_blob}"
                    effective_res = {
                        **res,
                        "content": [res.get("content"), details.get("content")],
                        "order_details_reconciliation": details,
                    }
        except Exception as e:  # noqa: BLE001 - retain the safe unknown-total gate
            reconciliation["error"] = str(e)[:160]

    order_context = reconciliation.get("order_context") or {}
    if (order_context.get("restaurant_matches") is False
            or order_context.get("items_match") is False):
        err(
            "The provider-created order does not match the approved restaurant/items. "
            "No payment artifact will be exposed."
        )
        out({
            "status": "blocked_created_order_mismatch",
            "provider_order_id": order_id,
            "approved_max_total": max_total,
            "preflight_total": live_total,
            "order_context": order_context,
            "stage": "post_placement",
            "payment_suppressed": True,
            "provider_response_suppressed": True,
            "action": "Do not pay; let the pending attempt expire and rebuild the cart.",
        })
        raise typer.Exit(5)

    placed_delivery_fee = structured_delivery_fee(res)
    delivery_verified = (
        live_delivery_fee is not None
        and placed_delivery_fee is not None
    )
    delivery_changed = (
        abs(live_delivery_fee - placed_delivery_fee) > 0.009
        if delivery_verified else None
    )
    delivery_guard = {
        "preflight": live_delivery_fee,
        "placed": placed_delivery_fee,
        "verified": delivery_verified,
        "changed": delivery_changed,
        "explanation": (
            "Post-placement delivery fee was not returned; the cause of any "
            "total change is unknown."
            if not delivery_verified else None
        ),
    }
    if placed_total is None or placed_total > max_total + 0.009:
        # UPI is still pending at this point. Deliberately do not resolve,
        # render or open the payment artefact, and do not wait/confirm: paying
        # it would accept a price the user never approved.
        status = "blocked_unverified_placed_total" if placed_total is None else "blocked_total_changed"
        if placed_total is None:
            reason = "Swiggy did not return a verifiable placement total"
        else:
            reason = (
                f"Swiggy changed the total from the approved ₹{max_total:.2f} "
                f"to ₹{placed_total:.2f} during placement"
            )
            if delivery_changed is True:
                before = "FREE" if live_delivery_fee == 0 else f"₹{live_delivery_fee:.2f}"
                after = "FREE" if placed_delivery_fee == 0 else f"₹{placed_delivery_fee:.2f}"
                reason += f"; delivery changed from {before} to {after}"
            elif not delivery_verified:
                reason += (
                    "; Swiggy did not return a post-placement delivery-fee "
                    "breakdown, so the cause is unknown"
                )
        err(
            f"\n⛔ {reason}. DO NOT PAY this payment attempt.\n"
            "No QR or payment link was opened. Let the pending attempt expire, "
            "then re-read the cart and ask the user before trying again.\n"
        )
        provider_status = next(
            (s for s in ("PENDING_PAYMENT", "CONFIRMED", "PLACED", "FAILED") if s in blob),
            "UNKNOWN",
        )
        out({
            "status": status,
            "provider_order_id": order_id,
            "provider_status": provider_status,
            "approved_max_total": max_total,
            "preflight_total": live_total,
            "placed_total": placed_total,
            "preflight_bill_breakdown": live_breakdown,
            "delivery_fee": delivery_guard,
            "order_details_reconciliation": reconciliation,
            "increase": (
                round(placed_total - max_total, 2) if placed_total is not None else None
            ),
            "stage": "post_placement",
            "payment_suppressed": True,
            "provider_response_suppressed": True,
            "action": "Do not pay; let the pending attempt expire and get fresh consent.",
        })
        raise typer.Exit(5)

    # The UPI intent amount is what the user's bank will authorise. Swiggy can
    # return a current order total beside a stale payment intent from another
    # cart, so validate it before exposing or persisting any payment artifact.
    link = payment_link_in(effective_res)
    found = qrmod.find_qr(effective_res)
    is_upi = str(args.get("paymentMethod") or "").upper() == "UPI"
    payment_guard = payment_artifact_guard(found, placed_total) if is_upi else None
    if payment_guard and not payment_guard["safe_to_present"]:
        status = (
            "blocked_payment_amount_mismatch"
            if payment_guard.get("amount") is not None
            and payment_guard.get("expected_total") is not None
            else "blocked_unverified_payment_amount"
        )
        err(
            f"\n⛔ {payment_guard['reason']}. DO NOT PAY this attempt.\n"
            "The QR and payment link were suppressed. Let this pending attempt "
            "expire before reviewing a fresh cart.\n"
        )
        out({
            "status": status,
            "provider_order_id": order_id,
            "provider_status": (
                "PENDING_PAYMENT" if "PENDING_PAYMENT" in blob else "UNKNOWN"
            ),
            "approved_max_total": max_total,
            "preflight_total": live_total,
            "placed_total": placed_total,
            "payment_artifact": payment_guard,
            "stage": "post_placement",
            "payment_suppressed": True,
            "provider_response_suppressed": True,
            "action": "Do not pay; let the pending attempt expire and review a fresh cart.",
        })
        raise typer.Exit(5)

    _log_order("food", effective_res, args.get("addressId"))
    payload = {**effective_res}
    payload["price_guard"] = {
        "approved_max_total": max_total,
        "preflight_total": live_total,
        "placed_total": placed_total,
        "preflight_bill_breakdown": live_breakdown,
        "delivery_fee": delivery_guard,
        "order_details_reconciliation": reconciliation,
        "payment_artifact": payment_guard,
        "verified": True,
    }
    payload["restaurant_availability"] = availability
    payload["restaurant_context"] = restaurant_context
    if intent_choice:
        payload["intent_app_choice"] = intent_choice
    if coupon_result:
        payload["coupon"] = {
            "status": coupon_result.get("status"),
            "applied": (coupon_result.get("best") or {}).get("code"),
            "saved": (coupon_result.get("best") or {}).get("saving"),
            "new_total": (coupon_result.get("best") or {}).get("new_total"),
            "topup_upside": coupon_result.get("topup_upside"),
        }
    if link:
        payload["payment_link"] = link
    oid = qrmod.extract_order_id(effective_res) or "food-order"
    if found:
        payload["qr"] = qrmod.present(found, order_ref=oid,
                                      open_browser=not _no_open_default(no_open))
        _remember_pending_payment(
            "food", oid, found,
            {
                **payload["qr"],
                "payment_link": link,
                "expected_amount": placed_total,
                "amount_verified": True,
            },
        )

    # Always present, even when empty: a caller should be able to read
    # payment.qr_png and payment.payment_link without first working out which
    # shape the response took.
    payload["payment"] = _payment_block(effective_res, payload.get("qr"), link, oid)

    if not found and "PENDING_PAYMENT" in blob:
        # Swiggy renders the QR in its own widget and, for food orders, returns
        # no transferable payload at all - verified against place_food_order and
        # check_payment_status. There is nothing to attach, and nothing to
        # reconstruct without inventing payment data. Say so, and give the
        # routes that do work.
        payload["payment_handoff"] = {
            "qr_available": False,
            "reason": "Swiggy returned no UPI intent or QR image for this order.",
            "options": [
                "Open the order in the Swiggy app and pay there - the order is live.",
                "Retry with --payment UPI --intent-app 'gpay://upi/' (or phonepe://) "
                "which may return an app deeplink instead of a widget-only QR.",
                "Where the restaurant supports it, --payment Cash needs no online step.",
            ],
        }
        err(
            "\n>>> No QR or UPI intent came back - Swiggy renders it only in its own\n"
            "    widget, so there is nothing to attach. The order is PENDING and NOT\n"
            "    placed. Pay it in the Swiggy app, or retry with --intent-app, or use\n"
            "    --payment Cash. Do not invent payment details.\n"
        )
    else:
        err(
            "\n>>> If payment is pending, the USER must complete it in their UPI app. "
            "Do not attempt to enter any payment credential.\n"
        )
    if wait:
        _wait_after_order("food", effective_res, payload, args["addressId"], wait_timeout)
    out(payload)


@food_app.command("suggest")
def food_suggest(
    budget: float = typer.Option(None, "--budget", help="Total rupees to stay within."),
    people: int = typer.Option(1, "--people", help="How many are eating."),
    address: str = typer.Option(None, "--address"),
    veg: bool = typer.Option(
        None, "--veg/--no-veg",
        help="Filter to vegetarian. Unset means no filtering - a LEARNED diet is "
             "only reported, never enforced.",
    ),
    max_eta: int = typer.Option(None, "--max-eta"),
    images: bool = typer.Option(False, "--images"),
    download: bool = typer.Option(False, "--download"),
    limit: int = typer.Option(8, "--limit"),
):
    """Suggest what to order, based on past orders and a budget.

    Searches live, so everything returned is currently orderable — unlike a
    plain reorder, which can offer a dish the kitchen has stopped serving.
    """
    addr = resolve_address(address)
    prefs = store.all_preferences()
    if not prefs:
        prefs = {}
        profile.learn()
        prefs = store.all_preferences()

    diet_pref = prefs.get("diet") or {}
    diet = diet_pref.get("value")
    diet_is_stated = diet_pref.get("source") == "explicit"

    # A LEARNED diet is an inference and must never silently remove options -
    # the user may be ordering for someone else, or the guess may just be wrong.
    # Filter only when the user asked (--veg) or explicitly stated their diet.
    if veg is not None:
        veg_only = veg
    elif diet_is_stated:
        veg_only = diet in ("vegetarian", "mostly_vegetarian")
    else:
        veg_only = False
    favs = (prefs.get("favourite_dishes") or {}).get("value") or []
    cuisines = (prefs.get("favourite_cuisines") or {}).get("value") or []
    spend = (prefs.get("food_budget") or {}).get("value") or {}

    per_head = (budget / people) if budget else None
    queries = (favs[:4] + cuisines[:3]) or ["meals"]

    seen: dict[str, dict] = {}
    for q in queries:
        try:
            res = call("food", "search_menu", {
                "addressId": addr, "query": q,
                **({"vegFilter": True} if veg_only else {}),
            })
        except Exception:  # noqa: BLE001
            continue
        for d in parse_dishes(text_of(res)):
            # Swiggy's vegFilter is not always honoured, so re-check. Its own
            # per-item Veg/Non-veg classification is authoritative; the name
            # heuristic is only a fallback for items it did not classify.
            if veg_only and not topup.is_veg(d):
                continue
            serves = _serves_count(d["name"])
            # A "serves 2-3" platter is one item for the whole table, so it is
            # judged against the TOTAL budget, not the per-head share.
            d["serves"] = serves
            d["suggested_quantity"] = 1 if serves >= people else people
            d["line_total"] = round(d["price"] * d["suggested_quantity"], 2)

            if budget and d["line_total"] > budget:
                continue
            d["matched_query"] = q
            d["from_history"] = q in favs
            seen.setdefault(d["item_id"], d)

    ranked = sorted(
        seen.values(),
        key=lambda d: (not d["from_history"], -(d["rating"] or 0), d["line_total"]),
    )

    if max_eta is not None:
        etas: dict[str, dict] = {}
        keep = []
        for d in ranked[: limit * 3]:
            rid = d["restaurant_id"]
            if rid not in etas:
                etas[rid] = restaurant_eta(rid, addr, d["restaurant"])
            d["eta_minutes"] = etas[rid].get("eta_minutes")
            if d["eta_minutes"] is not None and d["eta_minutes"] <= max_eta:
                keep.append(d)
        ranked = keep

    ranked = ranked[:limit]
    if images or download:
        _attach_images(ranked, addr, do_download=download)

    out({
        "based_on": {
            "diet": diet,
            "diet_source": diet_pref.get("source"),
            "diet_confidence": diet_pref.get("confidence"),
            "veg_only_enforced": veg_only,
            "diet_note": (
                None if veg_only or not diet else
                f"Profile suggests '{diet}' (inferred, not stated) - results were NOT "
                "filtered. Ask the user, or pass --veg, if that matters."
            ),
            "favourite_dishes": favs[:4], "cuisines": cuisines[:3],
            "typical_spend": spend.get("typical_order"),
        },
        "budget": {"total": budget, "people": people, "per_head": per_head},
        "suggestions": ranked,
        "note": (
            "Live availability: every item here was returned by a search just now. "
            "Prices are per item; multiply by --people for a shared order."
        ),
    })
