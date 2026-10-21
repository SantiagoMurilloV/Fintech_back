"""What the agent may read, and what every column means.

Only the tables listed here exist for the agent. Users, sessions, settings
(API keys, tokens), integration credentials, conversations and agent traces
are not listed, so a query that names them is refused before it runs.
"""
from __future__ import annotations

# table -> (what a row is, {column: meaning}). Kept short: it travels in the
# prompt of every turn.
TABLES: dict[str, tuple[str, dict[str, str]]] = {
    "orders": (
        "One order. Since the OTC import, mostly one OTC trade per row.",
        {
            "id": "text; OTC trades look like OTC-2025-00042 (year + sheet row)",
            "customer": "client name, upper-case for OTC trades",
            "amount": "the USD side of the trade (USD wired out for off-ramp, received for on-ramp)",
            "currency": "currency of amount (USD for OTC)",
            "status": "approved | pending | rejected | refunded (OTC trades are all approved)",
            "gateway": "provider that executed it: Conduit, Reap, Mesta, One Trade, Stillman…",
            "date": "YYYY-MM-DD as text; use substr(date,1,7) for the month",
            "extra": "JSON. OTC keys: tipo ('Off-ramp'|'On-ramp'), direccion ('SELL'|'BUY'), "
                     "activo (USDT, USDC…), usdt (crypto amount), precio_cliente, fee_wire, "
                     "spread_bruto (gross spread, USD), costo_proveedor (provider cost, USD), "
                     "utilidad (operating profit, USD), beneficiario, source "
                     "('otc'|'quickbooks'|'api'|'excel'|'manual')",
        },
    ),
    "expenses": (
        "One expense.",
        {
            "id": "integer", "description": "concept", "category": "category name",
            "vendor": "supplier", "amount": "amount in `currency`", "currency": "USD, COP, USDT…",
            "owner": "person responsible", "date": "YYYY-MM-DD as text",
            "receipt_name": "receipt file label, null when missing",
            "extra": "JSON; source ('quickbooks'|'api'|'excel'|'manual') and feed fields",
        },
    ),
    "rate_snapshots": (
        "One market price as a source quoted it at one moment (the price history).",
        {
            "pair": "'USDT/USD', 'USDC/USD', 'BTC/USD', 'ETH/USD', 'USD/COP', 'USD/BRL', 'USD/MXN', 'USD/EUR', 'USD/GBP'",
            "source": "'tradingview' | 'coinbase' | 'kraken' (live) | 'trm' | 'ecb' (daily official)",
            "price": "the price (for USD/XXX: units of XXX per 1 USD)",
            "bid": "best bid when the source gives it", "ask": "best ask when the source gives it",
            "rate_date": "publication day for daily sources (YYYY-MM-DD)",
            "taken_at": "UTC timestamp 'YYYY-MM-DDTHH:MM:SS' when we read it",
        },
    ),
    "accounts": (
        "The company's accounting plan (chart of accounts) for the income statement.",
        {
            "key": "account key", "name": "account name", "parent_key": "parent account",
            "kind": "input | total | group", "formula": "recipe of a total account",
            "nature": "income | cost | result", "definition": "what the account means",
        },
    ),
    "account_values": (
        "The figure of one input account in one month (the income statement inputs).",
        {
            "account_key": "accounts.key", "period": "YYYY-MM", "amount": "USD",
            "origin": "where the figure came from (typed, import:<file>)",
        },
    ),
    "reports": (
        "A document the panel generated (PDF, chart, invoice, receipt).",
        {"id": "integer", "title": "title", "kind": "chart | financial | analysis | statement | invoice | receipt",
         "period": "YYYY-MM", "created_at": "timestamp"},
    ),
}

DIALECT_NOTES = {
    "postgresql": "PostgreSQL. JSON fields: (extra->>'utilidad')::float, extra->>'tipo'.",
    "sqlite": "SQLite. JSON fields: CAST(json_extract(extra,'$.utilidad') AS REAL), "
              "json_extract(extra,'$.tipo').",
}


def allowed() -> set[str]:
    return set(TABLES)


def describe(dialect: str = "postgresql") -> str:
    """The catalog as prompt text."""
    lines = [f"Database: {DIALECT_NOTES.get(dialect, DIALECT_NOTES['postgresql'])}"]
    for table, (meaning, columns) in TABLES.items():
        lines.append(f"\n{table} — {meaning}")
        lines.extend(f"  {column}: {note}" for column, note in columns.items())
    return "\n".join(lines)
