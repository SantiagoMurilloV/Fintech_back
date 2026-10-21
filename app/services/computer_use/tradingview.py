"""Spot prices from TradingView symbol pages: recipe first, computer use as fallback.

Three layers, cheapest first, for each symbol of `tv.watchlist`:

    1. Recipe       Playwright opens the saved URL and reads the saved CSS
                    selector. No model, no screenshot; a few seconds.
    2. Check        The number must parse and sit within TOLERANCE of an
                    independent reference (our latest exchange reading, or
                    Coinbase's public ticker). A page that moved its numbers
                    around fails here instead of storing a wrong price.
    3. Computer use When the recipe misses or the check fails, Claude looks
                    at the page as a person would and reads the price. The
                    code then finds which element shows that number and saves
                    it as the new recipe — the next hour is automatic again.

A new symbol starts with the default TradingView recipe (its symbol page and
the headline price element); computer use only steps in if that does not hold.

Readings are stored in `rate_snapshots` with source «tradingview», next to
the exchange readings. TradingView offers no data API and its terms restrict
automated collection, so this is off by default (`tv.enabled`): running it
is the operator's decision. Spot prices only; history comes from exchanges.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import RateSnapshot, ScrapeRecipe
from . import agent
from .browser import Browser, BrowserUnavailable

log = logging.getLogger("tradingview")

URL = "https://www.tradingview.com/symbols/{slug}/"
DEFAULT_SELECTOR = '[data-qa-id="symbol-last-value"]'
SYMBOL = re.compile(r"^[A-Z0-9_]+:[A-Z0-9_.]+$")
MAX_STEPS = 6
TOLERANCE = 0.03            # a reading more than 3 % off the reference is suspect
REFERENCE_MAX_AGE = timedelta(hours=3)
EXCHANGE_SOURCES = ("coinbase", "kraken")

TASK = """You are looking at the TradingView page of the symbol {symbol}.
The screenshot below is the current screen. If the price is visible in it,
answer RIGHT AWAY without taking any action — do not take another
screenshot, do not scroll, do not click.
Only if a cookie banner, sign-in prompt or pop-up covers the price, close it.
This IS the right page: do not search, do not open other pages.
Then read the CURRENT price shown at the top of the page (the large number
next to the symbol name) and the daily change in percent.
Do not sign in and do not buy or change anything.
When you can read it, answer with ONLY this JSON and nothing else:
{{"price": <number>, "change_pct": <number or null>, "currency": "<code>"}}
If the page does not show a price, answer {{"price": null, "reason": "<why>"}}."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


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


def parse_price(text: str) -> float | None:
    """«67,950.12» / «0.99925» / «−1.5» -> float; anything else -> None."""
    cleaned = str(text or "").replace("−", "-").replace(",", "").strip()
    match = re.fullmatch(r"-?\d+(\.\d+)?", cleaned)
    return float(cleaned) if match else None


# ---------------------------------------------------------------- reference

def reference_price(db: Session, pair: str, fetch=None) -> tuple[float | None, str | None]:
    """An independent price for `pair`: our fresh exchange reading, else Coinbase now."""
    since = (datetime.now(timezone.utc) - REFERENCE_MAX_AGE).strftime("%Y-%m-%dT%H:%M:%S")
    row = db.scalars(select(RateSnapshot).where(
        RateSnapshot.pair == pair, RateSnapshot.source.in_(EXCHANGE_SOURCES),
        RateSnapshot.taken_at >= since).order_by(RateSnapshot.taken_at.desc())).first()
    if row is not None:
        return row.price, row.source
    base, _, quote = pair.partition("/")
    if not base or quote not in ("USD", "USDT", "USDC"):
        return None, None
    try:
        price = (fetch or _coinbase_spot)(base)
    except Exception:  # noqa: BLE001 — no reference means «unchecked», not failure
        return None, None
    return (price, "coinbase") if price else (None, None)


def _coinbase_spot(base: str) -> float | None:
    body = httpx.get(f"https://api.exchange.coinbase.com/products/{base}-USD/ticker",
                     timeout=8).json()
    try:
        return float(body["price"])
    except (KeyError, TypeError, ValueError):
        return None


def check(price: float | None, reference: float | None) -> str | None:
    """Why a reading should not be trusted, or None when it holds."""
    if price is None or not 0 < price < 10_000_000:
        return "no es un precio válido"
    if reference and abs(price - reference) / reference > TOLERANCE:
        return f"se aleja {abs(price - reference) / reference:.1%} de la referencia {reference}"
    return None


# ------------------------------------------------------------------ recipes

def recipe_for(db: Session, symbol: str) -> ScrapeRecipe:
    recipe = db.scalars(select(ScrapeRecipe).where(ScrapeRecipe.symbol == symbol)).first()
    if recipe is None:
        recipe = ScrapeRecipe(symbol=symbol, url=URL.format(slug=symbol.replace(":", "-")),
                              selector=DEFAULT_SELECTOR, learned_by="default",
                              learned_at=_now(), failures=0)
        db.add(recipe)
        db.flush()
    return recipe


