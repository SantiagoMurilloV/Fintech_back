"""Reporting tools: saved charts, financial report PDFs, invoices and receipts."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Expense, Order, Report
from ...services import charts, finance, pdf, receipts, storage
from .. import blocks
from ..formatting import (
    amount, delta, integer, month_name, percent, period_name, short_date,
    status_label, usd_compact,
)
from ..registry import ToolResult, tool

CHART_SOURCES = {
    "weekly_revenue": "Ingresos por semana",
    "gateway_volume": "Volumen por gateway",
    "expense_categories": "Gastos por categoría",
    "order_status": "Órdenes por estado",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _series(db: Session, source: str, period: str) -> tuple[list[dict], str]:
    """Build a chart series from raw aggregates."""
    report = finance.report(db, period)
    if source == "weekly_revenue":
        return ([{"label": f"Sem {w['week']}", "value": w["usd"],
                  "muted": len(report["weekly"]) == 5 and w["week"] == 5}
                 for w in report["weekly"]], "Ingresos por semana (USD eq.)")
    if source == "gateway_volume":
        return ([{"label": g["name"], "value": g["usd"]} for g in report["gateways"]],
                "Volumen por gateway (USD eq.)")
    if source == "expense_categories":
        return ([{"label": c["name"], "value": c["usd"]} for c in report["expense_categories"]],
                "Gastos por categoría (USD eq.)")
    if source == "order_status":
        return ([{"label": status_label(code), "value": count}
                 for code, count in report["by_status"].items()], "Órdenes por estado")
    raise ValueError(f"source debe ser uno de: {', '.join(CHART_SOURCES)}")


@tool(
    name="generate_chart",
    description="Genera un gráfico a partir de los datos reales y lo guarda en Reportes. "
                "Fuentes: weekly_revenue, gateway_volume, expense_categories, order_status. "
                "Tipos: bar, line, pie, hbar.",
    parameters={
        "type": "object",
        "properties": {
            "source": {"type": "string", "enum": list(CHART_SOURCES)},
            "chart_type": {"type": "string", "enum": charts.CHART_TYPES, "default": "bar"},
            "period": {"type": "string", "pattern": r"^\d{4}-\d{2}$"},
            "title": {"type": "string"},
        },
        "required": ["source"],
    },
    mutates=True,
    examples=["Grafica los ingresos por semana", "Hazme un gráfico de torta por gateway"],
)
def generate_chart(db: Session, source: str, chart_type: str = "bar",
                   period: str | None = None, title: str | None = None) -> ToolResult:
    period = period or finance.current_period(db)
    series, default_title = _series(db, source, period)
    if not series:
        return ToolResult(blocks=[blocks.notice("No hay datos para graficar en ese periodo.", "info")],
                          data={}, summary="Serie vacía.")

    heading = title or f"{default_title} · {period_name(period)}"
    svg = charts.render(chart_type, series, heading)

    report = Report(title=heading, kind="chart", period=period,
                    spec={"source": source, "chart_type": chart_type, "series": series},
                    svg=svg, summary=f"Gráfico {chart_type} de {CHART_SOURCES[source]}.",
                    created_at=_now())
    db.add(report)
    db.commit()

    return ToolResult(
        blocks=[
            blocks.chart(svg, heading, chart_type),
            blocks.notice(f"Guardado en Reportes como «{heading}».", tone="success"),
        ],
        data={"report_id": report.id, "period": period},
        summary=f"Gráfico «{heading}» guardado (#{report.id}).",
    )


@tool(
    name="generate_financial_report",
    description="Genera el reporte financiero del periodo en PDF (KPIs, gráficos y tablas) y lo "
                "guarda en Reportes. Úsala para «genera el PDF del mes», «reporte ejecutivo».",
    parameters={
        "type": "object",
        "properties": {"period": {"type": "string", "pattern": r"^\d{4}-\d{2}$"}},
        "required": [],
    },
    mutates=True,
    examples=["Genera el reporte financiero en PDF", "Dame el informe ejecutivo de julio"],
)
def generate_financial_report(db: Session, period: str | None = None) -> ToolResult:
    period = period or finance.current_period(db)
    report = finance.report(db, period)
    d = report["deltas"]
    previous = month_name(report["prev_period"])

    kpis = [
        {"label": "Ingresos", "value": usd_compact(report["revenue_usd"]),
         "delta": f"{delta(d['revenue_pct'])} vs. {previous}"},
        {"label": "Gastos", "value": usd_compact(report["expenses_usd"]),
         "delta": f"{delta(d['expenses_pct'])} vs. {previous}"},
        {"label": "Margen", "value": percent(report["margin_pct"]),
         "delta": f"{delta(d['margin_pp'], ' pp')} vs. {previous}"},
        {"label": "Órdenes", "value": integer(report["orders_count"]),
         "delta": f"{delta(d['orders'], '')} vs. {previous}"},
    ]

    chart_sections = [
        {"title": "Ingresos por semana", "axis_label": "Semana",
         "series": [{"label": f"Semana {w['week']}", "value": w["usd"],
                     "display": usd_compact(w["usd"])} for w in report["weekly"]]},
        {"title": "Volumen por gateway", "axis_label": "Gateway",
         "series": [{"label": g["name"], "value": g["usd"],
                     "display": f"{usd_compact(g['usd'])} ({percent(g['pct'])})"}
                    for g in report["gateways"]]},
        {"title": "Gastos por categoría", "axis_label": "Categoría",
         "series": [{"label": c["name"], "value": c["usd"], "display": usd_compact(c["usd"])}
                    for c in report["expense_categories"]]},
    ]

    status_rows = [[status_label(code), integer(count),
                    percent(100 * count / report["orders_count"]) if report["orders_count"] else "0%"]
                   for code, count in report["by_status"].items()]

    notes = []
    if report["missing_receipts"]:
        notes.append(f"{len(report['missing_receipts'])} gastos sin comprobante: "
                     f"{', '.join(report['missing_receipts'])}.")
    if report["rejections_by_gateway"]:
        gateway, count = max(report["rejections_by_gateway"].items(), key=lambda i: i[1])
        notes.append(f"Los rechazos se concentran en {gateway} ({integer(count)} casos).")

    summary_text = (
        f"En {period_name(period)} los ingresos alcanzaron {usd_compact(report['revenue_usd'])} "
        f"({delta(d['revenue_pct'])} frente a {previous}), con gastos por "
        f"{usd_compact(report['expenses_usd'])} ({delta(d['expenses_pct'])}) y un margen operativo "
        f"de {percent(report['margin_pct'])}. Se procesaron {integer(report['orders_count'])} órdenes."
    )

    content = pdf.financial_report({
        "title": f"Reporte financiero · {period_name(period)}",
        "subtitle": f"Periodo {period} · comparado contra {previous}",
        "kpis": kpis,
        "summary": summary_text,
        "charts": [section for section in chart_sections if section["series"]],
        "tables": [{
            "title": "Órdenes por estado",
            "columns": ["Estado", "Órdenes", "Participación"],
            "rows": status_rows,
            "align_right": [1, 2],
        }],
        "notes": notes,
    })

    stored = storage.save(content, f"reporte_{period}.pdf", subfolder="reports")
    saved = Report(title=f"Reporte financiero · {period_name(period)}", kind="financial",
                   period=period, spec={"kpis": kpis}, pdf_url=stored["url"],
                   summary=summary_text, created_at=_now())
    db.add(saved)
    db.commit()

    return ToolResult(
        blocks=[
            blocks.text(summary_text),
            blocks.kpis([{**k, "positive": not k["delta"].startswith("-")} for k in kpis]),
            blocks.file(f"reporte_{period}.pdf", stored["url"], "pdf",
                        f"Reporte financiero de {period_name(period)}"),
        ],
        data={"report_id": saved.id, "url": stored["url"]},
        summary=f"Reporte financiero de {period_name(period)} generado.",
    )


CANDIDATES = 8


def _pick_order_blocks(db: Session) -> ToolResult:
    """No order given: show recent ones so the user can name one."""
    rows = list(db.scalars(select(Order).order_by(Order.date.desc(), Order.id.desc())
                           .limit(CANDIDATES)).all())
    if not rows:
        return ToolResult(blocks=[blocks.notice("Todavía no hay órdenes para facturar.", "info")],
                          data={}, summary="Sin órdenes.")
    return ToolResult(
        blocks=[
            blocks.notice("¿De cuál orden desea la factura? Dígame su ID, por ejemplo "
                          f"«genera la factura de la orden {rows[0].id}».", tone="info"),
            blocks.table(
                columns=[
                    {"key": "id", "label": "ID orden"},
                    {"key": "customer", "label": "Cliente"},
                    {"key": "amount", "label": "Monto", "align": "right"},
                    {"key": "date", "label": "Fecha", "align": "right"},
                ],
                rows=[{
                    "id": order.id, "customer": order.customer,
                    "amount": f"{amount(order.amount, order.currency)} {order.currency}",
                    "date": short_date(order.date),
                } for order in rows],
                caption="Órdenes más recientes",
            ),
        ],
        data={"candidates": [order.id for order in rows]},
        summary="Falta el ID de la orden.",
    )


@tool(
    name="generate_invoice",
    description="Genera la factura en PDF de una orden y la guarda en Reportes. Si no se indica "
                "order_id, muestra las órdenes recientes para que el usuario elija.",
    parameters={
        "type": "object",
        "properties": {"order_id": {"type": "string", "description": "Ej. ORD-4554"}},
        "required": [],
    },
    mutates=True,
    examples=["Genera la factura de la orden ORD-4554", "Genérame una factura"],
)
def generate_invoice(db: Session, order_id: str | None = None) -> ToolResult:
    if not order_id:
        return _pick_order_blocks(db)

    order = db.get(Order, order_id.strip().upper())
    if order is None:
        result = _pick_order_blocks(db)
        result.blocks.insert(0, blocks.notice(f"No encontré la orden {order_id}.", "warning"))
        return result

    usd = finance.usd_eq(order.amount, order.currency)
    content = pdf.invoice({
        "title": f"Factura {order.id}",
        "subtitle": f"Emitida el {order.date}",
        "number": order.id,
        "customer": order.customer,
        "date": order.date,
        "status": status_label(order.status),
        "gateway": order.gateway,
        "lines": [[f"Orden {order.id} — {order.customer}", order.currency,
                   amount(order.amount, order.currency), usd_compact(usd)]],
        "total": f"{amount(order.amount, order.currency)} {order.currency}",
        "notes": [f"Equivalente en dólares: {usd_compact(usd)}."],
    })

    stored = storage.save(content, f"factura_{order.id}.pdf", subfolder="invoices")
    saved = Report(title=f"Factura {order.id}", kind="invoice", period=order.date[:7],
                   spec={"order_id": order.id}, pdf_url=stored["url"],
                   summary=f"Factura de {order.customer}.", created_at=_now())
    db.add(saved)
    db.commit()

    return ToolResult(
        blocks=[
            blocks.notice(f"Factura de la orden {order.id} generada.", tone="success"),
            blocks.file(f"factura_{order.id}.pdf", stored["url"], "pdf",
                        f"{order.customer} · {amount(order.amount, order.currency)} {order.currency}"),
        ],
        data={"report_id": saved.id, "url": stored["url"]},
        summary=f"Factura {order.id} generada.",
    )


def _recent_expenses(db: Session) -> list[Expense]:
    return list(db.scalars(select(Expense).order_by(Expense.date.desc(), Expense.id.desc())
                           .limit(CANDIDATES)).all())


def _pick_expense_blocks(db: Session) -> ToolResult:
    """No expense given: show recent ones so the user can name one."""
    rows = _recent_expenses(db)
    if not rows:
        return ToolResult(blocks=[blocks.notice("Todavía no hay gastos registrados.", "info")],
                          data={}, summary="Sin gastos.")
    return ToolResult(
        blocks=[
            blocks.notice("¿De cuál gasto desea el comprobante? Dígame su número, por ejemplo "
                          f"«genera el comprobante del gasto {rows[0].id}».", tone="info"),
            blocks.table(
                columns=[
                    {"key": "id", "label": "#", "align": "right"},
                    {"key": "description", "label": "Concepto"},
                    {"key": "amount", "label": "Monto", "align": "right"},
                    {"key": "date", "label": "Fecha", "align": "right"},
                ],
                rows=[{
                    "id": str(expense.id), "description": expense.description,
                    "amount": f"{amount(expense.amount, expense.currency)} {expense.currency}",
                    "date": short_date(expense.date),
                } for expense in rows],
                caption="Gastos más recientes",
            ),
        ],
        data={"candidates": [expense.id for expense in rows]},
        summary="Falta el número del gasto.",
    )


@tool(
    name="generate_receipt",
    description="Genera el comprobante en PDF de un gasto (incluye el enlace al archivo cargado). "
                "Acepta expense_id, o latest=true para tomar el gasto más reciente. Sin ninguno de "
                "los dos, muestra los gastos recientes para elegir.",
    parameters={
        "type": "object",
        "properties": {
            "expense_id": {"type": "integer"},
            "latest": {"type": "boolean", "description": "Usar el gasto más reciente."},
        },
        "required": [],
    },
    mutates=True,
    examples=["Genera el comprobante del gasto 12", "Comprobante del último gasto"],
)
def generate_receipt(db: Session, expense_id: int | None = None,
                     latest: bool = False) -> ToolResult:
    if expense_id is None:
        if not latest:
            return _pick_expense_blocks(db)
        rows = _recent_expenses(db)
        if not rows:
            return ToolResult(blocks=[blocks.notice("Todavía no hay gastos registrados.", "info")],
                              data={}, summary="Sin gastos.")
        expense_id = rows[0].id

    expense = db.get(Expense, int(expense_id))
    if expense is None:
        result = _pick_expense_blocks(db)
        result.blocks.insert(0, blocks.notice(f"No encontré el gasto {expense_id}.", "warning"))
        return result

    generated = receipts.generate(db, expense)

    return ToolResult(
        blocks=[
            blocks.notice(
                f"Comprobante del gasto #{expense.id} generado"
                + (" y adjuntado en la columna Comprobante." if generated["attached"] else "."),
                tone="success"),
            blocks.file(generated["filename"], generated["url"], "pdf", expense.description),
        ],
        data={"report_id": generated["report_id"], "url": generated["url"]},
        summary=f"Comprobante {expense.id} generado.",
    )


@tool(
    name="list_saved_reports",
    description="Lista los reportes ya generados (gráficos, PDFs, facturas y comprobantes).",
    parameters={
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
        "required": [],
    },
    examples=["¿Qué reportes tengo guardados?"],
)
def list_saved_reports(db: Session, limit: int = 10) -> ToolResult:
    limit = max(1, min(int(limit), 50))
    items = list(db.scalars(
        select(Report).order_by(Report.created_at.desc(), Report.id.desc()).limit(limit)).all())
    if not items:
        return ToolResult(blocks=[blocks.notice("Todavía no hay reportes guardados.", "info")],
                          data={"count": 0}, summary="Sin reportes.")

    rows = [{
        "title": report.title,
        "kind": {"label": report.kind, "tone": "neutral"},
        "period": period_name(report.period) if report.period else "—",
        "created": report.created_at[:10],
        "file": ({"label": "Abrir PDF", "url": report.pdf_url} if report.pdf_url else "gráfico"),
    } for report in items]

    return ToolResult(
        blocks=[blocks.table(
            columns=[
                {"key": "title", "label": "Reporte"},
                {"key": "kind", "label": "Tipo", "kind": "badge"},
                {"key": "period", "label": "Periodo"},
                {"key": "created", "label": "Creado", "align": "right"},
                {"key": "file", "label": "Archivo"},
            ],
            rows=rows,
            caption=f"{len(rows)} reportes guardados",
        )],
        data={"count": len(rows)},
        summary=f"{len(rows)} reportes listados.",
    )
