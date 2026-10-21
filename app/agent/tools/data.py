"""query_database: the agent reads any fact the catalog exposes, with SQL it writes.

The general tool behind questions no fixed tool covers: a coin's price on a
date, a comparison between periods, the OTC profit of a client. The model
writes the SQL; agent/data/query.py checks it (one read statement over
catalog tables), runs it read-only with a time limit and caps the rows. The
database does the arithmetic — sums, differences, percentages — so every
figure in the answer comes from the data, never from the model.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .. import blocks
from ..data import query
from ..registry import ToolResult, tool

SHOWN_ROWS = 50


@tool(
    name="query_database",
    description=(
        "Lee la base con una consulta SQL de solo lectura (un único SELECT). Úsela cuando "
        "ninguna otra herramienta responda la pregunta: precios de monedas y su historia "
        "(rate_snapshots), operaciones OTC y su utilidad (orders.extra), gastos, cifras del "
        "plan de cuentas (account_values) o comparaciones entre períodos. Haga los cálculos "
        "(sumas, diferencias, porcentajes) DENTRO del SQL. Las tablas y columnas están en el "
        "catálogo del sistema."),
    parameters={
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "Un único SELECT sobre tablas del catálogo."},
            "purpose": {"type": "string",
                        "description": "Qué responde esta consulta, en una frase en español."},
        },
        "required": ["sql"],
    },
    examples=["¿A cuánto está el USDT hoy según TradingView?",
              "¿Cuál fue la utilidad OTC de octubre de 2025?"],
)
def query_database(db: Session, sql: str = "", purpose: str = "") -> ToolResult:
    try:
        result = query.run(db, sql)
    except query.QueryRefused as err:
        return ToolResult(blocks=[blocks.notice(str(err), tone="warning")],
                          data={"error": str(err), "sql": sql})
    rows = [dict(zip(result["columns"], row)) for row in result["rows"][:SHOWN_ROWS]]
    table = blocks.table(
        columns=[{"key": column, "label": column} for column in result["columns"]],
        rows=rows, caption=f"{purpose + ' · ' if purpose else ''}{result['coverage']}",
        wide=len(result["columns"]) > 4)
    return ToolResult(blocks=[table], data=result,
                      summary=f"{purpose or 'Consulta'}: {result['coverage']}.")
