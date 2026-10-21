"""Spot prices read from TradingView symbol pages with computer use.

For each symbol of the watchlist (`tv.watchlist`, e.g. COINBASE:USDTUSD) the
code opens its page directly — typing a URL is not worth a model call — and
then hands the screen to Claude, which may dismiss a cookie banner or a
sign-up pop-up and reads the price the page shows. The answer is checked
(a number, inside a sane range) and stored in `rate_snapshots` with source
«tradingview», next to the exchange readings, so both can be compared.

This reads what a person would read on screen, nothing more: TradingView
offers no data API, and its terms restrict automated collection — running
this is the operator's decision, switched off by default (`tv.enabled`).
History does not come from here: it comes from the exchanges.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ...models import RateSnapshot
from . import agent
from .browser import Browser, BrowserUnavailable

log = logging.getLogger("tradingview")

BASE_URL = "https://www.tradingview.com/symbols/{slug}/"
SYMBOL = re.compile(r"^[A-Z0-9_]+:[A-Z0-9_.]+$")
MAX_STEPS = 6

TASK = """You are looking at the TradingView page of the symbol {symbol}.
If a cookie banner, sign-in prompt or pop-up covers the page, close it.
Then read the CURRENT price shown at the top of the page (the large number
next to the symbol name) and the daily change in percent.
Do not open other pages, do not sign in, do not type anything.
When you can read it, answer with ONLY this JSON and nothing else:
{{"price": <number>, "change_pct": <number or null>, "currency": "<code>"}}
If the page does not show a price, answer {{"price": null, "reason": "<why>"}}."""


def parse_watchlist(raw: str) -> list[str]:
    """«COINBASE:USDTUSD, BINANCE:BTCUSDT» -> valid symbols, upper-cased."""
    symbols = []
    for part in str(raw or "").split(","):
        symbol = part.strip().upper()
        if symbol and SYMBOL.match(symbol) and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def pair_of(symbol: str) -> str:
    """COINBASE:USDTUSD -> USDT/USD; BINANCE:BTCUSDT -> BTC/USDT (best effort)."""
    ticker = symbol.split(":", 1)[1]
    for quote in ("USDT", "USDC", "USD", "EUR", "BRL", "COP", "MXN", "BTC"):
        if ticker.endswith(quote) and len(ticker) > len(quote):
            return f"{ticker[:-len(quote)]}/{quote}"
    return ticker


def read_symbol(browser: Browser, symbol: str) -> dict:
    """Open one symbol page and let Claude read it; returns the parsed answer."""
    browser.goto(BASE_URL.format(slug=symbol.replace(":", "-")))
    result = agent.run(browser, TASK.format(symbol=symbol), max_steps=MAX_STEPS)
    answer = result.get("json") or {}
    price = answer.get("price")
    if not isinstance(price, (int, float)) or not 0 < price < 10_000_000:
        return {"symbol": symbol, "ok": False, "steps": result["steps"],
                "error": answer.get("reason") or result.get("error")
                or "no pude leer un precio válido"}
    return {"symbol": symbol, "ok": True, "steps": result["steps"], "price": float(price),
            "change_pct": answer.get("change_pct"), "currency": answer.get("currency")}


def run(db: Session, watchlist: list[str]) -> dict:
    """Read every symbol and store the prices that came back valid."""
    taken_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    report = {"taken_at": taken_at, "symbols": []}
    if not agent.is_available():
        report["error"] = "Falta ANTHROPIC_API_KEY en el backend."
        return report
    try:
        with Browser() as browser:
            for symbol in watchlist:
                try:
                    reading = read_symbol(browser, symbol)
                except Exception as err:  # noqa: BLE001 — one page failing never stops the rest
                    log.warning("TradingView %s failed: %s", symbol, err)
                    reading = {"symbol": symbol, "ok": False, "error": str(err)[:200]}
                if reading["ok"]:
                    db.add(RateSnapshot(source="tradingview", pair=pair_of(symbol),
                                        price=reading["price"], taken_at=taken_at))
                report["symbols"].append(reading)
    except BrowserUnavailable as err:
        report["error"] = str(err)
        return report
    db.commit()
    return report
