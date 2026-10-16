"""Orders listing: order the whole selection and total it, not only one page."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Order, User
from app.services import security


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def auth(db):
    user = User(email="member@test", name="member", role="miembro",
                password_hash=security.hash_password("irrelevant-pass"),
                created_at=security.stamp())
    db.add(user)
    db.commit()
    token, _ = security.create_access_token(user)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def orders(db):
    rows = [
        # id, customer, amount, currency, status, date
        ("ORD-1", "beta", 100.0, "USD", "approved", "2026-05-02"),
        ("ORD-2", "Alpha", 4_000_000.0, "COP", "approved", "2026-05-10"),   # 1,000 USD
        ("ORD-3", "gamma", 50.0, "USDT", "pending", "2026-04-28"),
        ("ORD-4", "delta", 9_999.0, "BTC", "approved", "2026-05-20"),       # no rate
        ("ORD-5", "alpha", 300.0, "usd", "rejected", "2026-05-15"),
    ]
    for id_, customer, amount, currency, status, date in rows:
        db.add(Order(id=id_, customer=customer, amount=amount, currency=currency,
                     status=status, date=date, extra={}))
    db.commit()


def _ids(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def test_default_is_newest_first(client, auth, orders):
    assert _ids(client.get("/api/orders", headers=auth)) == [
        "ORD-4", "ORD-5", "ORD-2", "ORD-1", "ORD-3"]


def test_amount_sort_compares_in_usd(client, auth, orders):
    # 4M COP is worth more than 300 USD; BTC has no rate so it counts as zero.
    ids = _ids(client.get("/api/orders", params={"sort": "amount_desc"}, headers=auth))
    assert ids == ["ORD-2", "ORD-5", "ORD-1", "ORD-3", "ORD-4"]
    ids = _ids(client.get("/api/orders", params={"sort": "amount_asc"}, headers=auth))
    assert ids[0] == "ORD-4" and ids[-1] == "ORD-2"


def test_sort_applies_before_paging(client, auth, orders):
    page = client.get("/api/orders", params={"sort": "amount_desc", "limit": 2, "offset": 2},
                      headers=auth)
    assert _ids(page) == ["ORD-1", "ORD-3"]


def test_customer_sort_ignores_case(client, auth, orders):
    ids = _ids(client.get("/api/orders", params={"sort": "customer_asc"}, headers=auth))
    # Both alphas first (newest of them first), then beta, delta, gamma.
    assert ids == ["ORD-5", "ORD-2", "ORD-1", "ORD-4", "ORD-3"]


def test_unknown_sort_is_refused(client, auth, orders):
    response = client.get("/api/orders", params={"sort": "drop table"}, headers=auth)
    assert response.status_code == 400
    assert "sort" in response.json()["detail"]


def test_summary_totals_the_whole_filtered_set(client, auth, orders):
    body = client.get("/api/orders", params={
        "limit": 1, "date_from": "2026-05-01", "date_to": "2026-05-31",
        "filters": json.dumps({"status": ["approved"]})}, headers=auth).json()
    assert body["total"] == 3 and len(body["items"]) == 1
    summary = body["summary"]
    assert summary["count"] == 3
    assert summary["usd"] == pytest.approx(1100.0)       # BTC left out, not guessed
    assert summary["unrated"] == 1
    assert summary["first_date"] == "2026-05-02" and summary["last_date"] == "2026-05-20"
    assert {item["currency"] for item in summary["by_currency"]} == {"USD", "COP", "BTC"}


def test_summary_of_nothing_is_zero(client, auth, orders):
    summary = client.get("/api/orders", params={"q": "nobody-matches-this"},
                         headers=auth).json()["summary"]
    assert summary == {"count": 0, "usd": 0, "unrated": 0, "by_currency": [],
                       "first_date": None, "last_date": None}


def test_export_follows_the_screen_order(client, auth, orders):
    response = client.get("/api/orders/export.csv", params={"sort": "amount_desc"}, headers=auth)
    lines = response.text.splitlines()
    assert lines[1].startswith("ORD-2,")
