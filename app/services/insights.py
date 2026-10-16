"""Analysis derived from the data that is actually there.

The old report assumed the demo's shape (July weeks, Wompi/ePayco, month
deltas). Real data has whatever shape the source gave it, so this engine works
the other way around: it profiles the dataset, discovers which dimensions
carry information — including the dynamic columns a feed created — and builds
the analysis FROM that.

The output is read in tabs, each answering one question a person running the
business asks (see TABS):

    Resumen     volume in every state, approval rate, typical ticket, and a
                short «Atención» list of what deserves a look
    Tendencia   volume per month (per week for short spans), split by state
    Clientes    who moves the volume, with each one's approval rate
    Operación   one bar chart per informative dimension: state, ticket size,
                gateway, currency and the categorical columns of the feed
    Datos       origin and quality of the data, plus the full statistics

Rejected and pending orders are never filtered out: they are counted,
coloured and called out, because they are information too.

Every figure is computed here, deterministically. The model contributes at
most the opening paragraph, constrained to the computed numbers — without it
the analysis is complete, just without prose.

Money rules hold: currencies without a configured USD rate are never
converted; they are aggregated apart, exactly, in their own currency.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from statistics import mean, median, quantiles

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent import blocks
from ..agent.formatting import amount as amount_text
from ..agent.formatting import integer, percent, status_label, usd_compact
from ..models import CustomColumn, Expense, Order, Setting
from . import charts, finance, llm

MAX_ROWS = 100_000      # a year of OTC trades is ~10k rows
MAX_CATEGORICAL = 15    # more distinct values than this is an id, not a category

NARRATOR_PROMPT = """You write the opening paragraph of a financial analysis.

Hard rules:
- Use ONLY figures present in the JSON. Never add, round or extrapolate.
- Two or three sentences, formal Colombian Spanish — address the reader as
  «usted», never «vos» or «tú» —, no markdown. Plain, direct, useful.
- Lead with what matters most: total volume and how it splits by state
  (approved, pending, rejected), then concentration or anything unusual.
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
# Bumped when the shape of the analysis changes: an older cached copy is then
# rebuilt once instead of being served in a layout the screen no longer reads.
CACHE_VERSION = 2


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
    if entry is None or refresh or entry.get("version") != CACHE_VERSION:
        stamp = datetime.now().isoformat(timespec="seconds")
        result = build(db)
        _store(db, {"fingerprint": mark, "generated_at": stamp, "result": result,
                    "version": CACHE_VERSION})
        return {**result, "generated_at": stamp, "stale": False}
    return {**entry.get("result", {}), "generated_at": entry.get("generated_at"),
            "stale": entry.get("fingerprint") != mark}


# ------------------------------------------------------------------- tabs
# The analysis is read in tabs, each answering one question a person running
# the business actually asks. Every state of an order stays visible — a
# rejected order is information (lost revenue, a failing provider), so it is
# counted and coloured, never filtered out of the totals.

TABS = [
    ("resumen", "Resumen", "Lo esencial: cuánto se movió, en qué estado quedó y qué merece atención."),
    ("tendencia", "Tendencia", "Cómo evoluciona el volumen en el tiempo, separado por estado."),
    ("clientes", "Clientes", "Quiénes mueven el volumen y con qué tasa de aprobación."),
    ("operacion", "Operación", "Cómo se reparten las órdenes por estado, tamaño, red, país y demás "
                               "dimensiones que trae la fuente."),
    ("datos", "Datos", "De dónde vienen los datos, su calidad y las estadísticas completas."),
]

# Order states in reading order, with plural names for totals.
STATE_ORDER = ["approved", "pending", "rejected", "refunded"]
STATE_PLURAL = {"approved": "Aprobadas", "pending": "Pendientes",
                "rejected": "Rechazadas", "refunded": "Reembolsadas"}

