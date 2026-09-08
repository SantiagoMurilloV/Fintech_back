"""Expenses: stats, registration, listing and Cloudinary receipts. Raw data only."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import CURRENCIES, CustomColumn, Expense
from ..services import columns as columns_service
from ..services import finance, media, receipts
from ..services import records as records_service
from .auth import AuthDep

router = APIRouter(tags=["expenses"], dependencies=[AuthDep])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def expense_view(e: Expense, custom: list[CustomColumn] | None = None) -> dict:
    """Raw payload; visual formatting belongs to the frontend."""
    payload = {
        "id": e.id, "description": e.description, "category": e.category,
        "vendor": e.vendor, "amount": e.amount, "currency": e.currency,
        "owner": e.owner, "date": e.date,
        "receipt_name": e.receipt_name, "receipt_url": e.receipt_url,
        "usd_eq": round(finance.usd_eq(e.amount, e.currency), 2),
    }
    # Values for user-defined columns, including recomputed formulas.
    payload["extra"] = columns_service.apply_to_row(payload, custom or [], e.extra)
    return payload


class ExpenseBody(BaseModel):
    description: str
    category: str
    vendor: str | None = None
    amount: float
    currency: str = "COP"
    owner: str | None = None
    date: str
    receipt_name: str | None = None


@router.get("/api/expenses")
def list_expenses(db: Session = Depends(get_db)):
    items = db.scalars(select(Expense).order_by(Expense.date.desc(), Expense.id.desc())).all()
    period = finance.current_period(db)
    rep = finance.report(db, period)
    top_category = rep["expense_categories"][0] if rep["expense_categories"] else None
    custom = columns_service.list_columns(db, "expenses")
    return {
        "items": [expense_view(e, custom) for e in items],
        "period": period,
        # Raw stats; the frontend turns them into KPI cards.
        "stats": {
            "month_total_usd": rep["expenses_usd"],
            "missing_receipts": len(rep["missing_receipts"]),
            "top_category": top_category["name"] if top_category else None,
        },
        # Capability flag so the frontend can enable/disable the upload UI.
        "cloudinary": media.is_configured(),
        "columns": [columns_service.column_view(column) for column in custom],
    }


@router.post("/api/expenses", status_code=201)
def create_expense(body: ExpenseBody, db: Session = Depends(get_db)):
    errors = []
    if not body.description.strip():
        errors.append("description es obligatorio")
    if not body.category.strip():
        errors.append("category es obligatoria")
    if body.amount <= 0:
        errors.append("amount debe ser mayor a 0")
    if body.currency not in CURRENCIES:
        errors.append(f"currency debe ser una de: {', '.join(CURRENCIES)}")
    if len(body.date) != 10 or body.date[4] != "-" or body.date[7] != "-":
        errors.append("date debe tener formato YYYY-MM-DD")
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    expense = Expense(description=body.description.strip(), category=body.category.strip(),
                      vendor=body.vendor, amount=body.amount, currency=body.currency,
                      owner=body.owner, date=body.date, receipt_name=body.receipt_name,
                      extra={})
    db.add(expense)
    db.commit()
    return expense_view(expense, columns_service.list_columns(db, "expenses"))


class ExpensePatch(BaseModel):
    """Partial edit: only the fields present are written."""
    description: str | None = None
    category: str | None = None
    vendor: str | None = None
    amount: float | None = None
    currency: str | None = None
    owner: str | None = None
    date: str | None = None


@router.patch("/api/expenses/{expense_id}")
def update_expense(expense_id: int, body: ExpensePatch, db: Session = Depends(get_db)):
    """Edit one or more fields of a expense — used by the editable table."""
    expense = records_service.get(db, "expenses", expense_id)
    if expense is None:
        raise HTTPException(status_code=404, detail="Gasto no encontrado.")
    try:
        applied = records_service.apply(
            db, expense, body.model_dump(exclude_unset=True), "expenses")
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None

    return {"item": expense_view(expense, columns_service.list_columns(db, "expenses")),
            "changed": list(applied)}


@router.post("/api/expenses/{expense_id}/receipt/generate", status_code=201)
def generate_receipt(expense_id: int, db: Session = Depends(get_db)):
    """Build the voucher PDF from the row's own data — the table's magic pencil.

    Unlike the upload, nothing external is needed: the document is rendered
    here, kept in the report library and attached as the expense's receipt
    when it had none.
    """
    expense = db.get(Expense, expense_id)
    if not expense:
        raise HTTPException(status_code=404, detail="Gasto no encontrado.")
    generated = receipts.generate(db, expense)
    return {"item": expense_view(expense, columns_service.list_columns(db, "expenses")),
            **generated}


@router.post("/api/expenses/{expense_id}/receipt")
async def upload_receipt(expense_id: int, file: UploadFile, db: Session = Depends(get_db)):
    """Upload the receipt to Cloudinary and persist its URL on the expense."""
    expense = db.get(Expense, expense_id)
    if not expense:
        raise HTTPException(status_code=404, detail="Gasto no encontrado.")
    if not media.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Cloudinary no está configurado (falta CLOUDINARY_URL en fintech_back/.env).",
        )
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="El archivo supera 10 MB.")
    uploaded = media.upload_receipt(content, file.filename or f"receipt_{expense_id}")
    expense.receipt_name = uploaded["name"]
    expense.receipt_url = uploaded["url"]
    db.commit()
    return expense_view(expense, columns_service.list_columns(db, "expenses"))
