"""Analysis derived from the data that is actually there.

The old report assumed the demo's shape (July weeks, Wompi/ePayco, month
deltas). Real data has whatever shape the source gave it, so this engine works
the other way around: it profiles the dataset, discovers which dimensions
carry information — including the dynamic columns a feed created — and builds
the analysis FROM that.

The output is ordered like a statistics report and every section is titled:

    1. Resumen general          the headline numbers
    2. Estadística descriptiva  central tendency and dispersion of the amounts
    3. Lecturas e inferencias   outliers (IQR), concentration (Pareto), CV,
                                data-quality observations
    4. Distribuciones           one titled chart per informative dimension

Every figure is computed here, deterministically. The model contributes at
most the opening paragraph, constrained to the computed numbers — without it
the analysis is complete, just without prose.

Money rules hold: currencies without a configured USD rate are never
converted; they are aggregated apart, exactly, in their own currency.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from statistics import mean, median, quantiles, stdev

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent import blocks
from ..agent.formatting import amount as amount_text
from ..agent.formatting import integer, percent, status_label, usd_compact
from ..models import CustomColumn, Expense, Order, Setting
from . import charts, finance, llm

MAX_ROWS = 5000
MAX_SLICES = 6          # a pie beyond this is unreadable; hbar takes over
MAX_CATEGORICAL = 15    # more distinct values than this is an id, not a category
CONCENTRATION = 0.40    # share that makes a single value worth calling out
HIGH_CV = 100.0         # coefficient of variation (%) that reads as high dispersion

NARRATOR_PROMPT = """You write the opening paragraph of a financial analysis.

Hard rules:
- Use ONLY figures present in the JSON. Never add, round or extrapolate.
- Two or three sentences, formal Colombian Spanish — address the reader as
  «usted», never «vos» or «tú» —, no markdown. Plain, direct, useful.
- Lead with what matters most (totals, concentration, anything unusual).
- If the data is trivial or empty, say so plainly."""

INTERPRETER_PROMPT = """You interpret the computed statistics of a financial
dataset for a business reader. Return ONLY JSON:
{"puntos": [{"titulo": "...", "detalle": "..."}, ...]}  (3 or 4 points)

Hard rules:
- Base every point ONLY on figures present in the JSON; quote them as given.
  Never invent, extrapolate or estimate a number.
- Each point explains what a figure MEANS in practice, in plain, formal
  Colombian Spanish (address the reader as «usted», never «vos» or «tú»): what
  the business depends on, what distorts an average, what deserves a look.
  Naming the number without saying why it matters is not an interpretation.
- No investment advice, no predictions, no imperatives beyond suggesting to
  review something. Titles of 4-8 words; details of 1-2 sentences."""


# ------------------------------------------------------------------- cache
# Building the analysis costs two LLM calls plus every chart, so it runs once
# and the result lives in the settings table. Reopening the screen serves the
# stored copy; only an explicit refresh rebuilds. The fingerprint says whether
# the data moved since — the frontend polls it to raise the "new data" alert
# no matter which writer (endpoint sync, sheet, manual edit) moved it.

CACHE_KEY = "insights.cache"


def fingerprint(db: Session) -> str:
    """Signature of everything the analysis reads, cheap enough to poll.

    `_raw` (the verbatim source record kept by the sync) is excluded: it can
    be kilobytes per row and any change it carries also shows up in the mapped
    fields or the dynamic columns.
    """
    hasher = hashlib.md5()

    def _extra(extra) -> str:
        if not isinstance(extra, dict):
            return ""
        return json.dumps({k: v for k, v in extra.items() if k != "_raw"},
                          ensure_ascii=False, sort_keys=True, default=str)

    for row in db.execute(
            select(Order.id, Order.customer, Order.amount, Order.currency, Order.status,
                   Order.gateway, Order.date, Order.extra)
            .order_by(Order.id).limit(MAX_ROWS)):
        hasher.update(f"o|{row.id}|{row.customer}|{row.amount}|{row.currency}|{row.status}|"
                      f"{row.gateway}|{row.date}|{_extra(row.extra)}\n".encode())
    for row in db.execute(
            select(Expense.id, Expense.description, Expense.category, Expense.vendor,
                   Expense.amount, Expense.currency, Expense.owner, Expense.date,
                   Expense.receipt_url, Expense.receipt_name, Expense.extra)
            .order_by(Expense.id).limit(MAX_ROWS)):
        hasher.update(f"e|{row.id}|{row.description}|{row.category}|{row.vendor}|"
                      f"{row.amount}|{row.currency}|{row.owner}|{row.date}|"
                      f"{bool(row.receipt_url or row.receipt_name)}|{_extra(row.extra)}\n".encode())
    for row in db.execute(
            select(CustomColumn.entity, CustomColumn.key, CustomColumn.label,
                   CustomColumn.data_type).order_by(CustomColumn.id)):
        hasher.update(f"c|{row.entity}|{row.key}|{row.label}|{row.data_type}\n".encode())
    return hasher.hexdigest()


def _cached(db: Session) -> dict | None:
    row = db.get(Setting, CACHE_KEY)
    return row.value if row is not None and isinstance(row.value, dict) else None


def _store(db: Session, entry: dict) -> None:
    row = db.get(Setting, CACHE_KEY)
    if row is None:
        db.add(Setting(key=CACHE_KEY, value=entry, updated_at=entry["generated_at"]))
    else:
        row.value, row.updated_at = entry, entry["generated_at"]
    db.commit()


def status(db: Session) -> dict:
    """Staleness check for the frontend's polling — no LLM, no charts."""
    entry = _cached(db)
    if entry is None:
        return {"generated": False, "generated_at": None, "stale": False}
    return {"generated": True, "generated_at": entry.get("generated_at"),
            "stale": entry.get("fingerprint") != fingerprint(db)}


