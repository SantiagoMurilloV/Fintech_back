"""Deterministic financial aggregation.

All money math happens here and is exposed as RAW DATA (numbers, ISO dates,
codes). Visual formatting is exclusively the frontend's responsibility, and
the chat LLM only narrates figures that were computed here.
"""
from __future__ import annotations

from datetime import date as _date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import FX_TO_USD, STABLECOINS
from ..models import ORDER_STATUSES, Expense, Order


def has_rate(currency: str | None) -> bool:
    """Whether a USD conversion exists for this currency."""
    return (currency or "USD").upper() in FX_TO_USD


def usd_rate(currency: str | None) -> float | None:
    """USD value of one unit of the currency, or None without a configured rate."""
    return FX_TO_USD.get((currency or "USD").upper())


def is_stablecoin(currency: str | None) -> bool:
    """Whether the currency is a dollar-pegged stablecoin counted at its peg."""
    return (currency or "").upper() in STABLECOINS


def usd_eq(amount: float, currency: str | None) -> float:
    """Convert an amount to its USD equivalent using configured FX rates.

    A currency without a configured rate (a volatile token, an exotic code)
    contributes ZERO to USD aggregates instead of being counted 1:1 — with
    money, a made-up exchange rate is worse than an explicit gap. Dollar-pegged
    stablecoins (USDT, USDC) do have a rate: their peg, configurable in
    config.FX_TO_USD. A record without a rate keeps its exact amount and
    currency; only the equivalence is declined. Callers can single those rows
    out with `has_rate`.
    """
    code = (currency or "USD").upper()
    rate = FX_TO_USD.get(code)
    return amount * rate if rate is not None else 0.0


# ---------------------------------------------------------------- periods

def month_bounds(period: str) -> tuple[str, str]:
    """Return [first day, first day of next month) for a 'YYYY-MM' period."""
    y, m = int(period[:4]), int(period[5:7])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}-{m:02d}-01", f"{ny:04d}-{nm:02d}-01"


def prev_period(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{py:04d}-{pm:02d}"


def current_period(db: Session) -> str:
    """Active period = month of the most recent order (or today if empty)."""
    max_date = db.scalar(select(func.max(Order.date)))
    return max_date[:7] if max_date else _date.today().isoformat()[:7]


# ---------------------------------------------------------------- aggregates

def month_summary(db: Session, period: str) -> dict:
    """Aggregate one month of orders and expenses into raw numbers."""
    lo, hi = month_bounds(period)
    orders = db.scalars(select(Order).where(Order.date >= lo, Order.date < hi)).all()
    expenses = db.scalars(select(Expense).where(Expense.date >= lo, Expense.date < hi)).all()

    approved = [o for o in orders if o.status == "approved"]
    revenue = sum(usd_eq(o.amount, o.currency) for o in approved)
    total_expenses = sum(usd_eq(e.amount, e.currency) for e in expenses)

    by_status = {s: sum(1 for o in orders if o.status == s) for s in ORDER_STATUSES}

    # Approved volume per gateway (USD equivalent + share of total).
    gw_volume: dict[str, float] = {}
    for o in approved:
        if o.gateway:
            gw_volume[o.gateway] = gw_volume.get(o.gateway, 0.0) + usd_eq(o.amount, o.currency)
    total_volume = sum(gw_volume.values()) or 1.0
    gateways = sorted(
        ({"name": k, "usd": round(v, 2), "pct": round(100 * v / total_volume, 1)}
         for k, v in gw_volume.items()),
        key=lambda x: -x["usd"],
    )

    rejections_by_gateway: dict[str, int] = {}
    for o in orders:
        if o.status == "rejected" and o.gateway:
            rejections_by_gateway[o.gateway] = rejections_by_gateway.get(o.gateway, 0) + 1

    # Weekly revenue buckets: week 1 = days 1-7, ... week 5 = days 29-31.
    week_totals: dict[int, float] = {}
    for o in approved:
        w = min((int(o.date[8:10]) - 1) // 7 + 1, 5)
        week_totals[w] = week_totals.get(w, 0.0) + usd_eq(o.amount, o.currency)
    weekly = [{"week": w, "usd": round(week_totals.get(w, 0.0), 2)}
              for w in range(1, 6) if w <= 4 or week_totals.get(5)]

    per_category: dict[str, float] = {}
    for e in expenses:
        per_category[e.category] = per_category.get(e.category, 0.0) + usd_eq(e.amount, e.currency)
    expense_categories = sorted(
        ({"name": k, "usd": round(v, 2)} for k, v in per_category.items()),
        key=lambda x: -x["usd"],
    )

    missing_receipts = [e.description for e in expenses if not e.receipt_name and not e.receipt_url]
    margin = (revenue - total_expenses) / revenue * 100 if revenue else 0.0

    return {
        "period": period,
        "revenue_usd": round(revenue, 2),
        "expenses_usd": round(total_expenses, 2),
        "margin_pct": round(margin, 1),
        "orders_count": len(orders),
        "by_status": by_status,
        "gateways": gateways,
        "rejections_by_gateway": rejections_by_gateway,
        "weekly": weekly,
        "expense_categories": expense_categories,
        "missing_receipts": missing_receipts,
    }


def report(db: Session, period: str) -> dict:
    """Month summary plus deltas against the previous month."""
    cur = month_summary(db, period)
    prev = month_summary(db, prev_period(period))

    def delta_pct(a: float, b: float) -> float | None:
        return round((a - b) / b * 100, 1) if b else None

    cur["prev_period"] = prev["period"]
    cur["deltas"] = {
        "revenue_pct": delta_pct(cur["revenue_usd"], prev["revenue_usd"]),
        "expenses_pct": delta_pct(cur["expenses_usd"], prev["expenses_usd"]),
        "margin_pp": round(cur["margin_pct"] - prev["margin_pct"], 1) if prev["revenue_usd"] else None,
        "orders": cur["orders_count"] - prev["orders_count"],
    }
    return cur


def next_order_id(db: Session) -> str:
    """Sequential ORD-#### id, continuing after the highest existing one."""
    ids = db.scalars(select(Order.id).where(Order.id.like("ORD-%"))).all()
    highest = 3900
    for oid in ids:
        try:
            highest = max(highest, int(oid[4:]))
        except ValueError:
            continue
    return f"ORD-{highest + 1}"
