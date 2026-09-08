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
from ..services import finance
from ..services import records as records_service
from .auth import AuthDep

router = APIRouter(tags=["orders"], dependencies=[AuthDep])


def order_view(o: Order, custom: list[CustomColumn] | None = None) -> dict:
    """Raw payload; visual formatting belongs to the frontend."""
    payload = {
        "id": o.id, "customer": o.customer, "amount": o.amount, "currency": o.currency,
        "status": o.status, "gateway": o.gateway, "date": o.date,
        "usd_eq": round(finance.usd_eq(o.amount, o.currency), 2),
    }
    # Values for user-defined columns, including recomputed formulas.
    payload["extra"] = columns_service.apply_to_row(payload, custom or [], o.extra)
    return payload


class OrderBody(BaseModel):
    customer: str
    amount: float
    currency: str = "COP"
    status: str = "pending"
    gateway: str | None = None
    date: str


@router.get("/api/orders")
def list_orders(status: str | None = None, limit: int = 25, offset: int = 0,
                db: Session = Depends(get_db)):
    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)
    query = select(Order)
    count_query = select(func.count(Order.id))
    if status in ORDER_STATUSES:
        query = query.where(Order.status == status)
        count_query = count_query.where(Order.status == status)
    total = db.scalar(count_query)
    items = db.scalars(
        query.order_by(Order.date.desc(), Order.id.desc()).limit(limit).offset(offset)
    ).all()
    custom = columns_service.list_columns(db, "orders")
    return {
        "items": [order_view(o, custom) for o in items],
        "total": total, "limit": limit, "offset": offset,
        "period": finance.current_period(db),
        # Catalogs so the frontend can build filters/forms without hardcoding.
        "statuses": ORDER_STATUSES, "gateways": GATEWAYS, "currencies": CURRENCIES,
        "columns": [columns_service.column_view(column) for column in custom],
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

    return {"item": order_view(order, columns_service.list_columns(db, "orders")),
            "changed": list(applied)}


@router.get("/api/orders/export.csv")
def export_csv(db: Session = Depends(get_db)):
    """CSV is a data export, so it lives in the backend."""
    rows = db.scalars(select(Order).order_by(Order.date.desc(), Order.id.desc())).all()
    buf = io.StringIO()
    buf.write("id,customer,amount,currency,status,gateway,date\n")
    for o in rows:
        customer = f'"{o.customer}"' if "," in o.customer else o.customer
        gateway = f'"{o.gateway}"' if o.gateway and "," in o.gateway else (o.gateway or "")
        buf.write(f"{o.id},{customer},{o.amount},{o.currency},{o.status},{gateway},{o.date}\n")
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="orders.csv"'},
    )
