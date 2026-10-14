"""Income statement (estado de resultados) from the company's own plan.

Generic by construction: nothing here names an account. The plan
(services/plan.py) says which boxes exist and how totals are made; this module
fills the input boxes with the month's figures, evaluates every recipe in
dependency order with the safe evaluator (services/formula.py), and adds the
two classic readings:

  vertical    each line as a share of the line it is measured against
              (`percent_of`): «Rent is 36 % of Fixed Costs».
  horizontal  each line against a comparison month: «+14,2 % vs April».

A missing figure is never invented and never hidden. An input without a
figure counts as zero inside the recipes, so the rest of the statement still
adds up, and every line it touches is marked `partial`; a line whose inputs
are all missing is `missing` and shows no value.
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent import blocks
from ..agent import formatting as fmt
from ..models import AccountValue
from . import finance
from . import formula as formula_service
from . import plan as plan_service

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
GOOD, BAD, NEUTRAL = "success", "danger", "neutral"


class StatementError(ValueError):
    pass


# -------------------------------------------------------------------- values

def periods_with_data(db: Session) -> list[str]:
    """Every month that has at least one figure, newest first."""
    found = db.scalars(select(AccountValue.period).distinct()).all()
    return sorted(set(found), reverse=True)


def values_for(db: Session, periods: list[str]) -> dict[str, dict[str, float]]:
    """{period: {account_key: amount}} for the requested months."""
    out: dict[str, dict[str, float]] = {period: {} for period in periods}
    if not periods:
        return out
    rows = db.scalars(select(AccountValue).where(AccountValue.period.in_(periods))).all()
    for row in rows:
        out.setdefault(row.period, {})[row.account_key] = row.amount
    return out


def set_values(db: Session, items: list[dict], actor: str | None,
               origin: str = "typed") -> dict:
    """Write figures of input accounts. `amount: null` erases a figure."""
    inputs = {row.key: row for row in plan_service.accounts(db) if row.kind == "input"}
    problems: list[str] = []
    written = erased = 0
    stamp = datetime.now().isoformat(timespec="seconds")

    for item in items or []:
        key = str(item.get("account_key") or "").strip()
        period = str(item.get("period") or "").strip()
        if key not in inputs:
            problems.append(f"«{key}» no es una cuenta de entrada del plan.")
            continue
        if not PERIOD_RE.fullmatch(period):
            problems.append(f"«{period}» no es un mes válido (AAAA-MM).")
            continue
        raw = item.get("amount")
        existing = db.scalars(select(AccountValue).where(
            AccountValue.account_key == key, AccountValue.period == period)).first()
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if existing is not None:
                db.delete(existing)
                erased += 1
            continue
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            problems.append(f"«{inputs[key].name}» {period}: «{raw}» no es un número.")
            continue
        if existing is None:
            db.add(AccountValue(account_key=key, period=period, amount=amount, origin=origin,
                                updated_at=stamp, updated_by=actor))
        else:
            existing.amount, existing.origin = amount, origin
            existing.updated_at, existing.updated_by = stamp, actor
        written += 1

    if problems and not (written or erased):
        db.rollback()
        raise StatementError(" ".join(problems[:5]))
    db.commit()
    return {"written": written, "erased": erased, "problems": problems[:20]}


def grid(db: Session, first: str | None = None, last: str | None = None) -> dict:
    """Input accounts × months, for the editable table and the template."""
    known = periods_with_data(db)
    last = last if last and PERIOD_RE.fullmatch(last) else (known[0] if known else _this_month())
    first = first if first and PERIOD_RE.fullmatch(first) else _shift(last, -11)
    if first > last:
        first, last = last, first
    periods = _span(first, last)
    if len(periods) > 120:
        raise StatementError("Pida como máximo 10 años a la vez.")
    rows = plan_service.accounts(db)
    depth = plan_service.levels(rows)
    stored = values_for(db, periods)
    # A heading earns its row only when something fillable sits under it.
    parents = {row.key: row.parent_key for row in rows}
    holding: set[str] = set()
    for row in rows:
        if row.kind == "input":
            current = row.parent_key
            while current and current not in holding:
                holding.add(current)
                current = parents.get(current)
    return {
        "periods": periods,
        "accounts": [{"key": row.key, "name": row.name, "level": depth.get(row.key, 0),
                      "kind": row.kind, "unit": row.unit} for row in rows
                     if row.kind == "input" or (row.kind == "group" and row.key in holding)],
        "values": {period: stored.get(period, {}) for period in periods},
    }


# -------------------------------------------------------------------- engine

def evaluate(rows: list[dict], inputs: dict[str, float]) -> dict[str, dict]:
    """Every account's value for one month: {key: {value, status}}.

    Pure: no database. Used by the statement and by the import checks, which
    evaluate a month that is not stored yet.
    """
    by_key = {row["key"]: row for row in rows}
    scope: dict[str, float | None] = {}
    result: dict[str, dict] = {}

    for row in rows:
        if row["kind"] == "input":
            present = row["key"] in inputs and inputs[row["key"]] is not None
            scope[row["key"]] = float(inputs[row["key"]]) if present else 0.0
            result[row["key"]] = {"value": scope[row["key"]] if present else None,
                                  "status": "ok" if present else "missing"}
        elif row["kind"] == "group":
            result[row["key"]] = {"value": None, "status": "group"}

    feeding = _inputs_behind(rows)
    for key in plan_service.evaluation_order(rows):
        value = formula_service.evaluate(by_key[key]["formula"], scope)
        value = round(float(value), 6) if isinstance(value, (int, float)) else None
        scope[key] = value if value is not None else 0.0
        behind = feeding.get(key, set())
        missing = [name for name in behind if result.get(name, {}).get("status") == "missing"]
        if behind and len(missing) == len(behind):
            status, value = "missing", None
        else:
            status = "partial" if missing else "ok"
        result[key] = {"value": value, "status": status}
    return result


def _inputs_behind(rows: list[dict]) -> dict[str, set[str]]:
    """Total -> every input its recipe ends up reading."""
    deps = plan_service.dependencies(rows)
    kinds = {row["key"]: row["kind"] for row in rows}
    memo: dict[str, set[str]] = {}

    def walk(key: str, trail: frozenset) -> set[str]:
        if key in memo:
            return memo[key]
        found: set[str] = set()
        for name in deps.get(key, set()):
            if kinds.get(name) == "input":
                found.add(name)
            elif name in deps and name not in trail:
                found |= walk(name, trail | {name})
        memo[key] = found
        return found

    return {key: walk(key, frozenset({key})) for key in deps}


def compute(db: Session, period: str | None = None, compare: str | None = "prev") -> dict:
    """The statement of `period` (default: latest month with figures)."""
    rows = [plan_service.view(row) for row in plan_service.accounts(db)]
    if not rows:
        raise StatementError("Todavía no hay plan de cuentas. Cárguelo en Configuración.")
    depth = plan_service.levels(rows)
    known = periods_with_data(db)

    period = period if period and PERIOD_RE.fullmatch(period) else (
        known[0] if known else _this_month())
    if compare in (None, "", "none"):
        compare = None
    elif compare == "prev":
        compare = finance.prev_period(period)
    elif compare == "year":
        compare = _shift(period, -12)
    elif not PERIOD_RE.fullmatch(compare):
        raise StatementError("Comparación inválida: use prev, year, none o AAAA-MM.")

    stored = values_for(db, [p for p in (period, compare) if p])
    current = evaluate(rows, stored.get(period, {}))
    previous = evaluate(rows, stored.get(compare, {})) if compare else {}

    lines = []
    for row in rows:
        now = current[row["key"]]
        before = previous.get(row["key"], {}) if compare else {}
        base = current.get(row["percent_of"] or "", {}).get("value")
        vertical = (now["value"] / base * 100
                    if now["value"] is not None and base not in (None, 0) else None)
        variation = variation_pct = None
        if now["value"] is not None and before.get("value") is not None:
            variation = now["value"] - before["value"]
            if row["unit"] == "usd" and before["value"] != 0:
                variation_pct = variation / abs(before["value"]) * 100
        lines.append({
            "key": row["key"], "name": row["name"], "kind": row["kind"], "unit": row["unit"],
            "nature": row["nature"], "level": depth.get(row["key"], 0),
            "headline": row["headline"], "definition": row["definition"],
            "percent_of": row["percent_of"],
            "value": _round(now["value"]), "status": now["status"],
            "vertical_pct": _round(vertical, 4),
            "previous": _round(before.get("value")), "previous_status": before.get("status"),
            "variation": _round(variation), "variation_pct": _round(variation_pct, 4),
            "tone": _tone(row["nature"], variation),
        })

    missing = [line["name"] for line in lines
               if line["kind"] == "input" and line["status"] == "missing"]
    return {
        "period": period, "compare": compare, "periods": known,
        "has_data": bool(stored.get(period)),
        "compare_has_data": bool(compare and stored.get(compare)),
        "rows": lines, "missing": missing,
    }


def _tone(nature: str, variation: float | None) -> str:
    if not variation:
        return NEUTRAL
    up = variation > 0
    return GOOD if up == (nature in ("income", "result")) else BAD


def _round(value, digits: int = 2):
    return round(value, digits) if isinstance(value, (int, float)) else None


# -------------------------------------------------------------------- blocks

def display(value, unit: str) -> str:
    """One figure as the statement prints it (es-CO)."""
    if value is None:
        return "—"
    if unit == "bps":
        return f"{fmt.thousands(value, 1)} pb"
    if unit == "pct":
        return fmt.percent(value)
    return fmt.thousands(value, 2)


def to_blocks(result: dict) -> list[dict]:
    """The statement as typed blocks: the same list feeds chat, PDF and audits."""
    period, compare = result["period"], result["compare"]
    title = f"Estado de resultados · {fmt.period_name(period)}"
    detail = f"Comparado con {fmt.period_name(compare)}" if compare else ""
    out = [blocks.heading(title, detail)]

    if not result["has_data"]:
        out.append(blocks.notice(f"No hay cifras cargadas para {fmt.period_name(period)}.",
                                 tone="warning"))
        return out

    headline = [line for line in result["rows"] if line["headline"]][:4]
    if headline:
        out.append(blocks.kpis([{
            "label": line["name"],
            "value": (fmt.usd_compact(line["value"])
                      if line["unit"] == "usd" and line["value"] is not None
                      else display(line["value"], line["unit"])),
            "delta": (fmt.delta(line["variation_pct"]) + f" vs {fmt.month_name(compare)}"
                      if line["variation_pct"] is not None else ""),
            "positive": line["tone"] != BAD,
        } for line in headline]))

    columns = [{"key": "account", "label": "Cuenta"},
               {"key": "value", "label": fmt.period_name(period), "align": "right"},
               {"key": "vertical", "label": "% vertical", "align": "right"}]
    if compare:
        columns += [{"key": "previous", "label": fmt.period_name(compare), "align": "right"},
                    {"key": "variation", "label": "Variación", "align": "right"},
                    {"key": "variation_pct", "label": "Var. %", "align": "right"}]
    rows = []
    for line in result["rows"]:
        # Non-breaking spaces: every PDF font has them and HTML does not collapse them.
        indent = "\u00a0" * 4 * line["level"]
        if line["kind"] == "group":
            rows.append({"account": f"{indent}{line['name']}"})
            continue
        rows.append({
            "account": f"{indent}{line['name']}" + (" *" if line["status"] == "partial" else ""),
            "value": display(line["value"], line["unit"]),
            "vertical": fmt.percent(line["vertical_pct"]) if line["vertical_pct"] is not None else "",
            "previous": display(line["previous"], line["unit"]),
            "variation": (display(line["variation"], line["unit"])
                          if line["variation"] is not None else "—"),
            "variation_pct": (fmt.delta(line["variation_pct"])
                              if line["variation_pct"] is not None else ""),
        })
    caption = "Cifras en USD. * Línea calculada con cifras faltantes." if any(
        line["status"] == "partial" for line in result["rows"]) else "Cifras en USD."
    out.append(blocks.table(columns, rows, caption=caption, wide=True))

    if result["missing"]:
        out.append(blocks.notice(
            f"Sin cifra en {fmt.period_name(period)}: {', '.join(result['missing'][:12])}"
            + ("…" if len(result["missing"]) > 12 else "") + ". Cuentan como cero.",
            tone="warning"))
    return out


# ------------------------------------------------------------------- periods

def _this_month() -> str:
    return datetime.now().strftime("%Y-%m")


def _shift(period: str, months: int) -> str:
    year, month = int(period[:4]), int(period[5:7])
    index = year * 12 + (month - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _span(first: str, last: str) -> list[str]:
    out, current = [], first
    while current <= last:
        out.append(current)
        current = _shift(current, 1)
    return out
