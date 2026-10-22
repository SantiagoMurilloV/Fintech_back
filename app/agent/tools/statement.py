"""get_income_statement: the company's income statement over any range of months.

A generic building block, not a report: it applies the client's own plan of
accounts (its formulas) to the months asked for, with vertical and horizontal
analysis, so the agent never rebuilds a statement with SQL. What to do with it
(a statement, a vertical or horizontal analysis, part of a dashboard) is in
the skills the agent loads.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ...services import financial_reports as reports
from .. import blocks
from ..registry import ToolResult, tool

_PDF = {"type": "boolean", "description": "true para guardar además el PDF en Reportes."}
# Declared here, not read from services/financial_reports at import time: that
# module imports the agent package, which imports these tools.
VIEWS = ["full", "vertical", "horizontal"]


def _result(report: dict, save_pdf: bool, db: Session, kind: str = "financial") -> ToolResult:
    out = list(report["blocks"])
    data = dict(report["data"])
    if save_pdf:
        saved = reports.save_pdf(db, report, kind)
        out.append(blocks.file(f"{saved['title']}.pdf", saved["pdf_url"], "pdf",
                               "Guardado en Reportes."))
        data["pdf"] = saved
    return ToolResult(blocks=out, data=data, summary=report["title"])


def _error(err: Exception) -> ToolResult:
    return ToolResult(blocks=[blocks.notice(str(err), tone="warning")], data={"error": str(err)})


@tool(
    name="get_income_statement",
    description=("Estado de resultados de un mes o de un rango de meses (desde–hasta, AAAA-MM), "
                 "según el plan de cuentas: los insumos se suman en el rango y los totales y "
                 "razones se recalculan. view: «full» (con % vertical y comparación), «vertical» "
                 "(cada cuenta como % de su base) u «horizontal» (variación contra otro período). "
                 "compare: «prev» (rango anterior de igual duración), «year» (mismos meses del año "
                 "anterior), «none», «AAAA-MM» o «AAAA-MM:AAAA-MM»."),
    parameters={"type": "object", "properties": {
        "period_from": {"type": "string", "description": "Primer mes AAAA-MM."},
        "period_to": {"type": "string", "description": "Último mes AAAA-MM (igual al primero para un mes)."},
        "view": {"type": "string", "enum": VIEWS},
        "compare": {"type": "string"},
        "save_pdf": _PDF}, "required": []},
    examples=["Estado de resultados de enero a marzo de 2025", "Análisis vertical de mayo 2025",
              "Análisis horizontal del primer trimestre 2025 contra 2024"],
)
def get_income_statement(db: Session, period_from: str = "", period_to: str = "",
                         view: str = "full", compare: str = "prev", save_pdf: bool = False,
                         period: str = "") -> ToolResult:
    try:
        report = reports.income_statement(db, period_from or period or None,
                                          period_to or period or None, view or "full",
                                          compare or "prev")
        return _result(report, save_pdf, db, "statement")
    except reports.ReportError as err:
        return _error(err)
