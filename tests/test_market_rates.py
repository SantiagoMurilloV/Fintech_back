"""Market rates: one copy per tick for live prices, one per day for daily rates."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import RateSnapshot, User
from app.services import market_rates, security


def fake_fetchers(trm_day="2026-10-21", fail=()):
    def live(_client):
        return [{"pair": "USDT/USD", "price": 0.999, "bid": 0.9989, "ask": 0.9991}]

    def trm(_client):
        return [{"pair": "USD/COP", "price": 3163.76, "rate_date": trm_day}]

    def broken(_client):
        raise RuntimeError("timeout")

    fetchers = {"kraken": live, "trm": trm}
    for name in fail:
        fetchers[name] = broken
    return fetchers


def test_live_prices_add_a_row_daily_rates_only_a_new_day(db, monkeypatch):
    stamps = iter(["2026-10-21T15:00:00", "2026-10-21T15:05:00", "2026-10-22T15:00:00"])
    monkeypatch.setattr(market_rates, "_now", lambda: next(stamps))
    market_rates.snapshot(db, fake_fetchers())
    market_rates.snapshot(db, fake_fetchers())
    market_rates.snapshot(db, fake_fetchers(trm_day="2026-10-22"))
    pairs = [(r.source, r.pair) for r in db.query(RateSnapshot)]
    assert pairs.count(("kraken", "USDT/USD")) == 3
    assert pairs.count(("trm", "USD/COP")) == 2          # one per published day


def test_one_source_down_never_stops_the_others(db):
    report = market_rates.snapshot(db, fake_fetchers(fail=("kraken",)))
    assert report["sources"]["kraken"]["ok"] is False
    assert report["sources"]["trm"] == {"ok": True, "quotes": 1, "stored": 1}


def test_latest_and_history_routes(db, monkeypatch):
    stamps = iter(["2026-10-21T15:00:00", "2026-10-21T15:05:00"])
    monkeypatch.setattr(market_rates, "_now", lambda: next(stamps))
    market_rates.snapshot(db, fake_fetchers())
    market_rates.snapshot(db, fake_fetchers())
    user = User(email="m@test", name="m", role="miembro",
                password_hash=security.hash_password("irrelevant-pass"),
                created_at=security.stamp())
    db.add(user)
    db.commit()
    token, _ = security.create_access_token(user)
    headers = {"Authorization": f"Bearer {token}"}
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        body = client.get("/api/rates", headers=headers).json()
        assert body["snapshots"] == 3 and body["interval_seconds"] == 300
        assert {(i["source"], i["pair"]) for i in body["items"]} == {
            ("kraken", "USDT/USD"), ("trm", "USD/COP")}
        history = client.get("/api/rates/history", headers=headers,
                             params={"pair": "USDT/USD"}).json()["items"]
        assert [h["taken_at"] for h in history] == ["2026-10-21T15:00:00", "2026-10-21T15:05:00"]
        assert client.post("/api/rates/refresh", headers=headers).status_code == 403
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("name", sorted(market_rates.FETCHERS))
def test_every_fetcher_reads_its_source_shape(name):
    samples = {
        "kraken": {"error": [], "result": {"USDTZUSD": {"c": ["0.999"], "b": ["0.9989"], "a": ["0.9991"]}}},
        "coinbase": {"price": "0.99904", "bid": "0.99904", "ask": "0.99905"},
        "trm": [{"valor": "3163.76", "vigenciadesde": "2026-10-21T00:00:00.000"}],
        "ecb": {"date": "2026-10-21", "rates": {"BRL": 5.0233}},
    }

    class Reply:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    class Client:
        def get(self, *_args, **_kwargs):
            return Reply(samples[name])

    quotes = market_rates.FETCHERS[name](Client())
    assert quotes and all(q["price"] > 0 for q in quotes)
