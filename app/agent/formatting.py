"""Display formatting for agent output.

The web UI formats its own data, but the agent also writes prose and PDFs, so
it needs the same rules here. Kept in one module so figures always read the
same way across chat, tables and documents.
"""
from __future__ import annotations

MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
          "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

ZERO_DECIMAL = {"COP"}

STATUS_LABELS = {
    "approved": "Aprobada", "pending": "Pendiente",
    "rejected": "Rechazada", "refunded": "Reembolsada",
}
STATUS_TONES = {
    "approved": "success", "pending": "warning",
    "rejected": "danger", "refunded": "neutral",
}


def thousands(value: float, decimals: int = 1) -> str:
    """es-CO grouping: 1234.5 -> '1.234,5'."""
    formatted = f"{value:,.{decimals}f}"
    return formatted.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def amount(value: float, currency: str) -> str:
    return thousands(value, 0 if (currency or "").upper() in ZERO_DECIMAL else 2)


def usd_compact(value: float) -> str:
    """Below a thousand the "k" notation reads as zero; show the real value."""
    if abs(value) < 1000:
        return f"USD {thousands(value, 2)}"
    return f"USD {thousands(value / 1000, 1)} k"


def integer(value: int) -> str:
    return thousands(value, 0)


def percent(value: float) -> str:
    return f"{thousands(value, 1)}%"


def delta(value, suffix: str = "%") -> str:
    if value is None:
        return "s/d"
    sign = "+" if value > 0 else ""
    return f"{sign}{thousands(value, 1)}{suffix}"


def short_date(iso: str) -> str:
    if not iso:
        return ""
    return f"{iso[8:10]} {MONTHS[int(iso[5:7]) - 1][:3]}"


def period_name(period: str) -> str:
    if not period:
        return ""
    month = MONTHS[int(period[5:7]) - 1]
    return f"{month.capitalize()} {period[:4]}"


def month_name(period: str) -> str:
    return MONTHS[int(period[5:7]) - 1] if period else ""


def status_label(code: str) -> str:
    return STATUS_LABELS.get(code, code)


def status_tone(code: str) -> str:
    return STATUS_TONES.get(code, "neutral")
