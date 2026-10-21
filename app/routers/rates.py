"""Market rates the backend keeps a copy of (see services/market_rates.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import RateSnapshot
from ..services import market_rates
from ..services import settings as settings_service
from .auth import AdminDep, AuthDep

router = APIRouter(tags=["rates"], dependencies=[AuthDep])


@router.get("/api/rates")
def latest_rates(db: Session = Depends(get_db)):
    """The newest reading of every pair from every source, plus the schedule."""
    values = settings_service.all_values(db)
    return {
        "items": market_rates.latest(db),
        "snapshots": db.scalar(select(func.count(RateSnapshot.id))) or 0,
        "last_taken_at": db.scalar(select(func.max(RateSnapshot.taken_at))),
        "enabled": values.get("rates.enabled"),
        "interval_seconds": values.get("rates.interval_seconds"),
    }


@router.get("/api/rates/history")
def rate_history(pair: str, source: str | None = None, since: str | None = None,
                 until: str | None = None, limit: int = 2000,
                 db: Session = Depends(get_db)):
    """Readings of one pair over time, oldest first (`since`/`until` in UTC ISO)."""
    return {"pair": pair, "items": market_rates.history(db, pair, source, since, until, limit)}


@router.post("/api/rates/refresh")
def refresh_rates(db: Session = Depends(get_db), _=AdminDep):
    """Take a snapshot now instead of waiting for the next tick."""
    return market_rates.snapshot(db)


@router.post("/api/rates/tradingview")
def read_tradingview(db: Session = Depends(get_db), _=AdminDep):
    """Read the TradingView watchlist with computer use now (admin)."""
    from ..services.computer_use import tradingview

    symbols = tradingview.parse_watchlist(settings_service.get(db, "tv.watchlist"))
    return tradingview.run(db, symbols)


@router.get("/api/rates/tradingview/recipes")
def tradingview_recipes(db: Session = Depends(get_db)):
    """How each watchlist symbol is read today: selector, who learned it, health."""
    from ..services.computer_use import tradingview

    return {"items": tradingview.recipes(db)}


@router.post("/api/rates/backfill")
def start_backfill(_=AdminDep):
    """Fill the daily price history (every Binance coin since listing) in the background."""
    import threading

    from ..database import SessionLocal
    from ..services import market_history

    if market_history.STATUS.get("running"):
        return {"started": False, "status": market_history.STATUS}

    def work():
        with SessionLocal() as db:
            try:
                market_history.backfill(db)
            except Exception:  # noqa: BLE001 — the status says it failed
                pass

    threading.Thread(target=work, daemon=True, name="price-backfill").start()
    return {"started": True}


@router.get("/api/rates/backfill")
def backfill_status():
    from ..services import market_history

    return market_history.STATUS
