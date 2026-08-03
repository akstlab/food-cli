"""Alembic migrations, including adoption of a pre-migrations database."""

from __future__ import annotations

import sqlite3

import pytest

from food_cli.core import migrations, store

TABLES = {"oauth", "prefs", "addresses", "cache", "orders", "order_items", "preferences"}


def tables_in(path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def version_of(path) -> str | None:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_a_fresh_database_gets_the_whole_schema(tmp_path):
    db = tmp_path / "fresh.db"
    migrations.upgrade(db)
    assert TABLES <= tables_in(db)
    assert version_of(db) == migrations.BASELINE


def test_upgrading_twice_is_harmless(tmp_path):
    db = tmp_path / "twice.db"
    migrations.upgrade(db)
    migrations.upgrade(db)
    assert TABLES <= tables_in(db)


def test_a_pre_migrations_database_is_adopted_not_rebuilt(tmp_path):
    """The schema existed before Alembic did. Re-applying the baseline over it
    would fail on tables that are already there, so it is stamped instead."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE prefs (key TEXT PRIMARY KEY, value TEXT, updated_at REAL);"
        "CREATE TABLE orders (id TEXT PRIMARY KEY, kind TEXT, amount REAL);"
        "INSERT INTO prefs VALUES ('default_address_id', '\"addr_1\"', 0);"
    )
    conn.commit()
    conn.close()

    assert migrations.adopt(db) is True
    assert version_of(db) == migrations.BASELINE

    conn = sqlite3.connect(db)
    kept = conn.execute("SELECT value FROM prefs WHERE key='default_address_id'").fetchone()
    conn.close()
    assert kept is not None, "adoption must not discard existing rows"


def test_adoption_is_a_no_op_on_a_fresh_database(tmp_path):
    db = tmp_path / "nothing.db"
    assert migrations.adopt(db) is False


def test_adoption_is_a_no_op_on_an_already_managed_database(tmp_path):
    db = tmp_path / "managed.db"
    migrations.upgrade(db)
    assert migrations.adopt(db) is False


def test_the_store_migrates_on_first_use(tmp_path, monkeypatch):
    db = tmp_path / "auto.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    migrations.reset_memo()
    store.set_pref("k", "v")
    assert store.get_pref("k") == "v"
    assert version_of(db) == migrations.BASELINE


def test_migrations_run_once_per_database(tmp_path, monkeypatch):
    db = tmp_path / "memo.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    migrations.reset_memo()
    calls = []
    real = migrations.upgrade
    monkeypatch.setattr(migrations, "upgrade", lambda p: (calls.append(p), real(p))[1])

    store.set_pref("a", 1)
    store.set_pref("b", 2)
    store.get_pref("a")
    assert len(calls) == 1, "the upgrade should be memoised per database file"


@pytest.mark.parametrize("var", ["FOOD_CLI_DB", "SWIGGY_CLI_DB"])
def test_both_env_vars_locate_the_database(tmp_path, monkeypatch, var):
    """The pre-rename variable still works, so an existing setup keeps running."""
    monkeypatch.delenv("FOOD_CLI_DB", raising=False)
    monkeypatch.delenv("SWIGGY_CLI_DB", raising=False)
    target = tmp_path / "env.db"
    monkeypatch.setenv(var, str(target))
    assert store._default_db_path() == target
