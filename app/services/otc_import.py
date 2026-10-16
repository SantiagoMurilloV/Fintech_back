"""Load an OTC trade log (the «OTC» tab of the yearly workbook) as orders.

Each row of that tab is one executed trade:

    SELL  the client sends USDT/USDC and Mandioca wires USD   -> off-ramp
    BUY   the client wires USD and Mandioca sends USDT/USDC   -> on-ramp

The order keeps the USD side as its amount and everything else the finance
team tracks per trade in `extra`: the crypto side, the client price, the wire
fee, the gross spread, what the provider charged and the operating profit.
Those fields become visible columns of the Órdenes table.

Columns are found by their header names, not by position: the 2024 and 2025
workbooks put them in different places. Values are the ones the workbook
shows (cached formula results), so the profit per trade is exactly theirs.

Each trade gets a stable id from its year and sheet row (OTC-2025-00042), so
loading the same file again replaces those orders instead of duplicating
them. Nothing is guessed silently: every row skipped, every date corrected
and every repeated-looking trade is listed in the report.
"""
from __future__ import annotations

import io
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CustomColumn, Order
from . import columns as columns_service

SHEET = "OTC"

# Our field -> header names it goes by in the workbooks (folded, see _fold).
HEADERS = {
    "date": ("date", "fecha"),
    "client": ("client", "cliente"),
    "base": ("base",),
    "direction": ("direction", "direccion"),
    "usd": ("wire usd sent", "usd"),
    "usdt": ("usdt",),
    "client_price": ("client price",),
    "wire_fee": ("wire fee",),
    "gross": ("gross revenue",),
    "provider": ("provider", "proveedor"),
    "cost": ("mandioca cost",),
    "profit": ("operating profit",),
    "beneficiary": ("customer company name",),
}
REQUIRED = ("client", "direction", "usd", "usdt")

# The columns each trade shows in Órdenes: (key, label, type).
TRADE_COLUMNS = [
    ("tipo", "Tipo", "text"),
    ("activo", "Activo", "text"),
    ("usdt", "USDT", "number"),
    ("precio_cliente", "Precio cliente", "number"),
    ("fee_wire", "Fee wire", "number"),
    ("spread_bruto", "Spread bruto", "number"),
    ("costo_proveedor", "Costo proveedor", "number"),
    ("utilidad", "Utilidad operativa", "number"),
    ("beneficiario", "Beneficiario", "text"),
]

KIND = {"SELL": "Off-ramp", "BUY": "On-ramp"}
MAX_LISTED = 25


class OTCImportError(ValueError):
    pass


def _fold(text) -> str:
    text = unicodedata.normalize("NFD", str(text or "").strip().lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text)


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None          # «#DIV/0!», notes, blanks


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _find_header(rows: list[tuple]) -> tuple[int, dict[str, int]]:
    """The header row (0-based index) and our field -> column index."""
    for index, row in enumerate(rows[:10]):
        folded = [_fold(cell) for cell in row]
        if "direction" in folded and "client" in folded:
            found: dict[str, int] = {}
            for field, names in HEADERS.items():
                for position, name in enumerate(folded):
                    if name in names and position not in found.values():
                        found[field] = position
                        break
            # 2025 leaves the date column's header blank: it is the first column.
            if "date" not in found and not folded[0]:
                found["date"] = 0
            missing = [f for f in REQUIRED if f not in found]
            if missing:
                raise OTCImportError(
                    f"La hoja {SHEET} no tiene las columnas: {', '.join(missing)}.")
            return index, found
    raise OTCImportError(f"No encontré la fila de títulos en la hoja {SHEET} "
                         "(debe tener «Client» y «Direction»).")


_TEXT_DATE = re.compile(r"^(\d{1,2})/+(\d{1,2})/+(\d{2,4})$")


def _raw_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    match = _TEXT_DATE.match(str(value or "").strip())
    if match:
        month, day, year = (int(part) for part in match.groups())
        try:
            return date(year if year > 1000 else 2000 + year % 100, month, day)
        except ValueError:
            return None
    return None


def _blank(row: tuple, columns: dict[str, int]) -> bool:
    return all(row[i] in (None, "") or str(row[i]).strip() == ""
               for i in columns.values() if i < len(row))


