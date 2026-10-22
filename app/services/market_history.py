"""Price history: the daily close of every coin since it was listed.

`market_rates` keeps prices from the first run onwards; this fills the past.
For every Binance pair quoted in USDT or USDC it pages through the daily
candles (`/api/v3/klines`, 1000 days per call) and stores each closed day's
close in `rate_snapshots`:

    source «binance_1d» · pair «BTC/USDT» · price = close · rate_date = the day
    taken_at = «<day>T23:59:59» (the candle closes at midnight UTC)

It resumes: a pair continues from the day after its last stored day, so
running it again only adds what is missing — after the first run, a few
seconds. Binance's free public API needs no key; the run respects its rate
limit (waits when asked to).

    python -m app.services.market_history            # every pair
    python -m app.services.market_history BTC ETH    # only these coins
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from ..models import RateSnapshot

log = logging.getLogger("market_history")

SOURCE = "binance_1d"
QUOTES = ("USDT", "USDC")
BASE_URL = "https://api.binance.com"
DAY_MS = 86_400_000
PAGE = 1000
PAUSE = 0.12            # ~500 calls a minute, well under Binance's weight limit

COINBASE_PRODUCTS = {"USDT-USD": "USDT/USD"}   # dollar stablecoin history (Binance has none)
COINBASE_SOURCE = "coinbase_1d"
COINBASE_START = "2021-05-01"
TRM_URL = "https://www.datos.gov.co/resource/32sa-8pi3.json"   # official COP rate since 1991

# Progress of the run in this process, for the status endpoint.
STATUS: dict = {"running": False}


def _pairs(client: httpx.Client, coins: list[str] | None) -> list[tuple[str, str]]:
    info = client.get(f"{BASE_URL}/api/v3/exchangeInfo",
                      params={"permissions": "SPOT", "symbolStatus": "TRADING"}).json()
    wanted = {coin.upper() for coin in coins} if coins else None
    return sorted((s["symbol"], f"{s['baseAsset']}/{s['quoteAsset']}")
                  for s in info.get("symbols", [])
                  if s.get("quoteAsset") in QUOTES
                  and (wanted is None or s["baseAsset"] in wanted))


def _klines(client: httpx.Client, symbol: str, start_ms: int) -> list[list]:
    for attempt in range(5):
        reply = client.get(f"{BASE_URL}/api/v3/klines",
                           params={"symbol": symbol, "interval": "1d",
                                   "startTime": start_ms, "limit": PAGE})
        if reply.status_code in (418, 429):
            wait = int(reply.headers.get("Retry-After", "30"))
            log.warning("Binance asked to wait %s s", wait)
            time.sleep(wait)
            continue
        reply.raise_for_status()
        return reply.json()
    raise RuntimeError("Binance kept rate-limiting")


def _last_day(db: Session, pair: str) -> str | None:
    return db.scalar(select(func.max(RateSnapshot.rate_date))
                     .where(RateSnapshot.source == SOURCE, RateSnapshot.pair == pair))


def backfill(db: Session, coins: list[str] | None = None, progress=None) -> dict:
    """Store every missing closed daily candle; returns what was done."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    report = {"pairs": 0, "days_added": 0, "pairs_failed": [], "started_at": now_ms}
    STATUS.update(running=True, done=0, total=0, days_added=0, current=None, error=None)
    try:
        with httpx.Client(timeout=30, headers={"User-Agent": "Mandioca-panel/1.0"}) as client:
            pairs = _pairs(client, coins)
            STATUS["total"] = len(pairs)
            for index, (symbol, pair) in enumerate(pairs, start=1):
                STATUS.update(done=index - 1, current=pair)
                last = _last_day(db, pair)
                start = (int(datetime.strptime(last, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc).timestamp() * 1000) + DAY_MS) if last else 0
                added = 0
                try:
                    while start < now_ms:
                        candles = _klines(client, symbol, start)
                        time.sleep(PAUSE)
                        closed = [c for c in candles if c[6] < now_ms]   # close time passed
                        rows = []
                        for candle in closed:
                            day = datetime.fromtimestamp(candle[0] / 1000, timezone.utc)
                            day = day.strftime("%Y-%m-%d")
                            rows.append({"source": SOURCE, "pair": pair, "price": float(candle[4]),
                                         "rate_date": day, "taken_at": f"{day}T23:59:59"})
                        if rows:
                            db.execute(insert(RateSnapshot), rows)
                            db.commit()
                            added += len(rows)
                        if len(candles) < PAGE:
                            break
                        start = candles[-1][0] + DAY_MS
                except Exception as err:  # noqa: BLE001 — one pair failing never stops the rest
                    db.rollback()
                    log.warning("History for %s failed: %s", pair, err)
                    report["pairs_failed"].append(pair)
                report["pairs"] += 1
                report["days_added"] += added
                STATUS["days_added"] = report["days_added"]
                if progress:
                    progress(index, len(pairs), pair, added)
    except Exception as err:  # noqa: BLE001
        STATUS["error"] = str(err)[:200]
        raise
    finally:
        STATUS.update(running=False, done=STATUS.get("total", 0), current=None)
    return report


