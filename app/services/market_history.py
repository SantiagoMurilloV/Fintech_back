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
from datetime import datetime, timezone

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
        result = backfill(session, sys.argv[1:] or None, progress=show)
    print(f"\nListo: {result['pairs']} pares, {result['days_added']} días agregados, "
          f"{len(result['pairs_failed'])} fallidos en {round(time.time() - started)} s.")
