"""Prices move between databases as a file; importing twice adds nothing."""
from __future__ import annotations

import io

import pytest

from app.models import RateSnapshot
from app.services import rates_transfer


def _seed(db):
    db.add_all([
        RateSnapshot(source="binance_1d", pair="BTC/USDT", price=69499.85,
                     rate_date="2024-03-15", taken_at="2024-03-15T23:59:59"),
        RateSnapshot(source="kraken", pair="USDT/USD", price=0.999, bid=0.9989, ask=0.9991,
                     taken_at="2026-10-21T19:16:59"),
    ])
    db.commit()


def test_export_then_import_is_identical_and_idempotent(db):
    _seed(db)
    buffer = io.BytesIO()
    assert rates_transfer.export(db, buffer) == 2
    content = buffer.getvalue()
    db.query(RateSnapshot).delete()
    db.commit()

    first = rates_transfer.import_file(db, content)
    assert first == {"read": 2, "added": 2, "skipped": 0, "total": 2}
    again = rates_transfer.import_file(db, content)
    assert again["added"] == 0 and again["skipped"] == 2 and again["total"] == 2
    kraken = db.query(RateSnapshot).filter_by(source="kraken").one()
    assert (kraken.bid, kraken.ask, kraken.rate_date) == (0.9989, 0.9991, None)


def test_a_wrong_file_is_refused(db):
    with pytest.raises(ValueError):
        rates_transfer.import_file(db, b"not a gzip")
