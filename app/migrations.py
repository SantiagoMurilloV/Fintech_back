"""Idempotent schema top-ups and data normalisations.

`Base.metadata.create_all` creates new tables but never alters existing ones,
so columns added after the first deploy are applied here, together with the
few row rewrites that keep old data consistent with new rules. Each statement
is safe to run repeatedly.
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

# (table, statement) — row rewrites that become no-ops once applied.
NORMALISED_ROWS = [
    # The Mandioca feed spelled USDT as "TETHER" in part of its history: same
    # asset, one code (mapping.CURRENCY_ALIASES). The original spelling is
    # still in extra._raw.
    ("orders", "UPDATE orders SET currency = 'USDT' WHERE currency = 'TETHER'"),
    ("expenses", "UPDATE expenses SET currency = 'USDT' WHERE currency = 'TETHER'"),
]


def apply(engine: Engine) -> list[str]:
    """Run pending column additions and row rewrites; returns what changed."""
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
        for table, statement in NORMALISED_ROWS:
            rewritten = conn.execute(text(statement)).rowcount
            if rewritten:
                applied.append(f"{table}.currency: {rewritten} TETHER -> USDT")
    return applied
