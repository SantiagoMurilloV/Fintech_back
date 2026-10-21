"""get_income_statement: the month's income statement, computed by the engine.

The same numbers as Resultados and its PDF (services/statement.py): every
total is the account's recipe evaluated over that month's figures, so the
chat never rebuilds the statement by hand.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ...services import statement
from ..registry import ToolResult, tool


@tool(
    name="get_income_statement",
    description=("Estado de resultados de un mes (YYYY-MM): ingresos, utilidad bruta y "
                 "operacional, costos, gastos y resultado neto según el plan de cuentas, con "
                 "la comparación contra otro período. Úsela para utilidad neta, EBITDA, costos "
                 "fijos o cualquier cuenta del estado de resultados."),
    parameters={
        "type": "object",
        "properties": {
            "period": {"type": "string", "description": "Mes YYYY-MM; vacío = el último con cifras."},
            "compare": {"type": "string",
                        "description": "«prev» (mes anterior, por defecto), «year» (mismo mes del "
                                       "año anterior), «none» o un mes YYYY-MM."},
        },
        "required": [],
    },
    examples=["¿Cuál fue la utilidad neta de enero 2025?", "Estado de resultados de mayo 2026"],
)
def get_income_statement(db: Session, period: str = "", compare: str = "prev") -> ToolResult:
    try:
        result = statement.compute(db, period or None, None if compare == "none" else compare)
    except statement.StatementError as err:
        return ToolResult(blocks=[], data={"error": str(err)})
    lines = {row["key"]: {"name": row["name"], "value": row.get("value"),
                          "previous": row.get("previous"), "variation_pct": row.get("variation_pct"),
                          "status": row.get("status")}
             for row in result["rows"] if row.get("value") is not None}
    return ToolResult(blocks=statement.to_blocks(result),
                      data={"period": result["period"], "compare": result["compare"],
                            "lines": lines},
                      summary=f"Estado de resultados de {result['period']}.")
