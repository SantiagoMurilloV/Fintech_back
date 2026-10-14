"""QuickBooks connection: consent URL, callback, token storage and refresh."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import jwt
import pytest

from app.config import JWT_ALGORITHM, JWT_ISSUER, JWT_SECRET
from app.models import IntegrationConnection
from app.services import quickbooks


def _state_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


def _row(db) -> IntegrationConnection:
    db.expire_all()
    return quickbooks.connection(db)


# ------------------------------------------------------------ consent URL

def test_authorization_url_carries_what_intuit_requires(credentials):
    url = quickbooks.authorization_url(credentials, actor="admin@test")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == quickbooks.AUTHORIZE_URL
    assert query["client_id"] == ["test-client-id"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["com.intuit.quickbooks.accounting"]
    assert query["redirect_uri"] == ["http://localhost:8000/api/integrations/quickbooks/callback"]

    claims = jwt.decode(query["state"][0], JWT_SECRET, algorithms=[JWT_ALGORITHM],
                        audience="quickbooks-connect", issuer=JWT_ISSUER)
    assert claims["sub"] == "admin@test"
    assert claims["env"] == "sandbox"


def test_authorization_url_refuses_without_app_credentials(db):
    with pytest.raises(quickbooks.QuickBooksError, match="Client ID"):
        quickbooks.authorization_url(db, actor="admin@test")


# --------------------------------------------------------------- callback

def test_callback_stores_the_company_and_encrypts_tokens(credentials, intuit):
    db = credentials
    state = _state_of(quickbooks.authorization_url(db, actor="admin@test"))

    conn = quickbooks.complete_connection(db, code="good-code", realm_id="9130", state=state)

    assert conn.status == "connected"
    assert conn.realm_id == "9130"
    assert conn.home_currency == "USD"
    assert conn.connected_by == "admin@test"
    # At rest the tokens are ciphertext, never the value Intuit handed out.
    assert conn.access_token and "access-1" not in conn.access_token
    assert conn.refresh_token and "refresh-1" not in conn.refresh_token
    assert quickbooks._open(conn.refresh_token) == "refresh-1"
    # The code exchange must repeat the exact redirect_uri of the consent URL.
    exchange = intuit.token_calls[0]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["redirect_uri"] == quickbooks.redirect_uri()


@pytest.mark.xfail(strict=True, reason=(
    "BUG: complete_connection sets company_name, then the preferences request "
    "calls _access_token -> db.refresh(conn), which discards the uncommitted "
    "name. The card shows the realm id instead of the company."))
def test_callback_keeps_the_company_name(credentials, intuit):
    db = credentials
    state = _state_of(quickbooks.authorization_url(db, actor="admin@test"))
    quickbooks.complete_connection(db, code="good-code", realm_id="9130", state=state)
    assert _row(db).company_name == "Sandbox Company_US_1"


def test_callback_rejects_a_state_this_panel_did_not_sign(credentials, intuit):
    forged = jwt.encode({"iss": JWT_ISSUER, "aud": "quickbooks-connect", "sub": "x",
                         "exp": 9_999_999_999}, "another-secret", algorithm=JWT_ALGORITHM)
    with pytest.raises(quickbooks.QuickBooksError, match="no corresponde"):
        quickbooks.complete_connection(credentials, code="good-code", realm_id="1", state=forged)
    assert intuit.token_calls == []   # nothing was exchanged


def test_callback_rejects_a_panel_access_token_as_state(credentials, intuit):
    """A session JWT is signed with the same secret but has no `aud`."""
    session_like = jwt.encode({"iss": JWT_ISSUER, "sub": "1", "typ": "access",
                               "exp": 9_999_999_999}, JWT_SECRET, algorithm=JWT_ALGORITHM)
    with pytest.raises(quickbooks.QuickBooksError):
        quickbooks.complete_connection(credentials, code="good-code", realm_id="1",
                                       state=session_like)


def test_callback_rejects_an_expired_state(credentials, intuit):
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    stale = jwt.encode({"iss": JWT_ISSUER, "aud": "quickbooks-connect", "sub": "admin@test",
                        "exp": int(past.timestamp())}, JWT_SECRET, algorithm=JWT_ALGORITHM)
    with pytest.raises(quickbooks.QuickBooksError, match="tardó demasiado"):
        quickbooks.complete_connection(credentials, code="good-code", realm_id="1", state=stale)


def test_callback_reports_a_code_intuit_refuses(credentials, intuit):
    state = _state_of(quickbooks.authorization_url(credentials, actor="admin@test"))
    with pytest.raises(quickbooks.QuickBooksError, match="invalid_grant"):
        quickbooks.complete_connection(credentials, code="used-code", realm_id="1", state=state)
    assert quickbooks.connection(credentials) is None


@pytest.mark.xfail(strict=True, reason=(
    "BUG: complete_connection overwrites realm_id before comparing it, so a "
    "different company keeps the previous company's incremental cursor."))
def test_connecting_a_different_company_restarts_the_incremental_history(connected, intuit):
    db = connected
    conn = _row(db)
    conn.cursor = "2026-09-01T00:00:00+00:00"
    db.commit()

    state = _state_of(quickbooks.authorization_url(db, actor="admin@test"))
    quickbooks.complete_connection(db, code="good-code", realm_id="OTHER-COMPANY", state=state)

    assert _row(db).cursor is None


# ------------------------------------------------------------ status view

def test_status_never_exposes_tokens(connected):
    view = quickbooks.status(connected)
    assert view["status"] == "connected"
    assert view["configured"] is True
    assert view["realm_id"] == "9130"
    flat = repr(view)
    assert "access_token" not in flat and "refresh_token" not in flat
    assert "access-1" not in flat and "refresh-1" not in flat
    assert "test-client-secret" not in flat


def test_status_before_connecting(credentials):
    view = quickbooks.status(credentials)
    assert view["status"] == "disconnected"
    assert view["redirect_uri"].endswith("/api/integrations/quickbooks/callback")


# ---------------------------------------------------------------- refresh

def _expire_access_token(db) -> None:
    conn = _row(db)
    conn.access_expires_at = quickbooks._utc_stamp(datetime.now(timezone.utc) - timedelta(seconds=1))
    db.commit()


def test_a_fresh_access_token_is_reused_without_calling_intuit(connected, intuit):
    calls = len(intuit.token_calls)
    token = quickbooks._access_token(connected, _row(connected))
    assert token == "access-1"
    assert len(intuit.token_calls) == calls


def test_an_expiring_token_is_refreshed_and_the_rotated_pair_persisted(connected, intuit):
    db = connected
    _expire_access_token(db)

    token = quickbooks._access_token(db, _row(db))

    assert token == "access-2"
    refresh_call = intuit.token_calls[-1]
    assert refresh_call == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}
    # The NEW refresh token is what is stored: the old one is dead at Intuit.
    assert quickbooks._open(_row(db).refresh_token) == "refresh-2"

    _expire_access_token(db)
    assert quickbooks._access_token(db, _row(db)) == "access-3"   # the chain keeps working


def test_a_refused_refresh_token_asks_a_person_to_reconnect(connected, intuit):
    db = connected
    _expire_access_token(db)
    intuit.refresh_fails_with = "invalid_grant"

    with pytest.raises(quickbooks.NotConnected, match="reconectar"):
        quickbooks._access_token(db, _row(db))

    assert _row(db).status == "needs_reconnect"
    assert quickbooks.is_connected(db) is False


def test_tokens_unreadable_after_a_secret_rotation_ask_to_reconnect(connected, intuit, monkeypatch):
    db = connected
    _expire_access_token(db)
    monkeypatch.setattr(quickbooks, "JWT_SECRET", "rotated-secret")

    with pytest.raises(quickbooks.NotConnected):
        quickbooks._access_token(db, _row(db))
    assert _row(db).status == "needs_reconnect"


def test_a_401_from_the_api_marks_the_connection_for_reconnect(connected, intuit):
    intuit.api_status = 401
    with pytest.raises(quickbooks.NotConnected):
        quickbooks.pull(connected)
    assert _row(connected).status == "needs_reconnect"


def test_a_429_is_reported_and_leaves_the_connection_alone(connected, intuit):
    intuit.api_status = 429
    with pytest.raises(quickbooks.QuickBooksError, match="429"):
        quickbooks.pull(connected)
    conn = _row(connected)
    assert conn.status == "connected"
    assert "429" in conn.last_error


# ------------------------------------------------------------- disconnect

def test_disconnect_revokes_at_intuit_and_forgets_the_connection(connected, intuit):
    result = quickbooks.disconnect(connected)
    assert result == {"disconnected": True, "revoked_at_intuit": True}
    assert intuit.revoked == ["refresh-1"]
    assert quickbooks.connection(connected) is None


def test_pull_without_a_connection_says_so(credentials):
    with pytest.raises(quickbooks.NotConnected):
        quickbooks.pull(credentials)
