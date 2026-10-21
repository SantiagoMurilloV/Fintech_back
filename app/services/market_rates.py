"""Market prices, fetched from free public sources and kept as our own copy.

Three times a day by default (`rates.interval_seconds`, 8 h) the backend calls:

    Binance    every coin quoted in USDT or USDC      one call, ~770 pairs
    Kraken     every coin quoted in USD, USDT, USDC   one call, ~770 pairs, bid/ask
    Coinbase   USDT/USD                               live, bid/ask
    TRM        USD/COP                                official daily rate (datos.gov.co)
    ECB        USD against 29 currencies              daily reference rates (Frankfurter)

and stores what each one said in `rate_snapshots`. Spot sources add a row per
pair per run; daily sources add one only when they publish a new day. The
history before the first run comes from `market_history.backfill` (daily
closes from Binance since each coin was listed, source «binance_1d»).

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
QUOTES = ("USD", "USDT", "USDC")      # a coin priced in anything else adds nothing here
# Kraken's legacy asset codes -> the names everyone else uses.
KRAKEN_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}


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
    """Every Kraken pair quoted in USD, USDT or USDC, with bid and ask."""
    pairs = client.get("https://api.kraken.com/0/public/AssetPairs").json()
    tickers = client.get("https://api.kraken.com/0/public/Ticker").json()
    for body in (pairs, tickers):
        if body.get("error"):
            raise RuntimeError("; ".join(body["error"]))
    names = {}
    for key, info in (pairs.get("result") or {}).items():
        base, _, quote = str(info.get("wsname") or "").partition("/")
        if quote in QUOTES and base:
            names[key] = f"{KRAKEN_ALIASES.get(base, base)}/{quote}"
    out, seen = [], set()
    for key, ticker in (tickers.get("result") or {}).items():
        pair = names.get(key)
        if not pair or pair in seen:
            continue
        seen.add(pair)
        out.append({"pair": pair, "price": _float(ticker["c"][0]),
                    "bid": _float(ticker["b"][0]), "ask": _float(ticker["a"][0])})
    return out


def fetch_binance(client: httpx.Client) -> list[dict]:
    """Every Binance coin quoted in USDT or USDC, last price."""
    info = client.get("https://api.binance.com/api/v3/exchangeInfo",
                      params={"permissions": "SPOT", "symbolStatus": "TRADING"}).json()
    names = {s["symbol"]: f"{s['baseAsset']}/{s['quoteAsset']}"
             for s in info.get("symbols", []) if s.get("quoteAsset") in QUOTES}
    prices = client.get("https://api.binance.com/api/v3/ticker/price").json()
    if not isinstance(prices, list):
        raise RuntimeError(f"Binance respondió {str(prices)[:120]}")
    return [{"pair": names[p["symbol"]], "price": _float(p["price"])}
            for p in prices if p.get("symbol") in names]


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
    body = client.get("https://api.frankfurter.dev/v1/latest", params={"base": "USD"}).json()
    return [{"pair": f"USD/{code}", "price": _float(value), "rate_date": body.get("date")}
            for code, value in (body.get("rates") or {}).items()]


FETCHERS = {"binance": fetch_binance, "kraken": fetch_kraken, "coinbase": fetch_coinbase,
            "trm": fetch_trm, "ecb": fetch_ecb}
SPOT_SOURCES = ("binance", "kraken", "coinbase")


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
            stored, done = 0, set()
            for quote in quotes:
                if quote["pair"] in done:      # a source listing a pair twice keeps the first
                    continue
                done.add(quote["pair"])
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


def seconds_since_last(db: Session, sources: tuple[str, ...]) -> float | None:
    """How long ago any of `sources` was read, or None if never.

    The schedulers use it so a restart (a deploy, a reload) does not take an
    extra round: the next run comes when the interval since the LAST run ends.
    """
    last = db.scalar(select(func.max(RateSnapshot.taken_at))
                     .where(RateSnapshot.source.in_(sources)))
    if not last:
        return None
    then = datetime.strptime(last, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


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
