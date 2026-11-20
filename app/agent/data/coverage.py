"""Data coverage: which months the database can actually answer for.

The agent must explain missing data instead of filling it. That needs to know,
on every question, what exists: the months with OTC operations, the months
with expenses and with commissions in the income statement, and where the two
disagree (operations but no expenses: that close has no Net Revenue).

Computed from the database, never written by hand, so it stays true as data
is loaded. Cached for a few minutes: it runs on every chat turn.
"""
from __future__ import annotations

import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import AccountValue, Order

TTL_SECONDS = 300
COMMISSIONS = "sales_commissions"

_cache: dict[str, tuple[float, str]] = {}


def _runs(months: list[str]) -> str:
    """['2024-01','2024-02','2024-04'] -> '2024-01 a 2024-02, 2024-04'."""
    def index(month: str) -> int:
        year, number = month.split("-")
        return int(year) * 12 + int(number) - 1

    runs: list[list[str]] = []
    for month in sorted(set(months)):
        if runs and index(month) == index(runs[-1][-1]) + 1:
            runs[-1].append(month)
        else:
            runs.append([month])
    return ", ".join(r[0] if len(r) == 1 else f"{r[0]} a {r[-1]}" for r in runs) or "ninguno"


def _otc_months(db: Session) -> list[str]:
    month = func.substr(Order.date, 1, 7)
    rows = db.execute(select(month).distinct().where(
        Order.extra["source"].as_string() == "otc")).scalars()
    return [m for m in rows if m]


def _account_months(db: Session, commissions: bool) -> list[str]:
    condition = (AccountValue.account_key == COMMISSIONS if commissions
                 else AccountValue.account_key != COMMISSIONS)
    return [m for m in db.execute(select(AccountValue.period).distinct()
                                  .where(condition)).scalars() if m]


def describe(db: Session) -> str:
    """The coverage, as a few lines of Spanish for the system prompt."""
    otc = _otc_months(db)
    expenses = _account_months(db, commissions=False)
    commissions = _account_months(db, commissions=True)
    no_expenses = [m for m in otc if m not in set(expenses)]
    no_commissions = [m for m in otc if m not in set(commissions)]
    lines = [
        f"- Operaciones OTC (orders): {_runs(otc)}.",
        f"- Gastos en el estado de resultados (account_values): {_runs(expenses)}.",
        f"- Comisiones (sales_commissions): {_runs(commissions)}.",
    ]
    if no_expenses:
        lines.append(f"- Meses con operaciones pero SIN gastos: {_runs(no_expenses)}. "
                     "Su cierre no tiene Net Revenue: muestre «sin dato» y explíquelo.")
    if no_commissions and no_commissions != no_expenses:
        lines.append(f"- Meses con operaciones pero SIN comisiones: {_runs(no_commissions)}.")
    return "\n".join(lines)


def cached(db: Session) -> str:
    """describe(), recomputed at most every TTL_SECONDS; never breaks a turn."""
    now = time.monotonic()
    hit = _cache.get("coverage")
    if hit and now - hit[0] < TTL_SECONDS:
        return hit[1]
    try:
        text = describe(db)
    except Exception:  # noqa: BLE001 — coverage helps; its absence must not fail the chat
        db.rollback()
        return hit[1] if hit else "(no disponible)"
    _cache["coverage"] = (now, text)
    return text


def clear() -> None:
    _cache.clear()
