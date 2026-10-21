"""Test setup: an in-memory database and a fake Intuit.

The environment is fixed BEFORE any `app` module is imported, because
app/config.py reads it at import time: SQLite instead of PostgreSQL, a known
JWT secret, and no LLM or Cloudinary, so nothing leaves the machine.
"""
from __future__ import annotations

import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET"] = "test-secret-" + "x" * 40
os.environ["SEED_DEMO"] = "false"
# Set, not removed: app/config.py loads the developer's .env afterwards, and
# load_dotenv only fills variables that are absent. Empty reads as "not set".
os.environ["PUBLIC_BASE_URL"] = "http://localhost:8000"
os.environ["CORS_ORIGINS"] = "http://localhost:3000"
for _key in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "CLOUDINARY_URL", "QUICKBOOKS_CLIENT_ID",
             "QUICKBOOKS_CLIENT_SECRET", "QUICKBOOKS_REDIRECT_URI", "FRONTEND_URL",
             "EXTERNAL_API_URL", "EXTERNAL_API_KEY", "ADMIN_PASSWORD"):
    os.environ[_key] = ""

import re  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from urllib.parse import parse_qs  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import models  # noqa: E402, F401 — registers every table on Base
from app.database import Base  # noqa: E402
from app.services import quickbooks  # noqa: E402
from app.services import settings as settings_service  # noqa: E402


@pytest.fixture
def db():
    # One shared connection: an in-memory SQLite database lives per connection.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def credentials(db):
    """Mandioca's app keys, as an admin would save them in Configuración."""
    settings_service.update(db, {
        "quickbooks.client_id": "test-client-id",
        "quickbooks.client_secret": "test-client-secret",
        "quickbooks.environment": "sandbox",
    })
    return db


def utc(minutes: int = 0) -> str:
    """An instant relative to now, in the shape Intuit writes MetaData times."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(
        microsecond=0).isoformat()


class FakeIntuit:
    """Stands in for Intuit's OAuth and accounting endpoints.

    Answers the same URLs services/quickbooks.py calls and applies the WHERE,
    STARTPOSITION and MAXRESULTS of each query, so paging and the incremental
    cursor are exercised for real. Every refresh rotates the refresh token and
    kills the previous one, as Intuit does.
    """

    def __init__(self):
        self.records: dict[str, list[dict]] = {
            "Purchase": [], "Bill": [], "Invoice": [], "SalesReceipt": []}
        self.company = {"CompanyName": "Sandbox Company_US_1"}
        self.home_currency = "USD"
        self.generation = 0
        self.live_refresh = None
        self.refresh_fails_with: str | None = None
        self.api_status: int | None = None
        self.token_calls: list[dict] = []
        self.queries: list[str] = []
        self.revoked: list[str] = []

    # --------------------------------------------------------------- tokens

    def _issue(self) -> dict:
        self.generation += 1
        self.live_refresh = f"refresh-{self.generation}"
        return {"access_token": f"access-{self.generation}", "token_type": "bearer",
                "expires_in": 3600, "refresh_token": self.live_refresh,
                "x_refresh_token_expires_in": 8_726_400}

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        if url == quickbooks.TOKEN_URL:
            self.token_calls.append(dict(data or {}))
            if data.get("grant_type") == "authorization_code":
                if data.get("code") != "good-code":
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json=self._issue())
            if self.refresh_fails_with:
                return httpx.Response(400, json={"error": self.refresh_fails_with})
            if data.get("refresh_token") != self.live_refresh:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._issue())
        if url == quickbooks.REVOKE_URL:
            self.revoked.append(json["token"])
            return httpx.Response(200)
        raise AssertionError(f"unexpected POST {url}")

    # ------------------------------------------------------------------ API

    def get(self, url, params=None, headers=None, timeout=None):
        assert headers["Authorization"] == f"Bearer access-{self.generation}", \
            "the API was called with a stale access token"
        if self.api_status:
            return httpx.Response(self.api_status, json={})
        if "/companyinfo/" in url:
            return httpx.Response(200, json={"CompanyInfo": self.company})
        if url.endswith("/preferences"):
            return httpx.Response(200, json={"Preferences": {
                "CurrencyPrefs": {"HomeCurrency": {"value": self.home_currency}}}})
        if url.endswith("/query"):
            return httpx.Response(200, json={"QueryResponse": self._query(params["query"])})
        raise AssertionError(f"unexpected GET {url}")

    def _query(self, sql: str) -> dict:
        self.queries.append(sql)
        match = re.match(
            r"SELECT \* FROM (\w+)(?: WHERE (.+?))? ORDERBY MetaData\.LastUpdatedTime "
            r"STARTPOSITION (\d+) MAXRESULTS (\d+)$", sql)
        assert match, f"query not understood: {sql}"
        entity, where, start, size = match.group(1), match.group(2), int(match.group(3)), int(match.group(4))
        rows = sorted(self.records[entity], key=lambda r: r["MetaData"]["LastUpdatedTime"])
        if where:
            field, value = re.match(r"([\w.]+) >= '([^']+)'", where).groups()
            if field == "TxnDate":
                rows = [r for r in rows if r["TxnDate"] >= value]
            else:
                bound = datetime.fromisoformat(value)
                rows = [r for r in rows
                        if datetime.fromisoformat(r["MetaData"]["LastUpdatedTime"]) >= bound]
        page = rows[start - 1:start - 1 + size]
        return {entity: page} if page else {}

    # ------------------------------------------------------------- records

    def add(self, entity: str, record_id: str, *, updated: str | None = None, **fields) -> dict:
        record = {"Id": record_id, "TxnDate": "2026-03-10",
                  "MetaData": {"LastUpdatedTime": updated or utc(-60)}, **fields}
        self.records[entity].append(record)
        return record


@pytest.fixture
def intuit(monkeypatch):
    fake = FakeIntuit()
    monkeypatch.setattr(quickbooks.httpx, "post", fake.post)
    monkeypatch.setattr(quickbooks.httpx, "get", fake.get)
    return fake


@pytest.fixture
def connected(credentials, intuit):
    """A panel whose admin already completed Intuit's consent screen."""
    db = credentials
    state = parse_qs(quickbooks.authorization_url(db, actor="admin@test").split("?", 1)[1])["state"][0]
    quickbooks.complete_connection(db, code="good-code", realm_id="9130", state=state)
    return db