# Ticket-size ranges (USD) a person reads at a glance; a linear histogram of
# skewed amounts puts everything in its first bar.
SIZE_BUCKETS = [(0, 100, "Menos de USD 100"), (100, 500, "USD 100 – 500"),
                (500, 1_000, "USD 500 – 1.000"), (1_000, 5_000, "USD 1.000 – 5.000"),
                (5_000, float("inf"), "USD 5.000 o más")]

DOMINANT = 0.95         # a dimension where one value is this share is a fact, not a chart
TOP_CLIENTS = 15
WIDE_W = 1120           # a chart that spans the row is drawn at that width, not stretched
ID_HINTS = ("id", "number", "numero", "document", "phone", "telefono", "nit", "cedula",
            "code", "codigo", "zip", "postal")


def build(db: Session) -> dict:
    """The full analysis: tabs of blocks, the same blocks flattened, and the data."""
    orders = list(db.scalars(select(Order).limit(MAX_ROWS)).all())
    expenses = list(db.scalars(select(Expense).limit(MAX_ROWS)).all())

    if not orders and not expenses:
        empty = [blocks.notice(
            "Todavía no hay datos para analizar. Registre movimientos o "
            "sincronice una fuente en Configuración.", tone="info")]
        return {"blocks": empty, "tabs": [], "data": {}}

    data: dict = {}
    attention: list[dict] = []
    tabs = {
        "resumen": _summary_tab(orders, expenses, data, attention),
        "tendencia": _trend_tab(orders, data),
        "clientes": _clients_tab(orders, data, attention),
        "operacion": _operation_tab(db, orders, expenses, data, attention),
        "datos": _data_tab(db, orders, expenses, data),
    }

    # «Atención» goes under the summary figures: the few things worth a look.
    if attention:
        attention.sort(key=lambda item: {"danger": 0, "warning": 1}.get(item["tone"], 2))
        tabs["resumen"].append(blocks.heading(
            "Atención", detail="Lo que conviene revisar, calculado sobre los datos."))
        tabs["resumen"].append(blocks.listing(attention[:5]))

    interpretation = _interpretation(data)
    if interpretation:
        tabs["resumen"].append(blocks.heading(
            "Interpretación",
            detail="Redactado por el modelo únicamente sobre los valores ya calculados; "
                   "los números nunca salen de él."))
        tabs["resumen"].append(interpretation)

    intro = llm.complete(
        NARRATOR_PROMPT, json.dumps(data, ensure_ascii=False, default=str)[:4000],
        temperature=0.2, max_tokens=220, caller="insights")
    if intro:
        tabs["resumen"].insert(0, blocks.text(intro))

    result_tabs = [{"key": key, "label": label, "detail": detail, "blocks": tabs[key]}
                   for key, label, detail in TABS if tabs.get(key)]
    # The flat list (chat, PDF) is the tabs one after another, each titled.
    flat: list[dict] = []
    for tab in result_tabs:
        flat.append(blocks.heading(tab["label"], detail=tab["detail"]))
        flat.extend(tab["blocks"])
    return {"blocks": flat, "tabs": result_tabs, "data": data}


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

def _usd(row) -> float:
    return finance.usd_eq(row.amount, row.currency) if finance.has_rate(row.currency) else 0.0


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


def _states(orders) -> list[str]:
    """The states present, known ones first in reading order."""
    present = {o.status for o in orders}
    return [s for s in STATE_ORDER if s in present] + sorted(present - set(STATE_ORDER))


def _state_color(state: str) -> str:
    return charts.STATUS_COLORS.get(state, charts.OTHER_COLOR)


def _share(part: float, whole: float) -> float:
    return part / whole * 100 if whole else 0.0


def _humanize(name: str) -> str:
    """`currency_out` -> «Currency out»: a feed column, readable."""
    text = re.sub(r"[_\-]+", " ", str(name)).strip()
    return text[:1].upper() + text[1:] if text else str(name)


def _id_like(name: str) -> bool:
    words = set(re.split(r"[^a-z]+", str(name).lower()))
    return bool(words & set(ID_HINTS))


