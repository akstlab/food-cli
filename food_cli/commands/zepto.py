"""Zepto quick commerce.

Zepto's model differs from Swiggy's in three ways that shape this module:

* **Store context.** Nothing can be searched or carted until a store is
  selected, which `select_saved_address` does as a side effect. `_ensure_store`
  applies the saved default automatically so an assistant never has to know.
* **Cash on delivery exists.** Swiggy food is UPI-only, so an order always ends
  with a payment step a sighted user has to complete. Zepto's `create_order`
  takes COD, which makes a genuinely hands-free grocery order possible.
* **Payment method picks the tool**, not a parameter: COD, online link, wallet
  and UPI reserve pay are four separate tools. `--payment` is still required -
  the CLI never chooses one, least of all COD.

`update_cart` wants a `deviceId`. It is generated once and kept locally so the
cart stays the same cart between commands.
"""

from __future__ import annotations

import json
import re
import uuid

import typer

from ..core import store
from .checkout import _log_order, order_id_in
from .common import call, err, out, text_of

# Zepto returns saved addresses as prose, then repeats the ids in a trailing
# block: `1. "Home" → ID: 0f3abe1-dcde-...`. The ids are the only way to select
# one, so they are parsed out rather than left for the caller to scrape.
_ADDR_ID_LINE = re.compile(
    r'(?P<n>\d+)\.\s*"(?P<label>[^"]+)"\s*(?:→|->)\s*ID:\s*(?P<id>[0-9a-fA-F-]{8,})'
)
# The prose lines above that block, e.g. `2. home: 12 Some Street, ...`.
_ADDR_TEXT_LINE = re.compile(
    r"^\s*(?P<n>\d+)\.\s*(?P<label>[^:\n]{1,40}):\s*(?P<text>.+)$", re.M
)

zepto_app = typer.Typer(no_args_is_help=True, help="Zepto quick-commerce groceries.")

SERVER = "zepto"

#: Payment method -> the tool that places that kind of order.
PAYMENT_TOOLS = {
    "cod": "create_order",
    "online": "create_online_payment_order",
    "wallet": "create_wallet_order",
    "reserve": "create_upi_reserve_pay_order",
}


def device_id() -> str:
    """A stable id for this installation's cart.

    Zepto keys the cart by device. A fresh id on every command would strand the
    previous cart, so it is generated once and stored.
    """
    existing = store.get_pref("zepto_device_id")
    if existing:
        return existing
    new = str(uuid.uuid4())
    store.set_pref("zepto_device_id", new)
    return new


def parse_addresses(text: str) -> list[dict]:
    """Pair each saved address with its id.

    Returns `[{index, label, id, text}]`. `text` is the human-readable address
    and `id` is what the tools take.
    """
    by_index: dict[str, dict] = {}
    for m in _ADDR_TEXT_LINE.finditer(text or ""):
        # The trailing `1. "Home" → ID: <uuid>` lines also look like
        # `<n>. <label>: <text>`, so skip anything the id pattern claims.
        if _ADDR_ID_LINE.search(m.group(0)):
            continue
        by_index[m.group("n")] = {
            "index": int(m.group("n")),
            "label": m.group("label").strip(),
            "text": m.group("text").strip(),
            "id": None,
        }
    for m in _ADDR_ID_LINE.finditer(text or ""):
        row = by_index.setdefault(
            m.group("n"),
            {"index": int(m.group("n")), "label": m.group("label"), "text": None, "id": None},
        )
        row["id"] = m.group("id")
        row["label"] = row["label"] or m.group("label")
    return [by_index[k] for k in sorted(by_index, key=int)]


def store_marker(address_id: str) -> str:
    """What "the store is already selected" means.

    Store context belongs to the session behind a token, not to this machine, so
    it must be re-established after signing in again. Keying the marker on the
    token's issue time makes a re-auth invalidate it automatically - otherwise
    the CLI skips `select_saved_address` and every later call runs without a
    store.
    """
    from ..mcp import oauth

    return f"{address_id}:{oauth.token_info(SERVER).get('issued_at')}"