def parse(content: bytes) -> dict:
    """Read the OTC tab into trades plus everything worth reporting."""
    import openpyxl

    try:
        book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:
        raise OTCImportError("No pude abrir el archivo. Use el .xlsx completo del año.") from None
    if SHEET not in book.sheetnames:
        raise OTCImportError(f"El archivo no tiene la hoja «{SHEET}». "
                             f"Hojas encontradas: {', '.join(book.sheetnames)}.")
    rows = list(book[SHEET].iter_rows(values_only=True))
    header_index, col = _find_header(rows)

    def cell(row, field):
        i = col.get(field)
        return row[i] if i is not None and i < len(row) else None

    # The trade log ends at its first empty row: what follows is the footer
    # (reconciliations, balances, scratch space), not trades.
    body: list[tuple[int, tuple]] = []
    for offset, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        if _blank(row, col):
            break
        body.append((offset, row))

    years = Counter(d.year for _, row in body if (d := _raw_date(cell(row, "date"))))
    if not years:
        raise OTCImportError("Ninguna fila de la hoja OTC tiene una fecha legible.")
    year = years.most_common(1)[0][0]

    trades, skipped, fixed_dates = [], [], []
    for sheet_row, row in body:
        direction = _clean(cell(row, "direction")).upper()
        usd = _number(cell(row, "usd"))
        usdt = _number(cell(row, "usdt"))
        client = _clean(cell(row, "client"))
        if direction not in KIND:
            skipped.append({"fila": sheet_row, "motivo": f"dirección «{direction or 'vacía'}»"})
            continue
        if not client:
            skipped.append({"fila": sheet_row, "motivo": "sin cliente"})
            continue
        if not usd or usd <= 0:
            skipped.append({"fila": sheet_row, "motivo": "monto USD en cero o vacío",
                            "cliente": client})
            continue

        raw = _raw_date(cell(row, "date"))
        if raw is None:
            skipped.append({"fila": sheet_row, "motivo": f"fecha ilegible «{cell(row, 'date')}»",
                            "cliente": client})
            continue
        when = raw
        if raw.year != year:
            # A typed or drag-filled year (2046, 2002, 2025…2029) inside one
            # year's log: the trade belongs to the log's year.
            try:
                when = raw.replace(year=year)
            except ValueError:
                when = raw.replace(year=year, day=28)
            fixed_dates.append({"fila": sheet_row, "original": raw.isoformat(),
                                "corregida": when.isoformat()})

        cost = _number(cell(row, "cost"))
        gross = _number(cell(row, "gross"))
        if gross is None and usdt is not None:
            gross = usdt - usd if direction == "SELL" else usd - usdt
        provider_cost = None
        if cost is not None:
            provider_cost = cost - usd if direction == "SELL" else usd - cost
        beneficiary = cell(row, "beneficiary")
        trades.append({
            "id": f"OTC-{year}-{sheet_row:05d}",
            "date": when.isoformat(),
            "customer": client.upper(),
            "amount": round(usd, 2),
            "provider": _clean(cell(row, "provider")) or None,
            "extra": {
                "tipo": KIND[direction],
                "activo": _clean(cell(row, "base")).upper() or None,
                "usdt": round(usdt, 2) if usdt is not None else None,
                "precio_cliente": _number(cell(row, "client_price")),
                "fee_wire": _number(cell(row, "wire_fee")),
                "spread_bruto": round(gross, 2) if gross is not None else None,
                "costo_proveedor": round(provider_cost, 2) if provider_cost is not None else None,
                "utilidad": (round(p, 2) if (p := _number(cell(row, "profit"))) is not None
                             else None),
                # Column N mixes beneficiaries with helper numbers: only text is a name.
                "beneficiario": _clean(beneficiary) if isinstance(beneficiary, str)
                and _clean(beneficiary) else None,
                "direccion": direction,
                "cliente_original": client,
                "source": "otc",
                "external_key": f"otc:{year}:{sheet_row}",
                "fila_hoja": sheet_row,
            },
        })
    return {"year": year, "trades": trades, "skipped": skipped, "fixed_dates": fixed_dates,
            "rows_read": len(body), "header_row": header_index + 1}


