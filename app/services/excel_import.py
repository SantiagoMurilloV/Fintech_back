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
from . import fingerprint
from .finance import next_order_id, usd_eq

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


def import_file(db: Session, kind: str, content: bytes, filename: str, *,
                dry_run: bool = False, batch_id: int | None = None) -> dict:
    """Validate rows and — unless `dry_run` — insert them.

    Returns counts, every rejected row with its reason, and control totals per
    month (rows and USD-equivalent amount) computed from the file, so the
    import can be reconciled against the panel later.
    """
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

    rejected: list[dict] = []
    totals: dict[str, dict] = {}
    inserted = 0
    planned_ids: set[str] = set()

    for i, raw in enumerate(rows):
        row_num = i + 2  # 1-indexed + header row
        r = {k: (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}

        def get(key):
            v = r.get(key)
            if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
                return None
            return v

        def reject(reason: str):
            rejected.append({"fila": row_num, "motivo": reason})

        parsed_date = _parse_date(get("date"))
        if not parsed_date:
            reject(f"fecha inválida ({raw.get('date', 'vacía')})")
            continue
        try:
            amount = float(get("amount"))
        except (TypeError, ValueError):
            amount = 0
        if amount <= 0:
            reject("monto inválido o sin columna de monto (amount / monto)")
            continue
        currency = str(get("currency") or "COP").upper()
        if currency not in CURRENCIES:
            reject(f"moneda «{currency}» no soportada ({'/'.join(CURRENCIES)})")
            continue

        if kind == "orders":
            customer = get("customer")
            if not customer:
                reject("falta cliente")
                continue
            status = STATUS_ALIASES.get(str(get("status") or "pending").strip().lower())
            if status not in ORDER_STATUSES:
                reject(f"estado «{get('status')}» no válido")
                continue
            order_id = str(get("id")) if get("id") else None
            if order_id and (order_id in planned_ids or db.get(Order, order_id)):
                reject(f"id «{order_id}» duplicado")
                continue
            if order_id:
                planned_ids.add(order_id)
            if not dry_run:
                gateway = str(get("gateway")) if get("gateway") else None
                db.add(Order(
                    id=order_id or next_order_id(db), customer=str(customer), amount=amount,
                    currency=currency, status=status, gateway=gateway, date=parsed_date,
                    extra={"source": "excel", "fila": row_num,
                           **({"import_batch": batch_id} if batch_id is not None else {}),
                           "huella": fingerprint.order_plain(parsed_date, str(customer), amount,
                                                             currency, status)}))
                db.flush()  # make next_order_id see this row within the same import
        else:
            description = get("description")
            if not description:
                reject("falta concepto")
                continue
            category = str(get("category") or "Sin categoría")
            if not dry_run:
                db.add(Expense(
                    description=str(description), category=category,
                    vendor=str(get("vendor")) if get("vendor") else None,
                    amount=amount, currency=currency,
                    owner=str(get("owner")) if get("owner") else None,
                    date=parsed_date,
                    receipt_name=str(get("receipt")) if get("receipt") else None,
                    extra={"source": "excel", "fila": row_num,
                           **({"import_batch": batch_id} if batch_id is not None else {}),
                           "huella": fingerprint.expense_plain(parsed_date, str(description),
                                                               amount, currency, category)},
                ))
        month = totals.setdefault(parsed_date[:7], {"rows": 0, "volume": 0.0})
        month["rows"] += 1
        month["volume"] = round(month["volume"] + usd_eq(amount, currency), 2)
        inserted += 1

    if not dry_run:
        db.commit()
    errors = [f"Fila {item['fila']}: {item['motivo']}" for item in rejected]
    verb = "listas para importar" if dry_run else "importadas"
    return {
        "inserted": inserted,
        "skipped": len(rows) - inserted,
        "errors": errors[:20],
        "rejected": rejected,
        "rows_read": len(rows),
        "totals": dict(sorted(totals.items())),
        # User-facing summary message (Spanish content).
        "message": f"{inserted} de {len(rows)} filas {verb} en {kind}.",
    }