def get(db: Session, refresh: bool = False) -> dict:
    """The analysis, served from cache; built only the first time or on refresh.

    `stale` tells the caller the data moved since the analysis was generated —
    the screen shows it and offers the refresh; nothing regenerates by itself.
    """
    entry = _cached(db)
    mark = fingerprint(db)
    if entry is None or refresh:
        stamp = datetime.now().isoformat(timespec="seconds")
        result = build(db)
        _store(db, {"fingerprint": mark, "generated_at": stamp, "result": result})
        return {**result, "generated_at": stamp, "stale": False}
    return {**entry.get("result", {}), "generated_at": entry.get("generated_at"),
            "stale": entry.get("fingerprint") != mark}


def build(db: Session) -> dict:
    """The full analysis: blocks ready to render plus the data behind them."""
    orders = list(db.scalars(select(Order).limit(MAX_ROWS)).all())
    expenses = list(db.scalars(select(Expense).limit(MAX_ROWS)).all())

    if not orders and not expenses:
        return {"blocks": [blocks.notice(
            "Todavía no hay datos para analizar. Registre movimientos o "
            "sincronice una fuente en Configuración.", tone="info")], "data": {}}

    data: dict = {}
    findings: list[dict] = []
    order_amounts = _usd_amounts(orders)
    expense_amounts = _usd_amounts(expenses)

    # ------------------------------------------------ 1 · resumen general
    output: list[dict] = [
        blocks.heading("Resumen general",
                       detail="Totales del conjunto de datos actual, sin filtros."),
        _kpis(orders, expenses, data, findings),
    ]

    # ------------------------------------------ 2 · estadística descriptiva
    descriptive: list[dict] = []
    if len(order_amounts) >= 2:
        descriptive.append(_stats_table("Órdenes", order_amounts, data))
    if len(expense_amounts) >= 2:
        descriptive.append(_stats_table("Gastos", expense_amounts, data))
    feed_table = _numeric_columns_table(db, orders, expenses, data)
    if descriptive or feed_table:
        output.append(blocks.heading(
            "Estadística descriptiva",
            detail="Tendencia central y dispersión de los montos, en USD equivalente "
                   "(solo monedas con tasa configurada)."))
        output.extend(descriptive)
        if feed_table:
            output.append(feed_table)

    # ------------------------------------------- 3 · lecturas e inferencias
    _dispersion_findings(order_amounts, "las órdenes", data, findings)
    _dispersion_findings(expense_amounts, "los gastos", data, findings)
    _outlier_findings(orders, order_amounts, data, findings)
    _pareto_finding(orders, data, findings)
    _quality_findings(orders, expenses, data, findings)

    if findings:
        output.append(blocks.heading(
            "Lecturas e inferencias",
            detail="Observaciones calculadas sobre los datos: atípicos (regla IQR), "
                   "concentración (Pareto) y calidad de la información."))
        output.append(blocks.listing(findings))

    # ------------------------------------------------- 4 · distribuciones
    chart_blocks = _distribution_charts(db, orders, expenses, data, findings)
    if chart_blocks:
        output.append(blocks.heading(
            "Distribuciones",
            detail="Cómo se reparten los montos y registros en cada dimensión con "
                   "información. Las dimensiones de un solo valor no se grafican."))
        output.extend(chart_blocks)

    interpretation = _interpretation(data)
    if interpretation:
        output.append(blocks.heading(
            "Interpretación",
            detail="Qué significan las cifras anteriores. Redactado por el modelo "
                   "únicamente sobre los valores ya calculados; los números nunca "
                   "salen de él."))
        output.append(interpretation)

    intro = llm.complete(
        NARRATOR_PROMPT, json.dumps(data, ensure_ascii=False, default=str)[:4000],
        temperature=0.2, max_tokens=220, caller="insights")
    if intro:
        output.insert(0, blocks.text(intro))

    return {"blocks": output, "data": data}


