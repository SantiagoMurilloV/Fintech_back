"""Plan and statement routes: who may change what, and what the screen gets."""
from __future__ import annotations

import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Report, User
from app.services import security, storage


@pytest.fixture
def client(db, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "STORAGE_DIR", tmp_path)   # PDFs land in a temp folder
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _bearer(db, role: str) -> dict:
    user = User(email=f"{role}@test", name=role, role=role,
                password_hash=security.hash_password("irrelevant-pass"),
                created_at=security.stamp())
    db.add(user)
    db.commit()
    token, _ = security.create_access_token(user)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin(db):
    return _bearer(db, "admin")


@pytest.fixture
def member(db):
    return _bearer(db, "miembro")


@pytest.fixture
def loaded(client, admin):
    response = client.post("/api/plan/template", json={"template": "mandioca"}, headers=admin)
    assert response.status_code == 200
    return client


def test_everything_needs_a_session(client):
    for path in ("/api/plan", "/api/plan/templates", "/api/statement", "/api/plan/values"):
        assert client.get(path).status_code == 401


def test_only_an_admin_changes_the_structure(client, member):
    assert client.post("/api/plan/template", json={"template": "mandioca"},
                       headers=member).status_code == 403
    assert client.put("/api/plan", json={"accounts": []}, headers=member).status_code == 403


def test_loading_a_template_twice_asks_to_confirm(loaded, admin):
    again = loaded.post("/api/plan/template", json={"template": "basic"}, headers=admin)
    assert again.status_code == 409
    replaced = loaded.post("/api/plan/template", json={"template": "basic", "replace": True},
                           headers=admin)
    assert replaced.status_code == 200 and replaced.json()["totals"] == 4


def test_a_broken_plan_comes_back_with_every_reason(loaded, admin):
    response = loaded.put("/api/plan", headers=admin, json={"accounts": [
        {"key": "a", "name": "A", "kind": "total", "formula": "ghost"},
        {"key": "a", "name": "B"}]})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["message"] == "El plan tiene errores."
    assert len(detail["problems"]) >= 2


def test_any_member_fills_the_month_and_reads_the_statement(loaded, member):
    saved = loaded.put("/api/plan/values", headers=member, json={"values": [
        {"account_key": "rent", "period": "2026-05", "amount": 9500},
        {"account_key": "salaries", "period": "2026-05", "amount": 12892}]})
    assert saved.status_code == 200 and saved.json()["written"] == 2

    result = loaded.get("/api/statement", params={"period": "2026-05"}, headers=member).json()
    fixed = next(line for line in result["rows"] if line["key"] == "fixed_costs")
    assert fixed["value"] == 22392.0 and fixed["status"] == "partial"
    assert result["periods"] == ["2026-05"]

    grid = loaded.get("/api/plan/values", params={"first": "2026-04", "last": "2026-05"},
                      headers=member).json()
    assert grid["periods"] == ["2026-04", "2026-05"]
    assert grid["values"]["2026-05"]["rent"] == 9500


def test_upload_previews_then_writes(loaded, member):
    buffer = io.BytesIO()
    pd.DataFrame([["Cuenta", "2026-05"], ["Rent", 9500]]).to_excel(
        buffer, header=False, index=False)
    files = {"file": ("cifras.xlsx", buffer.getvalue())}

    preview = loaded.post("/api/plan/values/import", files=files, data={"dry_run": "true"},
                          headers=member).json()
    assert preview["dry_run"] is True and preview["written"] == 1
    assert loaded.get("/api/statement", headers=member).json()["periods"] == []

    done = loaded.post("/api/plan/values/import", files=files, headers=member).json()
    assert done["dry_run"] is False
    assert loaded.get("/api/statement", headers=member).json()["periods"] == ["2026-05"]


def test_template_download_is_a_workbook(loaded, member):
    response = loaded.get("/api/plan/values/template.xlsx", headers=member)
    assert response.status_code == 200
    frame = pd.read_excel(io.BytesIO(response.content))
    assert frame.columns[0] == "Cuenta"


def test_statement_pdf_lands_in_reports(loaded, member, db):
    loaded.put("/api/plan/values", headers=member, json={"values": [
        {"account_key": "rent", "period": "2026-05", "amount": 9500}]})

    response = loaded.post("/api/statement/report", json={"period": "2026-05"}, headers=member)

    assert response.status_code == 201
    report = db.get(Report, response.json()["id"])
    assert report.kind == "statement" and report.period == "2026-05"
    assert report.title == "Estado de resultados · Mayo 2026"


def test_csv_export_has_one_row_per_account(loaded, member):
    response = loaded.get("/api/statement/export.csv", params={"period": "2026-05"},
                          headers=member)
    assert response.status_code == 200
    assert response.text.splitlines()[0].startswith("account,name,level")
    assert len(response.text.splitlines()) == 1 + 31


def test_statement_without_a_plan_explains_what_to_do(client, member):
    response = client.get("/api/statement", headers=member)
    assert response.status_code == 400
    assert "plan de cuentas" in response.json()["detail"]
