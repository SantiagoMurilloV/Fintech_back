"""The income statement as blocks, and saving any report as a PDF.

    income_statement(from, to, view)   the statement of a range; view = full,
                                       vertical (each line as % of its base) or
                                       horizontal (change vs another range)
    save_pdf(report)                   the blocks of any report into Reportes

What a daily close or a dashboard contains is not code: it is in the agent's
skills (agent/skills), and the agent builds them from queries with `present`.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..agent import blocks
from ..agent import formatting as fmt
from ..models import Report
from . import pdf, statement, storage



class ReportError(ValueError):
    pass


# ------------------------------------------------------------------ helpers

def _pct(value) -> str:
    return "—" if value is None else fmt.percent(value)


def _cost_gap_notice(months: list[str]) -> dict:
    names = ", ".join(fmt.period_name(m) for m in months)
    return blocks.notice(f"Sin comisiones ni gastos cargados para {names}: el resultado neto de "
                         "esos meses está sobreestimado hasta que se carguen en Resultados.",
                         tone="warning")


# ---------------------------------------------------------- income statement

VIEWS = ("full", "vertical", "horizontal")


def income_statement(db: Session, first: str | None = None, last: str | None = None,
                     view: str = "full", compare: str | None = "prev") -> dict:
    if view not in VIEWS:
        raise ReportError("La vista debe ser full, vertical u horizontal.")
    try:
        result = statement.compute_range(db, first, last,
                                         None if view == "vertical" else compare)
    except statement.StatementError as err:
        raise ReportError(str(err)) from None
    first, last = result["first"], result["last"]
    span = fmt.period_name(first) if first == last else \
        f"{fmt.period_name(first)} – {fmt.period_name(last)}"
    against = result["compare"]
    against_label = None if not against else (
        fmt.period_name(against[0]) if against[0] == against[1]
        else f"{fmt.period_name(against[0])} – {fmt.period_name(against[1])}")
    names = {"full": "Estado de resultados", "vertical": "Análisis vertical",
             "horizontal": "Análisis horizontal"}
    title = f"{names[view]} · {span}"
    bases = {row["key"]: row["name"] for row in result["rows"]}

    columns = [{"key": "cuenta", "label": "Cuenta"},
               {"key": "valor", "label": span if first != last else "Valor", "align": "right"}]
    if view in ("full", "vertical"):
        columns.append({"key": "vertical", "label": "% vertical", "align": "right"})
    if view == "vertical":
        columns.append({"key": "base", "label": "Sobre"})
    if view in ("full", "horizontal") and against:
        columns += [{"key": "anterior", "label": against_label, "align": "right"},
                    {"key": "variacion", "label": "Variación", "align": "right"},
                    {"key": "variacion_pct", "label": "Var. %", "align": "right"}]
    rows = []
    for line in result["rows"]:
        if line["kind"] == "group" and line["value"] is None:
            label = line["name"]
        else:
            label = (" " * 4 * line["level"]) + line["name"]
        rows.append({
            "cuenta": label,
            "valor": statement.display(line["value"], line["unit"]),
            "vertical": _pct(line["vertical_pct"]),
            "base": bases.get(line["percent_of"] or "", ""),
            "anterior": statement.display(line["previous"], line["unit"]),
            "variacion": statement.display(line["variation"], line["unit"]),
            "variacion_pct": _pct(line["variation_pct"]),
        })
    out = [blocks.heading(title, f"Comparado con {against_label}" if against_label else
                          "Cada línea como porcentaje de su cuenta base." if view == "vertical" else ""),
           blocks.table(columns=columns, rows=rows, wide=True,
                        caption=f"{len(result['months_with_data'])} de {result['months']} mes(es) "
                                f"con cifras · totales calculados con las fórmulas del plan")]
    gaps = statement.months_missing_costs(db, first, last)
    if gaps:
        out.insert(1, _cost_gap_notice(gaps))
    if view == "horizontal" and against and not result["compare_has_data"]:
        out.insert(1, blocks.notice(f"No hay cifras para {against_label}: no hay con qué comparar.",
                                    tone="warning"))
    data = {line["key"]: {"nombre": line["name"], "valor": line["value"],
                          "vertical_pct": line["vertical_pct"], "anterior": line["previous"],
                          "variacion": line["variation"], "variacion_pct": line["variation_pct"]}
            for line in result["rows"] if line["value"] is not None}
    return {"title": title, "subtitle": span, "period": last, "blocks": out,
            "data": {"desde": first, "hasta": last, "comparado_con": against,
                     "meses_con_cifras": result["months_with_data"],
                     "meses_sin_gastos_cargados": gaps, "cuentas": data}}


# ---------------------------------------------------------------------- pdf

def save_pdf(db: Session, report: dict, kind: str = "financial") -> dict:
    """The same blocks as a PDF in Reportes."""
    stamp = datetime.now()
    content = pdf.analysis_report(report["title"], report.get("subtitle", ""), report["blocks"])
    slug = "".join(c if c.isalnum() else "_" for c in report["title"].lower())[:60]
    stored = storage.save(content, f"{slug}_{stamp.strftime('%Y%m%d_%H%M%S')}.pdf",
                          subfolder="reports")
    saved = Report(title=report["title"], kind=kind, period=report.get("period"),
                   spec={"source": "financial_reports"}, pdf_url=stored["url"],
                   summary=report.get("subtitle"), created_at=stamp.isoformat(timespec="seconds"))
    db.add(saved)
    db.commit()
    return {"id": saved.id, "title": saved.title, "pdf_url": saved.pdf_url}