def _interpretation(data: dict) -> dict | None:
    """3-4 interpretive points, model-written, number-constrained."""
    content = llm.complete(
        INTERPRETER_PROMPT, json.dumps(data, ensure_ascii=False, default=str)[:4000],
        temperature=0.3, max_tokens=500, json_object=True, caller="interpreter")
    if not content:
        return None
    try:
        points = json.loads(content).get("puntos", [])
    except ValueError:
        return None
    items = [{"title": str(p.get("titulo", "")).strip()[:120],
              "detail": str(p.get("detalle", "")).strip()[:400], "tone": "info"}
             for p in points if isinstance(p, dict) and p.get("titulo")]
    return blocks.listing(items[:4]) if items else None


# ------------------------------------------------------------------ helpers

def _usd_amounts(rows) -> list[float]:
    return sorted(finance.usd_eq(r.amount, r.currency) for r in rows
                  if finance.has_rate(r.currency))


def _split_sums(rows) -> tuple[float, dict[str, float]]:
    """(USD-convertible sum, {currency: exact sum} for the ones without rate)."""
    convertible = 0.0
    apart: dict[str, float] = defaultdict(float)
    for row in rows:
        if finance.has_rate(row.currency):
            convertible += finance.usd_eq(row.amount, row.currency)
        else:
            apart[row.currency] += row.amount
    return convertible, dict(apart)


def _date_span(rows) -> tuple[str, str] | None:
    dates = sorted({row.date for row in rows if row.date})
    return (dates[0], dates[-1]) if dates else None


def _money(usd: float, apart: dict[str, float]) -> str:
    parts = [usd_compact(usd)] if usd else []
    parts += [f"{amount_text(total, code)} {code}" for code, total in sorted(apart.items())]
    return " + ".join(parts) if parts else usd_compact(0)


# ------------------------------------------------------------- section 1

def _kpis(orders, expenses, data: dict, findings: list) -> dict:
    orders_usd, orders_apart = _split_sums(orders)
    expenses_usd, expenses_apart = _split_sums(expenses)
    items = []
    if orders:
        items.append({"label": "Órdenes", "value": integer(len(orders)),
                      "delta": _money(orders_usd, orders_apart), "positive": True})
    if expenses:
        items.append({"label": "Gastos", "value": integer(len(expenses)),
                      "delta": _money(expenses_usd, expenses_apart), "positive": False})
    if orders and expenses and (orders_usd or expenses_usd):
        balance = orders_usd - expenses_usd
        items.append({"label": "Balance (solo convertible)", "value": usd_compact(balance),
                      "delta": "órdenes − gastos", "positive": balance >= 0})

    span = _date_span(list(orders) + list(expenses))
    if span:
        label = span[0] if span[0] == span[1] else f"{span[0]} → {span[1]}"
        items.append({"label": "Rango de fechas", "value": label, "delta": "", "positive": True})
        if span[0] == span[1]:
            findings.append({
                "title": f"Todos los registros datan del {span[0]}",
                "detail": "Suele ser la fecha de importación, no la del movimiento: "
                          "el feed no traía fecha propia.", "tone": "warning"})

    data["totales"] = {
        "ordenes": {"n": len(orders), "usd": round(orders_usd, 2), "sin_tasa": orders_apart},
        "gastos": {"n": len(expenses), "usd": round(expenses_usd, 2), "sin_tasa": expenses_apart},
        "rango_fechas": span,
    }
    for entity, apart in (("órdenes", orders_apart), ("gastos", expenses_apart)):
        for code, total in apart.items():
            findings.append({
                "title": f"{amount_text(total, code)} {code} en {entity} fuera de los totales USD",
                "detail": f"No hay tasa configurada para {code}; el monto se conserva "
                          "exacto pero no se convierte.", "tone": "warning"})
    return blocks.kpis(items)


