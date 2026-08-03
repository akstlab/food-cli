"""Learned and explicitly stated user preferences."""

from __future__ import annotations

import json

import typer

from ..core import profile, store
from .common import call, err, out, resolve_address, text_of
from .orders import parse_instamart_orders, parse_order_history

prefs_app = typer.Typer(no_args_is_help=True, help="Learned + explicit user preferences.")


@prefs_app.command("learn")
def prefs_learn(
    sync: bool = typer.Option(True, "--sync/--no-sync", help="Pull fresh history first."),
    address: str = typer.Option(None, "--address"),
):
    """Derive preferences from order history and store them."""
    if sync:
        try:
            addr = resolve_address(address)
            for k, res in (("food", call("food", "get_food_orders", {"addressId": addr})),
                           ("instamart", call("instamart", "get_orders", {"count": 50}))):
                rows = (parse_order_history(text_of(res), k) if k == "food"
                        else parse_instamart_orders(res))
                for o in rows:
                    store.upsert_history_order(
                        o["id"], k, o["vendor"], o["amount"], o["ordered_at"],
                        o["status"], o["items"], payload=o,
                    )
        except Exception as e:  # noqa: BLE001
            err(f"(history sync skipped: {e})")
    out(profile.learn())


@prefs_app.command("show")
def prefs_show():
    """Everything known about the user, learned or stated."""
    out(store.all_preferences())


@prefs_app.command("set")
def prefs_set(
    key: str = typer.Argument(..., help="e.g. diet, favourite_cuisines, food_budget"),
    value: str = typer.Argument(..., help="JSON, or a plain string."),
):
    """State a preference explicitly. Explicit always beats learned."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    store.set_preference(key, parsed, source="explicit", evidence="stated by user")
    out({key: parsed, "source": "explicit"})


@prefs_app.command("forget")
def prefs_forget(
    everything: bool = typer.Option(False, "--all", help="Also delete explicitly stated ones."),
):
    """Delete learned preferences (and optionally explicit ones)."""
    n = store.clear_preferences(learned_only=not everything)
    out({"deleted": n, "kept_explicit": not everything})