def _by_state(orders) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for order in orders:
        entry = out.setdefault(order.status, {"n": 0, "usd": 0.0})
        entry["n"] += 1
        entry["usd"] += _usd(order)
    return out


# ------------------------------------------------------------ 1 · resumen

def _summary_tab(orders, expenses, data: dict, attention: list) -> list[dict]:
    output: list[dict] = []
    if orders:
        total_usd, apart = _split_sums(orders)
        states = _by_state(orders)
        items = [{"label": "Volumen total", "value": usd_compact(total_usd),
                  "delta": f"{integer(len(orders))} órdenes · todos los estados",
                  "tone": "neutral"}]
        for state in _states(orders):
            entry = states[state]
            items.append({
                "label": STATE_PLURAL.get(state, status_label(state)),
                "value": usd_compact(entry["usd"]),
                "delta": f"{integer(entry['n'])} órdenes · "
                         f"{percent(_share(entry['usd'], total_usd))} del volumen",
                "tone": {"approved": "success", "pending": "warning",
                         "rejected": "danger"}.get(state, "neutral")})
        output.append(blocks.kpis(items))

        approved = states.get("approved", {"n": 0, "usd": 0.0})
        rate = _share(approved["n"], len(orders))
        amounts = _usd_amounts(orders)
        customers = {o.customer for o in orders if o.customer}
        span = _date_span(orders)
        second = [
            {"label": "Tasa de aprobación", "value": percent(rate),
             "delta": f"{integer(approved['n'])} de {integer(len(orders))} órdenes",
             "tone": "success" if rate >= 50 else "danger"},
            {"label": "Ticket típico", "value": usd_compact(median(amounts)) if amounts else "—",
             "delta": "mediana por orden", "tone": "neutral"},
            {"label": "Clientes", "value": integer(len(customers)),
             "delta": "con al menos una orden", "tone": "neutral"},
        ]
        if span:
            second.append({"label": "Período de los datos",
                           "value": f"{_month_short(span[0])} – {_month_short(span[1])}",
                           "delta": f"{span[0]} a {span[1]}", "tone": "neutral"})
        if expenses:
            expenses_usd, _ = _split_sums(expenses)
            second.append({"label": "Gastos", "value": usd_compact(expenses_usd),
                           "delta": f"{integer(len(expenses))} registrados", "tone": "neutral"})
        output.append(blocks.kpis(second))

        rejected = states.get("rejected", {"n": 0, "usd": 0.0})
        if rejected["n"] and _share(rejected["n"], len(orders)) >= 30:
            attention.append({
                "title": f"{percent(_share(rejected['n'], len(orders)))} de las órdenes "
                         "fueron rechazadas",
                "detail": f"{integer(rejected['n'])} órdenes por {usd_compact(rejected['usd'])} "
                          "no se completaron. Revise en Operación y Clientes de dónde vienen.",
                "tone": "danger"})
        pending = states.get("pending", {"n": 0, "usd": 0.0})
        if pending["n"] and _share(pending["n"], len(orders)) >= 10:
            attention.append({
                "title": f"{integer(pending['n'])} órdenes siguen pendientes",
                "detail": f"{usd_compact(pending['usd'])} esperan una resolución.",
                "tone": "warning"})
        for code, value in apart.items():
            attention.append({
                "title": f"{amount_text(value, code)} {code} sin tasa de cambio",
                "detail": f"No hay tasa configurada para {code}: esas órdenes se cuentan "
                          "pero no se suman en USD.", "tone": "warning"})

        data["totales"] = {
            "ordenes": {"n": len(orders), "usd": round(total_usd, 2), "sin_tasa": apart},
            "por_estado": {status_label(k): {"n": v["n"], "usd": round(v["usd"], 2)}
                           for k, v in states.items()},
            "tasa_aprobacion_pct": round(rate, 1),
            "ticket_mediana_usd": round(median(amounts), 2) if amounts else None,
            "clientes": len(customers),
            "rango_fechas": span,
        }
    if expenses:
        expenses_usd, expenses_apart = _split_sums(expenses)
        data.setdefault("totales", {})["gastos"] = {
            "n": len(expenses), "usd": round(expenses_usd, 2), "sin_tasa": expenses_apart}
        if not orders:
            output.append(blocks.kpis([{"label": "Gastos", "value": usd_compact(expenses_usd),
                                        "delta": f"{integer(len(expenses))} registrados",
                                        "tone": "neutral"}]))
    return output