# ------------------------------------------------------------- section 2

def _stats_table(label: str, amounts: list[float], data: dict) -> dict:
    """Classic descriptive statistics of the USD-equivalent amounts."""
    q1, q2, q3 = quantiles(amounts, n=4) if len(amounts) >= 4 else (
        amounts[0], median(amounts), amounts[-1])
    avg = mean(amounts)
    deviation = stdev(amounts) if len(amounts) >= 2 else 0.0
    cv = (deviation / avg * 100) if avg else 0.0

    stats = [
        ("Registros (convertibles)", integer(len(amounts))),
        ("Suma", usd_compact(sum(amounts))),
        ("Media", usd_compact(avg)),
        ("Mediana", usd_compact(q2)),
        ("Desviación estándar", usd_compact(deviation)),
        ("Coeficiente de variación", percent(cv)),
        ("Mínimo", usd_compact(amounts[0])),
        ("Percentil 25", usd_compact(q1)),
        ("Percentil 75", usd_compact(q3)),
        ("Máximo", usd_compact(amounts[-1])),
    ]
    data[f"descriptiva_{label.lower()}"] = {
        "n": len(amounts), "suma": round(sum(amounts), 2), "media": round(avg, 2),
        "mediana": round(q2, 2), "desv_std": round(deviation, 2), "cv_pct": round(cv, 1),
        "min": round(amounts[0], 2), "p25": round(q1, 2), "p75": round(q3, 2),
        "max": round(amounts[-1], 2),
    }
    return blocks.table(
        columns=[{"key": "medida", "label": "Medida"},
                 {"key": "valor", "label": "Valor", "align": "right"}],
        rows=[{"medida": name, "valor": value} for name, value in stats],
        caption=f"{label} · montos en USD equivalente",
    )


def _numeric_columns_table(db: Session, orders, expenses, data: dict) -> dict | None:
    """One statistics table for every numeric column the feed brought.

    A tile saying «discountedTotal 651.758,23» tells the reader nothing; a row
    with range and average at least says how the variable behaves. The column
    keeps its source name — inventing a translation would be inventing.
    """
    rows_out: list[dict] = []
    for entity, rows, entity_label in (("orders", orders, "órdenes"),
                                       ("expenses", expenses, "gastos")):
        for column in db.scalars(
                select(CustomColumn).where(CustomColumn.entity == entity)).all():
            if column.data_type == "formula":
                continue
            values = [(row.extra or {}).get(column.key) for row in rows
                      if isinstance(row.extra, dict)]
            numeric = [v for v in values
                       if isinstance(v, (int, float)) and not isinstance(v, bool)]
            present = [v for v in values if v not in (None, "")]
            if len(present) < 2 or len(numeric) < len(present) * 0.6:
                continue
            rows_out.append({
                "columna": column.label,
                "entidad": entity_label,
                "n": integer(len(numeric)),
                "total": amount_text(sum(numeric), "USD"),
                "promedio": amount_text(mean(numeric), "USD"),
                "minimo": amount_text(min(numeric), "USD"),
                "maximo": amount_text(max(numeric), "USD"),
            })
            data.setdefault("columnas_numericas", {})[column.label] = {
                "suma": round(sum(numeric), 2), "promedio": round(mean(numeric), 2),
                "min": round(min(numeric), 2), "max": round(max(numeric), 2)}
    if not rows_out:
        return None
    return blocks.table(
        columns=[
            {"key": "columna", "label": "Columna de la fuente"},
            {"key": "entidad", "label": "En"},
            {"key": "n", "label": "Valores", "align": "right"},
            {"key": "total", "label": "Total", "align": "right"},
            {"key": "promedio", "label": "Promedio", "align": "right"},
            {"key": "minimo", "label": "Mínimo", "align": "right"},
            {"key": "maximo", "label": "Máximo", "align": "right"},
        ],
        rows=rows_out,
        caption="Variables numéricas adicionales que trajo la fuente, con sus "
                "estadísticos básicos (unidades propias de cada variable).",
        wide=True,
    )


