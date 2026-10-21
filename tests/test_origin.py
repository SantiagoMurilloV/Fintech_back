"""Every order and expense says where it came from, and can be filtered by it."""
from __future__ import annotations

import io
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Expense, Order, User
from app.services import origin, security


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def auth(db):
    user = User(email="a@test", name="a", role="admin",
                password_hash=security.hash_password("irrelevant-pass"),
                created_at=security.stamp())
    db.add(user)
    db.commit()
    token, _ = security.create_access_token(user)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def mixed(db):
    for i, source in enumerate(["otc", "otc", "quickbooks", "api", None]):
        db.add(Order(id=f"O-{i}", customer="C", amount=10.0 + i, currency="USD",
                     status="approved", date="2026-01-0" + str(i + 1),
                     extra={"source": source} if source else {}))
    db.add(Expense(description="Hosting", category="Tech", amount=50, currency="USD",
                   date="2026-01-02", extra={"source": "quickbooks"}))
    db.commit()


def test_the_rule_is_the_same_in_python_and_sql(db, mixed):
    from sqlalchemy import select
    sql = dict(db.execute(select(Order.id, origin.expression(Order))).all())
    python = {o.id: origin.of(o.extra) for o in db.query(Order)}
    assert sql == python == {"O-0": "otc", "O-1": "otc", "O-2": "quickbooks",
                             "O-3": "api", "O-4": "manual"}


def test_orders_show_and_filter_by_origin(client, auth, mixed):
    body = client.get("/api/orders", headers=auth).json()
    assert {item["origin"] for item in body["items"]} == {"otc", "quickbooks", "api", "manual"}
    facet = next(f for f in body["facets"] if f["key"] == "origin")
    assert {v["value"]: v["count"] for v in facet["values"]}["otc"] == 2
    assert body["origins"]["otc"] == "Excel OTC"

    only_qb = client.get("/api/orders", headers=auth,
                         params={"filters": json.dumps({"origin": ["quickbooks"]})}).json()
    assert [item["id"] for item in only_qb["items"]] == ["O-2"]


def test_expenses_carry_their_origin(client, auth, mixed):
    items = client.get("/api/expenses", headers=auth).json()["items"]
    assert items[0]["origin"] == "quickbooks"


def test_an_excel_upload_is_stamped_as_excel(client, auth, db):
    buffer = io.BytesIO()
    pd.DataFrame([{"customer": "X", "amount": 5, "currency": "USD", "status": "approved",
                   "date": "2026-02-01"}]).to_excel(buffer, index=False)
    client.post("/api/import/excel", headers=auth, data={"kind": "orders"},
                files={"file": ("o.xlsx", buffer.getvalue())})
    assert origin.of(db.query(Order).one().extra) == "excel"
