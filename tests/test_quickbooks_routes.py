"""QuickBooks HTTP routes: who may call them and where the callback lands."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import User
from app.services import quickbooks, security


@pytest.fixture
def client(credentials):
    db = credentials
    app.dependency_overrides[get_db] = lambda: db
    # No `with`: the lifespan (schema on PostgreSQL, background sync) stays off.
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


def _landing(response) -> tuple[str, dict]:
    assert response.status_code == 303
    target = urlparse(response.headers["location"])
    return f"{target.scheme}://{target.netloc}{target.path}#{target.fragment}", parse_qs(target.query)


def test_connect_is_admin_only(client, credentials):
    assert client.post("/api/integrations/quickbooks/connect").status_code == 401
    member = _bearer(credentials, "miembro")
    assert client.post("/api/integrations/quickbooks/connect", headers=member).status_code == 403


def test_connect_returns_intuits_url_to_an_admin(client, credentials):
    response = client.post("/api/integrations/quickbooks/connect",
                           headers=_bearer(credentials, "admin"))
    assert response.status_code == 200
    assert response.json()["url"].startswith(quickbooks.AUTHORIZE_URL)


def test_sync_and_disconnect_are_admin_only(client, credentials):
    member = _bearer(credentials, "miembro")
    for path in ("sync", "disconnect"):
        assert client.post(f"/api/integrations/quickbooks/{path}", headers=member).status_code == 403


def test_any_signed_in_user_sees_the_status(client, credentials):
    response = client.get("/api/integrations/quickbooks", headers=_bearer(credentials, "miembro"))
    assert response.status_code == 200
    assert response.json()["status"] == "disconnected"


def test_sync_before_connecting_is_a_conflict(client, credentials):
    response = client.post("/api/integrations/quickbooks/sync", headers=_bearer(credentials, "admin"))
    assert response.status_code == 409


def test_callback_lands_on_settings_connected(client, credentials, intuit):
    state = parse_qs(urlparse(quickbooks.authorization_url(credentials, "admin@test")).query)["state"][0]

    response = client.get("/api/integrations/quickbooks/callback",
                          params={"code": "good-code", "realmId": "9130", "state": state},
                          follow_redirects=False)

    where, marks = _landing(response)
    assert where == "http://localhost:3000/#/settings"
    assert marks["quickbooks"] == ["connected"]
    assert quickbooks.is_connected(credentials)


def test_callback_when_the_person_cancels_at_intuit(client):
    response = client.get("/api/integrations/quickbooks/callback",
                          params={"error": "access_denied"}, follow_redirects=False)
    _, marks = _landing(response)
    assert marks["quickbooks"] == ["cancelled"]


def test_callback_with_a_bad_state_never_connects(client, credentials, intuit):
    response = client.get("/api/integrations/quickbooks/callback",
                          params={"code": "good-code", "realmId": "1", "state": "junk"},
                          follow_redirects=False)
    _, marks = _landing(response)
    assert marks["quickbooks"] == ["error"]
    assert quickbooks.connection(credentials) is None
    assert intuit.token_calls == []
