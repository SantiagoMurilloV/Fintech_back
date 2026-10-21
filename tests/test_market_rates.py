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
        assert body["snapshots"] == 3 and body["interval_seconds"] == 28800
        assert {(i["source"], i["pair"]) for i in body["items"]} == {
            ("kraken", "USDT/USD"), ("trm", "USD/COP")}
        history = client.get("/api/rates/history", headers=headers,
                             params={"pair": "USDT/USD"}).json()["items"]
        assert [h["taken_at"] for h in history] == ["2026-10-21T15:00:00", "2026-10-21T15:05:00"]
        assert client.post("/api/rates/refresh", headers=headers).status_code == 403
    finally:
        app.dependency_overrides.clear()


class Reply:
    def __init__(self, body):
        self.body, self.status_code, self.headers = body, 200, {}

    def json(self):
        return self.body

    def raise_for_status(self):
        return None


class FakeHTTP:
    """Answers by URL fragment, like the real APIs would."""

    def __init__(self, routes):
        self.routes = routes

    def get(self, url, **_kwargs):
        for fragment, body in self.routes.items():
            if fragment in url:
                return Reply(body(url, _kwargs) if callable(body) else body)
        raise AssertionError(f"unexpected call {url}")


SAMPLES = {
    "kraken": {
        "AssetPairs": {"error": [], "result": {
            "XXBTZUSD": {"wsname": "XBT/USD"}, "USDTZUSD": {"wsname": "USDT/USD"},
            "XXBTZEUR": {"wsname": "XBT/EUR"}}},
        "Ticker": {"error": [], "result": {
            "XXBTZUSD": {"c": ["84500.1"], "b": ["84500"], "a": ["84501"]},
            "USDTZUSD": {"c": ["0.999"], "b": ["0.9989"], "a": ["0.9991"]},
            "XXBTZEUR": {"c": ["78000"], "b": ["1"], "a": ["1"]}}},
    },
    "binance": {
        "exchangeInfo": {"symbols": [
            {"symbol": "SOLUSDT", "baseAsset": "SOL", "quoteAsset": "USDT"},
            {"symbol": "SOLTRY", "baseAsset": "SOL", "quoteAsset": "TRY"}]},
        "ticker/price": [{"symbol": "SOLUSDT", "price": "152.3"},
                         {"symbol": "SOLTRY", "price": "6000"}],
    },
    "coinbase": {"ticker": {"price": "0.99904", "bid": "0.99904", "ask": "0.99905"}},
    "trm": {"datos.gov.co": [{"valor": "3163.76", "vigenciadesde": "2026-10-21T00:00:00.000"}]},
    "ecb": {"frankfurter": {"date": "2026-10-21", "rates": {"BRL": 5.0233, "JPY": 151.2}}},
}


@pytest.mark.parametrize("name", sorted(market_rates.FETCHERS))
def test_every_fetcher_reads_its_source_shape(name):
    quotes = market_rates.FETCHERS[name](FakeHTTP(SAMPLES[name]))
    assert quotes and all(q["price"] > 0 for q in quotes)


def test_only_dollar_quotes_are_kept_and_kraken_names_are_normal():
    kraken = {q["pair"] for q in market_rates.fetch_kraken(FakeHTTP(SAMPLES["kraken"]))}
    assert kraken == {"BTC/USD", "USDT/USD"}
    binance = [q["pair"] for q in market_rates.fetch_binance(FakeHTTP(SAMPLES["binance"]))]
    assert binance == ["SOL/USDT"]


def test_a_restart_does_not_add_a_round(db, monkeypatch):
    assert market_rates.seconds_since_last(db, ("kraken",)) is None
    db.add(RateSnapshot(source="kraken", pair="USDT/USD", price=1.0,
                        taken_at=market_rates._now()))
    db.commit()
    assert market_rates.seconds_since_last(db, ("kraken",)) < 60


def test_history_backfill_adds_closed_days_and_resumes(db, monkeypatch):
    from app.services import market_history
    day = market_history.DAY_MS
    calls = []

    def klines(url, kwargs):
        calls.append(kwargs["params"]["startTime"])
        start = kwargs["params"]["startTime"]
        base = start or day * 19000          # 2022-01-08
        return [[base + i * day, "0", "0", "0", str(100 + i), "0", base + i * day + day - 1]
                for i in range(3)]

    routes = {"exchangeInfo": {"symbols": [{"symbol": "SOLUSDT", "baseAsset": "SOL",
                                            "quoteAsset": "USDT"}]},
              "klines": klines}
    monkeypatch.setattr(market_history, "PAUSE", 0)
    monkeypatch.setattr(market_history.httpx, "Client", lambda **_k: _ctx(FakeHTTP(routes)))
    first = market_history.backfill(db)
    assert first["days_added"] == 3
    rows = db.query(RateSnapshot).filter_by(source="binance_1d").order_by(RateSnapshot.rate_date).all()
    assert [r.price for r in rows] == [100.0, 101.0, 102.0] and rows[0].pair == "SOL/USDT"
    market_history.backfill(db)
    assert calls[-1] == day * 19000 + 3 * day          # resumed after the last stored day


class _ctx:
    def __init__(self, inner):
        self.inner = inner

    def __enter__(self):
        return self.inner

    def __exit__(self, *_):
        return None
