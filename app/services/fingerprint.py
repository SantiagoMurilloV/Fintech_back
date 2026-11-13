"""A short fingerprint of a record's values, to tell later whether it changed.

Taken when a row is imported and stored with it; recomputing it from the row
as it is now and comparing says whether someone edited it since. Numbers are
rounded to cents so a float's last digits never count as a change.
"""
from __future__ import annotations

import hashlib


def of(*values) -> str:
    parts = []
    for value in values:
        if isinstance(value, float):
            # +0.0 turns -0.0 into 0.0: the database stores a negative zero as
            # plain 0, and «-0.00» vs «0.00» must not read as an edit.
            value = f"{round(value, 2) + 0.0:.2f}"
        parts.append("" if value is None else str(value).strip().upper())
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def order_otc(date, customer, amount, gateway, extra: dict) -> str:
    extra = extra or {}
    return of(date, customer, _num(amount), gateway, _num(extra.get("usdt")),
              _num(extra.get("spread_bruto")), _num(extra.get("costo_proveedor")),
              _num(extra.get("utilidad")), extra.get("tipo"))


def order_plain(date, customer, amount, currency, status) -> str:
    return of(date, customer, _num(amount), currency, status)


def expense_plain(date, description, amount, currency, category) -> str:
    return of(date, description, _num(amount), currency, category)


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
