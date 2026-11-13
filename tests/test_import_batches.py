"""Import history: preview writes nothing, confirm loads, reconcile proves it."""
from __future__ import annotations

import io
from datetime import datetime

import openpyxl
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import ImportBatch, Order, User
from app.services import security


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


def _otc(rows) -> bytes:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Sheet1"
    sheet.append(["Date", "Month", "Client", "Base", "Direction", "Wire USD Sent", "USDT",
                  "Client Price", "Wire Fee", "Gross Revenue", "Provider", "Mandioca Cost",
                  "Bps", "Operating Profit"])
    for day, client, usd in rows:
        sheet.append([datetime(2026, 1 if day < 32 else 2, (day - 1) % 31 + 1), "x", client,
                      "USDT", "SELL", usd, usd + 3, 1.003, 0, 3.0, "Mesta", usd + 1, 0.001, 2.0])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _preview(client, headers, content, kind="orders", name="otc 2026.xlsx"):
    return client.post("/api/imports/preview", headers=headers, data={"kind": kind},
                       files={"file": (name, content)})


def test_preview_checks_the_file_and_writes_nothing(client, db):
    admin = _bearer(db, "admin")
    reply = _preview(client, admin, _otc([(2, "Wind", 1000), (2, "Wind", 1000), (33, "Acme", 500)]))
    assert reply.status_code == 201
    batch = reply.json()
    assert batch["status"] == "preview" and batch["label"] == "Libro OTC 2026"
    assert batch["rows_read"] == 3 and batch["rows_valid"] == 3
    assert batch["totals"]["2026-01"] == {"rows": 2, "volume": 2000.0, "spread": 6.0,
                                          "cost": 2.0, "profit": 4.0}
    assert batch["exception_counts"]["posibles_duplicados"] == 1
    assert db.query(Order).count() == 0


def test_confirm_loads_and_the_reconciliation_holds(client, db):
    admin = _bearer(db, "admin")
    batch_id = _preview(client, admin, _otc([(2, "Wind", 1000), (33, "Acme", 500)])).json()["id"]
    done = client.post(f"/api/imports/{batch_id}/confirm", headers=admin).json()
    assert done["status"] == "applied" and done["rows_loaded"] == 2
    rec = done["reconciliation"]
    assert rec["ok"] is True and rec["rows_file"] == rec["rows_panel"] == 2
    assert all(m[k]["ok"] for m in rec["months"] for k in rec["metrics"])
    order = db.get(Order, "OTC-2026-00002")
    assert order.extra["import_batch"] == batch_id and order.extra["huella"]
    assert db.get(ImportBatch, batch_id).content is None           # the file is not kept


def test_an_edit_or_a_deletion_after_import_shows_up(client, db):
    admin = _bearer(db, "admin")
    batch_id = _preview(client, admin, _otc([(2, "Wind", 1000), (3, "Acme", 500)])).json()["id"]
    client.post(f"/api/imports/{batch_id}/confirm", headers=admin)
    order = db.get(Order, "OTC-2026-00002")
    order.amount = 999.0
    db.delete(db.get(Order, "OTC-2026-00003"))
    db.commit()
    rec = client.get(f"/api/imports/{batch_id}", headers=admin).json()["reconciliation"]
    assert rec["ok"] is False and rec["missing"] == 1
    assert [c["id"] for c in rec["changed"]] == ["OTC-2026-00002"]
    january = rec["months"][0]
    assert january["rows"] == {"file": 2, "panel": 1, "ok": False}


def test_reloading_the_year_supersedes_the_earlier_import(client, db):
    admin = _bearer(db, "admin")
    first = _preview(client, admin, _otc([(2, "Wind", 1000)])).json()["id"]
    client.post(f"/api/imports/{first}/confirm", headers=admin)
    second = _preview(client, admin, _otc([(2, "Wind", 1000), (4, "New", 10)])).json()["id"]
    client.post(f"/api/imports/{second}/confirm", headers=admin)
    history = client.get("/api/imports", headers=admin).json()["items"]
    states = {item["id"]: item["status"] for item in history}
    assert states == {second: "applied", first: "superseded"}
    assert next(i for i in history if i["id"] == second)["ok"] is True


def test_a_plain_excel_records_its_rejected_rows(client, db):
    member = _bearer(db, "miembro")
    buffer = io.BytesIO()
    pd.DataFrame([{"cliente": "A", "monto": 100, "moneda": "USD", "estado": "aprobada",
                   "fecha": "2026-03-01"},
                  {"cliente": "B", "monto": 0, "moneda": "USD", "estado": "aprobada",
                   "fecha": "2026-03-02"}]).to_excel(buffer, index=False)
    batch = _preview(client, member, buffer.getvalue(), name="ordenes.xlsx").json()
    assert batch["kind"] == "orders" and batch["rows_rejected"] == 1
    assert batch["exceptions"]["rechazadas"][0]["fila"] == 3
    done = client.post(f"/api/imports/{batch['id']}/confirm", headers=member).json()
    assert done["reconciliation"]["ok"] is True and done["rows_loaded"] == 1


def test_discard_and_exceptions_workbook(client, db):
    admin = _bearer(db, "admin")
    batch_id = _preview(client, admin, _otc([(2, "Wind", 1000), (2, "Wind", 1000)])).json()["id"]
    xlsx = client.get(f"/api/imports/{batch_id}/exceptions.xlsx", headers=admin)
    sheets = pd.read_excel(io.BytesIO(xlsx.content), sheet_name=None)
    assert {"Resumen", "Conciliación", "Rechazadas", "Posibles duplicados"} <= set(sheets)
    assert client.post(f"/api/imports/{batch_id}/discard", headers=admin).json()["status"] == "discarded"
    assert client.get("/api/imports", headers=admin).json()["items"] == []


def test_a_member_cannot_preview_an_otc_log(client, db):
    reply = _preview(client, _bearer(db, "miembro"), _otc([(2, "Wind", 1000)]))
    assert reply.status_code == 403


def test_a_zero_cost_is_not_read_as_an_edit(client, db):
    """Cost equal to the amount gives −0.0; the database keeps 0.0."""
    from app.services import fingerprint
    assert fingerprint.of(-0.0) == fingerprint.of(0.0)
    admin = _bearer(db, "admin")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["Date", "Client", "Base", "Direction", "Wire USD Sent", "USDT", "Client Price",
                  "Wire Fee", "Gross Revenue", "Provider", "Mandioca Cost", "Bps", "Operating Profit"])
    sheet.append([datetime(2026, 3, 1), "Zero", "USDT", "SELL", 1000.0, 1003.0, 1.003, 0, 3.0,
                  "HiFi", 1000.0, 0, 3.0])                       # cost − usd = −0.0
    buffer = io.BytesIO()
    book.save(buffer)
    batch_id = _preview(client, admin, buffer.getvalue()).json()["id"]
    rec = client.post(f"/api/imports/{batch_id}/confirm", headers=admin).json()["reconciliation"]
    assert rec["ok"] is True and rec["changed"] == []