def _ensure_store(address_id: str | None = None) -> str | None:
    """Make sure a store is selected, since shopping tools require one.

    Selecting a saved address also sets the store, so that is the cheapest way
    to establish context. Without one, every search and cart call fails upstream
    with "Store not selected", so say so here instead of passing that on.
    """
    addr = address_id or store.get_pref("zepto_address_id")
    if not addr:
        err(
            "No Zepto delivery address selected, and Zepto cannot search or "
            "build a cart without one.\n"
            "    food zepto addresses            # ask the user which one\n"
            "    food zepto use-address <id>"
        )
        raise typer.Exit(2)
    if store.get_pref("zepto_store_ready") == store_marker(addr) and not address_id:
        return addr
    call(SERVER, "select_saved_address", {"addressId": addr})
    store.set_pref("zepto_store_ready", store_marker(addr))
    return addr


# ------------------------------------------------------------------ account

@zepto_app.command("whoami")
def zepto_whoami():
    """The signed-in Zepto profile."""
    out(call(SERVER, "get_user_details", {}))


@zepto_app.command("addresses")
def zepto_addresses():
    """List saved Zepto delivery addresses, with the ids needed to select one.

    Zepto asks that ids not be shown to the user - refer to an address by its
    label or number when speaking, and pass the id to `use-address`.
    """
    res = call(SERVER, "list_saved_addresses", {})
    parsed = parse_addresses(text_of(res))
    out({
        **res,
        "addresses": parsed,
        "selected": store.get_pref("zepto_address_id"),
        "note": (
            "Refer to an address by label or number when talking to the user; "
            "ids are for `food zepto use-address`."
        ),
    })


@zepto_app.command("use-address")
def zepto_use_address(
    address_id: str = typer.Argument(..., help="addressId from `food zepto addresses`."),
    remember: bool = typer.Option(True, "--remember/--no-remember"),
):
    """Select a delivery address, which also sets the store for this session."""
    res = call(SERVER, "select_saved_address", {"addressId": address_id})
    if remember:
        store.set_pref("zepto_address_id", address_id)
        store.set_pref("zepto_store_ready", store_marker(address_id))
    out(res)


@zepto_app.command("serviceable")
def zepto_serviceable(
    latitude: float = typer.Option(..., "--lat"),
    longitude: float = typer.Option(..., "--lon"),
):
    """Check whether Zepto delivers to a coordinate."""
    out(call(SERVER, "get_location_serviceability",
             {"latitude": latitude, "longitude": longitude}))


# ------------------------------------------------------------------- search

@zepto_app.command("usual")
def zepto_usual():
    """Products this user orders most often.

    Worth calling before a search: Zepto's own guidance is to search using the
    exact product name from past orders, which is what makes "the usual" resolve
    to the brand they actually buy rather than a generic match.
    """
    out(call(SERVER, "get_past_order_items", {}))


@zepto_app.command("search")
def zepto_search(
    query: str = typer.Argument(..., help="ONE product. Use `search-many` for a list."),
    page: int = typer.Option(None, "--page"),
    address: str = typer.Option(None, "--address"),
):
    """Search Zepto's catalogue for a single product."""
    _ensure_store(address)
    args: dict = {"query": query}
    if page is not None:
        args["pageNumber"] = page
    out(call(SERVER, "search_products", args))


@zepto_app.command("search-many")
def zepto_search_many(
    queries: list[str] = typer.Argument(..., help="Several products, one search each."),
    page: int = typer.Option(None, "--page"),
    address: str = typer.Option(None, "--address"),
):
    """Search for several products at once, grouped by query."""
    _ensure_store(address)
    args: dict = {"queries": list(queries)}
    if page is not None:
        args["pageNumber"] = page
    out(call(SERVER, "search_multiple_products", args))


