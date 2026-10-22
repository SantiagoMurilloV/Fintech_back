"""The finance team's recurring deliverables as chat tools.

Each tool calls services/financial_reports, which computes everything from
the database and returns blocks; `save_pdf` stores the same blocks as a PDF
in Reportes. The model only picks the tool and its dates.
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
KINDS = ["operations", "finance", "management"]


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
    name="daily_close_report",
    description=("Reporte de cierre diario de una fecha (AAAA-MM-DD): operaciones, volumen "
                 "off-ramp/on-ramp, utilidad y margen del día contra el día anterior con "
                 "operaciones, acumulado del mes, detalle por proveedor y clientes, USDT/USD y "
                 "TRM del día, y gastos del día."),
    parameters={"type": "object", "properties": {
        "date": {"type": "string", "description": "Día AAAA-MM-DD; vacío = hoy."},
        "save_pdf": _PDF}, "required": []},
    examples=["Prepara el cierre diario del 15 de octubre de 2025"],
)
def daily_close_report(db: Session, date: str = "", save_pdf: bool = False) -> ToolResult:
    try:
        return _result(reports.daily_close(db, date or None), save_pdf, db)
    except reports.ReportError as err:
        return _error(err)


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


@tool(
    name="department_dashboard",
    description=("Tablero para un área, para un mes o rango de meses (AAAA-MM): «operations» "
                 "(operaciones, volumen, ticket, clientes, proveedores y su costo, activos), "
                 "«finance» (utilidad bruta, operacional y neta, márgenes, costos, estado de "
                 "resultados y control contra la utilidad OTC) o «management» (volumen, utilidad, "
                 "resultado neto, acumulado del año, concentración, tendencia de 12 meses y "
                 "alertas). Compara con el período anterior de igual duración."),
    parameters={"type": "object", "properties": {
        "kind": {"type": "string", "enum": KINDS},
        "period_from": {"type": "string", "description": "Primer mes AAAA-MM; vacío = el último con datos."},
        "period_to": {"type": "string", "description": "Último mes AAAA-MM."},
        "save_pdf": _PDF}, "required": ["kind"]},
    examples=["Prepara el tablero de operaciones de octubre 2025",
              "Dashboard de gerencia del último trimestre de 2025"],
)
def department_dashboard(db: Session, kind: str = "management", period_from: str = "",
                         period_to: str = "", save_pdf: bool = False) -> ToolResult:
    try:
        return _result(reports.dashboard(db, kind, period_from or None, period_to or None),
                       save_pdf, db, "analysis")
    except reports.ReportError as err:
        return _error(err)