def _report(parsed: dict) -> dict:
    """Monthly totals, the same way the workbook adds them, plus what to review."""
    months: dict[str, dict] = defaultdict(lambda: {
        "operaciones": 0, "off_ramp_usd": 0.0, "on_ramp_usd": 0.0, "spread_bruto": 0.0,
        "costo_proveedor": 0.0, "utilidad": 0.0})
    for trade in parsed["trades"]:
        m = months[trade["date"][:7]]
        extra = trade["extra"]
        m["operaciones"] += 1
        m["off_ramp_usd" if extra["direccion"] == "SELL" else "on_ramp_usd"] += trade["amount"]
        m["spread_bruto"] += extra["spread_bruto"] or 0.0
        m["costo_proveedor"] += extra["costo_proveedor"] or 0.0
        m["utilidad"] += extra["utilidad"] or 0.0
    by_month = [{"mes": key, **{k: round(v, 2) if isinstance(v, float) else v
                               for k, v in value.items()}}
                for key, value in sorted(months.items())]
    totals = {k: round(sum(m[k] for m in by_month), 2) for k in by_month[0]
              if k != "mes"} if by_month else {}

    # Same date, client, direction and both amounts: listed, never dropped —
    # a client may genuinely repeat a ticket; the finance team decides.
    keys = Counter((t["date"], t["customer"], t["extra"]["direccion"], t["amount"],
                    t["extra"]["usdt"]) for t in parsed["trades"])
    repeated = [{"fecha": k[0], "cliente": k[1], "tipo": KIND[k[2]], "usd": k[3], "veces": n}
                for k, n in keys.items() if n > 1]
    repeated.sort(key=lambda item: -item["usd"] * item["veces"])
    return {
        "anio": parsed["year"],
        "filas_leidas": parsed["rows_read"],
        "operaciones": len(parsed["trades"]),
        "omitidas": len(parsed["skipped"]),
        "omitidas_detalle": parsed["skipped"][:MAX_LISTED],
        "fechas_corregidas": len(parsed["fixed_dates"]),
        "fechas_corregidas_detalle": parsed["fixed_dates"][:MAX_LISTED],
        "posibles_duplicados": sum(item["veces"] - 1 for item in repeated),
        "posibles_duplicados_detalle": repeated[:MAX_LISTED],
        "por_mes": by_month,
        "totales": totals,
    }


def _ensure_columns(db: Session) -> list[str]:
    """The trade fields as columns of Órdenes; existing ones are kept."""
    existing = {c.key for c in db.scalars(
        select(CustomColumn).where(CustomColumn.entity == "orders")).all()}
    created = []
    for key, label, data_type in TRADE_COLUMNS:
        if key not in existing:
            columns_service.create_column(db, "orders", label, data_type, key=key)
            created.append(label)
    return created


def run(db: Session, content: bytes, *, dry_run: bool = False) -> dict:
    """Parse, report and — unless `dry_run` — write the trades as orders."""
    parsed = parse(content)
    report = _report(parsed)
    report["dry_run"] = dry_run
    if dry_run:
        report["message"] = (f"Vista previa: {report['operaciones']} operaciones de "
                             f"{report['anio']} listas para cargar. No se escribió nada.")
        return report

    report["columnas_creadas"] = _ensure_columns(db)
    ids = [t["id"] for t in parsed["trades"]]
    existing = {o.id: o for o in db.scalars(select(Order).where(Order.id.in_(ids))).all()} \
        if ids else {}
    written = replaced = 0
    for trade in parsed["trades"]:
        order = existing.get(trade["id"])
        if order is None:
            db.add(Order(id=trade["id"], customer=trade["customer"], amount=trade["amount"],
                         currency="USD", status="approved", gateway=trade["provider"],
                         date=trade["date"], extra=trade["extra"]))
            written += 1
        else:
            order.customer, order.amount, order.currency = trade["customer"], trade["amount"], "USD"
            order.status, order.gateway, order.date = "approved", trade["provider"], trade["date"]
            order.extra = trade["extra"]
            replaced += 1
    db.commit()
    report["nuevas"], report["reemplazadas"] = written, replaced
    report["message"] = (f"Se cargaron {written + replaced} operaciones de {report['anio']} "
                         f"({written} nuevas, {replaced} reemplazadas).")
    return report
