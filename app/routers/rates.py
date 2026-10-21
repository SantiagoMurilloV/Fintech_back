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
