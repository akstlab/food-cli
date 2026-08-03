"""Local preference storage."""

from __future__ import annotations

import json

import typer

from ..core import store
from .common import out


def config(
    key: str = typer.Argument(None),
    value: str = typer.Argument(None),
):
    """Get or set a local preference (no args lists all)."""
    if key is None:
        with store.connect() as c:
            rows = c.execute("SELECT key, value FROM prefs").fetchall()
        out({r["key"]: json.loads(r["value"]) for r in rows})
    elif value is None:
        out({key: store.get_pref(key)})
    else:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        store.set_pref(key, parsed)
        out({key: parsed})
