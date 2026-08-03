"""Alembic environment for the local SQLite store.

There is no ORM model here on purpose: the store is plain SQL, and the
migrations are the single description of its schema. `target_metadata` stays
None, so autogenerate is not available - revisions are written by hand, which is
the honest thing for a handful of tables.

The URL is injected by `food_cli.core.migrations`, because the database lives
wherever FOOD_CLI_DB points rather than at a fixed path.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most things in place; batch mode rewrites the
            # table instead, so future column drops and type changes work.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
