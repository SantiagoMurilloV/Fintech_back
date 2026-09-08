"""Analytics tools: period summary, KPI dashboard and anomaly detection.

All arithmetic comes from services/finance.py; these tools only shape the
results into blocks.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ...services import finance
from .. import blocks
from ..formatting import (
    delta, integer, month_name, percent, period_name, status_label, usd_compact,
)
from ..registry import ToolResult, tool

PERIOD_PARAM = {
    "type": "string",
    "description": "Periodo YYYY-MM. Si se omite, se usa el periodo activo.",
    "pattern": r"^\d{4}-\d{2}$",
}

# Thresholds that make anomaly detection reproducible instead of a judgement call.
ANOMALY_RULES = {
    "rejection_rate_pct": 5.0,       # share of rejected orders that raises a flag
    "gateway_concentration_pct": 50.0,  # share of rejections in a single gateway
    "expense_growth_pct": 20.0,      # month-over-month expense growth
    "category_growth_pct": 30.0,     # per-category growth
}


def _resolve_period(db: Session, period: str | None) -> str:
    return period if period else finance.current_period(db)


def _kpi_items(report: dict) -> list[dict]:
    d = report["deltas"]
    caption = f"vs. {month_name(report['prev_period'])}"
    return [
        {"label": "Ingresos", "value": usd_compact(report["revenue_usd"]),
         "delta": delta(d["revenue_pct"]), "positive": (d["revenue_pct"] or 0) >= 0, "caption": caption},
        {"label": "Gastos", "value": usd_compact(report["expenses_usd"]),
         "delta": delta(d["expenses_pct"]), "positive": (d["expenses_pct"] or 0) <= 0, "caption": caption},
        {"label": "Margen operativo", "value": percent(report["margin_pct"]),
         "delta": delta(d["margin_pp"], " pp"), "positive": (d["margin_pp"] or 0) >= 0, "caption": caption},
        {"label": "Órdenes", "value": integer(report["orders_count"]),
         "delta": delta(d["orders"], ""), "positive": (d["orders"] or 0) >= 0, "caption": caption},
    ]


@tool(
    name="get_period_summary",
    description="Resumen financiero de un mes: ingresos, gastos, margen, órdenes y variación "
                "contra el mes anterior. Úsala para «cómo cerró el mes», «resumen», «ingresos».",
    parameters={"type": "object", "properties": {"period": PERIOD_PARAM}, "required": []},
    examples=["¿Cómo cerró el mes?", "Resumen de julio", "¿Cuánto facturamos?"],
)
def get_period_summary(db: Session, period: str | None = None) -> ToolResult:
    period = _resolve_period(db, period)
    report = finance.report(db, period)

    status_rows = [
        {"status": status_label(code), "count": integer(count),
         "share": percent(100 * count / report["orders_count"]) if report["orders_count"] else "0%"}
        for code, count in report["by_status"].items()
    ]

    result_blocks = [
        blocks.text(f"Resumen de {period_name(period)}:"),
        blocks.kpis(_kpi_items(report)),
        blocks.table(
            columns=[
                {"key": "status", "label": "Estado"},
                {"key": "count", "label": "Órdenes", "align": "right"},
                {"key": "share", "label": "Participación", "align": "right"},
            ],
            rows=status_rows,
            caption=f"Distribución de {integer(report['orders_count'])} órdenes",
        ),
    ]
    if report["gateways"]:
        leader = report["gateways"][0]
        result_blocks.append(blocks.text(
            f"El mayor volumen vino de {leader['name']} ({percent(leader['pct'])})."))

    return ToolResult(blocks=result_blocks, data=report,
                      summary=f"Resumen de {period_name(period)} generado.")


@tool(
    name="get_kpi_dashboard",
    description="Tablero de KPIs con gráficos: ingresos por semana, volumen por gateway y "
                "gastos por categoría. Úsala para «dashboard», «tablero», «análisis de KPIs».",
    parameters={"type": "object", "properties": {"period": PERIOD_PARAM}, "required": []},
    examples=["Muéstrame el dashboard", "Análisis de KPIs del mes"],
)
def get_kpi_dashboard(db: Session, period: str | None = None) -> ToolResult:
    from ...services import charts

    period = _resolve_period(db, period)
    report = finance.report(db, period)

    weekly = [{"label": f"Sem {w['week']}", "value": w["usd"],
               "muted": len(report["weekly"]) == 5 and w["week"] == 5}
              for w in report["weekly"]]
    gateways = [{"label": g["name"], "value": g["usd"]} for g in report["gateways"]]
    categories = [{"label": c["name"], "value": c["usd"]} for c in report["expense_categories"]]

    result_blocks = [
        blocks.text(f"Tablero de {period_name(period)}:"),
        blocks.kpis(_kpi_items(report)),
    ]
    if weekly:
        result_blocks.append(blocks.chart(
            charts.render("bar", weekly, "Ingresos por semana (USD eq.)"),
            "Ingresos por semana", "bar"))
    if gateways:
        result_blocks.append(blocks.chart(
            charts.render("pie", gateways, "Volumen por gateway (USD eq.)"),
            "Volumen por gateway", "pie"))
    if categories:
        result_blocks.append(blocks.chart(
            charts.render("hbar", categories, "Gastos por categoría (USD eq.)"),
            "Gastos por categoría", "hbar"))

    return ToolResult(blocks=result_blocks, data=report,
                      summary=f"Tablero de {period_name(period)} generado.")


@tool(
    name="detect_anomalies",
    description="Revisa el periodo con reglas fijas y devuelve alertas: tasa de rechazo alta, "
                "concentración de rechazos en un gateway, crecimiento de gastos y gastos sin "
                "comprobante. Úsala para «alertas», «anomalías», «qué anda mal».",
    parameters={"type": "object", "properties": {"period": PERIOD_PARAM}, "required": []},
    examples=["¿Hay alertas o anomalías?", "¿Qué está raro este mes?"],
)
def detect_anomalies(db: Session, period: str | None = None) -> ToolResult:
    period = _resolve_period(db, period)
    report = finance.report(db, period)
    findings: list[dict] = []

    total_orders = report["orders_count"] or 1
    rejected = report["by_status"].get("rejected", 0)
    rejection_rate = 100 * rejected / total_orders
    if rejection_rate >= ANOMALY_RULES["rejection_rate_pct"]:
        findings.append({
            "title": f"Tasa de rechazo en {percent(rejection_rate)}",
            "detail": f"{integer(rejected)} de {integer(total_orders)} órdenes "
                      f"(umbral: {percent(ANOMALY_RULES['rejection_rate_pct'])}).",
            "tone": "danger",
        })

    if report["rejections_by_gateway"]:
        gateway, count = max(report["rejections_by_gateway"].items(), key=lambda item: item[1])
        concentration = 100 * count / (rejected or 1)
        if concentration >= ANOMALY_RULES["gateway_concentration_pct"] and rejected:
            findings.append({
                "title": f"Rechazos concentrados en {gateway}",
                "detail": f"{integer(count)} de {integer(rejected)} rechazos "
                          f"({percent(concentration)} del total).",
                "tone": "warning",
            })

    growth = report["deltas"]["expenses_pct"]
    if growth is not None and growth >= ANOMALY_RULES["expense_growth_pct"]:
        findings.append({
            "title": f"Gastos crecieron {delta(growth)}",
            "detail": f"{usd_compact(report['expenses_usd'])} frente a "
                      f"{month_name(report['prev_period'])} "
                      f"(umbral: {percent(ANOMALY_RULES['expense_growth_pct'])}).",
            "tone": "warning",
        })

    # Per-category growth against the previous month.
    previous = finance.month_summary(db, finance.prev_period(period))
    previous_by_category = {c["name"]: c["usd"] for c in previous["expense_categories"]}
    for category in report["expense_categories"]:
        before = previous_by_category.get(category["name"])
        if not before:
            continue
        change = 100 * (category["usd"] - before) / before
        if change >= ANOMALY_RULES["category_growth_pct"]:
            findings.append({
                "title": f"{category['name']} creció {delta(change)}",
                "detail": f"{usd_compact(category['usd'])} vs. {usd_compact(before)} el mes anterior.",
                "tone": "warning",
            })

    if report["missing_receipts"]:
        findings.append({
            "title": f"{len(report['missing_receipts'])} gastos sin comprobante",
            "detail": ", ".join(report["missing_receipts"]),
            "tone": "warning",
        })

    if not findings:
        result_blocks = [blocks.notice(
            f"Sin alertas en {period_name(period)}: ningún indicador superó los umbrales definidos.",
            tone="success")]
    else:
        result_blocks = [
            blocks.text(f"Detecté {len(findings)} alertas en {period_name(period)}:"),
            blocks.listing(findings, title="Alertas"),
        ]

    return ToolResult(blocks=result_blocks,
                      data={"period": period, "findings": findings, "rules": ANOMALY_RULES},
                      summary=f"{len(findings)} alertas en {period_name(period)}.")
