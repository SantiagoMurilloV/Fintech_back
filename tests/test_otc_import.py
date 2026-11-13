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


def _single_tab_workbook(rows) -> bytes:
    """Like the 2026 export: one tab called «Sheet1», header in row 1."""
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Sheet1"
    sheet.append(["Date", "Month", "Client", "Base", "Direction", "Wire USD Sent", "USDT",
                  "Client Price", "Wire Fee", "Gross Revenue", "Provider", "Mandioca Cost",
                  "Bps", "Operating Profit"])
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _trade(day, client, usd):
    return [datetime(2026, 1, day), "Jan", client, "USDT", "SELL", usd, usd * 1.003, 1.003, 0,
            usd * 0.003, "Mesta", usd * 1.001, 0.001, usd * 0.002]


def test_the_import_button_recognises_an_otc_log_in_any_tab(client, db):
    admin = _bearer(db, "admin")
    content = _single_tab_workbook([_trade(2, "Wind", 1000), _trade(3, "Acme", 2000)])
    reply = client.post("/api/import/excel", headers=admin, data={"kind": "orders"},
                        files={"file": ("otc 2026.xlsx", content)}).json()
    assert reply["inserted"] == 2 and "Libro OTC 2026" in reply["message"]
    order = db.get(Order, "OTC-2026-00002")
    assert order.extra["tipo"] == "Off-ramp" and order.gateway == "Mesta"


def test_a_member_cannot_load_an_otc_log_through_the_button(client, db):
    content = _single_tab_workbook([_trade(2, "Wind", 1000)])
    reply = client.post("/api/import/excel", headers=_bearer(db, "miembro"),
                        data={"kind": "orders"}, files={"file": ("otc.xlsx", content)})
    assert reply.status_code == 403


def test_a_living_log_reloaded_replaces_the_year_as_a_whole(client, db):
    admin = _bearer(db, "admin")
    first = _single_tab_workbook([_trade(2, "Wind", 1000), _trade(3, "Acme", 2000)])
    client.post("/api/import/excel", headers=admin, data={"kind": "orders"},
                files={"file": ("otc.xlsx", first)})
    # A month later: a trade inserted in the middle shifts the rows below it.
    later = _single_tab_workbook([_trade(2, "Wind", 1000), _trade(2, "New", 500),
                                  _trade(3, "Acme", 2000)])
    reply = client.post("/api/import/excel", headers=admin, data={"kind": "orders"},
                        files={"file": ("otc.xlsx", later)}).json()
    assert reply["inserted"] == 3
    customers = sorted(o.customer for o in db.query(Order).filter(Order.id.like("OTC-2026-%")))
    assert customers == ["ACME", "NEW", "WIND"]          # nothing doubled, nothing lost
