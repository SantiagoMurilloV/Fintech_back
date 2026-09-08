"""Editing orders and expenses.

One place decides what a record accepts and how each value is validated, so
the agent tool, the REST endpoint behind the editable table and any future
importer all write the same way. Returns what actually changed — the before
and after of every field — which is what both the chat and the audit trail
show.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import CURRENCIES, GATEWAYS, ORDER_STATUSES, Expense, Order

# field -> (label shown to the user, validation rule)
EXPENSE_FIELDS = {
    "description": ("Concepto", "text"),
    "category": ("Categoría", "text"),
    "amount": ("Monto", "positive"),
    "currency": ("Moneda", CURRENCIES),
    "vendor": ("Proveedor", "optional"),
    "owner": ("Responsable", "optional"),
    "date": ("Fecha", "date"),
}

ORDER_FIELDS = {
    "customer": ("Cliente", "text"),
    "amount": ("Monto", "positive"),
    "currency": ("Moneda", CURRENCIES),
    "status": ("Estado", ORDER_STATUSES),
    "gateway": ("Pasarela", GATEWAYS + [""]),
    "date": ("Fecha", "date"),
}

FIELDS = {"expenses": EXPENSE_FIELDS, "orders": ORDER_FIELDS}


def fields_of(entity: str) -> dict:
    return FIELDS.get(entity, {})


def get(db: Session, entity: str, identifier):
    """Load a record, normalising the id to the type of its table."""
    if entity == "expenses":
        return db.get(Expense, int(identifier))
    return db.get(Order, str(identifier).upper())


def apply(db: Session, record, changes: dict, entity: str) -> dict:
    """Write the requested fields; return {field: (label, before, after)}.

    Values equal to what is stored are skipped, so an edit that changes
    nothing reports nothing instead of a false success.
    """
    allowed = fields_of(entity)
    applied: dict[str, tuple] = {}

    for key, value in changes.items():
        if key not in allowed or value is None:
            continue
        label, rule = allowed[key]
        clean = _validated(value, rule, label)
        previous = getattr(record, key)
        if previous == clean:
            continue
        setattr(record, key, clean)
        applied[key] = (label, previous, clean)

    if applied:
        db.commit()
    return applied


def _validated(value, rule, label: str):
    if rule == "positive":
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label} debe ser un número.") from None
        if number <= 0:
            raise ValueError(f"{label} debe ser mayor a 0.")
        return number
    if isinstance(rule, list):
        if value not in rule:
            options = ", ".join(option for option in rule if option)
            raise ValueError(f"{label} debe ser uno de: {options}.")
        return value or None
    if rule == "date":
        text = str(value).strip()
        if len(text) != 10 or text[4] != "-" or text[7] != "-":
            raise ValueError(f"{label} debe tener el formato YYYY-MM-DD.")
        return text
    text = str(value).strip()
    if rule == "optional":
        return text or None
    if not text:
        raise ValueError(f"{label} no puede quedar vacío.")
    return text