def backfill_coinbase(db: Session) -> int:
    """Daily USDT/USD closes from Coinbase (300 days a call), resumable."""
    added = 0
    today = datetime.now(timezone.utc).date()
    with httpx.Client(timeout=30, headers={"User-Agent": "Mandioca-panel/1.0"}) as client:
        for product, pair in COINBASE_PRODUCTS.items():
            last = db.scalar(select(func.max(RateSnapshot.rate_date)).where(
                RateSnapshot.source == COINBASE_SOURCE, RateSnapshot.pair == pair))
            start = (datetime.strptime(last, "%Y-%m-%d").date() + timedelta(days=1)) if last \
                else datetime.strptime(COINBASE_START, "%Y-%m-%d").date()
            while start < today:
                end = min(start + timedelta(days=299), today - timedelta(days=1))
                candles = client.get(
                    f"https://api.exchange.coinbase.com/products/{product}/candles",
                    params={"granularity": 86400, "start": f"{start}T00:00:00Z",
                            "end": f"{end}T23:59:59Z"}).json()
                rows = []
                for stamp, _low, _high, _open, close, _vol in (candles if isinstance(candles, list) else []):
                    day = datetime.fromtimestamp(stamp, timezone.utc).date()
                    if start <= day <= end:
                        rows.append({"source": COINBASE_SOURCE, "pair": pair, "price": float(close),
                                     "rate_date": day.isoformat(), "taken_at": f"{day}T23:59:59"})
                if rows:
                    db.execute(insert(RateSnapshot), rows)
                    db.commit()
                    added += len(rows)
                start = end + timedelta(days=1)
                time.sleep(PAUSE)
    return added


def backfill_trm(db: Session) -> int:
    """Every official TRM since 1991 (source «trm»), only the days not stored yet."""
    known = set(db.scalars(select(RateSnapshot.rate_date).where(
        RateSnapshot.source == "trm", RateSnapshot.pair == "USD/COP")).all())
    with httpx.Client(timeout=60, headers={"User-Agent": "Mandioca-panel/1.0"}) as client:
        records = client.get(TRM_URL, params={"$order": "vigenciadesde ASC",
                                              "$limit": 50000}).json()
    rows = []
    for record in records:
        first = datetime.strptime(record["vigenciadesde"][:10], "%Y-%m-%d").date()
        last = datetime.strptime((record.get("vigenciahasta") or record["vigenciadesde"])[:10],
                                 "%Y-%m-%d").date()
        day = first
        while day <= last:          # a weekend rate covers several days
            if day.isoformat() not in known:
                rows.append({"source": "trm", "pair": "USD/COP", "price": float(record["valor"]),
                             "rate_date": day.isoformat(), "taken_at": f"{day}T00:00:00"})
                known.add(day.isoformat())
            day += timedelta(days=1)
    for start in range(0, len(rows), 5000):
        db.execute(insert(RateSnapshot), rows[start:start + 5000])
        db.commit()
    return len(rows)


if __name__ == "__main__":
    from ..database import Base, SessionLocal, engine

    logging.basicConfig(level=logging.WARNING)
    Base.metadata.create_all(engine)
    started = time.time()

    def show(index, total, pair, added):
        if added or index % 50 == 0 or index == total:
            print(f"[{index}/{total}] {pair}: +{added} días  ({round(time.time() - started)} s)",
                  flush=True)

    with SessionLocal() as session:
        if sys.argv[1:] in (["trm"], ["usdt"]):
            added = backfill_trm(session) if sys.argv[1] == "trm" else backfill_coinbase(session)
            sys.exit(print(f"Listo: {added} días agregados.") or 0)
        result = backfill(session, sys.argv[1:] or None, progress=show)
        result["days_added"] += backfill_coinbase(session) + backfill_trm(session)
    print(f"\nListo: {result['pairs']} pares, {result['days_added']} días agregados, "
          f"{len(result['pairs_failed'])} fallidos en {round(time.time() - started)} s.")