# ------------------------------------------------------------- section 3

def _dispersion_findings(amounts: list[float], entity_label: str,
                         data: dict, findings: list) -> None:
    if len(amounts) < 4:
        return
    avg = mean(amounts)
    if not avg:
        return
    cv = stdev(amounts) / avg * 100
    if cv >= HIGH_CV:
        findings.append({
            "title": f"Alta dispersión en los montos de {entity_label} (CV {percent(cv)})",
            "detail": "Conviven tickets muy pequeños y muy grandes: la media "
                      f"({usd_compact(avg)}) no representa el caso típico; use la "
                      f"mediana ({usd_compact(median(amounts))}).", "tone": "info"})


def _outlier_findings(orders, amounts: list[float], data: dict, findings: list) -> None:
    """Tukey's IQR rule: beyond Q3 + 1.5·IQR is atypical, and named."""
    if len(amounts) < 4:
        return
    q1, _, q3 = quantiles(amounts, n=4)
    fence = q3 + 1.5 * (q3 - q1)
    outliers = [value for value in amounts if value > fence]
    if not outliers:
        return
    top = max(orders, key=lambda o: finance.usd_eq(o.amount, o.currency))
    findings.append({
        "title": f"{integer(len(outliers))} órdenes atípicas por monto (regla IQR)",
        "detail": f"Por encima de {usd_compact(fence)}. La mayor: {top.id} de "
                  f"{top.customer} por {amount_text(top.amount, top.currency)} "
                  f"{top.currency}.", "tone": "warning"})
    data["atipicos_ordenes"] = {"n": len(outliers), "umbral_usd": round(fence, 2),
                                "maximo": {"id": top.id, "monto": top.amount,
                                           "moneda": top.currency}}


def _pareto_finding(orders, data: dict, findings: list) -> None:
    """How few customers explain 80 % of the converted order value."""
    by_customer: dict[str, float] = defaultdict(float)
    for order in orders:
        if finance.has_rate(order.currency) and order.customer:
            by_customer[order.customer] += finance.usd_eq(order.amount, order.currency)
    if len(by_customer) < 5:
        return
    values = sorted(by_customer.values(), reverse=True)
    total = sum(values) or 1
    running, needed = 0.0, 0
    for value in values:
        running += value
        needed += 1
        if running >= total * 0.8:
            break
    share = needed / len(values)
    if share <= 0.5:
        findings.append({
            "title": f"El {round(share * 100)}% de los clientes explica el 80% del monto",
            "detail": f"{integer(needed)} de {integer(len(values))} clientes concentran "
                      f"{usd_compact(total * 0.8)} — patrón Pareto.", "tone": "info"})
        data["pareto_clientes"] = {"clientes": needed, "total_clientes": len(values),
                                   "participacion_pct": round(share * 100, 1)}


def _quality_findings(orders, expenses, data: dict, findings: list) -> None:
    missing = sum(1 for e in expenses if not e.receipt_url and not e.receipt_name)
    if missing:
        findings.append({
            "title": f"{integer(missing)} gastos sin comprobante",
            "detail": "Pídale al agente «gastos sin comprobante» para verlos.",
            "tone": "warning"})
        data["gastos_sin_comprobante"] = missing

    imported = sum(1 for row in list(orders) + list(expenses)
                   if isinstance(row.extra, dict) and str(row.extra.get("source", "")) == "api")
    manual = len(orders) + len(expenses) - imported
    if imported:
        findings.append({
            "title": f"{integer(imported)} registros vienen de la fuente sincronizada",
            "detail": f"{integer(manual)} registrados a mano." if manual
            else "Ninguno registrado a mano todavía.", "tone": "info"})
    data["origen"] = {"importados": imported, "manuales": manual}


