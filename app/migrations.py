"""Idempotent schema top-ups.

`Base.metadata.create_all` creates new tables but never alters existing ones,
so columns added after the first deploy are applied here. Each statement is
safe to run repeatedly.
"""
from sqlalchemy import text
from sqlalchemy.engine import Engine

# Statements applied unconditionally; each one must be idempotent on its own.
ALTERED_COLUMNS = [
    # Crypto codes (USDT, USDC, WBTC) exceed the original VARCHAR(3).
    "ALTER TABLE orders ALTER COLUMN currency TYPE VARCHAR(10)",
    "ALTER TABLE expenses ALTER COLUMN currency TYPE VARCHAR(10)",
]

# (table, column, DDL type) — added with IF NOT EXISTS so re-runs are no-ops.
ADDED_COLUMNS = [
    ("orders", "extra", "JSONB DEFAULT '{}'::jsonb"),
    ("expenses", "extra", "JSONB DEFAULT '{}'::jsonb"),
    ("messages", "blocks", "JSONB DEFAULT '[]'::jsonb"),
    ("conversations", "draft", "JSONB"),
]


def apply(engine: Engine) -> list[str]:
    """Run pending column additions; returns the ones actually applied."""
    # The DDL above is PostgreSQL-specific. On SQLite (tests) create_all
    # already built the current schema, so there is nothing to top up.
    if engine.dialect.name != "postgresql":
        return []
    applied: list[str] = []
    with engine.begin() as conn:
        for statement in ALTERED_COLUMNS:
            conn.execute(text(statement))
        for table, column, ddl in ADDED_COLUMNS:
            exists = conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ), {"t": table, "c": column}).first()
            if exists:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}"))
            applied.append(f"{table}.{column}")
    return applied
