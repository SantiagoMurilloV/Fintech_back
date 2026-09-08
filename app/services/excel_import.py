"""Excel/CSV import for orders and expenses — all parsing happens server-side.

Canonical column names are English; common Spanish header aliases are accepted
so operations teams can keep their existing spreadsheets.
"""
from __future__ import annotations

import io
import unicodedata
from datetime import date, datetime

import pandas as pd
from sqlalchemy.orm import Session

from ..models import CURRENCIES, ORDER_STATUSES, Expense, Order
from .finance import next_order_id

ORDER_COLUMNS = ["id", "customer", "amount", "currency", "status", "gateway", "date"]
EXPENSE_COLUMNS = ["description", "category", "vendor", "amount", "currency", "owner", "date", "receipt"]

# Spanish header aliases -> canonical English field names.
HEADER_ALIASES = {
    "cliente": "customer", "monto": "amount", "moneda": "currency", "estado": "status",
    "fecha": "date", "concepto": "description", "categoria": "category",
    "proveedor": "vendor", "responsable": "owner", "comprobante": "receipt",
}

# Spanish status labels -> status codes.
STATUS_ALIASES = {
    "aprobada": "approved", "aprobado": "approved", "approved": "approved",
    "pendiente": "pending", "pending": "pending",
    "rechazada": "rejected", "rechazado": "rejected", "rejected": "rejected",
    "reembolsada": "refunded", "reembolsado": "refunded", "refunded": "refunded",
}


def template_xlsx(kind: str) -> bytes:
    """Build a downloadable .xlsx template with one example row."""
    if kind == "orders":
        df = pd.DataFrame([{
            "id": "", "customer": "Cliente SAS", "amount": 1500000, "currency": "COP",
            "status": "approved", "gateway": "Wompi", "date": "2026-07-15",
        }], columns=ORDER_COLUMNS)
    else:
        df = pd.DataFrame([{
            "description": "Ejemplo", "category": "Operación", "vendor": "Proveedor",
            "amount": 120, "currency": "USD", "owner": "N. Apellido",
            "date": "2026-07-15", "receipt": "",
        }], columns=EXPENSE_COLUMNS)
    buf = io.BytesIO()
    df.to_excel(buf, index=False, sheet_name=kind)
    return buf.getvalue()


def _normalize_key(key: str) -> str:
    """Lowercase, trim and strip accents; then resolve Spanish aliases."""
    s = unicodedata.normalize("NFD", str(key).strip().lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return HEADER_ALIASES.get(s, s)


def _parse_date(value) -> str | None:
    """Accept ISO dates, datetime objects and dd/mm/yyyy strings."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return str(value)[:10]
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    for sep in ("/", "-"):
        parts = s.split(sep)
        if len(parts) == 3 and len(parts[2]) == 4:  # dd/mm/yyyy
            try:
                d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
                return f"{y:04d}-{m:02d}-{d:02d}"
            except ValueError:
                pass
    return None


def import_file(db: Session, kind: str, content: bytes, filename: str) -> dict:
    """Validate and insert rows; returns counts plus per-row errors."""
    try:
        if filename.lower().endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content))
        else:
            df = pd.read_excel(io.BytesIO(content))
    except Exception:
        raise ValueError("No pude leer el archivo. Use .xlsx, .xls o .csv.")

    if df.empty:
        raise ValueError("El archivo no tiene filas de datos.")

    df.columns = [_normalize_key(c) for c in df.columns]
    rows = df.to_dict(orient="records")

    errors: list[str] = []
    inserted = 0

    for i, raw in enumerate(rows):
        row_num = i + 2  # 1-indexed + header row
        r = {k: (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}

        def get(key):
            v = r.get(key)
            if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
                return None
            return v

        parsed_date = _parse_date(get("date"))
        if not parsed_date:
            errors.append(f"Fila {row_num}: fecha inválida ({raw.get('date', 'vacía')})")
            continue
        try:
            amount = float(get("amount"))
        except (TypeError, ValueError):
            amount = 0
        if amount <= 0:
            errors.append(f"Fila {row_num}: monto inválido")
            continue
        currency = str(get("currency") or "COP").upper()
        if currency not in CURRENCIES:
            errors.append(f"Fila {row_num}: moneda \"{currency}\" no soportada ({'/'.join(CURRENCIES)})")
            continue

        if kind == "orders":
            customer = get("customer")
            if not customer:
                errors.append(f"Fila {row_num}: falta cliente")
                continue
            status = STATUS_ALIASES.get(str(get("status") or "pending").strip().lower())
            if status not in ORDER_STATUSES:
                errors.append(f"Fila {row_num}: estado \"{get('status')}\" no válido")
                continue
            order_id = str(get("id")) if get("id") else next_order_id(db)
            if db.get(Order, order_id):
                errors.append(f"Fila {row_num}: id \"{order_id}\" duplicado")
                continue
            db.add(Order(id=order_id, customer=str(customer), amount=amount, currency=currency,
                         status=status, gateway=str(get("gateway")) if get("gateway") else None,
                         date=parsed_date))
            db.flush()  # make next_order_id see this row within the same import
            inserted += 1
        else:
            description = get("description")
            if not description:
                errors.append(f"Fila {row_num}: falta concepto")
                continue
            db.add(Expense(
                description=str(description), category=str(get("category") or "Sin categoría"),
                vendor=str(get("vendor")) if get("vendor") else None,
                amount=amount, currency=currency,
                owner=str(get("owner")) if get("owner") else None,
                date=parsed_date,
                receipt_name=str(get("receipt")) if get("receipt") else None,
            ))
            inserted += 1

    db.commit()
    return {
        "inserted": inserted,
        "skipped": len(rows) - inserted,
        "errors": errors[:20],
        # User-facing summary message (Spanish content).
        "message": f"Se importaron {inserted} de {len(rows)} filas en {kind}.",
    }