def recipe_view(recipe: ScrapeRecipe) -> dict:
    return {"symbol": recipe.symbol, "url": recipe.url, "selector": recipe.selector,
            "learned_by": recipe.learned_by, "learned_at": recipe.learned_at,
            "last_ok_at": recipe.last_ok_at, "last_price": recipe.last_price,
            "failures": recipe.failures, "last_error": recipe.last_error}


# -------------------------------------------------------------------- read

def read_symbol(db: Session, browser: Browser, symbol: str, *, reference_fetch=None) -> dict:
    """Recipe, check, and computer use only if needed. Returns a report entry."""
    recipe = recipe_for(db, symbol)
    reference, reference_source = reference_price(db, pair_of(symbol), reference_fetch)
    entry = {"symbol": symbol, "pair": pair_of(symbol), "reference": reference,
             "reference_source": reference_source}

    # 1–2: the saved recipe, checked.
    try:
        browser.goto(recipe.url)
        price = parse_price(browser.read_text(recipe.selector))
        problem = check(price, reference)
    except Exception as err:  # noqa: BLE001 — a missing element is a recipe miss
        price, problem = None, f"la receta falló: {str(err)[:120]}"
    if problem is None:
        recipe.failures, recipe.last_error = 0, None
        recipe.last_ok_at, recipe.last_price = _now(), price
        return {**entry, "ok": True, "mode": "receta", "price": price, "steps": 0}

    # 3: computer use reads the page and re-teaches the recipe.
    recipe.failures = (recipe.failures or 0) + 1
    recipe.last_error = problem[:300]
    if not agent.is_available():
        return {**entry, "ok": False, "mode": "receta", "error": problem,
                "note": "Sin ANTHROPIC_API_KEY no hay computer use de respaldo."}
    try:
        result = agent.run(browser, TASK.format(symbol=symbol), max_steps=MAX_STEPS)
    except Exception as err:  # noqa: BLE001 — say why the fallback could not help
        reason = str(err)
        if "authentication" in reason or "401" in reason:
            reason = "Anthropic rechazó la clave (ANTHROPIC_API_KEY no es válida)."
        return {**entry, "ok": False, "mode": "computer_use", "error": reason[:200],
                "recipe_error": problem}
    answer = result.get("json") or {}
    raw = answer.get("price")
    price = float(raw) if isinstance(raw, (int, float)) else None
    problem_cu = check(price, reference)
    if problem_cu is not None:
        return {**entry, "ok": False, "mode": "computer_use", "steps": result["steps"],
                "actions": result.get("actions"), "error": answer.get("reason") or result.get("error") or problem_cu,
                "recipe_error": problem}

    # Learn on the symbol page itself, even if Claude wandered elsewhere.
    learned = None
    try:
        learned = browser.derive_selector(price)
        if not learned and browser.url != recipe.url:
            browser.goto(recipe.url)
            learned = browser.derive_selector(price)
    except Exception as err:  # noqa: BLE001 — the price still counts; the recipe waits
        log.info("Could not derive a selector for %s: %s", symbol, err)
    if learned:
        recipe.selector = learned
        recipe.learned_by, recipe.learned_at = "computer_use", _now()
        recipe.failures = 0
    recipe.last_ok_at, recipe.last_price = _now(), price
    return {**entry, "ok": True, "mode": "computer_use", "price": price,
            "steps": result["steps"], "actions": result.get("actions"), "recipe_error": problem,
            "recipe_relearned": bool(learned), "selector": learned}


def run(db: Session, watchlist: list[str], *, reference_fetch=None) -> dict:
    """Read every symbol of the watchlist and store the readings that hold."""
    taken_at = _now()
    report = {"taken_at": taken_at, "symbols": []}
    try:
        with Browser() as browser:
            for symbol in watchlist:
                try:
                    reading = read_symbol(db, browser, symbol, reference_fetch=reference_fetch)
                except Exception as err:  # noqa: BLE001 — one page failing never stops the rest
                    log.warning("TradingView %s failed: %s", symbol, err)
                    reading = {"symbol": symbol, "ok": False, "error": str(err)[:200]}
                if reading.get("ok"):
                    db.add(RateSnapshot(source="tradingview", pair=reading["pair"],
                                        price=reading["price"], taken_at=taken_at))
                report["symbols"].append(reading)
    except BrowserUnavailable as err:
        report["error"] = str(err)
        return report
    db.commit()
    modes = [s.get("mode") for s in report["symbols"] if s.get("ok")]
    report["summary"] = {"leidos": len(modes), "por_receta": modes.count("receta"),
                         "por_computer_use": modes.count("computer_use"),
                         "fallidos": len(report["symbols"]) - len(modes)}
    return report


def recipes(db: Session) -> list[dict]:
    return [recipe_view(r) for r in db.scalars(select(ScrapeRecipe).order_by(ScrapeRecipe.symbol))]
