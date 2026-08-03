"""Order history, spend analytics and live tracking."""

from __future__ import annotations

import re

import typer

from ..core import store
from .common import call, out, resolve_address, text_of

orders_app = typer.Typer(no_args_is_help=True, help="Order history and spend.")


@orders_app.command("list")
def orders_list(
    kind: str = typer.Option(None, "--kind", help="food | instamart"),
    limit: int = typer.Option(20, "--limit"),
    remote: bool = typer.Option(False, "--remote", help="Query Swiggy instead of the local log."),
    address: str = typer.Option(None, "--address"),
    active: bool = typer.Option(False, "--active", help="Active orders only (remote)."),
):
    """List orders - locally logged by default, or live from Swiggy."""
    if not remote:
        out(store.list_orders(limit=limit, kind=kind))
        return
    if kind == "instamart":
        args = {"activeOnly": active} if active else {}
        out(call("instamart", "get_orders", args))
    else:
        out(call("food", "get_food_orders", {
            "addressId": resolve_address(address), "activeOnly": active,
        }))


# "1. Order 1234567890 — Sample Diner | July 15, 10:38 PM | Delivered | ₹441
#  [reorderable] — Item A (1),Item B (2)"
_HISTORY_LINE = re.compile(
    r"Order\s+(?P<id>[A-Za-z0-9]+)\s*—\s*(?P<vendor>[^|]+?)\s*\|"
    r"\s*(?P<when>[^|]+?)\s*\|"
    r"\s*(?P<status>[^|]+?)\s*\|"
    r"\s*₹(?P<amount>[\d.]+)"
    r"(?P<rest>.*)$",
    re.I,
)
_ITEM = re.compile(r"([^,]+?)\s*\((\d+)\)\s*(?:,|$)")


def parse_order_history(text: str, kind: str) -> list[dict]:
    """Turn the prose order list into structured rows for analytics."""
    orders = []
    for line in (text or "").splitlines():
        m = _HISTORY_LINE.search(line)
        if not m:
            continue
        g = m.groupdict()
        rest = g["rest"] or ""
        # Items follow the last em dash on the line.
        items = []
        if "—" in rest:
            for nm, qty in _ITEM.findall(rest.rsplit("—", 1)[-1]):
                nm = nm.strip().strip("[]")
                if nm and "reorderable" not in nm.lower():
                    items.append({"name": nm, "quantity": int(qty)})
        orders.append({
            "id": g["id"],
            "kind": kind,
            "vendor": g["vendor"].strip(),
            "ordered_at": g["when"].strip(),
            "status": g["status"].strip(),
            "amount": float(g["amount"]),
            "items": items,
        })
    return orders


def parse_instamart_orders(res) -> list[dict]:
    """Instamart's history is JSON, not prose - read it directly."""
    content = res.get("content")
    blocks = content if isinstance(content, list) else [content]
    rows: list[dict] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        for o in (b.get("orders") or b.get("data", {}).get("orders") or []):
            items = [
                {"name": it.get("name") or it.get("itemName"),
                 "quantity": it.get("quantity", 1)}
                for it in (o.get("items") or [])
                if (it.get("name") or it.get("itemName"))
            ]
            rows.append({
                "id": str(o.get("orderId")),
                "kind": "instamart",
                "vendor": "Instamart",
                "ordered_at": o.get("createdAt"),
                "status": o.get("status"),
                "amount": o.get("totalAmount"),
                "items": items,
            })
    return rows


@orders_app.command("sync")
def orders_sync(
    address: str = typer.Option(None, "--address"),
    kind: str = typer.Option("all", "--kind", help="food | instamart | all"),
    count: int = typer.Option(50, "--count", help="How many Instamart orders to pull."),  # noqa: E501
):
    """Pull order history from Swiggy into local SQLite, for analytics.

    Safe to re-run: existing rows are updated, and orders this CLI placed itself
    are never overwritten by the synced (less detailed) version.
    """
    addr = resolve_address(address)
    summary = {}

    for k in (["food", "instamart"] if kind == "all" else [kind]):
        if k == "food":
            res = call("food", "get_food_orders", {"addressId": addr})
            parsed = parse_order_history(text_of(res), k)
        else:
            # Instamart returns structured JSON rather than prose.
            res = call("instamart", "get_orders", {"count": count})
            parsed = parse_instamart_orders(res)
        new = 0
        for o in parsed:
            if store.upsert_history_order(
                o["id"], k, o["vendor"], o["amount"], o["ordered_at"],
                o["status"], o["items"], payload=o,
            ):
                new += 1
        summary[k] = {"fetched": len(parsed), "new": new,
                      "items": sum(len(o["items"]) for o in parsed)}

    out({"synced": summary, "totals": store.spend_summary()})


@orders_app.command("stats")
def orders_stats(
    limit: int = typer.Option(10, "--limit"),
    kind: str = typer.Option(None, "--kind"),
):
    """Analytics over the locally stored order history."""
    out({
        "spend": store.spend_summary(),
        "top_items": store.top_items(limit=limit, kind=kind),
        "by_vendor": store.vendor_summary(limit=limit),
    })


@orders_app.command("spend")
def orders_spend(days: int = typer.Option(None, "--days", help="Limit to the last N days.")):
    """Summarise how much has been spent and saved."""
    since = None
    if days:
        import time as _t
        since = _t.time() - days * 86400
    out(store.spend_summary(since))


@orders_app.command("track")
def orders_track(
    order_id: str = typer.Argument(...),
    kind: str = typer.Option("food", "--kind"),
    address: str = typer.Option(None, "--address"),
):
    """Track an order's delivery status."""
    if kind == "instamart":
        out(call("instamart", "get_delivery_status", {
            "orderId": order_id, "addressId": resolve_address(address),
        }))
    else:
        out(call("food", "get_food_delivery_status", {"orderId": order_id}))
