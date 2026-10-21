"""Run a query the model wrote, inside boundaries the server enforces.

The model plans; the server decides what may be read. A query is refused
before it runs unless it is ONE read statement (SELECT or WITH … SELECT) over
tables of the catalog only. It then runs in a read-only transaction with a
time limit, and at most MAX_ROWS rows come back, with a coverage note saying
how much was returned and whether it was cut — so an answer can say what it
is based on, and nothing can be written or deleted whatever the model types.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import catalog

MAX_ROWS = 200
TIMEOUT_MS = 5000

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|upsert|drop|alter|create|truncate|grant|revoke|"
    r"copy|vacuum|analyze|attach|detach|pragma|call|execute|do|lock|listen|notify|"
    r"set|reset|commit|rollback|savepoint|into)\b"
    r"|pg_|information_schema|sqlite_|;\s*\S|--|/\*", re.I)
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([A-Za-z_][\w.]*)", re.I)
_CTE_NAME = re.compile(r"(?:with|,)\s*([A-Za-z_]\w*)\s+as\s*\(", re.I)


class QueryRefused(ValueError):
    """The query was not run: the reason is safe to show the model."""


def check(sql: str) -> str:
    """The statement to run, or QueryRefused saying why not."""
    statement = (sql or "").strip().rstrip(";").strip()
    if not statement:
        raise QueryRefused("La consulta está vacía.")
    if not re.match(r"^(select|with)\b", statement, re.I):
        raise QueryRefused("Solo se permiten consultas de lectura (SELECT).")
    if _FORBIDDEN.search(statement):
        raise QueryRefused("La consulta usa algo que no está permitido (solo un SELECT de lectura, "
                           "sin comentarios ni varias sentencias).")
    ctes = {name.lower() for name in _CTE_NAME.findall(statement)}
    tables = {ref.split(".")[-1].lower() for ref in _TABLE_REF.findall(statement)}
    unknown = sorted(tables - ctes - catalog.allowed())
    if unknown:
        raise QueryRefused(f"Tablas no disponibles: {', '.join(unknown)}. "
                           f"Disponibles: {', '.join(sorted(catalog.allowed()))}.")
    return statement


def _plain(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float):
        return round(value, 6)
    return value


def run(db: Session, sql: str, max_rows: int = MAX_ROWS) -> dict:
    """Execute a checked read query; returns columns, rows and coverage."""
    statement = check(sql)
    bind = db.get_bind()
    dialect = bind.dialect.name
    wrapped = f"SELECT * FROM ({statement}) AS agent_query LIMIT {int(max_rows) + 1}"
    with bind.connect() as conn:
        transaction = conn.begin()
        try:
            if dialect == "postgresql":
                conn.execute(text("SET TRANSACTION READ ONLY"))
                conn.execute(text(f"SET LOCAL statement_timeout = {TIMEOUT_MS}"))
            elif dialect == "sqlite":
                conn.execute(text("PRAGMA query_only = ON"))
            result = conn.execute(text(wrapped))
            columns = list(result.keys())
            rows = [[_plain(value) for value in row] for row in result.fetchall()]
        except Exception as err:  # noqa: BLE001 — the model can rewrite a failing query
            raise QueryRefused(f"La base no pudo ejecutar la consulta: {str(err).splitlines()[0][:240]}")
        finally:
            if dialect == "sqlite":
                conn.execute(text("PRAGMA query_only = OFF"))
            transaction.rollback()      # read-only either way: nothing is ever committed
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    coverage = (f"{len(rows)} fila(s)" + (f", recortado a {max_rows}: afine la consulta"
                                          if truncated else ", resultado completo"))
    return {"columns": columns, "rows": rows, "row_count": len(rows),
            "truncated": truncated, "coverage": coverage, "sql": statement}