MONTHS_SHORT = ["ene", "feb", "mar", "abr", "may", "jun",
                "jul", "ago", "sep", "oct", "nov", "dic"]


def _month_short(iso: str) -> str:
    """"2026-09-22" -> «sep 2026»."""
    try:
        return f"{MONTHS_SHORT[int(iso[5:7]) - 1]} {iso[:4]}"
    except (ValueError, IndexError):
        return iso


# ---------------------------------------------------------- 2 · tendencia

def _bucket(iso: str, weekly: bool) -> str:
    if not weekly:
        return iso[:7]
    day = datetime.strptime(iso[:10], "%Y-%m-%d")
    monday = day.toordinal() - day.weekday()
    return datetime.fromordinal(monday).strftime("%Y-%m-%d")


def _bucket_label(key: str, weekly: bool) -> str:
    if weekly:
        return f"{key[8:10]} {MONTHS_SHORT[int(key[5:7]) - 1]}"
    return f"{MONTHS_SHORT[int(key[5:7]) - 1]} {key[2:4]}"


def _trend_tab(orders, data: dict) -> list[dict]:
    dated = [o for o in orders if o.date and len(o.date) >= 10]
    span = _date_span(dated)
    if not span or span[0] == span[1]:
        return [blocks.notice("Las órdenes no tienen fechas distintas: no hay evolución "
                              "que mostrar.", tone="info")] if orders else []
    first = datetime.strptime(span[0][:10], "%Y-%m-%d")
    last = datetime.strptime(span[1][:10], "%Y-%m-%d")
    weekly = (last - first).days <= 92
    unit = "semana" if weekly else "mes"

    buckets: dict[str, dict[str, dict]] = defaultdict(dict)
    for order in dated:
        key = _bucket(order.date, weekly)
        entry = buckets[key].setdefault(order.status, {"n": 0, "usd": 0.0})
        entry["n"] += 1
        entry["usd"] += _usd(order)
    keys = sorted(buckets)
    states = _states(dated)

    shown = keys[-18:]
    series = []
    for key in shown:
        total = sum(v["usd"] for v in buckets[key].values())
        series.append({
            "label": _bucket_label(key, weekly),
            "display": usd_compact(total).replace("USD ", ""),
            "parts": [{"name": STATE_PLURAL.get(s, status_label(s)),
                       "value": round(buckets[key].get(s, {"usd": 0.0})["usd"], 2),
                       "color": _state_color(s)} for s in states],
        })
    output = [blocks.chart(
        charts.render("stacked", series, width=WIDE_W, height=380),
        title=f"Volumen por {unit} (USD), separado por estado",
        chart_type="stacked",
        # The PDF redraws the totals per period as a line.
        series=[{"label": point["label"],
                 "value": round(sum(p["value"] for p in point["parts"]), 2),
                 "display": usd_compact(sum(p["value"] for p in point["parts"]))}
                for point in series])]
    output[0]["wide"] = True
    if len(keys) > len(shown):
        output.append(blocks.notice(
            f"El gráfico muestra los últimos {len(shown)} períodos; la tabla tiene todos.",
            tone="info"))

    rows = []
    for key in reversed(keys):
        entry = buckets[key]
        n = sum(v["n"] for v in entry.values())
        usd = sum(v["usd"] for v in entry.values())
        approved = entry.get("approved", {"n": 0, "usd": 0.0})
        row = {"periodo": (f"Semana del {_bucket_label(key, True)} {key[:4]}" if weekly
                           else f"{MONTHS_SHORT[int(key[5:7]) - 1].capitalize()} {key[:4]}"),
               "ordenes": integer(n), "volumen": usd_compact(usd),
               "aprobado": usd_compact(approved["usd"]),
               "tasa": percent(_share(approved["n"], n))}
        for state in states:
            row[state] = integer(entry.get(state, {"n": 0})["n"])
        rows.append(row)
    columns = [{"key": "periodo", "label": "Período"},
               {"key": "ordenes", "label": "Órdenes", "align": "right"}]
    columns += [{"key": s, "label": STATE_PLURAL.get(s, status_label(s)), "align": "right"}
                for s in states]
    columns += [{"key": "tasa", "label": "Tasa aprobación", "align": "right"},
                {"key": "volumen", "label": "Volumen", "align": "right"},
                {"key": "aprobado", "label": "Aprobado", "align": "right"}]
    output.append(blocks.table(columns=columns, rows=rows, wide=True,
                               caption=f"Detalle por {unit}, del más reciente al más antiguo."))
    data["tendencia"] = [{"periodo": k, "n": sum(v["n"] for v in buckets[k].values()),
                          "usd": round(sum(v["usd"] for v in buckets[k].values()), 2)}
                         for k in keys[-12:]]
    return output


