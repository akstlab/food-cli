"""Run Alembic migrations against the local SQLite store.

The CLI must stay zero-setup: nobody should have to run `alembic upgrade` before
ordering groceries. So the schema is brought to head lazily, once per process
per database file, the first time the store is opened.

Databases created before migrations existed are *adopted*: if the tables are
already there but `alembic_version` is not, the database is stamped at the
baseline revision instead of having the baseline re-applied (which would fail on
tables that already exist).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from alembic import command
from alembic.config import Config

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = _PACKAGE_ROOT / "migrations"

#: Baseline revision. A pre-migrations database is stamped here.
BASELINE = "0001"

#: A table that only exists once the baseline schema has been applied.
_SENTINEL_TABLE = "prefs"

_lock = threading.Lock()
_done: set[str] = set()


def config_for(db_path: os.PathLike | str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # SQLAlchemy needs a POSIX-style URL; Path handles the separators.
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{Path(db_path).as_posix()}")
    # Alembic logs to stdout by default, which would corrupt the JSON contract.
    cfg.set_main_option("configure_logging", "false")
    return cfg


def _has_table(db_path: os.PathLike | str, name: str) -> bool:
    if not Path(db_path).exists():
        return False
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def adopt(db_path: os.PathLike | str, cfg: Config | None = None) -> bool:
    """Stamp a pre-migrations database at the baseline. True if adopted."""
    if not _has_table(db_path, _SENTINEL_TABLE):
        return False
    if _has_table(db_path, "alembic_version"):
        return False
    command.stamp(cfg or config_for(db_path), BASELINE)
    return True


def upgrade(db_path: os.PathLike | str) -> None:
    """Bring one database to head, adopting it first if it predates migrations."""
    cfg = config_for(db_path)
    adopt(db_path, cfg)
    command.upgrade(cfg, "head")


def ensure_current(db_path: os.PathLike | str) -> None:
    """Upgrade once per process per database file.

    Called on every store connection, so it has to be cheap after the first
    time - hence the memo. Tests point at a fresh file per test and are
    unaffected, because the key is the path.
    """
    key = str(Path(db_path).resolve())
    if key in _done:
        return
    with _lock:
        if key in _done:
            return
        upgrade(db_path)
        _done.add(key)


def reset_memo() -> None:
    """Forget which databases have been upgraded. For tests."""
    with _lock:
        _done.clear()
