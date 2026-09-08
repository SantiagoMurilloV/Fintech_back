"""User-defined columns for orders and expenses.

Stored values live in the row's `extra` JSON map; `formula` columns are never
stored — they are recomputed deterministically on every read.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CUSTOM_COLUMN_ENTITIES, CUSTOM_COLUMN_TYPES, CustomColumn, Expense, Order
from . import formula as formula_service

ENTITY_MODELS = {"orders": Order, "expenses": Expense}

# Base fields a formula may reference, per entity.
BASE_FIELDS = {
    "orders": ["amount", "usd_eq", "currency", "status", "gateway", "customer", "date"],
    "expenses": ["amount", "usd_eq", "currency", "category", "vendor", "owner", "description", "date"],
}


class ColumnError(ValueError):
    pass


def slugify(label: str) -> str:
    """'IVA 19%' -> 'iva_19'. Keys are snake_case ASCII identifiers."""
    text = unicodedata.normalize("NFD", label.strip().lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    if not text:
        raise ColumnError("El nombre de la columna no es válido.")
    if text[0].isdigit():
        text = f"col_{text}"
    return text[:60]


def list_columns(db: Session, entity: str | None = None) -> list[CustomColumn]:
    query = select(CustomColumn).order_by(CustomColumn.entity, CustomColumn.position, CustomColumn.id)
    if entity:
        query = query.where(CustomColumn.entity == entity)
    return list(db.scalars(query).all())


def create_column(db: Session, entity: str, label: str, data_type: str = "text",
                  formula: str | None = None, key: str | None = None) -> CustomColumn:
    """Register a new column; validates the formula before persisting it."""
    if entity not in CUSTOM_COLUMN_ENTITIES:
        raise ColumnError(f"entity debe ser uno de: {', '.join(CUSTOM_COLUMN_ENTITIES)}")
    if data_type not in CUSTOM_COLUMN_TYPES:
        raise ColumnError(f"data_type debe ser uno de: {', '.join(CUSTOM_COLUMN_TYPES)}")
    if data_type == "formula":
        if not formula:
            raise ColumnError("Una columna calculada necesita una fórmula.")
        formula_service.validate(formula)

    column_key = slugify(key or label)
    existing = db.scalars(
        select(CustomColumn).where(CustomColumn.entity == entity, CustomColumn.key == column_key)
    ).first()
    if existing:
        raise ColumnError(f'Ya existe una columna "{column_key}" en {entity}.')

    position = len(list_columns(db, entity))
    column = CustomColumn(
        entity=entity, key=column_key, label=label.strip(), data_type=data_type,
        formula=formula, position=position,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    db.add(column)
    db.commit()
    return column


def find_column(db: Session, needle: str, entity: str | None = None) -> CustomColumn | None:
    """Locate a column by key or by visible label (accent/case insensitive)."""
    candidates = list_columns(db, entity)
    target = slugify(needle)
    for column in candidates:
        if column.key == target or slugify(column.label) == target:
            return column
    # Fall back to a partial match so "iva" finds "IVA 19%".
    for column in candidates:
        if target in column.key or target in slugify(column.label):
            return column
    return None


def delete_column(db: Session, column_id: int) -> CustomColumn:
    column = db.get(CustomColumn, column_id)
    if not column:
        raise ColumnError("La columna no existe.")
    db.delete(column)
    db.commit()
    return column


def set_value(db: Session, entity: str, row_id, key: str, value) -> dict:
    """Write a stored (non-formula) value on one row."""
    model = ENTITY_MODELS.get(entity)
    if model is None:
        raise ColumnError(f"entity debe ser uno de: {', '.join(CUSTOM_COLUMN_ENTITIES)}")
    row = db.get(model, row_id)
    if row is None:
        raise ColumnError(f"No encontré el registro {row_id} en {entity}.")

    column = db.scalars(
        select(CustomColumn).where(CustomColumn.entity == entity, CustomColumn.key == key)
    ).first()
    if column is None:
        raise ColumnError(f'La columna "{key}" no existe en {entity}.')
    if column.data_type == "formula":
        raise ColumnError(f'"{key}" es una columna calculada; su valor no se escribe.')

    # Reassign the whole dict so SQLAlchemy detects the JSON change.
    row.extra = {**(row.extra or {}), key: value}
    db.commit()
    return {"entity": entity, "id": row_id, "key": key, "value": value}


def column_view(column: CustomColumn) -> dict:
    return {
        "id": column.id, "entity": column.entity, "key": column.key,
        "label": column.label, "data_type": column.data_type, "formula": column.formula,
    }


def apply_to_row(row_payload: dict, columns: list[CustomColumn], stored: dict | None) -> dict:
    """Return the `extra` map for one row: stored values + computed formulas."""
    stored = stored or {}
    result: dict = {}
    for column in columns:
        if column.data_type == "formula":
            # Formulas see base fields plus previously resolved custom values.
            scope = {**row_payload, **result}
            result[column.key] = formula_service.evaluate(column.formula, scope)
        else:
            result[column.key] = stored.get(column.key)
    return result
