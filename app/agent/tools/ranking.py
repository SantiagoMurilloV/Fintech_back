"""Ranking tools: the largest orders and the leading customers.

«¿Cuál es la orden más grande?», «¿qué cliente factura más?», «top 5 clientes
por número de órdenes». A listing cannot answer these and the narrator must
never guess them: sorting and summing happen here, in Python over the rows,
and the narrator only names what came first.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import ORDER_STATUSES, Order
from ...services import finance
from .. import blocks
from ..formatting import (
    amount, integer, percent, period_name, short_date, status_label, status_tone, usd_compact,
)
from ..registry import ToolResult, tool

MAX_ROWS = 50

PERIOD_PARAM = {
    "type": "string",
    "description": "Periodo YYYY-MM. Si se omite, se consideran todas las órdenes.",
    "pattern": r"^\d{4}-\d{2}$",
}


def _orders(db: Session, status: str | None, customer: str | None,
            period: str | None) -> tuple[list[Order], list[str]]:
    query = select(Order)
    filters = []
    if status in ORDER_STATUSES:
        query = query.where(Order.status == status)
        filters.append(f"estado {status_label(status)}")
    if customer:
        query = query.where(Order.customer.ilike(f"%{customer}%"))
        filters.append(f'cliente ~ "{customer}"')
    if period:
        low, high = finance.month_bounds(period)
        query = query.where(Order.date >= low, Order.date < high)
        filters.append(period_name(period))
    return list(db.scalars(query).all()), filters


@tool(
    name="rank_orders",
    description="Órdenes ordenadas por monto (equivalente USD), de mayor a menor o de menor "
                "a mayor, con filtros opcionales por estado, cliente y periodo. Úsala para "
                "«la orden más grande», «el cliente con la orden más elevada», «las 5 órdenes "
                "más altas», «la orden más pequeña de agosto».",
    parameters={
        "type": "object",
        "properties": {
            "order": {"type": "string", "enum": ["desc", "asc"], "default": "desc",
                      "description": "desc = las más grandes primero; asc = las más pequeñas."},
            "status": {"type": "string", "enum": ORDER_STATUSES},
            "customer": {"type": "string", "description": "Coincidencia parcial del nombre."},
            "period": PERIOD_PARAM,
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS, "default": 5},
        },
        "required": [],
    },
    examples=["¿Cuál es la orden más grande?", "¿Qué cliente tiene la orden más elevada?",
              "Las 5 órdenes más altas de agosto"],
)
def rank_orders(db: Session, order: str = "desc", status: str | None = None,
                customer: str | None = None, period: str | None = None,
                limit: int = 5) -> ToolResult:
    limit = max(1, min(int(limit), MAX_ROWS))
    rows, filters = _orders(db, status, customer, period)
    if not rows:
        return ToolResult(blocks=[blocks.notice("No hay órdenes que cumplan ese filtro.", tone="info")],
                          data={"count": 0})

    ranked = sorted(rows, key=lambda o: finance.usd_eq(o.amount, o.currency),
                    reverse=(order != "asc"))[:limit]
    top = ranked[0]
    word = "pequeña" if order == "asc" else "grande"
    scope = f" ({', '.join(filters)})" if filters else ""
    lead = (f"La orden más {word}{scope} es {top.id}, de {top.customer}, por "
            f"{amount(top.amount, top.currency)} {top.currency} "
            f"({usd_compact(finance.usd_eq(top.amount, top.currency))}), "
            f"{status_label(top.status).lower()} el {short_date(top.date)}.")

    table_rows = [{
        "rank": integer(position),
        "id": o.id, "customer": o.customer,
        "amount": f"{amount(o.amount, o.currency)} {o.currency}",
        "usd": usd_compact(finance.usd_eq(o.amount, o.currency)),
        "status": {"label": status_label(o.status), "tone": status_tone(o.status)},
        "date": short_date(o.date),
    } for position, o in enumerate(ranked, start=1)]
    columns = [
        {"key": "rank", "label": "#", "align": "right"},
        {"key": "id", "label": "ID"},
        {"key": "customer", "label": "Cliente"},
        {"key": "amount", "label": "Monto", "align": "right"},
        {"key": "usd", "label": "Equiv. USD", "align": "right"},
        {"key": "status", "label": "Estado", "kind": "badge"},
        {"key": "date", "label": "Fecha", "align": "right"},
    ]
    caption = f"{integer(len(ranked))} de {integer(len(rows))} órdenes consideradas"

    def row_data(o: Order) -> dict:
        return {"id": o.id, "customer": o.customer, "amount": o.amount, "currency": o.currency,
                "usd_eq": round(finance.usd_eq(o.amount, o.currency), 2),
                "status": status_label(o.status), "date": o.date}

    return ToolResult(
        blocks=[blocks.text(lead), blocks.table(columns, table_rows, caption)],
        data={"order": order, "filters": filters, "considered_count": len(rows),
              "top": row_data(top), "rows": [row_data(o) for o in ranked]},
        summary=lead,
    )


@tool(
    name="rank_customers",
    description="Clientes ordenados por monto aprobado (equivalente USD) o por número de "
                "órdenes, con periodo opcional. Úsala para «qué cliente factura más», «cliente "
                "principal», «cliente con más órdenes», «top 5 clientes».",
    parameters={
        "type": "object",
        "properties": {
            "metric": {"type": "string", "enum": ["amount", "count"], "default": "amount",
                       "description": "amount = monto en USD; count = cantidad de órdenes."},
            "status": {"type": "string", "enum": ORDER_STATUSES,
                       "description": "Por defecto, aprobadas cuando se ordena por monto."},
            "period": PERIOD_PARAM,
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS, "default": 5},
        },
        "required": [],
    },
    examples=["¿Qué cliente factura más?", "¿Qué cliente tiene más órdenes?", "Top 5 clientes"],
)
def rank_customers(db: Session, metric: str = "amount", status: str | None = None,
                   period: str | None = None, limit: int = 5) -> ToolResult:
    limit = max(1, min(int(limit), MAX_ROWS))
    by_amount = metric != "count"
    if by_amount and status is None:
        status = "approved"      # revenue is approved money, not attempted money
    rows, filters = _orders(db, status, None, period)
    if not rows:
        return ToolResult(blocks=[blocks.notice("No hay órdenes que cumplan ese filtro.", tone="info")],
                          data={"count": 0})

    usd: dict[str, float] = defaultdict(float)
    count: dict[str, int] = defaultdict(int)
    for o in rows:
        usd[o.customer] += finance.usd_eq(o.amount, o.currency)
        count[o.customer] += 1
    total_usd = sum(usd.values())
    total_count = len(rows)
    key = (lambda c: usd[c]) if by_amount else (lambda c: count[c])
    ranked = sorted(usd, key=key, reverse=True)[:limit]
    leader = ranked[0]

    scope = f" ({', '.join(filters)})" if filters else ""
    if by_amount:
        lead = (f"El cliente con mayor monto{scope} es {leader}, con "
                f"{usd_compact(usd[leader])} en {integer(count[leader])} órdenes, el "
                f"{percent(100 * usd[leader] / total_usd) if total_usd else '0%'} del total.")
    else:
        lead = (f"El cliente con más órdenes{scope} es {leader}, con {integer(count[leader])} "
                f"de {integer(total_count)} órdenes "
                f"({percent(100 * count[leader] / total_count)}), por {usd_compact(usd[leader])}.")

    def share(c: str) -> float:
        return (100 * usd[c] / total_usd if total_usd else 0.0) if by_amount \
            else 100 * count[c] / total_count

    table_rows = [{
        "rank": integer(position), "customer": c, "count": integer(count[c]),
        "usd": usd_compact(usd[c]), "share": percent(share(c)),
    } for position, c in enumerate(ranked, start=1)]
    columns = [
        {"key": "rank", "label": "#", "align": "right"},
        {"key": "customer", "label": "Cliente"},
        {"key": "count", "label": "Órdenes", "align": "right"},
        {"key": "usd", "label": "Monto USD", "align": "right"},
        {"key": "share", "label": "Participación", "align": "right"},
    ]
    caption = (f"{integer(len(ranked))} de {integer(len(usd))} clientes · "
               f"ordenados por {'monto' if by_amount else 'número de órdenes'}")

    return ToolResult(
        blocks=[blocks.text(lead), blocks.table(columns, table_rows, caption)],
        data={"metric": metric, "status": status_label(status) if status else "todas",
              "filters": filters, "total_usd": round(total_usd, 2), "orders_count": total_count,
              "leader": {"customer": leader, "usd": round(usd[leader], 2),
                         "count": count[leader], "pct": round(share(leader), 1)},
              "rows": [{"customer": c, "usd": round(usd[c], 2), "count": count[c],
                        "pct": round(share(c), 1)} for c in ranked]},
        summary=lead,
    )
