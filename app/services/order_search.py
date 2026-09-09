"""Search and faceted filters over orders.

The table follows the data. One box matches whatever a person types against
every field a row has — the base columns and everything the feed brought —
and the filters on offer are built from the values that actually exist in
each column, with their counts. All of it is SQL; the frontend only paints
the controls this module describes.
"""
from __future__ import annotations

import json
import re

from sqlalchemy import String, Text, cast, func, or_, select
from sqlalchemy.orm import Session

from ..models import ORDER_STATUSES, CustomColumn, Order

# A column is offered as a filter only while its distinct values stay this
# few. Past that it is an identifier (document numbers, references) and the
# search box is the right tool for it.
FACET_MAX_VALUES = 40

# Base columns that may become filters. Status is one more facet: its values
# are our category codes, counted like any other column.
BASE_FACETS = ("status", "currency", "gateway", "customer")
# Values the sync keeps in `extra` without a user column, still worth filtering.
EXTRA_FACETS = ("source_status",)
# User columns become filters by their data type; numbers and dates are not
# categories.
FACETABLE_TYPES = ("text",)

_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# What the panel calls each status, so typing «rechazadas» finds the category.
# Only our own labels: a feed word such as «Expired» must match its text, not
# the whole category it was filed under.
STATUS_SEARCH_WORDS = {
    "aprobada": "approved", "aprobadas": "approved", "aprobado": "approved",
    "pendiente": "pending", "pendientes": "pending",
    "rechazada": "rejected", "rechazadas": "rejected", "rechazado": "rejected",
    "reembolsada": "refunded", "reembolsadas": "refunded", "reembolso": "refunded",
}


class SearchError(ValueError):
    pass


def parse_filters(raw: str | None) -> dict[str, list[str]]:
    """The `filters` query parameter: a JSON object {field: value | [values]}."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise SearchError("filters debe ser un objeto JSON.") from None
    if not isinstance(data, dict):
        raise SearchError("filters debe ser un objeto JSON.")
    parsed: dict[str, list[str]] = {}
    for key, value in data.items():
        values = value if isinstance(value, list) else [value]
        chosen = [str(v) for v in values if v is not None and str(v) != ""]
        if chosen:
            parsed[str(key)] = chosen
    return parsed


def _day(value: str | None, name: str) -> str | None:
    if value is None or not value.strip():
        return None
    if not _ISO_DAY.match(value.strip()):
        raise SearchError(f"{name} debe tener formato YYYY-MM-DD.")
    return value.strip()


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Search:
    """Everything one listing request asked for, turned into SQL conditions."""

    def __init__(self, columns: list[CustomColumn], q: str | None = None,
                 status: str | None = None, filters: str | None = None,
                 date_from: str | None = None, date_to: str | None = None):
        self.columns = columns
        self.custom_keys = {column.key for column in columns}
        self.q = (q or "").strip()
        self.status = status if status in ORDER_STATUSES else None
        self.filters = parse_filters(filters)
        self.date_from = _day(date_from, "date_from")
        self.date_to = _day(date_to, "date_to")
        # (facet key or None, condition): keyed so a facet can be counted
        # without its own selection applied.
        self._conditions = self._build()

    # ------------------------------------------------------------ conditions

    def _expression(self, key: str):
        """SQL expression for a filterable field, or None if it does not exist."""
        if key in BASE_FACETS or key == "status":
            return getattr(Order, key)
        if key in EXTRA_FACETS or key in self.custom_keys:
            return Order.extra[key].as_string()
        return None

    def _search_condition(self):
        pattern = f"%{_escape_like(self.q)}%"
        clauses = [
            Order.id.ilike(pattern, escape="\\"),
            Order.customer.ilike(pattern, escape="\\"),
            Order.currency.ilike(pattern, escape="\\"),
            Order.status.ilike(pattern, escape="\\"),
            Order.gateway.ilike(pattern, escape="\\"),
            Order.date.ilike(pattern, escape="\\"),
            cast(Order.amount, String).ilike(pattern, escape="\\"),
            # Everything the feed brought: dynamic columns, the source status
            # and the raw record alike.
            cast(Order.extra, Text).ilike(pattern, escape="\\"),
        ]
        # «rechazada» must find rejected rows although the stored code is English.
        code = STATUS_SEARCH_WORDS.get(self.q.lower())
        if code:
            clauses.append(Order.status == code)
        return or_(*clauses)

    def _build(self) -> list[tuple[str | None, object]]:
        conditions: list[tuple[str | None, object]] = []
        if self.status:
            conditions.append((None, Order.status == self.status))
        if self.date_from:
            conditions.append((None, Order.date >= self.date_from))
        if self.date_to:
            conditions.append((None, Order.date <= self.date_to))
        for key, values in self.filters.items():
            expression = self._expression(key)
            if expression is None:
                raise SearchError(f"No existe la columna «{key}».")
            conditions.append((key, expression.in_(values)))
        if self.q:
            conditions.append((None, self._search_condition()))
        return conditions

    def where(self, exclude: str | None = None) -> list:
        """Conditions to apply, optionally without one facet's own selection.

        Search, status and dates carry no facet key and are always applied.
        """
        return [condition for key, condition in self._conditions
                if exclude is None or key != exclude]

    @property
    def active(self) -> bool:
        return bool(self._conditions)

    # ---------------------------------------------------------------- facets

    def facets(self, db: Session) -> list[dict]:
        """Distinct values with counts for every column that reads as a category.

        Counts come from the rows matching every OTHER filter, so with «TRON»
        selected the network facet still says how many rows ETHEREUM would give.
        """
        candidates = [(key, "base") for key in BASE_FACETS]
        candidates += [(key, "extra") for key in EXTRA_FACETS]
        candidates += [(column.key, "column") for column in self.columns
                       if column.data_type in FACETABLE_TYPES]
        facets = []
        for key, kind in candidates:
            expression = self._expression(key)
            count = func.count(Order.id)
            rows = db.execute(
                select(expression.label("value"), count)
                .where(*self.where(exclude=key), expression.is_not(None), expression != "")
                .group_by(expression)
                .order_by(count.desc(), expression)
                .limit(FACET_MAX_VALUES + 1)
            ).all()
            selected = key in self.filters
            if not selected and (len(rows) < 2 or len(rows) > FACET_MAX_VALUES):
                # One value gives nothing to choose; too many is an identifier.
                continue
            facets.append({
                "key": key, "kind": kind,
                "values": [{"value": value, "count": total}
                           for value, total in rows[:FACET_MAX_VALUES]],
            })
        return facets
