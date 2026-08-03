"""SQLite-backed persistent state for the CLI.

Holds OAuth tokens, the default delivery address, arbitrary key/value prefs, a
small cache and the order log. One file, owner-readable only.

The schema is defined by Alembic revisions under `food_cli/migrations`, not
here. `connect()` brings the database to head on first use, so the CLI stays
zero-setup - nobody has to run a migration before ordering groceries.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
import time
from pathlib import Path

from . import migrations, paths


def _default_db_path() -> Path:
    """Where the store lives.

    FOOD_CLI_DB wins. Otherwise a database left by the pre-rename `swiggy-cli`
    is reused where it exists, so upgrading does not silently strand somebody's
    tokens and order history in a directory nothing reads any more.
    """
    explicit = os.environ.get("FOOD_CLI_DB") or os.environ.get("SWIGGY_CLI_DB")
    if explicit:
        return Path(explicit)
    legacy = Path.home() / paths.LEGACY_DIR_NAME / "swiggy.db"
    if legacy.exists():
        return legacy
    return paths.data_dir() / "food.db"


DB_PATH = _default_db_path()

@contextmanager
def connect():
    """Open the store, commit on success, and always close.

    `with sqlite3.connect(...)` commits but does NOT close the handle, which
    leaks a file descriptor per call - noticeable in a long-running agent.
    """
    # Owner-only directory: it holds OAuth tokens, saved addresses (with names
    # and phone numbers) and full order history.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DB_PATH.parent, 0o700)
    except OSError:
        pass

    # Cheap after the first call per database file.
    migrations.ensure_current(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # Tokens are credentials - keep the file private to the owning user.
        try:
            os.chmod(DB_PATH, 0o600)
        except OSError:
            pass
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- prefs

def get_pref(key: str, default=None):
    with connect() as c:
        row = c.execute("SELECT value FROM prefs WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_pref(key: str, value) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO prefs(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), time.time()),
        )


# ------------------------------------------------------------- addresses

def save_address(addr_id: str, label: str, payload: dict, is_default: bool = False) -> None:
    with connect() as c:
        if is_default:
            c.execute("UPDATE addresses SET is_default=0")
        c.execute(
            "INSERT INTO addresses(id,label,payload,is_default,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET label=excluded.label, payload=excluded.payload, "
            "is_default=excluded.is_default, updated_at=excluded.updated_at",
            (addr_id, label, json.dumps(payload), int(is_default), time.time()),
        )


def get_default_address() -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM addresses WHERE is_default=1").fetchone()
    return json.loads(row["payload"]) if row else None


def list_addresses() -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM addresses ORDER BY is_default DESC, label").fetchall()
    return [
        {"id": r["id"], "label": r["label"], "is_default": bool(r["is_default"]),
         **json.loads(r["payload"])}
        for r in rows
    ]


# ----------------------------------------------------------------- cache

def cache_get(key: str):
    with connect() as c:
        row = c.execute("SELECT value,expires_at FROM cache WHERE key=?", (key,)).fetchone()
    if not row or row["expires_at"] < time.time():
        return None
    return json.loads(row["value"])


def cache_set(key: str, value, ttl: float = 900) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO cache(key,value,expires_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at",
            (key, json.dumps(value), time.time() + ttl),
        )


# ---------------------------------------------------------------- orders

def record_order(
    order_id: str,
    kind: str,
    payload,
    vendor: str | None = None,
    amount: float | None = None,
    original: float | None = None,
    discount: float | None = None,
    coupon: str | None = None,
    address_id: str | None = None,
    status: str | None = None,
) -> None:
    """Log an order and what it cost, so spend can be reviewed later."""
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO orders"
            "(id,kind,vendor,amount,original,discount,coupon,address_id,status,payload,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, kind, vendor, amount, original, discount, coupon,
             address_id, status, json.dumps(payload, default=str), time.time()),
        )


def upsert_history_order(
    order_id: str,
    kind: str,
    vendor: str | None,
    amount: float | None,
    ordered_at: str | None,
    status: str | None,
    items: list[dict] | None = None,
    payload=None,
) -> bool:
    """Insert a historical order. Returns True if it was new.

    Never overwrites an order this CLI placed itself (source='placed'), so a
    sync cannot clobber richer local data.
    """
    with connect() as c:
        existing = c.execute("SELECT source FROM orders WHERE id=?", (order_id,)).fetchone()
        if existing and existing["source"] == "placed":
            return False
        is_new = existing is None
        c.execute(
            "INSERT INTO orders(id,kind,vendor,amount,ordered_at,status,payload,source,created_at)"
            " VALUES(?,?,?,?,?,?,?,'synced',?)"
            " ON CONFLICT(id) DO UPDATE SET vendor=excluded.vendor, amount=excluded.amount,"
            " ordered_at=excluded.ordered_at, status=excluded.status, payload=excluded.payload",
            (order_id, kind, vendor, amount, ordered_at, status,
             json.dumps(payload, default=str) if payload is not None else None, time.time()),
        )
        for it in items or []:
            c.execute(
                "INSERT OR REPLACE INTO order_items(order_id,name,quantity,kind,created_at)"
                " VALUES(?,?,?,?,?)",
                (order_id, it["name"], it.get("quantity", 1), kind, time.time()),
            )
    return is_new


def set_preference(
    key: str, value, source: str = "learned",
    confidence: float | None = None, evidence: str | None = None,
) -> bool:
    """Record a preference. An inference never overwrites a stated preference."""
    with connect() as c:
        row = c.execute("SELECT source FROM preferences WHERE key=?", (key,)).fetchone()
        if row and row["source"] == "explicit" and source == "learned":
            return False
        c.execute(
            "INSERT INTO preferences(key,value,source,confidence,evidence,updated_at)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, source=excluded.source,"
            " confidence=excluded.confidence, evidence=excluded.evidence,"
            " updated_at=excluded.updated_at",
            (key, json.dumps(value), source, confidence, evidence, time.time()),
        )
    return True


def get_preference(key: str, default=None):
    with connect() as c:
        row = c.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def all_preferences() -> dict:
    with connect() as c:
        rows = c.execute("SELECT * FROM preferences ORDER BY key").fetchall()
    return {
        r["key"]: {
            "value": json.loads(r["value"]),
            "source": r["source"],
            "confidence": r["confidence"],
            "evidence": r["evidence"],
        }
        for r in rows
    }


def clear_preferences(learned_only: bool = True) -> int:
    with connect() as c:
        q = "DELETE FROM preferences" + (" WHERE source='learned'" if learned_only else "")
        return c.execute(q).rowcount


def top_items(limit: int = 15, kind: str | None = None) -> list[dict]:
    """Most frequently ordered items - the basis for 'the usual'."""
    q = ("SELECT name, kind, SUM(quantity) AS units, COUNT(DISTINCT order_id) AS orders"
         " FROM order_items")
    args: list = []
    if kind:
        q += " WHERE kind=?"
        args.append(kind)
    q += " GROUP BY name, kind ORDER BY orders DESC, units DESC LIMIT ?"
    args.append(limit)
    with connect() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def vendor_summary(limit: int = 15) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT vendor, kind, COUNT(*) n, COALESCE(SUM(amount),0) spent,"
            " COALESCE(AVG(amount),0) avg_order FROM orders"
            " WHERE vendor IS NOT NULL GROUP BY vendor, kind"
            " ORDER BY spent DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def list_orders(limit: int = 50, kind: str | None = None) -> list[dict]:
    q = "SELECT * FROM orders"
    args: list = []
    if kind:
        q += " WHERE kind=?"
        args.append(kind)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with connect() as c:
        rows = c.execute(q, args).fetchall()
    return [
        {k: r[k] for k in r.keys() if k != "payload"} | {"created_at": r["created_at"]}
        for r in rows
    ]


def spend_summary(since: float | None = None) -> dict:
    q = "SELECT kind, COUNT(*) n, COALESCE(SUM(amount),0) total, COALESCE(SUM(discount),0) saved FROM orders"
    args: list = []
    if since:
        q += " WHERE created_at >= ?"
        args.append(since)
    q += " GROUP BY kind"
    with connect() as c:
        rows = c.execute(q, args).fetchall()
    per = {r["kind"]: {"orders": r["n"], "spent": r["total"], "saved": r["saved"]} for r in rows}
    return {
        "by_kind": per,
        "total_spent": sum(v["spent"] for v in per.values()),
        "total_saved": sum(v["saved"] for v in per.values()),
    }
