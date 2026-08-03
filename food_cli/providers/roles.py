"""Map abstract capabilities onto whatever a server actually calls its tools.

Swiggy documents its tool names, so they are stated outright in `KNOWN`. Zepto
documents capabilities ("Search Products", "Cart Management", "Order
Placement", "Order History") but not tool names, and inventing them would
produce a CLI that fails at runtime against the real server.

So for anything not in `KNOWN`, the roles are resolved by listing the server's
tools once and scoring each name and description against the vocabulary below.
The result is cached in SQLite; `--refresh` re-discovers.

This is deliberately conservative. A role that cannot be matched confidently
resolves to `None`, and the command says so rather than calling a guessed tool
that might charge money.
"""

from __future__ import annotations

import re

from ..core import store

#: Capabilities the CLI knows how to drive, independent of vendor naming.
ROLES = (
    "search",
    "cart_get",
    "cart_update",
    "cart_clear",
    "order_place",
    "order_history",
    "order_status",
    "payment_options",
    "payment_status",
)

#: Servers whose tool names are published. No guessing where we have facts.
KNOWN: dict[str, dict[str, str]] = {
    "food": {
        "search": "search_menu",
        "cart_get": "get_food_cart",
        "cart_update": "update_food_cart",
        "cart_clear": "flush_food_cart",
        "order_place": "place_food_order",
        "order_history": "get_food_orders",
        "order_status": "get_food_delivery_status",
        "payment_options": "get_payment_options",
        "payment_status": "check_payment_status",
    },
    "zepto": {
        "search": "search_products",
        "cart_get": "view_cart",
        # Zepto has no dedicated clear tool; emptying the cart is update_cart
        # with replaceCart and an empty list.
        "cart_update": "update_cart",
        "cart_clear": "update_cart",
        # COD. The online / wallet / reserve-pay variants are separate tools -
        # see commands/zepto.py, which picks by the payment method the user
        # actually chose rather than defaulting to one.
        "order_place": "create_order",
        "order_history": "list_order_history",
        "order_status": "get_order_detail",
        "payment_options": "get_payment_methods",
        "payment_status": "check_payment_status",
    },
    "instamart": {
        "search": "search_products",
        "cart_get": "get_cart",
        "cart_update": "update_cart",
        "cart_clear": "clear_cart",
        "order_place": "checkout",
        "order_history": "get_orders",
        "order_status": "get_delivery_status",
        "payment_options": "get_payment_options",
        "payment_status": "check_payment_status",
    },
}

# (must-have term, supporting terms). A tool only scores for a role if one of
# the must-have terms appears; supporting terms then break ties.
_VOCAB: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "search":          (("search", "find", "browse", "discover"), ("product", "item", "catalog")),
    "cart_get":        (("cart", "basket"), ("get", "view", "show", "fetch", "read")),
    "cart_update":     (("cart", "basket"), ("update", "add", "modify", "set", "quantity")),
    "cart_clear":      (("cart", "basket"), ("clear", "empty", "flush", "remove all", "reset")),
    "order_place":     (("order", "checkout", "place"), ("place", "create", "submit", "checkout")),
    "order_history":   (("order",), ("history", "past", "previous", "list", "orders")),
    "order_status":    (("status", "track", "delivery", "eta"), ("order", "delivery", "track")),
    "payment_options": (("payment", "pay"), ("option", "method", "mode", "available")),
    "payment_status":  (("payment", "pay"), ("status", "check", "verify", "poll")),
}