# ----------------------------------------------------------- 3 · clientes

def _clients_tab(orders, data: dict, attention: list) -> list[dict]:
    by_client: dict[str, dict] = {}
    for order in orders:
        if not order.customer:
            continue
        entry = by_client.setdefault(order.customer, {"n": 0, "usd": 0.0, "approved_n": 0,
                                                      "approved_usd": 0.0, "rejected_n": 0})
        entry["n"] += 1
        entry["usd"] += _usd(order)
        if order.status == "approved":
            entry["approved_n"] += 1
            entry["approved_usd"] += _usd(order)
        elif order.status == "rejected":
            entry["rejected_n"] += 1
    if len(by_client) < 2:
        return []
    ranked = sorted(by_client.items(), key=lambda item: (item[1]["usd"], item[1]["n"]),
                    reverse=True)
    total = sum(v["usd"] for v in by_client.values()) or 1

    output = []
    top = ranked[:10]
    output.append(blocks.chart(
        charts.render("hbar", width=WIDE_W, series=[{"label": name[:24], "value": round(v["usd"], 2),
                                "display": f"{usd_compact(v['usd'])} · "
                                           f"{percent(_share(v['usd'], total))}"}
                               for name, v in top]),
        title="Los 10 clientes con más volumen (USD, todos los estados)",
        chart_type="hbar",
        series=[{"label": name[:28], "value": round(v["usd"], 2),
                 "display": usd_compact(v["usd"])} for name, v in top]))
    output[-1]["wide"] = True

    rows = []
    for name, v in ranked[:TOP_CLIENTS]:
        rows.append({"cliente": name, "ordenes": integer(v["n"]),
                     "volumen": usd_compact(v["usd"]),
                     "participacion": percent(_share(v["usd"], total)),
                     "aprobado": usd_compact(v["approved_usd"]),
                     "rechazadas": integer(v["rejected_n"]),
                     "tasa": percent(_share(v["approved_n"], v["n"]))})
    output.append(blocks.table(
        columns=[{"key": "cliente", "label": "Cliente"},
                 {"key": "ordenes", "label": "Órdenes", "align": "right"},
                 {"key": "volumen", "label": "Volumen", "align": "right"},
                 {"key": "participacion", "label": "% del total", "align": "right"},
                 {"key": "aprobado", "label": "Aprobado", "align": "right"},
                 {"key": "rechazadas", "label": "Rechazadas", "align": "right"},
                 {"key": "tasa", "label": "Tasa aprobación", "align": "right"}],
        rows=rows, wide=True,
        caption=(f"Los {len(rows)} clientes con más volumen de {integer(len(by_client))}."
                 if len(by_client) > len(rows) else None)))

    # Concentration: how few clients make 80 % of the volume.
    running, needed = 0.0, 0
    for _, v in ranked:
        running += v["usd"]
        needed += 1
        if running >= total * 0.8:
            break
    leader_name, leader = ranked[0]
    leader_share = _share(leader["usd"], total)
    if leader_share >= 40:
        attention.append({
            "title": f"{leader_name} es el {percent(leader_share)} del volumen",
            "detail": f"Un solo cliente concentra {usd_compact(leader['usd'])}; si se va o "
                      "falla, el volumen cae en esa proporción.", "tone": "warning"})
    elif len(ranked) >= 5 and needed / len(ranked) <= 0.2:
        attention.append({
            "title": f"{integer(needed)} de {integer(len(ranked))} clientes hacen el 80% del volumen",
            "detail": "El negocio depende de pocos clientes.", "tone": "info"})
    data["clientes"] = {"total": len(ranked), "para_80_pct": needed,
                        "top": [{"cliente": n, "n": v["n"], "usd": round(v["usd"], 2),
                                 "tasa_aprobacion_pct": round(_share(v["approved_n"], v["n"]), 1)}
                                for n, v in ranked[:5]]}
    return output


