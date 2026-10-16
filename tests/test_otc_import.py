"""OTC trade log -> orders: every trade field kept, nothing guessed silently."""
from __future__ import annotations

import io
from datetime import datetime

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import CustomColumn, Order, User
from app.services import security

HEADER = ["Date", "Client", "Base", "Direction", "Wire USD Sent", "USDT", "Client Price",
          "Wire Fee", "Gross Revenue", "Provider", "Mandioca Cost", "Platform Bps",
          "Operating Profit", "Customer company name "]


def _workbook() -> bytes:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "OTC"
    sheet.append([None] * 14 + ["@dropdown"])
    sheet.append(HEADER)
    sheet.append([datetime(2024, 3, 1), " Acme  ", "USDT", "SELL", 1000, 1010, 1.01, 0, 10,
                  "Reap", 1003, 0.003, 7, "Shenzhen Parts Co"])
    sheet.append([datetime(2046, 3, 2), "Acme", "USDT ", "BUY", 500, 495, 1.0101, 0, 5,
                  "Conduit ", 497, 0.006, 2, 12.5])        # year typo; number in column N
    sheet.append([datetime(2024, 3, 3), "Paisas", "USDT", "SELL", 0, 0, None, 0, 0,
                  "Bridge", 0, None, 0, None])             # zero placeholder
    sheet.append([None] * 14)                              # end of the log
    sheet.append(["Total", None, None, "SELL", 999999, 999999])  # footer, not a trade
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _bearer(db, role):
    user = User(email=f"{role}@test", name=role, role=role,
                password_hash=security.hash_password("irrelevant-pass"),
                created_at=security.stamp())
    db.add(user)
    db.commit()
    token, _ = security.create_access_token(user)
    return {"Authorization": f"Bearer {token}"}


def _upload(client, headers, dry_run=False):
    return client.post("/api/orders/import/otc", headers=headers,
                       files={"file": ("OTC 2024.xlsx", _workbook())},
                       data={"dry_run": "true" if dry_run else "false"})


def test_only_an_admin_loads_trades(client, db):
    assert _upload(client, _bearer(db, "miembro")).status_code == 403


def test_preview_reports_and_writes_nothing(client, db):
    report = _upload(client, _bearer(db, "admin"), dry_run=True).json()
    assert report["dry_run"] and report["operaciones"] == 2
    assert report["omitidas"] == 1 and report["fechas_corregidas"] == 1
    assert report["totales"]["utilidad"] == 9.0
    assert db.query(Order).count() == 0


def test_trades_become_orders_with_their_fields(client, db):
    admin = _bearer(db, "admin")
    report = _upload(client, admin).json()
    assert report["nuevas"] == 2

    sell = db.get(Order, "OTC-2024-00003")
    assert (sell.customer, sell.amount, sell.currency, sell.status, sell.gateway) == (
        "ACME", 1000, "USD", "approved", "Reap")
    assert sell.extra["tipo"] == "Off-ramp" and sell.extra["costo_proveedor"] == 3
    assert sell.extra["utilidad"] == 7 and sell.extra["beneficiario"] == "Shenzhen Parts Co"

    buy = db.get(Order, "OTC-2024-00004")
    assert buy.date == "2024-03-02"                      # 2046 belongs to the 2024 log
    assert buy.extra["tipo"] == "On-ramp" and buy.extra["costo_proveedor"] == 3
    assert buy.extra["beneficiario"] is None and buy.gateway == "Conduit"
    assert {c.key for c in db.query(CustomColumn)} >= {"tipo", "utilidad", "costo_proveedor"}

    again = _upload(client, admin).json()                # same file: replaced, not doubled
    assert again["nuevas"] == 0 and again["reemplazadas"] == 2
    assert db.query(Order).count() == 2


def test_a_file_without_the_trade_log_is_explained(client, db):
    book = openpyxl.Workbook()
    book.active.title = "Overview"
    buffer = io.BytesIO()
    book.save(buffer)
    response = client.post("/api/orders/import/otc", headers=_bearer(db, "admin"),
                           files={"file": ("x.xlsx", buffer.getvalue())})
    assert response.status_code == 400 and "OTC" in response.json()["detail"]
