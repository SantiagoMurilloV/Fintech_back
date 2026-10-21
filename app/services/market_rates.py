"""Market prices, fetched from free public sources and kept as our own copy.

Every tick (hourly by default, `rates.interval_seconds`) the backend calls:

    Kraken     USDT/USD, USDC/USD   live, last trade + bid/ask, no key
    Coinbase   USDT/USD             live, last trade + bid/ask, no key
    TRM        USD/COP              official daily rate (Superfinanciera, datos.gov.co)
    ECB        USD/BRL, USD/MXN,    daily reference rates (Frankfurter)
               USD/EUR, USD/GBP

and stores what each one said in `rate_snapshots`. Live sources add a row
per tick; daily sources add one only when they publish a new day, so the
table holds a history, not 288 copies of the same TRM.

Each source is fetched on its own: one being down never stops the others,
and the result says which answered. Nothing here changes how the panel
converts money yet — that stays a separate, deliberate decision.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import RateSnapshot

log = logging.getLogger("rates")
TIMEOUT = 10.0
DAILY_SOURCES = {"trm", "ecb"}
ECB_SYMBOLS = ("BRL", "MXN", "EUR", "GBP")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ fetchers
# Each returns [{pair, price, bid, ask, rate_date}] or raises.

def fetch_kraken(client: httpx.Client) -> list[dict]:
    body = client.get("https://api.kraken.com/0/public/Ticker",
                      params={"pair": "USDTUSD,USDCUSD"}).json()
    if body.get("error"):
        raise RuntimeError("; ".join(body["error"]))
    names = {"USDTZUSD": "USDT/USD", "USDTUSD": "USDT/USD", "USDCUSD": "USDC/USD"}
    out = []
    for key, ticker in (body.get("result") or {}).items():
        if key in names:
            out.append({"pair": names[key], "price": _float(ticker["c"][0]),
                        "bid": _float(ticker["b"][0]), "ask": _float(ticker["a"][0])})
    return out


def fetch_coinbase(client: httpx.Client) -> list[dict]:
    body = client.get("https://api.exchange.coinbase.com/products/USDT-USD/ticker").json()
    return [{"pair": "USDT/USD", "price": _float(body.get("price")),
             "bid": _float(body.get("bid")), "ask": _float(body.get("ask"))}]


def fetch_trm(client: httpx.Client) -> list[dict]:
    rows = client.get("https://www.datos.gov.co/resource/32sa-8pi3.json",
                      params={"$order": "vigenciadesde DESC", "$limit": 1}).json()
    if not rows:
        raise RuntimeError("datos.gov.co no devolvió la TRM.")
    return [{"pair": "USD/COP", "price": _float(rows[0]["valor"]),
             "rate_date": str(rows[0]["vigenciadesde"])[:10]}]


def fetch_ecb(client: httpx.Client) -> list[dict]:
    body = client.get("https://api.frankfurter.dev/v1/latest",
                      params={"base": "USD", "symbols": ",".join(ECB_SYMBOLS)}).json()
    return [{"pair": f"USD/{code}", "price": _float(value), "rate_date": body.get("date")}
            for code, value in (body.get("rates") or {}).items()]


FETCHERS = {"kraken": fetch_kraken, "coinbase": fetch_coinbase,
            "trm": fetch_trm, "ecb": fetch_ecb}


# ------------------------------------------------------------------ snapshot

def snapshot(db: Session, fetchers: dict | None = None) -> dict:
    """Ask every source once and store what is new; report per source."""
    taken_at = _now()
    report: dict[str, dict] = {}
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "Mandioca-panel/1.0"}) as client:
        for source, fetch in (fetchers or FETCHERS).items():
            try:
                quotes = [q for q in fetch(client) if q.get("price")]
            except Exception as err:  # noqa: BLE001 — one source down never stops the others
                log.warning("Rates from %s failed: %s", source, err)
                report[source] = {"ok": False, "error": str(err)[:200]}
                continue
            stored = 0
            for quote in quotes:
                if source in DAILY_SOURCES:
                    seen = db.scalar(select(RateSnapshot.id).where(
                        RateSnapshot.source == source, RateSnapshot.pair == quote["pair"],
                        RateSnapshot.rate_date == quote.get("rate_date")))
                    if seen:
                        continue
                db.add(RateSnapshot(source=source, pair=quote["pair"], price=quote["price"],
                                    bid=quote.get("bid"), ask=quote.get("ask"),
                                    rate_date=quote.get("rate_date"), taken_at=taken_at))
                stored += 1
            report[source] = {"ok": True, "quotes": len(quotes), "stored": stored}
    db.commit()
    return {"taken_at": taken_at, "sources": report}


def latest(db: Session) -> list[dict]:
    """The most recent reading of each pair from each source."""
    newest = (select(RateSnapshot.source, RateSnapshot.pair,
                     func.max(RateSnapshot.id).label("id"))
              .group_by(RateSnapshot.source, RateSnapshot.pair).subquery())
    rows = db.scalars(select(RateSnapshot).join(newest, RateSnapshot.id == newest.c.id)
                      .order_by(RateSnapshot.pair, RateSnapshot.source)).all()
    return [view(row) for row in rows]


def history(db: Session, pair: str, source: str | None = None,
            since: str | None = None, until: str | None = None,
            limit: int = 2000) -> list[dict]:
    query = select(RateSnapshot).where(RateSnapshot.pair == pair)
    if source:
        query = query.where(RateSnapshot.source == source)
    if since:
        query = query.where(RateSnapshot.taken_at >= since)
    if until:
        query = query.where(RateSnapshot.taken_at <= until)
    rows = db.scalars(query.order_by(RateSnapshot.taken_at.desc())
                      .limit(max(1, min(limit, 10_000)))).all()
    return [view(row) for row in reversed(rows)]


def view(row: RateSnapshot) -> dict:
    return {"pair": row.pair, "source": row.source, "price": row.price, "bid": row.bid,
            "ask": row.ask, "rate_date": row.rate_date, "taken_at": row.taken_at}