# ---------------------------------------------------------- 4 · operación

def _dimension(rows, value_fn) -> list[tuple[str, int, float]]:
    """[(value, count, usd_sum)] sorted by weight (usd first, count as tiebreak)."""
    counts: Counter = Counter()
    sums: dict[str, float] = defaultdict(float)
    for row in rows:
        value = value_fn(row)
        value = str(value).strip()[:40] if value not in (None, "") else None
        if value is None:
            continue
        counts[value] += 1
        sums[value] += _usd(row)
    return sorted(((v, counts[v], round(sums[v], 2)) for v in counts),
                  key=lambda item: (item[2], item[1]), reverse=True)


def _dimension_chart(title: str, entries, dominant: list, data: dict, key: str,
                     colors: dict | None = None, noun: str = "órdenes") -> dict | None:
    """One horizontal bar chart per dimension; a dimension that is one value is
    said in a sentence instead (a 99.8 % pie tells nothing)."""
    if len(entries) < 2:
        return None
    count_total = sum(c for _, c, _ in entries) or 1
    top_value, top_count, _ = entries[0]
    if top_count / count_total >= DOMINANT:
        dominant.append(f"{title}: «{top_value}» en el {percent(_share(top_count, count_total))} "
                        f"de los registros")
        return None
    by_usd = any(u > 0 for _, _, u in entries)
    shown = entries[:10]
    series = [{"label": v[:24], "value": u if by_usd else c,
               "display": (f"{integer(c)} · {usd_compact(u)}" if by_usd else integer(c)),
               "color": (colors or {}).get(v)} for v, c, u in shown]
    data[key] = [{"valor": v, "n": c, "usd": u} for v, c, u in shown]
    return blocks.chart(
        charts.render("hbar", series),
        title=f"{title} — {noun} · {'volumen USD' if by_usd else 'cantidad'}",
        chart_type="hbar",
        series=[{"label": p["label"], "value": p["value"], "display": p["display"]}
                for p in series])


