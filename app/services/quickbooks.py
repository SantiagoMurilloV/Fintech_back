"""QuickBooks Online (Intuit): OAuth connection and incremental pull.

One app, registered by Mandioca at developer.intuit.com, connects any
customer's QuickBooks company: the person clicks "Conectar", authorises in
Intuit's own screen and comes back connected. Nothing to type.

What this module owns:

  connection   the OAuth dance (authorization URL, code exchange, refresh,
               revoke) and the encrypted storage of the tokens. Intuit's
               refresh token ROTATES on every refresh and the old one dies,
               so every refresh persists the new pair before anything else
               and refreshes are serialised behind a lock — two workers
               refreshing at once would revoke the whole chain.
  pull         the incremental sync. Money out (Purchase, Bill) becomes our
               expenses; money in (Invoice, SalesReceipt) becomes our orders.
               Idempotent by external key, exactly like api_sync: running it
               twice updates instead of duplicating, and the report says what
               came in, what was updated and what was skipped and why.

The mapping is FIXED, not guessed: QuickBooks has one documented schema, so
the intake agent has nothing to decide here and the LLM is never involved.
Amounts and currencies are copied verbatim and verified after the write.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import (
    CORS_ORIGINS, FRONTEND_URL, JWT_ALGORITHM, JWT_ISSUER, JWT_SECRET, PUBLIC_BASE_URL,
    QUICKBOOKS_REDIRECT_URI,
)
from ..models import Expense, IntegrationConnection, Order
from . import api_sync, mapping
from . import settings as settings_service

log = logging.getLogger("quickbooks")

PROVIDER = "quickbooks"

# Intuit endpoints (from https://developer.intuit.com/.well-known/openid_configuration).
AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
API_BASE = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
SCOPES = "com.intuit.quickbooks.accounting"
# Minor versions below 75 were retired by Intuit in August 2025.
MINOR_VERSION = 75
CALLBACK_PATH = "/api/integrations/quickbooks/callback"

TIMEOUT = 30
PAGE_SIZE = 1000          # Intuit's maximum per query page
MAX_RECORDS = 20000       # per entity per pull; the report says when it is hit
# Refresh the access token this long before it actually expires.
REFRESH_MARGIN = timedelta(minutes=5)
# The `state` we send Intuit is a signed token valid this long.
STATE_MINUTES = 15
REPORT_KEY = "quickbooks.last_pull_report"

# Intuit's own words for a transaction's payment state, kept in the table.
STATUS_PAID, STATUS_OPEN = "Paid", "Open"

_refresh_lock = threading.Lock()


class QuickBooksError(RuntimeError):
    """Anything the caller should show as a sentence."""


class NotConnected(QuickBooksError):
    pass


# ------------------------------------------------------------------ crypto

def _fernet() -> Fernet:
    """Tokens at rest are encrypted with a key derived from JWT_SECRET.

    A leaked database dump then holds no usable QuickBooks session. The
    trade-off is explicit: rotating JWT_SECRET makes the stored tokens
    unreadable, which surfaces as "needs_reconnect" — one click to fix.
    """
    digest = hashlib.sha256(f"quickbooks:{JWT_SECRET}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _seal(value: str | None) -> str | None:
    return _fernet().encrypt(value.encode()).decode() if value else None


def _open(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return None


# ----------------------------------------------------------------- helpers

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utc_stamp(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _local_stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def redirect_uri() -> str:
    """The callback Intuit must send the browser to; register it verbatim."""
    return QUICKBOOKS_REDIRECT_URI or f"{PUBLIC_BASE_URL}{CALLBACK_PATH}"


def frontend_url() -> str:
    return FRONTEND_URL or (CORS_ORIGINS[0] if CORS_ORIGINS else "http://localhost:3000")


def _credentials(db: Session) -> tuple[str, str, str]:
    values = settings_service.all_values(db)
    client_id = str(values.get("quickbooks.client_id") or "").strip()
    secret = str(values.get("quickbooks.client_secret") or "").strip()
    environment = str(values.get("quickbooks.environment") or "sandbox").strip().lower()
    if not client_id or not secret:
        raise QuickBooksError(
            "Faltan el Client ID y el Client Secret de la app de QuickBooks. Se cargan en "
            "Configuración → Integraciones o con QUICKBOOKS_CLIENT_ID / QUICKBOOKS_CLIENT_SECRET.")
    if environment not in API_BASE:
        raise QuickBooksError("El entorno de QuickBooks debe ser «sandbox» o «production».")
    return client_id, secret, environment


def _basic_auth(client_id: str, secret: str) -> dict:
    token = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"}


def connection(db: Session) -> IntegrationConnection | None:
    return db.scalars(select(IntegrationConnection)
                      .where(IntegrationConnection.provider == PROVIDER)).first()


def _require_connection(db: Session) -> IntegrationConnection:
    conn = connection(db)
    if conn is None or conn.status == "disconnected" or not conn.refresh_token:
        raise NotConnected("QuickBooks no está conectado. Use «Conectar» en Configuración.")
    return conn


# ------------------------------------------------------------------- OAuth

def authorization_url(db: Session, actor: str) -> str:
    """Where to send the browser so the person authorises Mandioca's app.

    `state` is a short-lived signed token: it proves the callback belongs to
    a request an admin of THIS panel started, and carries who started it, so
    the unauthenticated callback needs no session of its own.
    """
    client_id, _, environment = _credentials(db)
    now = _utcnow()
    state = jwt.encode({
        "iss": JWT_ISSUER, "aud": "quickbooks-connect", "sub": actor,
        "env": environment, "nonce": secrets.token_urlsafe(16),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=STATE_MINUTES)).timestamp()),
    }, JWT_SECRET, algorithm=JWT_ALGORITHM)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": redirect_uri(),
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _read_state(state: str) -> dict:
    try:
        return jwt.decode(state, JWT_SECRET, algorithms=[JWT_ALGORITHM],
                          audience="quickbooks-connect", issuer=JWT_ISSUER)
    except jwt.ExpiredSignatureError:
        raise QuickBooksError("La autorización tardó demasiado. Vuelva a intentar «Conectar».") from None
    except jwt.InvalidTokenError:
        raise QuickBooksError("La respuesta de Intuit no corresponde a una conexión iniciada aquí.") from None


def _token_request(db: Session, data: dict) -> dict:
    client_id, secret, _ = _credentials(db)
    try:
        response = httpx.post(TOKEN_URL, data=data, headers=_basic_auth(client_id, secret),
                              timeout=TIMEOUT)
    except httpx.HTTPError as err:
        raise QuickBooksError(f"No se pudo hablar con Intuit: {err}") from err
    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            body = {}
        code = body.get("error") or f"HTTP {response.status_code}"
        raise QuickBooksError(f"Intuit rechazó la solicitud de tokens ({code}).")
    return response.json()


def _store_tokens(conn: IntegrationConnection, tokens: dict) -> None:
    """Persist a fresh token pair. Called before anything else uses them."""
    now = _utcnow()
    conn.access_token = _seal(tokens.get("access_token"))
    conn.refresh_token = _seal(tokens.get("refresh_token"))
    conn.access_expires_at = _utc_stamp(now + timedelta(seconds=int(tokens.get("expires_in", 3600))))
    refresh_seconds = tokens.get("x_refresh_token_expires_in")
    if refresh_seconds:
        conn.refresh_expires_at = _utc_stamp(now + timedelta(seconds=int(refresh_seconds)))
    conn.status = "connected"
    conn.last_error = None
    conn.updated_at = _local_stamp()


def complete_connection(db: Session, code: str, realm_id: str, state: str) -> IntegrationConnection:
    """Intuit sent the browser back: exchange the code and record the company."""
    claims = _read_state(state)
    tokens = _token_request(db, {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
    })
    conn = connection(db)
    if conn is None:
        conn = IntegrationConnection(provider=PROVIDER)
        db.add(conn)
    conn.environment = claims.get("env") or "sandbox"
    conn.realm_id = str(realm_id)
    conn.connected_by = claims.get("sub")
    conn.connected_at = _local_stamp()
    # A different company than before starts its own incremental history.
    if conn.cursor and conn.realm_id != str(realm_id):
        conn.cursor = None
    _store_tokens(conn, tokens)
    db.commit()

    # Name and home currency, for the settings card and the currency fallback.
    try:
        info = _get(db, conn, "companyinfo/" + conn.realm_id).get("CompanyInfo") or {}
        conn.company_name = (info.get("CompanyName") or "")[:160] or None
        prefs = _get(db, conn, "preferences").get("Preferences") or {}
        home = ((prefs.get("CurrencyPrefs") or {}).get("HomeCurrency") or {}).get("value")
        conn.home_currency = (home or "").upper()[:10] or None
        db.commit()
    except QuickBooksError as err:
        log.warning("Connected but could not read company info: %s", err)
    return conn


def _access_token(db: Session, conn: IntegrationConnection) -> str:
    """A valid access token, refreshing under the lock when close to expiry."""
    with _refresh_lock:
        db.refresh(conn)
        expires = conn.access_expires_at
        token = _open(conn.access_token)
        if token and expires and datetime.fromisoformat(expires) - _utcnow() > REFRESH_MARGIN:
            return token
        refresh = _open(conn.refresh_token)
        if not refresh:
            _mark_reconnect(db, conn, "La conexión guardada ya no se puede leer.")
            raise NotConnected("QuickBooks requiere reconectar.")
        try:
            tokens = _token_request(db, {"grant_type": "refresh_token", "refresh_token": refresh})
        except QuickBooksError as err:
            # invalid_grant: revoked by the user, rotated elsewhere or expired.
            # Nothing automatic fixes it; a person has to reconnect.
            if "invalid_grant" in str(err):
                _mark_reconnect(db, conn, "Intuit ya no acepta la conexión guardada.")
                raise NotConnected("QuickBooks requiere reconectar: Intuit rechazó la sesión guardada.") from err
            raise
        _store_tokens(conn, tokens)
        db.commit()
        return _open(conn.access_token) or ""


def _mark_reconnect(db: Session, conn: IntegrationConnection, reason: str) -> None:
    conn.status = "needs_reconnect"
    conn.last_error = reason
    conn.updated_at = _local_stamp()
    db.commit()


def disconnect(db: Session) -> dict:
    """Revoke the tokens at Intuit (best effort) and forget the connection."""
    conn = connection(db)
    if conn is None:
        return {"disconnected": False}
    refresh = _open(conn.refresh_token)
    revoked = False
    if refresh:
        try:
            client_id, secret, _ = _credentials(db)
            response = httpx.post(REVOKE_URL, json={"token": refresh},
                                  headers={**_basic_auth(client_id, secret),
                                           "Content-Type": "application/json"}, timeout=TIMEOUT)
            revoked = response.status_code < 400
        except (QuickBooksError, httpx.HTTPError) as err:
            log.warning("Could not revoke QuickBooks token: %s", err)
    db.delete(conn)
    db.commit()
    return {"disconnected": True, "revoked_at_intuit": revoked}


# --------------------------------------------------------------------- API

def _get(db: Session, conn: IntegrationConnection, path: str, params: dict | None = None) -> dict:
    token = _access_token(db, conn)
    base = API_BASE.get(conn.environment, API_BASE["sandbox"])
    url = f"{base}/v3/company/{conn.realm_id}/{path}"
    query = {"minorversion": MINOR_VERSION, **(params or {})}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        response = httpx.get(url, params=query, headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as err:
        raise QuickBooksError(f"No se pudo consultar QuickBooks: {err}") from err
    if response.status_code == 401:
        _mark_reconnect(db, conn, "QuickBooks rechazó el acceso (401).")
        raise NotConnected("QuickBooks requiere reconectar.")
    if response.status_code == 429:
        raise QuickBooksError("QuickBooks limitó las consultas (429). Se reintenta en el próximo ciclo.")
    if response.status_code >= 400:
        detail = ""
        try:
            fault = (response.json().get("Fault") or {}).get("Error") or []
            detail = "; ".join(e.get("Message", "") for e in fault if e.get("Message"))
        except ValueError:
            pass
        raise QuickBooksError(f"QuickBooks respondió {response.status_code}. {detail}".strip())
    return response.json()


def _query_all(db: Session, conn: IntegrationConnection, entity: str,
               where: str) -> tuple[list[dict], dict]:
    """Every record of `entity` matching `where`, page by page."""
    records: list[dict] = []
    pages = 0
    start = 1
    truncated = False
    while True:
        sql = (f"SELECT * FROM {entity}{' WHERE ' + where if where else ''} "
               f"ORDERBY MetaData.LastUpdatedTime STARTPOSITION {start} MAXRESULTS {PAGE_SIZE}")
        payload = _get(db, conn, "query", {"query": sql})
        batch = (payload.get("QueryResponse") or {}).get(entity) or []
        pages += 1
        records.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
        if len(records) >= MAX_RECORDS:
            truncated = True
            break
    return records, {"pages": pages, "truncated": truncated}


def _since_clause(conn: IntegrationConnection, since_date: str) -> tuple[str, str]:
    """The WHERE for this pull and a sentence saying which window it covers.

    First pull: everything dated from the configured start. Afterwards:
    anything Intuit updated since the previous pull (the cursor), whatever
    its transaction date — an old invoice paid today comes through.
    """
    if conn.cursor:
        return f"MetaData.LastUpdatedTime >= '{conn.cursor}'", f"cambios desde {conn.cursor[:19]}"
    return f"TxnDate >= '{since_date}'", f"transacciones desde {since_date}"


# ----------------------------------------------------------------- mapping

def _ref_name(record: dict, key: str) -> str:
    ref = record.get(key) or {}
    return str(ref.get("name") or "").strip() if isinstance(ref, dict) else ""


def _line_account(record: dict) -> str:
    """The expense account of the first detail line: our category."""
    for line in record.get("Line") or []:
        for detail_key in ("AccountBasedExpenseLineDetail", "ItemBasedExpenseLineDetail"):
            detail = line.get(detail_key) or {}
            name = _ref_name(detail, "AccountRef") or _ref_name(detail, "ItemRef")
            if name:
                return name
    return ""


def _line_description(record: dict) -> str:
    for line in record.get("Line") or []:
        text = str(line.get("Description") or "").strip()
        if text:
            return text
    return ""


def _amount(record: dict) -> float | None:
    return mapping.to_amount(record.get("TotalAmt"))


def _currency(record: dict, fallback: str, note, remark) -> str:
    raw = (record.get("CurrencyRef") or {}).get("value") if isinstance(record.get("CurrencyRef"), dict) else None
    # Same reporting as the endpoint sync: fallbacks, pegs and missing rates
    # are said out loud in the report, never applied quietly.
    return api_sync.resolve_currency(raw, fallback, note, remark)


def _payment_state(record: dict) -> str:
    balance = mapping.to_amount(record.get("Balance"))
    return STATUS_PAID if balance is not None and balance <= 0 else STATUS_OPEN


def _expense_payload(kind: str, record: dict, fallback: str, note, remark) -> dict | None:
    amount = _amount(record)
    if amount is None or amount <= 0:
        return None
    vendor = _ref_name(record, "EntityRef") or _ref_name(record, "VendorRef")
    description = (str(record.get("PrivateNote") or "").strip() or _line_description(record)
                   or (f"{'Factura de proveedor' if kind == 'Bill' else 'Gasto'} "
                       f"{record.get('DocNumber') or record.get('Id')}"
                       + (f" · {vendor}" if vendor else "")))
    return dict(
        description=description[:160],
        category=(_line_account(record) or "Sin categoría")[:60],
        vendor=vendor[:120] or None,
        owner=None,
        amount=amount,
        currency=_currency(record, fallback, note, remark),
        date=mapping.to_date(record.get("TxnDate")) or mapping.today(),
    )


def _order_payload(kind: str, record: dict, fallback: str, note, remark) -> dict | None:
    amount = _amount(record)
    if amount is None or amount <= 0:
        return None
    customer = _ref_name(record, "CustomerRef") or f"Cliente QuickBooks {record.get('Id')}"
    # A sales receipt is paid by definition; an invoice is revenue once its
    # balance is zero and waits as pending until then.
    state = STATUS_PAID if kind == "SalesReceipt" else _payment_state(record)
    return dict(
        customer=customer[:120],
        amount=amount,
        currency=_currency(record, fallback, note, remark),
        status="approved" if state == STATUS_PAID else "pending",
        date=mapping.to_date(record.get("TxnDate")) or mapping.today(),
        gateway="QuickBooks",
    )


def _key(entity: str, kind: str, external_id) -> str:
    return f"qbo:{entity}:{kind.lower()}:{external_id}"


def _order_id(kind: str, external_id) -> str:
    """Fits Order.id (20 chars) and stays readable: QB-INV-123, QB-SR-45."""
    prefix = "QB-INV" if kind == "Invoice" else "QB-SR"
    readable = f"{prefix}-{external_id}"
    if len(readable) <= 20:
        return readable
    return "QB-" + hashlib.sha1(readable.encode()).hexdigest()[:17]


def _extra(entity: str, kind: str, record: dict, state: str | None) -> dict:
    extra = {
        "source": "quickbooks", "external_key": _key(entity, kind, record.get("Id")),
        "qbo_type": kind, "doc_number": record.get("DocNumber") or None,
        "_raw": api_sync.raw_record(record),
    }
    if state is not None:
        extra["source_status"] = state
    return extra


# -------------------------------------------------------------------- pull

def pull(db: Session) -> dict:
    """One incremental pull from the connected company. Returns the report."""
    conn = _require_connection(db)
    values = settings_service.all_values(db)
    fallback = (conn.home_currency or values.get("api.default_currency") or "COP").upper()
    since = str(values.get("quickbooks.since") or "").strip() or f"{datetime.now().year}-01-01"
    where, window = _since_clause(conn, since)
    started = _utcnow()

    report = {"ran_at": mapping.today(), "source": f"QuickBooks · {conn.company_name or conn.realm_id}",
              "window": window, "entities": {}}
    try:
        if values.get("quickbooks.sync_expenses"):
            for kind in ("Purchase", "Bill"):
                report["entities"][f"expenses:{kind.lower()}"] = _pull_expenses(db, conn, kind, where, fallback)
        if values.get("quickbooks.sync_sales"):
            for kind in ("Invoice", "SalesReceipt"):
                report["entities"][f"orders:{kind.lower()}"] = _pull_orders(db, conn, kind, where, fallback)
    except QuickBooksError as err:
        conn.last_error = str(err)
        conn.updated_at = _local_stamp()
        db.commit()
        raise

    if not report["entities"]:
        raise QuickBooksError("No hay nada que traer: active gastos o ventas en la tarjeta de QuickBooks.")

    # The next pull asks for what changed since THIS one started, so nothing
    # updated while we were reading slips through the gap.
    conn.cursor = _utc_stamp(started - timedelta(minutes=1))
    conn.last_sync_at = _local_stamp()
    conn.last_error = None
    conn.updated_at = conn.last_sync_at
    db.commit()
    settings_service.save_report(db, report, key=REPORT_KEY)
    return report


def _new_result(kind: str, window: str, fetched: dict, count: int) -> dict:
    result = {"path": kind, "window": window, "received": count, "pages": fetched["pages"],
              "imported": 0, "updated": 0, "skipped": 0, "problems": [], "notes": []}
    if fetched["truncated"]:
        result["problems"].append(f"Traje los primeros {MAX_RECORDS} registros de {kind}; hay más.")
    return result


def _pull_expenses(db: Session, conn: IntegrationConnection, kind: str, where: str,
                   fallback: str) -> dict:
    records, fetched = _query_all(db, conn, kind, where)
    result = _new_result(kind, where, fetched, len(records))
    if not records:
        return result
    note, remark = api_sync.collectors(result)
    remark("Concepto: nota privada o primera línea; categoría: cuenta contable de la primera línea.")

    known = api_sync.existing_expenses(db)
    expected: dict[str, tuple[float, str]] = {}
    for record in records:
        payload = _expense_payload(kind, record, fallback, note, remark)
        if payload is None:
            result["skipped"] += 1
            note(f"Algunos {kind} no tienen un monto usable.")
            continue
        state = _payment_state(record) if kind == "Bill" else None
        extra = _extra("expenses", kind, record, state)
        key = extra["external_key"]
        expected[key] = (payload["amount"], payload["currency"])
        existing = known.get(key)
        if existing is None:
            db.add(Expense(extra=extra, **payload))
            result["imported"] += 1
        else:
            for field, value in payload.items():
                setattr(existing, field, value)
            existing.extra = {**(existing.extra or {}), **extra}
            result["updated"] += 1
    db.commit()
    result["verification"] = api_sync.verify_copy(db, "expenses", expected)
    if not result["verification"]["exact"]:
        result["problems"].append(
            "VERIFICACIÓN FALLIDA: lo guardado no coincide con QuickBooks. "
            + "; ".join(result["verification"]["mismatches"]))
    return result


def _pull_orders(db: Session, conn: IntegrationConnection, kind: str, where: str,
                 fallback: str) -> dict:
    records, fetched = _query_all(db, conn, kind, where)
    result = _new_result(kind, where, fetched, len(records))
    if not records:
        return result
    note, remark = api_sync.collectors(result)
    if kind == "Invoice":
        remark("Una factura cuenta como ingreso cuando su saldo es cero; con saldo queda pendiente.")

    expected: dict[str, tuple[float, str]] = {}
    for record in records:
        payload = _order_payload(kind, record, fallback, note, remark)
        if payload is None:
            result["skipped"] += 1
            note(f"Algunos {kind} no tienen un monto usable.")
            continue
        state = STATUS_PAID if kind == "SalesReceipt" else _payment_state(record)
        extra = _extra("orders", kind, record, state)
        key = extra["external_key"]
        expected[key] = (payload["amount"], payload["currency"])
        order_id = _order_id(kind, record.get("Id"))
        existing = db.get(Order, order_id)
        if existing is None:
            db.add(Order(id=order_id, extra=extra, **payload))
            result["imported"] += 1
        else:
            for field, value in payload.items():
                setattr(existing, field, value)
            existing.extra = {**(existing.extra or {}), **extra}
            result["updated"] += 1
    db.commit()
    result["verification"] = api_sync.verify_copy(db, "orders", expected)
    if not result["verification"]["exact"]:
        result["problems"].append(
            "VERIFICACIÓN FALLIDA: lo guardado no coincide con QuickBooks. "
            + "; ".join(result["verification"]["mismatches"]))
    return result


# ------------------------------------------------------------------ status

def status(db: Session) -> dict:
    """What the settings card shows. Never includes a token."""
    values = settings_service.all_values(db)
    conn = connection(db)
    view = {
        "configured": bool(str(values.get("quickbooks.client_id") or "").strip()
                           and str(values.get("quickbooks.client_secret") or "").strip()),
        "environment": values.get("quickbooks.environment") or "sandbox",
        "redirect_uri": redirect_uri(),
        "status": "disconnected",
        "company_name": None, "realm_id": None, "home_currency": None,
        "connected_by": None, "connected_at": None, "last_sync_at": None,
        "last_error": None, "connected_environment": None,
        "last_pull": settings_service.last_report(db, key=REPORT_KEY),
    }
    if conn is not None:
        view.update({
            "status": conn.status, "company_name": conn.company_name, "realm_id": conn.realm_id,
            "home_currency": conn.home_currency, "connected_by": conn.connected_by,
            "connected_at": conn.connected_at, "last_sync_at": conn.last_sync_at,
            "last_error": conn.last_error, "connected_environment": conn.environment,
        })
    return view


def is_connected(db: Session) -> bool:
    conn = connection(db)
    return conn is not None and conn.status == "connected"
