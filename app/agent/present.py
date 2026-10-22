"""`present`: the agent lays out a report; the server formats it and checks it.

The agent decides WHAT a report shows (which KPIs, charts, tables, notes),
following the skills it loaded. The server decides HOW (Colombian number
format, the panel's charts) and whether it may be shown at all: every number
in the layout must have come back from a tool in this same answer — a query
result or the income statement. A number that did not (a sum, a percentage or
a difference the model worked out itself) is refused, and the model is told
which ones to query first. That keeps the house rule: figures come from the
data, never from the model.
"""
from __future__ import annotations

import re

from . import blocks
from . import formatting as fmt
from ..services import charts

UNITS = ("usd", "pct", "bps", "count", "number", "rate")
CHART_TYPES = ("bar", "line", "hbar", "pie")
WIDE = 1120
_NUMBER = re.compile(r"-?\d[\d.,]*\d|-?\d")

SPEC = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string", "description": "Período y contra qué se compara."},
        "kpis": {"type": "array", "items": {"type": "object", "properties": {
            "label": {"type": "string"}, "value": {"type": "number"},
            "unit": {"type": "string", "enum": list(UNITS)},
            "note": {"type": "string", "description": "Texto corto bajo el valor (p. ej. la variación)."},
            "tone": {"type": "string", "enum": ["neutral", "success", "warning", "danger"]}},
            "required": ["label", "value", "unit"]}},
        "charts": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "type": {"type": "string", "enum": list(CHART_TYPES)},
            "labels": {"type": "array", "items": {"type": "string"}},
            "values": {"type": "array", "items": {"type": "number"}},
            "unit": {"type": "string", "enum": list(UNITS)}},
            "required": ["title", "type", "labels", "values"]}},
        "tables": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"},
            "columns": {"type": "array", "items": {"type": "string"}},
            "units": {"type": "array", "items": {"type": ["string", "null"]},
                      "description": "Unidad de cada columna numérica (usd, pct…) o null."},
            "rows": {"type": "array", "items": {"type": "array"}}},
            "required": ["columns", "rows"]}},
        "notes": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"},
            "tone": {"type": "string", "enum": ["info", "warning", "danger", "success"]}},
            "required": ["text"]}},
        "save_pdf": {"type": "boolean", "description": "true para guardar además el PDF en Reportes."},
    },
    "required": ["title"],
}

DESCRIPTION = (
    "Arma un reporte o tablero en pantalla (y en PDF si save_pdf) con KPIs, gráficos, tablas y "
    "notas. Úselo cuando el pedido es un reporte, un cierre o un tablero. Pase los números "
    "crudos (sin formato) tal como vinieron de query_database o get_income_statement; el panel "
    "los formatea. Un número que no vino de una consulta en esta respuesta es rechazado: "
    "calcule sumas, diferencias y porcentajes en el SQL.")


def _readings(token: str) -> list[float]:
    """A written number read both ways: «1.234,56» and «1,234.56»; «0.99917» as is."""
    options = []
    for reading in (token.replace(".", "").replace(",", "."), token.replace(",", ""),
                    token if token.count(".") == 1 and "," not in token else None):
        try:
            if reading is not None and float(reading) not in options:
                options.append(float(reading))
        except ValueError:
            pass
    return options


def numbers_in(value) -> list[float]:
    """Every number in a value: raw numbers, and numbers written in text in either
    convention («259.618,06», «259,618.06», «0.99917»)."""
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, dict):
        return [n for v in value.values() for n in numbers_in(v)]
    if isinstance(value, (list, tuple)):
        return [n for v in value for n in numbers_in(v)]
    return [n for token in _NUMBER.findall(str(value)) for n in _readings(token)]


def _backed(number: float, known: list[float]) -> bool:
    if abs(number) < 1 and number == int(number):          # 0
        return True
    tolerance = max(0.011, abs(number) * 1e-6)
    return any(abs(number - k) <= tolerance for k in known)


