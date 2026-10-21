"""Where a record came from: one word every screen, filter and report shares.

Orders and expenses arrive from several places at once — the OTC trade log
loaded from Excel, QuickBooks, an endpoint, a plain Excel upload, the chat or
a person typing. Each writer stamps `extra.source`; a record without a stamp
predates the stamps or was typed by hand, and counts as «manual».
"""
from __future__ import annotations

from sqlalchemy import case, func

# source stamp -> origin code shown to people (several stamps may share one).
_CODES = {
    "otc": "otc",
    "quickbooks": "quickbooks",
    "api": "api",
    "external": "api",
    "excel": "excel",
    "manual": "manual",
}
ORIGINS = ("otc", "quickbooks", "api", "excel", "manual")
LABELS = {
    "otc": "Excel OTC",
    "quickbooks": "QuickBooks",
    "api": "Endpoint",
    "excel": "Excel",
    "manual": "Manual",
}


def of(extra) -> str:
    """The origin code of a record from its `extra`."""
    source = (extra or {}).get("source") if isinstance(extra, dict) else None
    return _CODES.get(str(source or "").strip().lower(), "manual")


def label(code: str) -> str:
    return LABELS.get(code, code)


def expression(model):
    """The same rule in SQL, for filters and counts.

    `external` (rows the chat imported from an endpoint) is folded into «api»
    so the SQL and `of` never disagree.
    """
    stamp = func.lower(func.coalesce(model.extra["source"].as_string(), "manual"))
    return case(
        (stamp.in_(("otc",)), "otc"),
        (stamp.in_(("quickbooks",)), "quickbooks"),
        (stamp.in_(("api", "external")), "api"),
        (stamp.in_(("excel",)), "excel"),
        else_="manual",
    )
