"""Orders: filtered listing, creation and CSV export. Raw data only."""
from __future__ import annotations

import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import CURRENCIES, GATEWAYS, ORDER_STATUSES, CustomColumn, Order
from ..services import columns as columns_service
from ..services import api_sync, finance, order_search
from ..services import records as records_service
from .auth import AuthDep

router = APIRouter(tags=["orders"], dependencies=[AuthDep])


def order_view(o: Order, custom: list[CustomColumn] | None = None) -> dict:
    """Raw payload; visual formatting belongs to the frontend."""
    payload = {
        "id": o.id, "customer": o.customer, "amount": o.amount, "currency": o.currency,
        "status": o.status, "gateway": o.gateway, "date": o.date,
        "usd_eq": round(finance.usd_eq(o.amount, o.currency), 2),
        # The feed's own wording for the state, when the row came from a sync.
        "source_status": (o.extra or {}).get("source_status"),
    }
    # Values for user-defined columns, including recomputed formulas.
    payload["extra"] = columns_service.apply_to_row(payload, custom or [], o.extra)
    return payload


# Base columns a source may legitimately lack; the table hides them while no
# row fills them, rather than showing a column of dashes the data never had.
OPTIONAL_FIELDS = ("gateway",)


def _empty_fields(db: Session) -> list[str]:
    """Optional base fields with no value in any order."""
    if not db.scalar(select(func.count(Order.id))):
        return []
    empty = []
    for name in OPTIONAL_FIELDS:
        column = getattr(Order, name)
        filled = db.scalar(select(func.count(Order.id)).where(column.is_not(None), column != ""))
        if not filled:
            empty.append(name)
    return empty


class OrderBody(BaseModel):
    customer: str
    amount: float
    currency: str = "COP"
    status: str = "pending"
    gateway: str | None = None
    date: str


def _search(db: Session, custom: list[CustomColumn], q, status, filters,
            date_from, date_to) -> order_search.Search:
    try:
        return order_search.Search(custom, q=q, status=status, filters=filters,
                                   date_from=date_from, date_to=date_to)
    except order_search.SearchError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None


@router.get("/api/orders")
def list_orders(status: str | None = None, q: str | None = None, filters: str | None = None,
                date_from: str | None = None, date_to: str | None = None,
                limit: int = 25, offset: int = 0, db: Session = Depends(get_db)):
    """One page of orders plus everything the table needs to filter them.

    `q` searches every field of the row; `filters` is a JSON object
    {column: value | [values]} over base or feed columns; `facets` in the
    response lists, per column, the values that exist and how many rows each
    one would give.
    """
    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)
    custom = columns_service.list_columns(db, "orders")
    search = _search(db, custom, q, status, filters, date_from, date_to)
    where = search.where()
    total = db.scalar(select(func.count(Order.id)).where(*where))
    items = db.scalars(
        select(Order).where(*where)
        .order_by(Order.date.desc(), Order.id.desc()).limit(limit).offset(offset)
    ).all()
    return {
        "items": [order_view(o, custom) for o in items],
        "total": total, "limit": limit, "offset": offset,
        "filtered": search.active,
        "facets": search.facets(db),
        "period": finance.current_period(db),
        # Catalogs so the frontend can build filters/forms without hardcoding.
        "statuses": ORDER_STATUSES, "gateways": GATEWAYS, "currencies": CURRENCIES,
        "columns": [columns_service.column_view(column) for column in custom],
        # Where each base column came from, and which ones the data never
        # fills: the table adapts to the feed, not the feed to the table.
        "source_fields": api_sync.source_fields(db, "orders"),
        "empty_fields": _empty_fields(db),
    }


@router.post("/api/orders", status_code=201)
def create_order(body: OrderBody, db: Session = Depends(get_db)):
    errors = []
    if not body.customer.strip():
        errors.append("customer es obligatorio")
    if body.amount <= 0:
        errors.append("amount debe ser mayor a 0")
    if body.currency not in CURRENCIES:
        errors.append(f"currency debe ser una de: {', '.join(CURRENCIES)}")
    if body.status not in ORDER_STATUSES:
        errors.append(f"status debe ser uno de: {', '.join(ORDER_STATUSES)}")
    if len(body.date) != 10 or body.date[4] != "-" or body.date[7] != "-":
        errors.append("date debe tener formato YYYY-MM-DD")
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    order = Order(id=finance.next_order_id(db), customer=body.customer.strip(),
                  amount=body.amount, currency=body.currency, status=body.status,
                  gateway=body.gateway or None, date=body.date, extra={})
    db.add(order)
    db.commit()
    return order_view(order, columns_service.list_columns(db, "orders"))


class OrderPatch(BaseModel):
    """Partial edit: only the fields present are written."""
    customer: str | None = None
    amount: float | None = None
    currency: str | None = None
    status: str | None = None
    gateway: str | None = None
    date: str | None = None


@router.patch("/api/orders/{order_id}")
def update_order(order_id: str, body: OrderPatch, db: Session = Depends(get_db)):
    """Edit one or more fields of an order — used by the editable table."""
    order = records_service.get(db, "orders", order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Orden no encontrada.")
    try:
        applied = records_service.apply(
            db, order, body.model_dump(exclude_unset=True), "orders")
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None
    if "status" in applied and isinstance(order.extra, dict) and "source_status" in order.extra:
        # A state chosen by hand supersedes the feed's wording for it.
        order.extra = {k: v for k, v in order.extra.items() if k != "source_status"}
        db.commit()

    return {"item": order_view(order, columns_service.list_columns(db, "orders")),
            "changed": list(applied)}


@router.get("/api/orders/export.csv")
def export_csv(status: str | None = None, q: str | None = None, filters: str | None = None,
               date_from: str | None = None, date_to: str | None = None,
               db: Session = Depends(get_db)):
    """CSV is a data export, so it lives in the backend. It honours the same
    search and filters as the table: what is exported is what is on screen."""
    custom = columns_service.list_columns(db, "orders")
    search = _search(db, custom, q, status, filters, date_from, date_to)
    rows = db.scalars(select(Order).where(*search.where())
                      .order_by(Order.date.desc(), Order.id.desc())).all()
    buf = io.StringIO()
    buf.write("id,customer,amount,currency,status,source_status,gateway,date\n")
    for o in rows:
        customer = f'"{o.customer}"' if "," in o.customer else o.customer
        source_status = (o.extra or {}).get("source_status") or ""
        source_status = f'"{source_status}"' if "," in source_status else source_status
        gateway = f'"{o.gateway}"' if o.gateway and "," in o.gateway else (o.gateway or "")
        buf.write(f"{o.id},{customer},{o.amount},{o.currency},{o.status},{source_status},"
                  f"{gateway},{o.date}\n")
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="orders.csv"'},
    )