_CACHE_KEY = "tool_roles:{server}"
_TOOLS_KEY = "tools_cache:{server}"
_WORD = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def score(name: str, description: str, role: str) -> int:
    """How well one tool fits one role. 0 means "not a candidate"."""
    must, support = _VOCAB[role]
    hay_name = name.lower().replace("_", " ")
    hay_all = f"{hay_name} {(description or '').lower()}"

    if not any(m in hay_all for m in must):
        return 0

    points = 0
    for m in must:
        if m in hay_name:
            points += 6          # the name is far stronger evidence than prose
        elif m in hay_all:
            points += 2
    for s in support:
        if s in hay_name:
            points += 3
        elif s in hay_all:
            points += 1

    # A "clear cart" tool also matches cart_update's vocabulary; require the
    # distinguishing verb to win so the two never collapse onto one tool.
    if role == "cart_update" and any(w in hay_name for w in ("clear", "empty", "flush")):
        return 0
    if role == "cart_get" and any(w in hay_name for w in ("update", "add", "clear", "remove")):
        return 0
    if role == "order_history" and any(w in hay_name for w in ("place", "create", "cancel")):
        return 0

    # Ambiguity guard: a bare word match with nothing supporting it is a guess.
    return points if points >= 6 else 0


def map_tools(tools: list[dict]) -> dict[str, str | None]:
    """Best tool per role, or None where nothing scored confidently."""
    mapping: dict[str, str | None] = {}
    for role in ROLES:
        ranked = sorted(
            ((score(t.get("name", ""), t.get("description", "") or "", role), t.get("name", ""))
             for t in tools),
            reverse=True,
        )
        best = ranked[0] if ranked else (0, "")
        mapping[role] = best[1] if best[0] > 0 else None
    return mapping


def cached(server: str) -> dict[str, str | None] | None:
    if server in KNOWN:
        return dict(KNOWN[server])
    return store.get_pref(_CACHE_KEY.format(server=server))


def discover(server: str, tools: list[dict] | None = None,
             refresh: bool = False) -> dict[str, str | None]:
    """Resolve every role for a server, listing its tools if needed."""
    if server in KNOWN:
        return dict(KNOWN[server])

    if not refresh:
        hit = store.get_pref(_CACHE_KEY.format(server=server))
        if hit:
            return hit

    if tools is None:
        tools = list_cached_tools(server, refresh=refresh)

    mapping = map_tools(tools)
    store.set_pref(_CACHE_KEY.format(server=server), mapping)
    return mapping


def list_cached_tools(server: str, refresh: bool = False) -> list[dict]:
    """The server's tool list, fetched once and cached.

    Cached because discovery costs a round trip and an authorized session, and
    a tool surface does not change between two commands in the same order.
    """
    if not refresh:
        hit = store.get_pref(_TOOLS_KEY.format(server=server))
        if hit:
            return hit

    import asyncio

    from ..mcp import client
    tools = asyncio.run(client.list_tools(server))
    store.set_pref(_TOOLS_KEY.format(server=server), tools)
    return tools


def schema_for(server: str, role: str, refresh: bool = False) -> dict:
    """The JSON Schema for a role's tool, so callers can build valid arguments."""
    name = resolve(server, role, refresh=refresh)
    for t in list_cached_tools(server, refresh=refresh):
        if t.get("name") == name:
            return t.get("input_schema") or {}
    return {}


def param_named(schema: dict, candidates: tuple[str, ...]) -> str | None:
    """Find the property a caller means, e.g. the search term.

    Providers name the same argument `query`, `q`, `search_term`... Matching the
    schema beats hard-coding one vendor's spelling.
    """
    props = (schema or {}).get("properties") or {}
    lowered = {k.lower(): k for k in props}
    for c in candidates:
        if c in lowered:
            return lowered[c]
    for c in candidates:
        for low, original in lowered.items():
            if c in low:
                return original
    return None


def resolve(server: str, role: str, refresh: bool = False) -> str:
    """The tool name for a role, or a clear error naming the escape hatch."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    name = discover(server, refresh=refresh).get(role)
    if not name:
        raise LookupError(
            f"{server} exposes no tool this CLI recognises for '{role}'. "
            f"Run `food mcp list {server}` to see what it does expose, then use "
            f"`food mcp call {server} <tool> --args '{{...}}'`."
        )
    return name
