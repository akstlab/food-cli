"""Delivery address management.

Addresses live with the provider, so the server is selectable. Swiggy shares one
address book between food and Instamart, which is why `--server` defaults to
food and the saved default is used by both.
"""

from __future__ import annotations

import json
import re

import typer

from ..core import store
from .common import call, out

addr_app = typer.Typer(no_args_is_help=True, help="Delivery address management.")


@addr_app.command("list")
def addr_list(
    page: int = typer.Option(None, "--page", help="Providers paginate 10 per page."),
    local: bool = typer.Option(False, "--local", help="Show locally-cached addresses instead."),
    server: str = typer.Option("food", "--server"),
):
    """List the user's saved delivery addresses."""
    if local:
        out(store.list_addresses())
        return
    args = {"page": page} if page else {}
    out(call(server, "get_addresses", args))


@addr_app.command("search")
def addr_search(
    query: str = typer.Argument(..., help="Match on label, name, area or pincode."),
    max_pages: int = typer.Option(5, "--max-pages", help="Providers paginate 10 per page."),
    server: str = typer.Option("food", "--server"),
):
    """Find a saved address by keyword.

    There is no address-search tool - only a paginated list - so this walks the
    pages and filters locally.
    """
    needle = query.lower().strip()
    entries: list[dict] = []
    seen: set[str] = set()

    for page in range(1, max_pages + 1):
        res = call(server, "get_addresses", {"page": page})
        body = res.get("content")
        text = body if isinstance(body, str) else json.dumps(body, default=str)
        # Lines look like: "3. [Home] Name: 12, Some Street, City (ID: abc123)"
        for line in text.splitlines():
            m = re.search(r"\(ID:\s*([A-Za-z0-9_-]+)\)", line)
            if not m:
                continue
            aid = m.group(1)
            if aid in seen:
                continue
            seen.add(aid)
            label_m = re.search(r"\[([^\]]+)\]", line)
            entries.append({
                "id": aid,
                "label": label_m.group(1) if label_m else None,
                "text": re.sub(r"^\s*\d+\.\s*", "", line).strip(),
            })
        if "page=" not in text and "Use page" not in text:
            break

    hits = [e for e in entries if needle in e["text"].lower()]
    out({
        "query": query,
        "searched": len(entries),
        "matches": hits,
        "hint": "food address set-default <id> --label <name>",
    })


@addr_app.command("set-default")
def addr_set_default(
    address_id: str = typer.Argument(..., help="addressId from `food address list`."),
    label: str = typer.Option("", "--label", help="Friendly name, e.g. 'Home'."),
):
    """Remember an address as the default for all future orders."""
    store.set_pref("default_address_id", address_id)
    if label:
        store.save_address(address_id, label, {"addressId": address_id}, is_default=True)
    out({"default_address_id": address_id, "label": label or None})


@addr_app.command("default")
def addr_default():
    """Show the saved default delivery address."""
    out({
        "default_address_id": store.get_pref("default_address_id"),
        "cached": store.get_default_address(),
    })