@zepto_app.command("product")
def zepto_product(
    variant_id: str = typer.Argument(..., help="productVariantId from a search result."),
):
    """Full details for one product."""
    out(call(SERVER, "get_product_details", {"product_variant_id": variant_id}))


# --------------------------------------------------------------------- cart

def _parse_item(spec: str) -> dict:
    """`productVariantId:storeProductId:quantity` -> a cart item."""
    parts = spec.split(":")
    if len(parts) < 2:
        raise typer.BadParameter(
            f"--item {spec!r} must be productVariantId:storeProductId[:quantity] "
            "(both ids come from `food zepto search`)."
        )
    pvid, spid = parts[0], parts[1]
    qty = int(parts[2]) if len(parts) > 2 and parts[2] else 1
    return {"productVariantId": pvid, "storeProductId": spid, "quantity": qty}


@zepto_app.command("cart")
def zepto_cart():
    """Show the Zepto cart."""
    out(call(SERVER, "view_cart", {}))


@zepto_app.command("add")
def zepto_add(
    item: list[str] = typer.Option(
        ..., "--item",
        help="productVariantId:storeProductId[:quantity], repeatable.",
    ),
    address: str = typer.Option(None, "--address"),
    replace: bool = typer.Option(
        False, "--replace",
        help="Replace the whole cart instead of merging these items into it.",
    ),
):
    """Add or update Zepto cart items.

    Quantity 0 removes an item, which is how `remove` is implemented.
    """
    _ensure_store(address)
    args = {
        "deviceId": device_id(),
        "cartItems": [_parse_item(s) for s in item],
    }
    if replace:
        args["replaceCart"] = True
    out(call(SERVER, "update_cart", args))


@zepto_app.command("remove")
def zepto_remove(
    item: list[str] = typer.Option(..., "--item", help="productVariantId:storeProductId"),
    address: str = typer.Option(None, "--address"),
):
    """Remove items from the Zepto cart."""
    _ensure_store(address)
    items = []
    for spec in item:
        parsed = _parse_item(spec if spec.count(":") >= 1 else f"{spec}:")
        parsed["quantity"] = 0
        items.append(parsed)
    out(call(SERVER, "update_cart", {"deviceId": device_id(), "cartItems": items}))


@zepto_app.command("clear")
def zepto_clear(address: str = typer.Option(None, "--address")):
    """Empty the Zepto cart."""
    _ensure_store(address)
    out(call(SERVER, "update_cart",
             {"deviceId": device_id(), "cartItems": [], "replaceCart": True}))


# ------------------------------------------------------------------ payment

@zepto_app.command("payment-options")
def zepto_payment_options():
    """What this account can actually pay with, for the current cart.

    Availability is per account and per cart, so read it live rather than
    assuming COD or a wallet is on.
    """
    res = call(SERVER, "get_payment_methods", {})
    blob = text_of(res).lower()
    out({
        **res,
        "cod_available": "cod" in blob or "cash on delivery" in blob,
        "hands_free_possible": "cod" in blob or "cash on delivery" in blob,
        "methods_hint": sorted(PAYMENT_TOOLS),
    })