# ------------------------------------------------------------- section 4

def _dimension(rows, value_fn) -> list[tuple[str, int, float]]:
    """[(value, count, usd_sum)] sorted by weight (usd first, count as tiebreak)."""
    counts: Counter = Counter()
    sums: dict[str, float] = defaultdict(float)
    for row in rows:
        value = value_fn(row)
        value = str(value).strip() if value not in (None, "") else None
        if value is None:
            continue
        counts[value] += 1
        if finance.has_rate(row.currency):
            sums[value] += finance.usd_eq(row.amount, row.currency)
    return sorted(((v, counts[v], round(sums[v], 2)) for v in counts),
                  key=lambda item: (item[2], item[1]), reverse=True)


def _chart_for(title: str, entries: list[tuple[str, int, float]],
               data: dict, data_key: str, findings: list,
               entity_label: str) -> dict | None:
    """A titled chart when the dimension informs; None when it does not."""
    if len(entries) < 2:
        return None
    by_usd = any(usd > 0 for _, _, usd in entries)
    series = [{"label": value[:24], "value": usd if by_usd else count}
              for value, count, usd in entries[:10]]
    kind = "pie" if len(series) <= MAX_SLICES else "hbar"
    svg = charts.render(kind, series, title="")

    top_value, top_count, top_usd = entries[0]
    share = (top_usd if by_usd else top_count) / (
        sum((usd if by_usd else count) for _, count, usd in entries) or 1)
    if share >= CONCENTRATION:
        findings.append({
            "title": f"«{top_value}» concentra {round(share * 100)}% de {entity_label}",
            "detail": usd_compact(top_usd) if by_usd else f"{integer(top_count)} registros",
            "tone": "info"})

    data[data_key] = [{"valor": v, "n": c, "usd": u} for v, c, u in entries[:10]]
    unit = "USD equivalente" if by_usd else "cantidad de registros (montos no convertibles)"
    pdf_series = [{"label": value[:28],
                   "value": usd if by_usd else count,
                   "display": usd_compact(usd) if by_usd else integer(count)}
                  for value, count, usd in entries[:10]]
    return blocks.chart(svg, title=f"{title} — por {unit}", chart_type=kind,
                        series=pdf_series)


def _histogram(amounts: list[float], title: str, data: dict, data_key: str) -> dict | None:
    """The shape of the amounts: how many fall in each range."""
    if len(amounts) < 8:
        return None
    low, high = amounts[0], amounts[-1]
    if high <= low:
        return None
    bins = 8
    step = (high - low) / bins
    counts = [0] * bins
    for value in amounts:
        index = min(int((value - low) / step), bins - 1)
        counts[index] += 1
    series = [{"label": f"≤{charts._fmt(low + step * (i + 1))}", "value": count}
              for i, count in enumerate(counts)]
    data[data_key] = {"bins": [{"hasta": round(low + step * (i + 1), 2), "n": c}
                               for i, c in enumerate(counts)]}
    return blocks.chart(charts.render("bar", series, title=""),
                        title=f"{title} — cantidad de registros por rango de monto (USD)",
                        chart_type="bar",
                        series=[{**point, "display": integer(point["value"])}
                                for point in series])


def _box_charts(order_amounts: list[float], expense_amounts: list[float]) -> list[dict]:
    """The five-number summary, drawn: the visual twin of the stats tables.

    One chart per entity, each with its own scale — sharing one axis squashes
    the smaller distribution into an unreadable sliver.
    """
    output = []
    for label, amounts in (("órdenes", order_amounts), ("gastos", expense_amounts)):
        if len(amounts) < 4:
            continue
        q1, q2, q3 = quantiles(amounts, n=4)
        series = [{"label": label.capitalize(), "min": amounts[0], "q1": q1,
                   "med": q2, "q3": q3, "max": amounts[-1]}]
        output.append(blocks.chart(
            charts.render("box", series, title="", height=220),
            title=f"Diagrama de caja de {label} (USD) — mediana, cuartiles y extremos",
            chart_type="box"))
    return output


