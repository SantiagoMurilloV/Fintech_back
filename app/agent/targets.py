"""Which record a message is talking about.

Editing and deleting need a target, and users name it in every way: «el gasto
12», «ORD-4123», «el último gasto», «el gasto de AWS de julio». This module
turns that reference into one id — or, when it matches several records, into
the list of candidates the agent shows so the user picks one. It never guesses
between two matches: an edit that hit the wrong row is worse than a question.

The span of the reference is returned too, so the caller can blank it before
reading the new values: in «cambia el gasto de AWS a Google Cloud» only the
second name is the change.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Expense, Order
from ..services import finance
from . import parsing
from .formatting import amount as amount_text
from .formatting import short_date, status_label

# Candidates shown when a reference matches more than one record.
MAX_CANDIDATES = 10

ID_FIELD = {"expenses": "expense_id", "orders": "order_id"}


@dataclass
class Located:
    """The outcome of resolving a reference."""
    id: str | int | None = None
    span: tuple[int, int] | None = None
    candidates: list[dict] = field(default_factory=list)
    query: str | None = None            # what was searched, for the message


def _expense_row(expense: Expense) -> dict:
    return {
        "id": str(expense.id),
        "description": expense.description,
        "category": expense.category,
        "vendor": expense.vendor or "—",
        "amount": f"{amount_text(expense.amount, expense.currency)} {expense.currency}",
        "date": short_date(expense.date),
    }


def _order_row(order: Order) -> dict:
    return {
        "id": order.id,
        "customer": order.customer,
        "amount": f"{amount_text(order.amount, order.currency)} {order.currency}",
        "status": status_label(order.status),
        "gateway": order.gateway or "—",
        "date": short_date(order.date),
    }


CANDIDATE_COLUMNS = {
    "expenses": [
        {"key": "id", "label": "ID"},
        {"key": "description", "label": "Concepto"},
        {"key": "category", "label": "Categoría"},
        {"key": "vendor", "label": "Proveedor"},
        {"key": "amount", "label": "Monto", "align": "right"},
        {"key": "date", "label": "Fecha", "align": "right"},
    ],
    "orders": [
        {"key": "id", "label": "ID"},
        {"key": "customer", "label": "Cliente"},
        {"key": "amount", "label": "Monto", "align": "right"},
        {"key": "status", "label": "Estado"},
        {"key": "gateway", "label": "Pasarela"},
        {"key": "date", "label": "Fecha", "align": "right"},
    ],
}


def exists(db: Session, entity: str, identifier) -> bool:
    model = Expense if entity == "expenses" else Order
    return db.get(model, identifier) is not None


def _search_text(text: str, entity: str, known: dict) -> str | None:
    """The words that name the record, ignoring the ones naming a new value."""
    stored = (["vendors", "categories", "owners"] if entity == "expenses" else ["customers"])
    for source in stored:
        match = parsing.match_known(text, known.get(source, ()))
        if match:
            return match
    noun = r"gastos?\s+(?:de|del)" if entity == "expenses" else r"(?:orden(?:es)?|operaci[oó]n)\s+(?:de|del|para)"
    return parsing.capture_after(text, noun)


def locate(db: Session, entity: str, text: str, known: dict | None = None) -> Located:
    """Resolve the reference in `text` to one record of `entity`."""
    known = known or {}

    identified = parsing.record_id(text)
    if identified and identified[0] == entity:
        _, identifier, span = identified
        if exists(db, entity, identifier):
            return Located(id=identifier, span=span)
        return Located(candidates=_recent(db, entity), query=str(identifier))

    model = Expense if entity == "expenses" else Order
    order_by = (model.date.desc(), model.id.desc())

    if parsing.mentions_latest(text):
        newest = db.scalars(select(model).order_by(*order_by).limit(1)).first()
        if newest is not None:
            return Located(id=newest.id)

    query = _search_text(text, entity, known)
    period = parsing.extract_period(text)
    if query or period:
        statement = select(model)
        if query:
            pattern = f"%{query}%"
            statement = statement.where(
                (Expense.description.ilike(pattern) | Expense.category.ilike(pattern)
                 | Expense.vendor.ilike(pattern)) if entity == "expenses"
                else Order.customer.ilike(pattern))
        if period:
            low, high = finance.month_bounds(period)
            statement = statement.where(model.date >= low, model.date < high)

        rows = list(db.scalars(statement.order_by(*order_by).limit(MAX_CANDIDATES + 1)).all())
        if len(rows) == 1:
            return Located(id=rows[0].id, query=query)
        if rows:
            return Located(candidates=_rows(entity, rows[:MAX_CANDIDATES]), query=query)

    return Located(candidates=_recent(db, entity), query=query)


def _recent(db: Session, entity: str) -> list[dict]:
    model = Expense if entity == "expenses" else Order
    rows = db.scalars(
        select(model).order_by(model.date.desc(), model.id.desc()).limit(MAX_CANDIDATES)).all()
    return _rows(entity, list(rows))


def _rows(entity: str, rows: list) -> list[dict]:
    builder = _expense_row if entity == "expenses" else _order_row
    return [builder(row) for row in rows]


def parse_edit(db: Session, entity: str, text: str, known: dict | None = None) -> tuple[dict, list[dict]]:
    """Read a whole edit command: which record, and what changes.

    Returns the tool arguments and the candidates to choose from when the
    reference was not conclusive.
    """
    known = known or {}
    located = locate(db, entity, text, known)

    # The reference must not be read as a new value: "el gasto de AWS a 250".
    remainder = text
    if located.span:
        remainder = parsing.blank(text, [located.span])
    elif located.query:
        remainder = _blank_first(text, located.query)

    arguments: dict = {}
    if located.id is not None:
        arguments[ID_FIELD[entity]] = located.id
    arguments.update(parsing.parse_changes(remainder, entity, known))
    return arguments, located.candidates


def _blank_first(text: str, needle: str) -> str:
    start = parsing.normalize(text).find(parsing.normalize(needle))
    if start < 0:
        return text
    return parsing.blank(text, [(start, start + len(needle))])
