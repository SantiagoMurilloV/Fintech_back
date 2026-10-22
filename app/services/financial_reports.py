"""The finance team's recurring deliverables, computed the same way every time.

    daily_close(date)                    closing report of one day
    income_statement(from, to, view)     the statement of a range; view = full,
                                         vertical (each line as % of its base)
                                         or horizontal (change vs another range)
    dashboard(kind, from, to)            operations · finance · management

Every figure comes from the database: trades from `orders` (the OTC fields in
`extra`), the statement from the accounting plan (`services/statement`),
prices from `rate_snapshots`. The output is typed blocks (KPIs, tables,
charts) — the chat shows them, and `save_pdf` turns the same blocks into a
PDF in Reportes. The model never computes any of it.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent import blocks
from ..agent import formatting as fmt
from ..models import Expense, Order, RateSnapshot, Report
from . import charts, finance, pdf, statement, storage

WIDE = 1120


class ReportError(ValueError):
    pass


# ------------------------------------------------------------------ helpers

def _f(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _usd(value) -> str:
    return f"USD {fmt.thousands(value, 2)}"


def _pct(value) -> str:
    return "—" if value is None else fmt.percent(value)


def _bps(profit: float, volume: float) -> float | None:
    return profit / volume * 10_000 if volume else None


def _day(text: str | None) -> str:
    if text:
        try:
            return datetime.strptime(str(text)[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            raise ReportError("La fecha debe tener formato AAAA-MM-DD.") from None
    return date.today().isoformat()


def _month_bounds(first: str, last: str) -> tuple[str, str]:
    year, month = int(last[:4]), int(last[5:7])
    end = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).isoformat()
    return f"{first}-01", end


def orders_between(db: Session, start: str, end: str) -> list[Order]:
    return list(db.scalars(select(Order).where(Order.date >= start, Order.date <= end)).all())


def latest_trade_month(db: Session) -> str | None:
    last = db.scalar(select(Order.date).order_by(Order.date.desc()).limit(1))
    return last[:7] if last else None


def metrics(orders: list[Order]) -> dict:
    """Volume, spread, provider cost and profit of a set of trades, with splits."""
    out = {"trades": len(orders), "volume": 0.0, "off_ramp": 0.0, "on_ramp": 0.0,
           "spread": 0.0, "provider_cost": 0.0, "profit": 0.0, "clients": set()}
    by_provider = defaultdict(lambda: {"trades": 0, "volume": 0.0, "cost": 0.0, "profit": 0.0})
    by_client = defaultdict(lambda: {"trades": 0, "volume": 0.0, "profit": 0.0})
    by_asset = defaultdict(lambda: {"trades": 0, "volume": 0.0})
    by_day = defaultdict(lambda: {"trades": 0, "volume": 0.0, "profit": 0.0})
    for order in orders:
        extra = order.extra if isinstance(order.extra, dict) else {}
        usd = finance.usd_eq(order.amount, order.currency)
        profit, cost = _f(extra.get("utilidad")), _f(extra.get("costo_proveedor"))
        out["volume"] += usd
        out["off_ramp" if extra.get("tipo") != "On-ramp" else "on_ramp"] += usd
        out["spread"] += _f(extra.get("spread_bruto"))
        out["provider_cost"] += cost
        out["profit"] += profit
        out["clients"].add(order.customer)
        provider = order.gateway or "Sin proveedor"
        for bucket, key in ((by_provider, provider), (by_client, order.customer)):
            bucket[key]["trades"] += 1
            bucket[key]["volume"] += usd
            bucket[key]["profit"] += profit
        by_provider[provider]["cost"] += cost
        asset = extra.get("activo") or order.currency
        by_asset[asset]["trades"] += 1
        by_asset[asset]["volume"] += usd
        by_day[order.date]["trades"] += 1
        by_day[order.date]["volume"] += usd
        by_day[order.date]["profit"] += profit
    out["clients"] = len(out["clients"])
    out["ticket"] = out["volume"] / out["trades"] if out["trades"] else 0.0
    out["margin_bps"] = _bps(out["profit"], out["volume"])
    out["by_provider"] = dict(by_provider)
    out["by_client"] = dict(by_client)
    out["by_asset"] = dict(by_asset)
    out["by_day"] = dict(sorted(by_day.items()))
    return out


def _change(now: float, before: float) -> str:
    if not before:
        return "sin base de comparación"
    return f"{fmt.delta((now - before) / abs(before) * 100)} vs. período anterior"


def _provider_table(m: dict, caption: str) -> dict:
    rows = []
    for name, p in sorted(m["by_provider"].items(), key=lambda i: -i[1]["volume"]):
        rows.append({"proveedor": name, "operaciones": fmt.integer(p["trades"]),
                     "volumen": _usd(p["volume"]), "costo": _usd(p["cost"]),
                     "costo_pb": "—" if not p["volume"] else
                     f"{fmt.thousands(p['cost'] / p['volume'] * 10_000, 1)} pb",
                     "utilidad": _usd(p["profit"])})
    return blocks.table(columns=[
        {"key": "proveedor", "label": "Proveedor"},
        {"key": "operaciones", "label": "Operaciones", "align": "right"},
        {"key": "volumen", "label": "Volumen", "align": "right"},
        {"key": "costo", "label": "Costo proveedor", "align": "right"},
        {"key": "costo_pb", "label": "Costo / volumen", "align": "right"},
        {"key": "utilidad", "label": "Utilidad", "align": "right"}],
        rows=rows, caption=caption, wide=True)


def _client_table(m: dict, caption: str, limit: int = 10) -> dict:
    total = m["volume"] or 1
    rows = [{"cliente": name, "operaciones": fmt.integer(c["trades"]), "volumen": _usd(c["volume"]),
             "participacion": fmt.percent(c["volume"] / total * 100), "utilidad": _usd(c["profit"])}
            for name, c in sorted(m["by_client"].items(), key=lambda i: -i[1]["volume"])[:limit]]
    return blocks.table(columns=[
        {"key": "cliente", "label": "Cliente"},
        {"key": "operaciones", "label": "Operaciones", "align": "right"},
        {"key": "volumen", "label": "Volumen", "align": "right"},
        {"key": "participacion", "label": "% del volumen", "align": "right"},
        {"key": "utilidad", "label": "Utilidad", "align": "right"}],
        rows=rows, caption=caption, wide=True)


def _price_on(db: Session, pair: str, day: str) -> tuple[float | None, str | None]:
    """The price of `pair` on `day`: a reading of that day, else the daily close."""
    row = db.scalars(select(RateSnapshot).where(
        RateSnapshot.pair == pair, RateSnapshot.taken_at >= f"{day}T00:00:00",
        RateSnapshot.taken_at <= f"{day}T23:59:59")
        .order_by(RateSnapshot.taken_at.desc())).first()
    if row is None:
        row = db.scalars(select(RateSnapshot).where(
            RateSnapshot.pair == pair, RateSnapshot.rate_date == day)).first()
    return (row.price, row.source) if row else (None, None)


def _data(summary: dict, extra: dict | None = None) -> dict:
    """Raw figures for the model: no sets, rounded."""
    clean = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in summary.items()
             if k not in ("by_provider", "by_client", "by_asset", "by_day")}
    return {**clean, **(extra or {})}


# --------------------------------------------------------------- daily close

def daily_close(db: Session, day: str | None = None) -> dict:
    """The closing report of one day: trades, providers, clients, market, expenses."""
    day = _day(day)
    today = metrics(orders_between(db, day, day))
    previous_day = db.scalar(select(Order.date).where(Order.date < day)
                             .order_by(Order.date.desc()).limit(1))
    before = metrics(orders_between(db, previous_day, previous_day)) if previous_day else None
    month = metrics(orders_between(db, f"{day[:7]}-01", day))
    expenses = list(db.scalars(select(Expense).where(Expense.date == day)).all())
    usdt, usdt_source = _price_on(db, "USDT/USD", day)
    trm, _ = _price_on(db, "USD/COP", day)

    title = f"Cierre diario · {fmt.short_date(day)} {day[:4]}"
    out = [blocks.heading(title, "Operaciones del día, proveedores, clientes, mercado y gastos.")]
    if not today["trades"]:
        out.append(blocks.notice(f"No hay operaciones registradas el {day}."
                                 + (f" La última fue el {previous_day}." if previous_day else ""),
                                 tone="info"))
    def vs(key):
        return _change(today[key], before[key]) if before else ""
    out.append(blocks.kpis([
        {"label": "Operaciones", "value": fmt.integer(today["trades"]),
         "delta": vs("trades"), "tone": "neutral"},
        {"label": "Volumen", "value": _usd(today["volume"]), "delta": vs("volume"), "tone": "neutral"},
        {"label": "Utilidad operativa", "value": _usd(today["profit"]), "delta": vs("profit"),
         "tone": "success" if today["profit"] >= 0 else "danger"},
        {"label": "Margen", "value": "—" if today["margin_bps"] is None else
         f"{fmt.thousands(today['margin_bps'], 1)} pb", "delta": "utilidad / volumen",
         "tone": "neutral"},
    ]))
    out.append(blocks.kpis([
        {"label": "Off-ramp", "value": _usd(today["off_ramp"]), "delta": "USDT → USD", "tone": "neutral"},
        {"label": "On-ramp", "value": _usd(today["on_ramp"]), "delta": "USD → USDT", "tone": "neutral"},
        {"label": "Costo proveedores", "value": _usd(today["provider_cost"]), "delta": "", "tone": "warning"},
        {"label": "Acumulado del mes", "value": _usd(month["profit"]),
         "delta": f"{fmt.integer(month['trades'])} operaciones · {_usd(month['volume'])}",
         "tone": "neutral"},
    ]))
    if today["trades"]:
        out.append(_provider_table(today, "Por proveedor, el día"))
        out.append(_client_table(today, "Clientes del día por volumen", 5))
    market = [{"dato": "USDT/USD", "valor": "—" if usdt is None else fmt.thousands(usdt, 5),
               "fuente": usdt_source or "sin lectura ese día"},
              {"dato": "TRM USD/COP", "valor": "—" if trm is None else fmt.thousands(trm, 2),
               "fuente": "Superfinanciera" if trm else "sin lectura ese día"},
              {"dato": "Gastos del día", "valor": _usd(sum(finance.usd_eq(e.amount, e.currency)
                                                         for e in expenses)),
               "fuente": f"{fmt.integer(len(expenses))} registrados"}]
    out.append(blocks.table(columns=[{"key": "dato", "label": "Mercado y gastos"},
                                     {"key": "valor", "label": "Valor", "align": "right"},
                                     {"key": "fuente", "label": "Fuente"}], rows=market))
    return {"title": title, "subtitle": f"Día {day}", "period": day[:7], "blocks": out,
            "data": {"dia": day, "dia_anterior_con_operaciones": previous_day,
                     **{f"dia_{k}": v for k, v in _data(today).items()},
                     **({f"anterior_{k}": v for k, v in _data(before).items()} if before else {}),
                     **{f"mes_{k}": v for k, v in _data(month).items()},
                     "usdt_usd": usdt, "trm": trm, "gastos_dia": len(expenses)}}


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


# ---------------------------------------------------------------- dashboards

KINDS = ("operations", "finance", "management")
KIND_NAMES = {"operations": "Operaciones", "finance": "Finanzas", "management": "Gerencia"}


def _range(db: Session, first: str | None, last: str | None) -> tuple[str, str]:
    default = latest_trade_month(db)
    if not default:
        known = statement.periods_with_data(db)
        default = known[0] if known else None
    last = last or first or default
    first = first or last
    if not last:
        raise ReportError("Todavía no hay datos para el tablero.")
    return tuple(sorted((first, last)))


def _trend_chart(series: list[dict], title: str) -> dict:
    chart = blocks.chart(charts.render("bar", series, width=WIDE, height=340), title=title,
                         chart_type="bar", series=series)
    chart["wide"] = True
    return chart


def _monthly(orders: list[Order]) -> dict[str, dict]:
    months = defaultdict(list)
    for order in orders:
        months[order.date[:7]].append(order)
    return {month: metrics(rows) for month, rows in sorted(months.items())}


def dashboard(db: Session, kind: str, first: str | None = None, last: str | None = None) -> dict:
    if kind not in KINDS:
        raise ReportError("El tablero debe ser operations, finance o management.")
    first, last = _range(db, first, last)
    start, end = _month_bounds(first, last)
    span = fmt.period_name(first) if first == last else \
        f"{fmt.period_name(first)} – {fmt.period_name(last)}"
    months = len(statement._span(first, last))
    prev_first, prev_last = statement._shift(first, -months), statement._shift(first, -1)
    now = metrics(orders_between(db, start, end))
    before = metrics(orders_between(db, *_month_bounds(prev_first, prev_last)))
    title = f"Tablero de {KIND_NAMES[kind]} · {span}"
    build = {"operations": _operations, "finance": _finance, "management": _management}[kind]
    out, data = build(db, now, before, first, last, start, end)
    return {"title": title, "subtitle": span, "period": last,
            "blocks": [blocks.heading(title, f"Comparado con el período anterior de igual duración "
                                             f"({fmt.period_name(prev_first)}"
                                             + (f" – {fmt.period_name(prev_last)}" if months > 1 else "")
                                             + ").")] + out,
            "data": {"desde": first, "hasta": last, **data}}


def _operations(db, now, before, first, last, start, end):
    out = [blocks.kpis([
        {"label": "Operaciones", "value": fmt.integer(now["trades"]),
         "delta": _change(now["trades"], before["trades"]), "tone": "neutral"},
        {"label": "Volumen", "value": _usd(now["volume"]),
         "delta": _change(now["volume"], before["volume"]), "tone": "neutral"},
        {"label": "Ticket promedio", "value": _usd(now["ticket"]),
         "delta": _change(now["ticket"], before["ticket"]), "tone": "neutral"},
        {"label": "Clientes activos", "value": fmt.integer(now["clients"]),
         "delta": _change(now["clients"], before["clients"]), "tone": "neutral"},
    ]), blocks.kpis([
        {"label": "Off-ramp", "value": _usd(now["off_ramp"]),
         "delta": _pct(now["off_ramp"] / now["volume"] * 100 if now["volume"] else None)
                  + " del volumen", "tone": "neutral"},
        {"label": "On-ramp", "value": _usd(now["on_ramp"]),
         "delta": _pct(now["on_ramp"] / now["volume"] * 100 if now["volume"] else None)
                  + " del volumen", "tone": "neutral"},
        {"label": "Costo proveedores", "value": _usd(now["provider_cost"]),
         "delta": "—" if not now["volume"] else
         f"{fmt.thousands(now['provider_cost'] / now['volume'] * 10_000, 1)} pb del volumen",
         "tone": "warning"},
    ])]
    daily = [{"label": day[5:], "value": round(v["volume"], 2),
              "display": fmt.usd_compact(v["volume"]).replace("USD ", "")}
             for day, v in now["by_day"].items()]
    if first != last:
        daily = [{"label": m[2:], "value": round(v["volume"], 2),
                  "display": fmt.usd_compact(v["volume"]).replace("USD ", "")}
                 for m, v in _monthly(orders_between(db, start, end)).items()]
    if len(daily) >= 2:
        out.append(_trend_chart(daily[-31:], "Volumen por " + ("mes" if first != last else "día")
                                + " (USD)"))
    out.append(_provider_table(now, "Proveedores: volumen, costo y costo por dólar operado"))
    assets = [{"label": a, "value": round(v["volume"], 2), "display": fmt.usd_compact(v["volume"])}
              for a, v in sorted(now["by_asset"].items(), key=lambda i: -i[1]["volume"])]
    if len(assets) >= 2:
        out.append(blocks.chart(charts.render("hbar", assets), title="Volumen por activo",
                                chart_type="hbar", series=assets))
    out.append(_client_table(now, "Clientes con más volumen", 10))
    return out, {"actual": _data(now), "anterior": _data(before)}


def _finance(db, now, before, first, last, start, end):
    result = income_statement(db, first, last, "full", "prev")
    lines = result["data"]["cuentas"]

    def line(key):
        return (lines.get(key) or {}).get("valor")

    gross, operational, net = line("gross_profit"), line("operational_profit"), line("net_result")
    costs = line("total_costs_expenses")
    out = [blocks.kpis([
        {"label": "Utilidad bruta", "value": "—" if gross is None else _usd(gross),
         "delta": _pct((lines.get("gross_profit") or {}).get("variacion_pct")) + " vs. anterior",
         "tone": "neutral"},
        {"label": "Utilidad operacional", "value": "—" if operational is None else _usd(operational),
         "delta": "margen " + _pct(operational / gross * 100 if gross and operational is not None else None),
         "tone": "success" if (operational or 0) >= 0 else "danger"},
        {"label": "Costos y gastos", "value": "—" if costs is None else _usd(costs),
         "delta": _pct((lines.get("total_costs_expenses") or {}).get("variacion_pct")) + " vs. anterior",
         "tone": "warning"},
        {"label": "Resultado neto", "value": "—" if net is None else _usd(net),
         "delta": "margen " + _pct(net / gross * 100 if gross and net is not None else None),
         "tone": "success" if (net or 0) >= 0 else "danger"},
    ])]
    series = statement.monthly_series(db, first, last,
                                      ["gross_profit", "operational_profit", "net_result"])
    if len(series) >= 2:
        trend = [{"label": m[2:], "value": v.get("net_result") or 0,
                  "display": fmt.usd_compact(v.get("net_result") or 0).replace("USD ", "")}
                 for m, v in series.items()]
        out.append(_trend_chart(trend, "Resultado neto por mes (USD)"))
    breakdown = [{"label": (lines.get(k) or {}).get("nombre", k), "value": round(abs(line(k) or 0), 2),
                  "display": _usd(line(k) or 0)}
                 for k in ("operational_costs", "variable_costs", "fixed_costs", "marketing",
                           "other_costs_expenses") if line(k)]
    if breakdown:
        out.append(blocks.chart(charts.render("hbar", breakdown), title="Dónde se va el dinero",
                                chart_type="hbar", series=breakdown))
    out.extend(result["blocks"][1:])           # the statement table itself
    otc_profit = now["profit"]
    out.append(blocks.notice(
        f"Control: la utilidad operativa de las operaciones OTC del período suma {_usd(otc_profit)}"
        + (f"; el estado de resultados muestra {_usd(operational)} de utilidad operacional."
           if operational is not None else "."), tone="info"))
    return out, {"estado": result["data"], "otc_utilidad": round(otc_profit, 2)}


def _management(db, now, before, first, last, start, end):
    result = income_statement(db, first, last, "full", "prev")
    lines = result["data"]["cuentas"]
    net = (lines.get("net_result") or {}).get("valor")
    net_change = (lines.get("net_result") or {}).get("variacion_pct")
    year_start = f"{last[:4]}-01"
    ytd = metrics(orders_between(db, f"{year_start}-01", _month_bounds(last, last)[1]))
    top5 = sorted(now["by_client"].values(), key=lambda c: -c["volume"])[:5]
    concentration = sum(c["volume"] for c in top5) / now["volume"] * 100 if now["volume"] else None
    out = [blocks.kpis([
        {"label": "Volumen", "value": _usd(now["volume"]),
         "delta": _change(now["volume"], before["volume"]), "tone": "neutral"},
        {"label": "Utilidad operativa OTC", "value": _usd(now["profit"]),
         "delta": _change(now["profit"], before["profit"]),
         "tone": "success" if now["profit"] >= before["profit"] else "danger"},
        {"label": "Resultado neto", "value": "—" if net is None else _usd(net),
         "delta": (_pct(net_change) + " vs. anterior") if net_change is not None else "",
         "tone": "success" if (net or 0) >= 0 else "danger"},
        {"label": "Margen", "value": "—" if now["margin_bps"] is None else
         f"{fmt.thousands(now['margin_bps'], 1)} pb", "delta": "utilidad / volumen", "tone": "neutral"},
    ]), blocks.kpis([
        {"label": f"Acumulado {last[:4]}", "value": _usd(ytd["profit"]),
         "delta": f"{_usd(ytd['volume'])} de volumen", "tone": "neutral"},
        {"label": "Clientes activos", "value": fmt.integer(now["clients"]),
         "delta": _change(now["clients"], before["clients"]), "tone": "neutral"},
        {"label": "Concentración top 5", "value": _pct(concentration), "delta": "del volumen",
         "tone": "warning" if (concentration or 0) >= 60 else "neutral"},
    ])]
    monthly = _monthly(orders_between(db, f"{statement._shift(last, -11)}-01",
                                      _month_bounds(last, last)[1]))
    trend = [{"label": m[2:], "value": round(v["profit"], 2),
              "display": fmt.usd_compact(v["profit"]).replace("USD ", "")} for m, v in monthly.items()]
    if len(trend) >= 2:
        out.append(_trend_chart(trend, "Utilidad operativa OTC, últimos 12 meses (USD)"))
    attention = []
    if concentration and concentration >= 60:
        attention.append({"title": f"Los 5 principales clientes son el {_pct(concentration)} del volumen",
                          "detail": "Alta dependencia de pocos clientes.", "tone": "warning"})
    cost_now = now["provider_cost"] / now["volume"] * 10_000 if now["volume"] else None
    cost_before = before["provider_cost"] / before["volume"] * 10_000 if before["volume"] else None
    if cost_now and cost_before and cost_now > cost_before * 1.1:
        attention.append({"title": f"El costo de proveedores subió a {fmt.thousands(cost_now, 1)} pb",
                          "detail": f"Antes: {fmt.thousands(cost_before, 1)} pb por dólar operado.",
                          "tone": "warning"})
    if before["profit"] and now["profit"] < before["profit"] * 0.9:
        attention.append({"title": "La utilidad OTC cayó más de 10 %",
                          "detail": _change(now["profit"], before["profit"]), "tone": "danger"})
    gaps = result["data"].get("meses_sin_gastos_cargados") or []
    if gaps:
        attention.insert(0, {"title": "Resultado neto incompleto",
                             "detail": f"Sin comisiones ni gastos cargados para "
                                       f"{', '.join(fmt.period_name(m) for m in gaps)}.",
                             "tone": "danger"})
    if attention:
        out.append(blocks.listing(attention, title="Atención"))
    out.append(_client_table(now, "Principales clientes", 5))
    return out, {"actual": _data(now), "anterior": _data(before), "acumulado_anio": _data(ytd),
                 "concentracion_top5_pct": round(concentration, 2) if concentration else None,
                 "resultado_neto": net, "meses_sin_gastos_cargados": gaps}


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