def _operation_tab(db: Session, orders, expenses, data: dict, attention: list) -> list[dict]:
    output: list[dict] = []
    dominant: list[str] = []
    if orders:
        state_entries = [(status_label(s), v["n"], round(v["usd"], 2))
                         for s, v in _by_state(orders).items()]
        state_entries.sort(key=lambda e: (e[2], e[1]), reverse=True)
        colors = {status_label(s): _state_color(s) for s in STATE_ORDER}
        chart = _dimension_chart("Por estado", state_entries, dominant, data,
                                 "ordenes_estado", colors)
        if chart:
            output.append(chart)

        amounts = [_usd(o) for o in orders if finance.has_rate(o.currency)]
        if len(amounts) >= 4:
            sizes = []
            for low, high, label in SIZE_BUCKETS:
                inside = [a for a in amounts if low <= a < high]
                if inside:
                    sizes.append({"label": label, "value": len(inside),
                                  "display": f"{integer(len(inside))} · {usd_compact(sum(inside))}"})
            if len(sizes) >= 2:
                output.append(blocks.chart(
                    charts.render("hbar", sizes),
                    title="Tamaño de las órdenes — cantidad · volumen USD",
                    chart_type="hbar",
                    series=[{**p, "display": p["display"]} for p in sizes]))
                data["tamanos"] = [{"rango": p["label"], "n": p["value"]} for p in sizes]

        for title, fn, key in (("Por pasarela", lambda o: o.gateway, "ordenes_pasarela"),
                               ("Por moneda", lambda o: o.currency, "ordenes_moneda")):
            chart = _dimension_chart(title, _dimension(orders, fn), dominant, data, key)
            if chart:
                output.append(chart)

        for column in db.scalars(select(CustomColumn)
                                 .where(CustomColumn.entity == "orders")).all():
            if column.data_type == "formula" or _id_like(column.key):
                continue
            values = [(o.extra or {}).get(column.key) for o in orders
                      if isinstance(o.extra, dict)]
            values = [v for v in values if v not in (None, "")
                      and (isinstance(v, bool) or not isinstance(v, (int, float)))]
            if len(values) < 2 or not 2 <= len(set(map(str, values))) <= MAX_CATEGORICAL:
                continue
            chart = _dimension_chart(
                f"Por {_humanize(column.label).lower()}",
                _dimension(orders, lambda o, k=column.key: (o.extra or {}).get(k)
                           if isinstance(o.extra, dict) else None),
                dominant, data, f"orders_{column.key}")
            if chart:
                output.append(chart)

    if len(expenses) >= 2:
        for title, fn, key in (("Gastos por categoría", lambda e: e.category, "gastos_categoria"),
                               ("Gastos por proveedor", lambda e: e.vendor, "gastos_proveedor")):
            chart = _dimension_chart(title, _dimension(expenses, fn), dominant, data, key,
                                     noun="gastos")
            if chart:
                output.append(chart)

    if dominant:
        output.append(blocks.listing(
            [{"title": line, "detail": "", "tone": "info"} for line in dominant],
            title="Dimensiones con un valor dominante (no se grafican)"))
    # Charts pair two by two on wide screens. A lone last chart keeps its
    # half of the row: stretching a drawing made for half the width would
    # blow its text up to twice the size of its neighbours'.
    return output


# --------------------------------------------------------------- 5 · datos

def _data_tab(db: Session, orders, expenses, data: dict) -> list[dict]:
    output: list[dict] = []
    notes: list[dict] = []

    imported = sum(1 for row in list(orders) + list(expenses)
                   if isinstance(row.extra, dict) and str(row.extra.get("source", "")) == "api")
    manual = len(orders) + len(expenses) - imported
    notes.append({"title": f"{integer(imported)} registros vienen de la fuente sincronizada",
                  "detail": f"{integer(manual)} registrados a mano o importados de Excel.",
                  "tone": "info"})
    missing = sum(1 for e in expenses if not e.receipt_url and not e.receipt_name)
    if missing:
        notes.append({"title": f"{integer(missing)} gastos sin comprobante",
                      "detail": "Pídale al chat «gastos sin comprobante» para verlos.",
                      "tone": "warning"})
        data["gastos_sin_comprobante"] = missing
    for entity, rows in (("órdenes", orders), ("gastos", expenses)):
        _, apart = _split_sums(rows)
        for code, total in apart.items():
            notes.append({"title": f"{amount_text(total, code)} {code} en {entity} sin tasa USD",
                          "detail": f"Configure una tasa para {code} si debe sumarse.",
                          "tone": "warning"})
    span = _date_span(list(orders) + list(expenses))
    if span and span[0] == span[1]:
        notes.append({"title": f"Todos los registros datan del {span[0]}",
                      "detail": "Suele ser la fecha de importación: la fuente no traía fecha.",
                      "tone": "warning"})
    data["origen"] = {"importados": imported, "manuales": manual}
    output.append(blocks.listing(notes, title="Origen y calidad"))

    for label, rows in (("Órdenes", orders), ("Gastos", expenses)):
        amounts = _usd_amounts(rows)
        if len(amounts) >= 4:
            output.append(_stats_table(label, amounts, data))
    feed_table = _numeric_columns_table(db, orders, expenses, data)
    if feed_table:
        output.append(feed_table)
    return output