def _trivial(number: float) -> bool:
    """Days, months, years and counts of things in a sentence need no source."""
    return (number == int(number) and (0 <= number <= 31 or 1990 <= number <= 2100))


def unbacked(spec: dict, known: list[float]) -> list[float]:
    """Numbers in the layout that no tool returned in this answer."""
    figures = []
    for kpi in spec.get("kpis") or []:
        figures.append(kpi.get("value"))
    for chart in spec.get("charts") or []:
        figures.extend(chart.get("values") or [])
    for table in spec.get("tables") or []:
        for row in table.get("rows") or []:
            figures.extend(cell for cell in row if isinstance(cell, (int, float))
                           and not isinstance(cell, bool))
    missing = []
    for note in spec.get("notes") or []:
        for token in _NUMBER.findall(note.get("text", "")):
            options = _readings(token)
            if options and not any(_trivial(n) or _backed(n, known) for n in options) \
                    and options[0] not in missing:
                missing.append(options[0])
    for figure in figures:
        if isinstance(figure, (int, float)) and not _backed(float(figure), known) \
                and figure not in missing:
            missing.append(figure)
    return missing


def show(value, unit: str | None) -> str:
    """One raw number in the panel's format."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—" if value is None else str(value)
    if unit == "usd":
        return f"USD {fmt.thousands(value, 2)}"
    if unit == "pct":
        return fmt.percent(value)
    if unit == "bps":
        return f"{fmt.thousands(value, 1)} pb"
    if unit == "count":
        return fmt.integer(int(round(value)))
    if unit == "rate":
        return fmt.thousands(value, 5 if abs(value) < 10 else 2)
    return fmt.thousands(value, 2)


def build(spec: dict) -> list[dict]:
    """The layout as the panel's blocks."""
    out = [blocks.heading(spec.get("title") or "Reporte", spec.get("subtitle") or "")]
    kpis = spec.get("kpis") or []
    for start in range(0, len(kpis), 4):
        out.append(blocks.kpis([{"label": k.get("label", ""),
                                 "value": show(k.get("value"), k.get("unit")),
                                 "delta": k.get("note") or "", "tone": k.get("tone") or "neutral"}
                                for k in kpis[start:start + 4]]))
    for chart in spec.get("charts") or []:
        labels, values = chart.get("labels") or [], chart.get("values") or []
        series = [{"label": str(label)[:24], "value": float(value),
                   "display": show(value, chart.get("unit")).replace("USD ", "")}
                  for label, value in zip(labels, values) if isinstance(value, (int, float))]
        if len(series) < 2:
            continue
        kind = chart.get("type") if chart.get("type") in CHART_TYPES else "bar"
        wide = kind in ("bar", "line")
        block = blocks.chart(charts.render(kind, series, width=WIDE if wide else 720,
                                           height=340 if wide else 320),
                             title=chart.get("title") or "", chart_type=kind, series=series)
        block["wide"] = wide
        out.append(block)
    for table in spec.get("tables") or []:
        columns = [str(c) for c in table.get("columns") or []]
        units = list(table.get("units") or []) + [None] * len(columns)
        keys = [f"c{i}" for i in range(len(columns))]
        rows = [{key: show(cell, units[i]) if isinstance(cell, (int, float)) else
                 ("—" if cell is None else str(cell))
                 for i, (key, cell) in enumerate(zip(keys, row))}
                for row in table.get("rows") or []]
        out.append(blocks.table(
            columns=[{"key": key, "label": label,
                      "align": "right" if units[i] else None}
                     for i, (key, label) in enumerate(zip(keys, columns))],
            rows=rows, caption=table.get("title"), wide=len(columns) > 4))
    for note in spec.get("notes") or []:
        out.append(blocks.notice(note.get("text", ""), tone=note.get("tone") or "info"))
    return out