def _timeline(rows, title: str, data: dict, data_key: str) -> dict | None:
    """Daily totals over time — only when the dates actually vary."""
    by_date: dict[str, float] = defaultdict(float)
    for row in rows:
        if row.date and finance.has_rate(row.currency):
            by_date[row.date] += finance.usd_eq(row.amount, row.currency)
    if len(by_date) < 3:
        return None
    points = sorted(by_date.items())[-30:]
    series = [{"label": day[5:], "value": round(total, 2)} for day, total in points]
    data[data_key] = [{"fecha": d, "usd": round(t, 2)} for d, t in points]
    return blocks.chart(charts.render("line", series, title=""),
                        title=f"{title} — total diario en USD equivalente",
                        chart_type="line",
                        series=[{**point, "display": usd_compact(point["value"])}
                                for point in series])


def _distribution_charts(db: Session, orders, expenses, data: dict,
                         findings: list) -> list[dict]:
    output: list[dict] = []

    order_amounts = _usd_amounts(orders)
    expense_amounts = _usd_amounts(expenses)

    # La forma primero: caja, histogramas y evolución; el reparto después.
    output.extend(_box_charts(order_amounts, expense_amounts))
    for amounts, title, key in ((order_amounts, "Histograma de órdenes", "hist_ordenes"),
                                (expense_amounts, "Histograma de gastos", "hist_gastos")):
        hist = _histogram(amounts, title, data, key)
        if hist:
            output.append(hist)
    for rows, title, key in ((orders, "Evolución de órdenes", "serie_ordenes"),
                             (expenses, "Evolución de gastos", "serie_gastos")):
        line = _timeline(rows, title, data, key)
        if line:
            output.append(line)

    for title, fn, key in (
        ("Órdenes por estado", lambda o: status_label(o.status), "ordenes_estado"),
        ("Órdenes por pasarela", lambda o: o.gateway, "ordenes_pasarela"),
        ("Órdenes por moneda", lambda o: o.currency, "ordenes_moneda"),
        ("Top clientes", lambda o: o.customer, "ordenes_clientes"),
    ):
        chart = _chart_for(title, _dimension(orders, fn), data, key, findings, "las órdenes")
        if chart:
            output.append(chart)

    for title, fn, key in (
        ("Gastos por categoría", lambda e: e.category, "gastos_categoria"),
        ("Gastos por proveedor", lambda e: e.vendor, "gastos_proveedor"),
        ("Gastos por responsable", lambda e: e.owner, "gastos_responsable"),
        ("Gastos por moneda", lambda e: e.currency, "gastos_moneda"),
    ):
        chart = _chart_for(title, _dimension(expenses, fn), data, key, findings, "los gastos")
        if chart:
            output.append(chart)

    for entity, rows, label in (("orders", orders, "Órdenes"), ("expenses", expenses, "Gastos")):
        output.extend(_categorical_column_charts(db, entity, rows, data, findings, label))

    # The layout pairs charts two by two: an odd count leaves a hole. Charts
    # are emitted in priority order (shape first, native dimensions, then feed
    # columns), so evening out means dropping the least critical tail — and
    # saying so in the data instead of hiding it.
    if len(output) > 1 and len(output) % 2 == 1:
        dropped = output.pop()
        data["grafica_omitida_por_paridad"] = dropped.get("title", "")
    return output


def _categorical_column_charts(db: Session, entity: str, rows, data: dict,
                               findings: list, entity_label: str) -> list[dict]:
    """Distribution charts for the categorical columns the feed brought."""
    output: list[dict] = []
    for column in db.scalars(select(CustomColumn).where(CustomColumn.entity == entity)).all():
        if column.data_type == "formula":
            continue
        values = [(row.extra or {}).get(column.key) for row in rows
                  if isinstance(row.extra, dict)]
        values = [v for v in values
                  if v not in (None, "")
                  and (isinstance(v, bool) or not isinstance(v, (int, float)))]
        if len(values) < 2:
            continue
        distinct = Counter(str(v)[:40] for v in values)
        if 2 <= len(distinct) <= MAX_CATEGORICAL:
            entries = [(value, count, 0.0) for value, count in distinct.most_common()]
            chart = _chart_for(f"{entity_label} por {column.label} (columna del feed)",
                               entries, data, f"{entity}_{column.key}", findings,
                               entity_label.lower())
            if chart:
                output.append(chart)
    return output
