"""Demo financial feed: a built-in stand-in for the external accountant API.

Same contract as the real provider (Mandioca/AUXO): DRF-style pagination
(`count` / `next` / `previous` / `results`) behind an `Authorization:
Api-Key <key>` header, with records shaped like its pay-in/pay-out orders.
It exists so the whole pull flow — probe, mapping, paginated sync, report —
can be demoed before the provider's keys work, against data that behaves
exactly like the real thing.

Every record is deterministic: same ids, amounts and statuses on every run.
Only the dates float, pinned to "the last 90 days", so the panel always looks
current. There is no panel auth here on purpose — this plays the role of an
EXTERNAL service, and the only credential it knows is the feed key.

Point Configuración at it:
    URL base            <backend>/api/demo-feed/
    Token               DEMO_FEED_KEY (.env; default "demo.mandioca-feed")
    Cómo se envía       api-key
    Rutas de órdenes    payins/, payouts/
    Rutas de gastos     fees/
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Header, HTTPException, Request

from ..config import DEMO_FEED_KEY

router = APIRouter(tags=["demo-feed"])

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 50  # low on purpose: the demo should show the paginated walk

# Weighted like a real operation: most orders succeed, the rest spread over
# the provider's documented lifecycle (see mapping.STATUS_WORDS).
STATUSES = ["Succeeded"] * 7 + [
    "Waiting approval", "Accepted", "Transferred to external",
    "Cancelled", "Expired",
]

COMPANIES = [
    "Comercializadora Andina SAS", "Inversiones El Dorado", "Logística Caribe",
    "TecnoSoluciones Bogotá", "Agroexportos del Valle", "Distribuidora Pacífico",
    "Constructora Horizonte", "Alimentos La Sabana", "Textiles Medellín",
    "Servicios Cafeteros SAS",
]

FEE_ITEMS = [
    ("Comisión pasarela de pagos", "Comisiones", "Wompi"),
    ("Fee de red TRON", "Comisiones", "TronGrid"),
    ("Infraestructura cloud", "Infraestructura", "AWS"),
    ("Licencias de software", "Infraestructura", "Google Workspace"),
    ("Honorarios contables", "Servicios profesionales", "Contadores Asociados"),
    ("Cumplimiento y KYC", "Servicios profesionales", "Truora"),
    ("Soporte y monitoreo", "Infraestructura", "Datadog"),
    ("Mensajería y notificaciones", "Operación", "Twilio"),
    ("Papelería y oficina", "Operación", "Panamericana"),
]


def _pid(feed: str, index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"demo-feed/{feed}/{index}"))


def _day(index: int) -> str:
    # Spread deterministically over the last 90 days, newest first-ish.
    return (date.today() - timedelta(days=(index * 7) % 90)).isoformat()


def _stamp(index: int) -> str:
    return f"{_day(index)}T{8 + (index % 10):02d}:{(index * 13) % 60:02d}:00Z"


def _payin(index: int) -> dict:
    in_cop = index % 4 != 0  # 3 of 4 collect in COP, the rest in USD
    amount = (150_000 + (index * 137_411) % 4_850_000 if in_cop
              else 120 + (index * 731) % 4_800)
    rate = 4_100 if in_cop else 1
    return {
        "pid": _pid("payins", index),
        "amount_in": f"{amount:.9f}",
        "currency_in": "COP" if in_cop else "USD",
        "amount_out": f"{amount / rate * 0.988:.9f}",  # minus a demo spread
        "currency_out": "USDT",
        "network": "TRON" if index % 3 else "ETHEREUM",
        "status": STATUSES[index % len(STATUSES)],
        "operation_type": "payin manual",
        "legal_name": COMPANIES[index % len(COMPANIES)],
        "document_number": f"9{(index * 37) % 100_000_000:08d}",
        "created_at": _stamp(index),
        "updated_at": _stamp(index),
    }


def _payout(index: int) -> dict:
    stable = index % 5 == 0  # a few stay in USDT: exercises the stablecoin-at-par note
    amount = 250 + (index * 977) % 9_500
    return {
        "pid": _pid("payouts", index),
        "amount_in": f"{amount:.9f}",
        "currency_in": "USDT" if stable else "USD",
        "amount_out": f"{amount * 4_050:.9f}",
        "currency_out": "COP",
        "network": "TRON" if index % 3 else "ETHEREUM",
        "status": STATUSES[(index * 5) % len(STATUSES)],
        "operation_type": "payout",
        "legal_name": COMPANIES[(index * 3) % len(COMPANIES)],
        "document_number": f"8{(index * 53) % 100_000_000:08d}",
        "created_at": _stamp(index),
        "updated_at": _stamp(index),
    }


def _fee(index: int) -> dict:
    concept, category, vendor = FEE_ITEMS[index % len(FEE_ITEMS)]
    in_cop = index % 3 != 0
    amount = (80_000 + (index * 91_237) % 2_400_000 if in_cop
              else 40 + (index * 217) % 1_900)
    return {
        "id": index + 1,
        "concept": concept,
        "category": category,
        "vendor": vendor,
        "amount": round(float(amount), 2),
        "currency": "COP" if in_cop else "USD",
        "created_at": _stamp(index),
    }


FEEDS = {
    "payins": [_payin(i) for i in range(120)],
    "payouts": [_payout(i) for i in range(80)],
    "fees": [_fee(i) for i in range(45)],
}


def _require_key(authorization: str | None) -> None:
    if authorization != f"Api-Key {DEMO_FEED_KEY}":
        # Mirrors the DRF wording so the demo failure mode matches the real one.
        raise HTTPException(
            status_code=403, detail="Authentication credentials were not provided.")


def _page(request: Request, rows: list[dict], page: int, page_size: int) -> dict:
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    start, end = (page - 1) * page_size, page * page_size

    def url_for(number: int | None):
        if number is None:
            return None
        return str(request.url.include_query_params(page=number, page_size=page_size))

    return {
        "count": len(rows),
        "next": url_for(page + 1) if end < len(rows) else None,
        "previous": url_for(page - 1) if page > 1 else None,
        "results": rows[start:end],
    }


@router.get("/api/demo-feed/{feed}/")
def demo_feed(feed: str, request: Request, page: int = 1,
              page_size: int = DEFAULT_PAGE_SIZE,
              authorization: str | None = Header(None)):
    _require_key(authorization)
    rows = FEEDS.get(feed)
    if rows is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return _page(request, rows, page, page_size)
