"""User-defined columns for orders and expenses."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import CUSTOM_COLUMN_ENTITIES, CUSTOM_COLUMN_TYPES
from ..services import columns as columns_service
from .auth import AuthDep

router = APIRouter(tags=["columns"], dependencies=[AuthDep])


class ColumnBody(BaseModel):
    entity: str
    label: str
    data_type: str = "text"
    formula: str | None = None


class CellBody(BaseModel):
    entity: str
    row_id: str
    key: str
    value: object = None


@router.get("/api/columns")
def list_columns(entity: str | None = None, db: Session = Depends(get_db)):
    items = columns_service.list_columns(db, entity)
    return {
        "items": [columns_service.column_view(column) for column in items],
        "entities": CUSTOM_COLUMN_ENTITIES,
        "data_types": CUSTOM_COLUMN_TYPES,
    }


@router.post("/api/columns", status_code=201)
def create_column(body: ColumnBody, db: Session = Depends(get_db)):
    try:
        column = columns_service.create_column(
            db, body.entity, body.label, body.data_type, body.formula)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return columns_service.column_view(column)


@router.delete("/api/columns/{column_id}")
def delete_column(column_id: int, db: Session = Depends(get_db)):
    try:
        column = columns_service.delete_column(db, column_id)
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err))
    return {"deleted": columns_service.column_view(column)}


@router.post("/api/columns/cell")
def set_cell(body: CellBody, db: Session = Depends(get_db)):
    identifier = int(body.row_id) if body.entity == "expenses" else body.row_id
    try:
        return columns_service.set_value(db, body.entity, identifier, body.key, body.value)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
