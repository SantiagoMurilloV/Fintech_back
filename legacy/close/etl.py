"""
ETL: raw downloaded statements → canonical Movement list.

Column names differ per portal; column_maps below translate each known
export format. Adding a portal = adding one map entry (or letting the
LLM propose one on first run, which a human then confirms into this file).
Money math is 100% pandas — no LLM.
"""

from datetime import datetime
from pathlib import Path

import pandas as pd

from close.models import Movement

# Per-source column mapping: canonical_field -> list of candidate column names
COLUMN_MAPS = {
    "default": {
        "txn_date": ["fecha", "date", "fecha transaccion", "fecha_transaccion", "f. valor"],
        "description": ["descripcion", "description", "concepto", "detalle", "observaciones"],
        "reference": ["referencia", "reference", "documento", "no. documento", "numero", "id"],
        "amount": ["valor", "amount", "monto", "importe"],
        "debit": ["debito", "debit", "cargo", "salida"],
        "credit": ["credito", "credit", "abono", "entrada"],
    },
}

DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d", "%d/%m/%y"]


def _find_col(df: pd.DataFrame, candidates: list) -> str | None:
    normalized = {str(c).strip().lower(): c for c in df.columns}
    for cand in candidates:
        if cand in normalized:
            return normalized[cand]
    return None


def _parse_date(value) -> datetime.date:
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "date"):
        return value.date()
    s = str(value).strip()[:10]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unparseable date: {value!r}")


def _parse_amount(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace("$", "").replace(" ", "").strip()
    # Colombian format: 1.234.567,89 → 1234567.89
    if "," in s and s.rfind(",") > s.rfind("."):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    return float(s) if s not in ("", "-") else 0.0


def load_statement(path: Path, source: str, side: str) -> list[Movement]:
    """Read an .xlsx/.csv export and normalize to Movements."""
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
    else:
        df = pd.read_excel(path)

    cmap = COLUMN_MAPS.get(source, COLUMN_MAPS["default"])

    date_col = _find_col(df, cmap["txn_date"])
    desc_col = _find_col(df, cmap["description"])
    ref_col = _find_col(df, cmap["reference"])
    amount_col = _find_col(df, cmap["amount"])
    debit_col = _find_col(df, cmap["debit"])
    credit_col = _find_col(df, cmap["credit"])

    if date_col is None:
        raise ValueError(f"{path.name}: no date column found (columns: {list(df.columns)})")
    if amount_col is None and not (debit_col or credit_col):
        raise ValueError(f"{path.name}: no amount/debit/credit column found")

    movements = []
    for _, row in df.iterrows():
        if pd.isna(row[date_col]):
            continue  # skip total/blank rows
        if amount_col is not None:
            amount = _parse_amount(row[amount_col])
        else:
            credit = _parse_amount(row[credit_col]) if credit_col and not pd.isna(row[credit_col]) else 0.0
            debit = _parse_amount(row[debit_col]) if debit_col and not pd.isna(row[debit_col]) else 0.0
            amount = credit - debit
        movements.append(
            Movement(
                source=source,
                side=side,
                txn_date=_parse_date(row[date_col]),
                amount=round(amount, 2),
                description=str(row[desc_col]).strip() if desc_col and not pd.isna(row[desc_col]) else "",
                reference=str(row[ref_col]).strip() if ref_col and not pd.isna(row[ref_col]) else "",
            )
        )
    print(f"📄 {path.name}: {len(movements)} movements loaded ({side})")
    return movements