def _stats_table(label: str, amounts: list[float], data: dict) -> dict:
    """The usual statistics of the USD amounts, for whoever wants them."""
    q1, q2, q3 = quantiles(amounts, n=4)
    avg = mean(amounts)
    stats = [
        ("Registros con tasa USD", integer(len(amounts))),
        ("Suma", usd_compact(sum(amounts))),
        ("Promedio", usd_compact(avg)),
        ("Mediana (caso típico)", usd_compact(q2)),
        ("Mínimo", usd_compact(amounts[0])),
        ("25 % de las órdenes está por debajo de", usd_compact(q1)),
        ("75 % de las órdenes está por debajo de", usd_compact(q3)),
        ("Máximo", usd_compact(amounts[-1])),
    ]
    data[f"descriptiva_{label.lower()}"] = {
        "n": len(amounts), "suma": round(sum(amounts), 2), "media": round(avg, 2),
        "mediana": round(q2, 2), "min": round(amounts[0], 2), "p25": round(q1, 2),
        "p75": round(q3, 2), "max": round(amounts[-1], 2)}
    return blocks.table(
        columns=[{"key": "medida", "label": "Medida"},
                 {"key": "valor", "label": "Valor", "align": "right"}],
        rows=[{"medida": name, "valor": value} for name, value in stats],
        caption=f"{label} · montos en USD equivalente, todos los estados")


def _numeric_columns_table(db: Session, orders, expenses, data: dict) -> dict | None:
    """Range and average of every numeric column the feed brought.

    Identifier-like columns (document numbers, phones) are numbers but not
    quantities — summing them is meaningless, so they are left out.
    """
    rows_out: list[dict] = []
    for entity, rows, entity_label in (("orders", orders, "órdenes"),
                                       ("expenses", expenses, "gastos")):
        for column in db.scalars(
                select(CustomColumn).where(CustomColumn.entity == entity)).all():
            if column.data_type == "formula" or _id_like(column.key) or _id_like(column.label):
                continue
            values = [(row.extra or {}).get(column.key) for row in rows
                      if isinstance(row.extra, dict)]
            numeric = [v for v in values
                       if isinstance(v, (int, float)) and not isinstance(v, bool)]
            present = [v for v in values if v not in (None, "")]
            if len(present) < 2 or len(numeric) < len(present) * 0.6:
                continue
            rows_out.append({
                "columna": _humanize(column.label), "entidad": entity_label,
                "n": integer(len(numeric)),
                "total": amount_text(sum(numeric), "USD"),
                "promedio": amount_text(mean(numeric), "USD"),
                "minimo": amount_text(min(numeric), "USD"),
                "maximo": amount_text(max(numeric), "USD"),
            })
            data.setdefault("columnas_numericas", {})[column.label] = {
                "suma": round(sum(numeric), 2), "promedio": round(mean(numeric), 2)}
    if not rows_out:
        return None
    return blocks.table(
        columns=[{"key": "columna", "label": "Columna de la fuente"},
                 {"key": "entidad", "label": "En"},
                 {"key": "n", "label": "Valores", "align": "right"},
                 {"key": "total", "label": "Total", "align": "right"},
                 {"key": "promedio", "label": "Promedio", "align": "right"},
                 {"key": "minimo", "label": "Mínimo", "align": "right"},
                 {"key": "maximo", "label": "Máximo", "align": "right"}],
        rows=rows_out, wide=True,
        caption="Otras cifras que trae la fuente, en sus propias unidades.")
