"""Field mapping between an external record and our orders/expenses.

Every API names things its own way (`client_name`, `total`, `paid_on`); our
tables have one way. This module guesses the correspondence from a synonym
table, coerces each value to what the column expects, and reports what it
could not place — so a sync never fails silently and the guesswork is visible
and correctable.

The guess can be overridden per entity from settings later; the synonym table
is the zero-configuration default that makes the first sync show something.
"""
from __future__ import annotations

import re
from datetime import date, datetime

# our field -> how the world tends to call it (lowercase, compared exactly)
ORDER_SYNONYMS = {
    "external_id": ["id", "order_id", "external_id", "uuid", "code", "folio",
                    "reference", "ref", "numero", "number", "pid", "payment_id"],
    "customer": ["customer", "customer_name", "client", "client_name", "cliente",
                 "buyer", "account", "company", "empresa", "legal_name"],
    "amount": ["amount", "total", "total_amount", "grand_total", "value", "price",
               "monto", "valor", "importe", "amount_in"],
    "currency": ["currency", "currency_code", "moneda", "divisa", "currency_in"],
    "status": ["status", "state", "payment_status", "estado"],
    "gateway": ["gateway", "pasarela", "payment_method", "metodo_pago", "processor"],
    "date": ["date", "created", "created_at", "fecha", "issued_at", "paid_on",
             "transaction_date", "datetime", "timestamp"],
}

EXPENSE_SYNONYMS = {
    "external_id": ["id", "expense_id", "external_id", "uuid", "code", "folio",
                    "reference", "ref", "numero", "number"],
    "description": ["description", "title", "concept", "concepto", "detail",
                    "detalle", "name", "nombre", "memo"],
    "category": ["category", "category_name", "categoria", "rubro", "type", "tipo"],
    "vendor": ["vendor", "provider", "proveedor", "supplier", "merchant"],
    "owner": ["owner", "responsable", "responsible"],
    "amount": ["amount", "total", "value", "price", "monto", "valor", "importe"],
    "currency": ["currency", "currency_code", "moneda", "divisa"],
    "date": ["date", "created", "created_at", "fecha", "paid_on", "issued_at",
             "transaction_date"],
}

# How feeds spell each of our status codes. The multi-word entries are the
# documented lifecycle of the Mandioca/AUXO accountant API, mapped so a
# cancelled or expired order never lands as revenue.
STATUS_WORDS = {
    "approved": ["approved", "paid", "completed", "complete", "success", "settled",
                 "captured", "aprobada", "aprobado", "pagado", "pagada", "exitosa",
                 "succeeded"],
    "pending": ["pending", "processing", "created", "open", "in_progress",
                "pendiente", "procesando",
                "waiting approval", "accepted", "waiting external",
                "transferred to middleware", "transferred to external"],
    "rejected": ["rejected", "failed", "declined", "error", "cancelled", "canceled",
                 "rechazada", "rechazado", "fallida", "cancelada",
                 "expired", "rejected signature", "rejected signature to middleware",
                 "exhausted to middleware", "exhausted to external",
                 "failed to middleware", "failed to external", "failed external"],
    "refunded": ["refunded", "reversed", "chargeback", "reembolsada", "reembolsado",
                 "devuelta", "devuelto"],
}
_STATUS_LOOKUP = {word: code for code, words in STATUS_WORDS.items() for word in words}


def guess_mapping(fields: list[str], entity: str) -> dict[str, str]:
    """our field -> their field, for the fields present in the payload."""
    synonyms = ORDER_SYNONYMS if entity == "orders" else EXPENSE_SYNONYMS
    lowered = {str(field).lower(): field for field in fields}
    mapping: dict[str, str] = {}
    for ours, candidates in synonyms.items():
        for candidate in candidates:
            if candidate in lowered:
                mapping[ours] = lowered[candidate]
                break
    return mapping


# ---------------------------------------------------------------- coercion

def to_amount(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip().replace("$", "").replace(" ", "")
    if not text:
        return None
    # "1.250,50" -> "1250.50"; "1,250.50" -> "1250.50"
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        text = text.replace(thousands, "").replace(decimal, ".")
    elif "," in text:
        parts = text.split(",")
        text = "".join(parts) if all(len(p) == 3 for p in parts[1:]) else text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def to_date(value) -> str | None:
    """Whatever the feed calls a date, reduced to ISO YYYY-MM-DD."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:  # unix timestamp, seconds or milliseconds
            stamp = float(value)
            if stamp > 1e11:
                stamp /= 1000
            return datetime.fromtimestamp(stamp).date().isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    for pattern in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], pattern).date().isoformat()
        except ValueError:
            continue
    return None


def to_status(value) -> str | None:
    return _STATUS_LOOKUP.get(str(value or "").strip().lower())


def to_currency(value, fallback: str) -> tuple[str, bool]:
    """Returns (currency, was recognised).

    Any plausible code — fiat or crypto (USDT, WBTC) — is kept VERBATIM: the
    record must store exactly the currency it arrived with. Whether a USD
    conversion exists for it is a separate question (finance.has_rate);
    without a rate the record still exists, it just adds zero to USD totals
    instead of being converted with an invented rate.
    """
    code = str(value or "").strip().upper()
    if not code:
        return fallback, True
    if re.fullmatch(r"[A-Z0-9]{2,8}", code):
        return code, True
    return fallback, False


def today() -> str:
    return date.today().isoformat()
