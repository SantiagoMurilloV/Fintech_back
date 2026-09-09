"""Display formatting for agent output.

The web UI formats its own data, but the agent also writes prose and PDFs, so
it needs the same rules here. Kept in one module so figures always read the
same way across chat, tables and documents.
"""
from __future__ import annotations

import re

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


# ------------------------------------------------------------ presentation

# Key conventions shared by every tool payload. A figure under one of these
# keys is money, a percentage or a count, and is written here — never by the
# language model, which only copies what it is given.
_MONEY_KEYS = {"usd", "usd_eq", "total_usd"}
_PERCENT_KEYS = {"pct", "share"}
_COUNT_KEYS = {"count", "n"}


def _present_value(key: str, value, siblings: dict):
    if value is None:
        return "s/d"
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    if key in _MONEY_KEYS or key.endswith("_usd"):
        return usd_compact(value)
    if key.endswith("_pp"):
        return delta(value, " pp")
    if key in _PERCENT_KEYS or key.endswith("_pct"):
        return percent(value)
    if key in _COUNT_KEYS or key.endswith("_count"):
        return integer(value)
    if key == "amount":
        currency = siblings.get("currency")
        return f"{amount(value, currency)} {currency}" if currency else thousands(value, 2)
    return value


def present(data):
    """Tool data with every figure already written the way the panel shows it.

    The narrator receives this instead of raw numbers, so "139.0" can never
    turn into «USD 139,0 k» on the way to the reader.
    """
    if isinstance(data, dict):
        return {key: present(_present_value(key, value, data)) if isinstance(value, (dict, list))
                else _present_value(key, value, data)
                for key, value in data.items()}
    if isinstance(data, list):
        return [present(item) for item in data]
    return data


# A money figure as prose may write it: «USD 139,00», «USD 512,4 k»,
# «139 mil dólares». Any of these must exist, verbatim, in the presented data.
# es-CO number: dots group thousands, a comma starts the decimals. Anchored so
# a sentence's own punctuation («USD 0,00.») is not read as part of the figure.
_NUM = r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?"
_MONEY_IN_PROSE = re.compile(
    rf"USD\s?(?:{_NUM})(?:\s?(?:millones|mil|k)\b)?"
    rf"|(?:{_NUM})\s?(?:(?:millones|mil|k)\s?)?(?:USD|d[oó]lares)\b",
    re.IGNORECASE,
)


def _figure_key(text: str) -> str:
    """Normalise a money figure for comparison: spacing, case, «,00» endings."""
    text = re.sub(r"\s+", " ", text.strip().lower())
    text = re.sub(r"\bd[oó]lares\b", "usd", text)
    text = re.sub(r",0+\b", "", text)
    return text


def unbacked_figures(narrative: str, presented) -> list[str]:
    """Money figures in the prose that the presented data does not contain."""
    haystack = _figure_key(str(presented))
    return [figure for figure in _MONEY_IN_PROSE.findall(narrative)
            if _figure_key(figure) not in haystack]


# ------------------------------------------------------------ prose hygiene

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÁÉÍÓÚÑ¿¡«])")


def strip_markdown(text: str) -> str:
    """Plain prose: the chat renders text as text, so markup would show as is."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)      # **bold**
    text = re.sub(r"__(.+?)__", r"\1", text)              # __bold__
    text = re.sub(r"(?<!\w)[*_](\S.*?\S|\S)[*_](?!\w)", r"\1", text)  # *italic*
    text = re.sub(r"`+([^`]*)`+", r"\1", text)             # `code`
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)      # # headings
    text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", text, flags=re.M)  # bullets
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def first_sentences(text: str, limit: int) -> str:
    """At most `limit` sentences; a model that ignores the length rule is cut."""
    parts = _SENTENCE_END.split(text.strip())
    return " ".join(parts[:limit]).strip()