@zepto_app.command("place")
def zepto_place(
    payment: str = typer.Option(
        None, "--payment",
        help="Required: cod | online | wallet | reserve. Ask the user - never "
             "assume, and never default to cash on delivery.",
    ),
    yes: bool = typer.Option(False, "-y", "--yes", help="Confirms you intend to spend money."),
    address: str = typer.Option(None, "--address", help="userAddressId to deliver to."),
    tip: float = typer.Option(None, "--tip", help="Rider tip in rupees."),
    zepto_cash: bool = typer.Option(
        False, "--zepto-cash", help="Apply wallet balance towards the total."
    ),
):
    """Place a Zepto order.

    Real money and a real delivery - Zepto's MCP is not a sandbox. The payment
    method is always the user's explicit choice, because it selects the tool,
    and because cash on delivery commits them to paying a rider at the door.
    """
    if not payment:
        err(
            "Refusing to order without an explicit --payment.\n"
            "  1. food zepto payment-options    # what this account can use\n"
            "  2. ask the user which one\n"
            f"  3. re-run with --payment <{' | '.join(sorted(PAYMENT_TOOLS))}>"
        )
        raise typer.Exit(2)

    method = payment.strip().lower()
    if method in ("cash", "cash on delivery"):
        method = "cod"
    if method not in PAYMENT_TOOLS:
        err(f"Unknown --payment {payment!r}. Use one of: {', '.join(sorted(PAYMENT_TOOLS))}.")
        raise typer.Exit(2)

    addr = address or store.get_pref("zepto_address_id")
    _ensure_store(addr)

    if not yes:
        err(
            "Refusing to place a Zepto order without --yes/-y.\n"
            "Confirm the cart and the total with the user first "
            "(`food zepto cart`)."
        )
        raise typer.Exit(2)

    args: dict = {"confirmOrder": True}
    if addr:
        args["userAddressId"] = addr
    if tip is not None:
        args["riderTip"] = tip
    if zepto_cash and method != "wallet":
        args["useZeptoCash"] = True

    tool = PAYMENT_TOOLS[method]
    res = call(SERVER, tool, args)
    _log_order(SERVER, res, addr)

    payload = {**res, "payment_method": method, "tool": tool}
    oid = order_id_in(text_of(res))
    if oid:
        payload["order_id"] = oid
        store.set_pref("last_order_id_zepto", oid)

    if method == "cod":
        err("\n✅ Cash on delivery — nothing to pay now. The rider collects on arrival.\n")
    else:
        err(
            "\n>>> Payment is pending and must be completed by the USER. "
            "Do not enter any payment credential on their behalf.\n"
            f"    Then: food zepto pay-status {oid or '<orderId>'}\n"
        )
    out(payload)


@zepto_app.command("pay-status")
def zepto_pay_status(
    order_id: str = typer.Argument(None, help="Defaults to the last Zepto order."),
    poll: bool = typer.Option(
        False, "--poll",
        help="Block until the payment reaches a terminal state. Only use this "
             "when Zepto's own response asks you to.",
    ),
):
    """Check whether a Zepto online payment has gone through."""
    oid = order_id or store.get_pref("last_order_id_zepto")
    if not oid:
        err("No orderId given and none stored.")
        raise typer.Exit(2)
    args: dict = {"orderId": oid}
    if poll:
        args["poll"] = True
    out(call(SERVER, "check_payment_status", args))


# ------------------------------------------------------------------- orders

@zepto_app.command("orders")
def zepto_orders(
    limit: int = typer.Option(None, "--limit"),
    page: int = typer.Option(None, "--page"),
):
    """Past Zepto orders, including status and tracking."""
    args: dict = {}
    if limit is not None:
        args["limit"] = limit
    if page is not None:
        args["pageNumber"] = page
    out(call(SERVER, "list_order_history", args))


@zepto_app.command("order")
def zepto_order(order_id: str = typer.Argument(...)):
    """Details for one Zepto order."""
    out(call(SERVER, "get_order_detail", {"orderId": order_id}))


@zepto_app.command("call")
def zepto_call(
    tool: str = typer.Argument(..., help="Any Zepto tool name."),
    args_json: str = typer.Option("{}", "--args", help="Tool arguments as JSON."),
):
    """Escape hatch: call a Zepto tool this group does not wrap."""
    try:
        parsed = json.loads(args_json)
    except json.JSONDecodeError as e:
        err(f"--args must be valid JSON: {e}")
        raise typer.Exit(2) from e
    out(call(SERVER, tool, parsed))
