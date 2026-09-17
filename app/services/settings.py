"""Runtime configuration.

Everything an operator changes without a deploy lives here: the external
endpoint, the spreadsheet, and the switches that pause each direction of the
sync. Values are stored one per row so a new option is a line in `SCHEMA`, not
a migration.

Secrets (an API token, the service-account JSON) are written but never read
back: `public()` replaces them with a flag saying whether they are set. An
empty string on update means "leave it as it is", so saving the form does not
wipe a secret the browser never received.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import (
    EXTERNAL_API_KEY, EXTERNAL_API_URL, QUICKBOOKS_CLIENT_ID, QUICKBOOKS_CLIENT_SECRET,
    QUICKBOOKS_ENVIRONMENT,
)
from ..models import Setting

# key -> (type, default). The type is what the value is coerced to on write.
SCHEMA: dict[str, tuple[str, object]] = {
    # --- External API ---------------------------------------------------
    "api.enabled": ("bool", False),
    "api.base_url": ("str", EXTERNAL_API_URL or ""),
    "api.token": ("secret", EXTERNAL_API_KEY or ""),
    # How the token travels: "bearer" (Authorization: Bearer <token>) or
    # "api-key" (Authorization: Api-Key <key>, DRF convention).
    "api.auth_scheme": ("str", "bearer"),
    # Each one accepts several comma-separated paths; every path is one feed.
    "api.orders_path": ("str", ""),
    "api.expenses_path": ("str", ""),
    # Currency stamped on imported records that do not carry one.
    "api.default_currency": ("str", "COP"),
    # Manual field mapping (JSON: our field -> feed field). When set, it
    # overrides whatever the intake agent decided — the human has the last word.
    "intake.orders_override": ("str", ""),
    "intake.expenses_override": ("str", ""),
    # --- Google Sheets ---------------------------------------------------
    "sheets.enabled": ("bool", False),
    "sheets.spreadsheet_id": ("str", ""),
    # Full service-account JSON, pasted by the admin.
    "sheets.credentials": ("secret", ""),
    "sheets.orders_tab": ("str", "Ordenes"),
    "sheets.expenses_tab": ("str", "Gastos"),
    # --- Sync ------------------------------------------------------------
    # Each direction pauses on its own: reading the sheet and writing to it are
    # separate decisions.
    "sync.pull_enabled": ("bool", True),
    "sync.push_enabled": ("bool", True),
    "sync.interval_seconds": ("int", 60),
    # --- QuickBooks Online ------------------------------------------------
    # Mandioca's app at developer.intuit.com. The customer never sees these:
    # connecting is one click plus Intuit's own consent screen.
    "quickbooks.client_id": ("str", QUICKBOOKS_CLIENT_ID or ""),
    "quickbooks.client_secret": ("secret", QUICKBOOKS_CLIENT_SECRET or ""),
    # "sandbox" (Intuit's test company) or "production"; separate key pairs.
    "quickbooks.environment": ("str", QUICKBOOKS_ENVIRONMENT or "sandbox"),
    # Whether the background loop pulls from the connected company.
    "quickbooks.enabled": ("bool", True),
    # What to bring: money out (Purchase, Bill) and/or money in (Invoice,
    # SalesReceipt). Each becomes our expenses / orders.
    "quickbooks.sync_expenses": ("bool", True),
    "quickbooks.sync_sales": ("bool", True),
    # First pull starts at this date (YYYY-MM-DD); empty = current year.
    "quickbooks.since": ("str", ""),
}

SECRET_KEYS = {key for key, (kind, _) in SCHEMA.items() if kind == "secret"}

MIN_INTERVAL = 15
MAX_INTERVAL = 3600


def _coerce(kind: str, value):
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "si", "sí", "on", "yes")
    if kind == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError("Tiene que ser un número entero.") from None
    return "" if value is None else str(value).strip()


def all_values(db: Session) -> dict:
    """Every setting, defaults filled in. Includes secrets: server-side only."""
    stored = {row.key: row.value for row in db.scalars(select(Setting)).all()}
    return {key: stored.get(key, default) if stored.get(key) is not None else default
            for key, (_, default) in SCHEMA.items()}


def get(db: Session, key: str):
    return all_values(db).get(key)


def public(db: Session) -> dict:
    """What the frontend may see: secrets become a boolean."""
    values = all_values(db)
    view = {key: value for key, value in values.items() if key not in SECRET_KEYS}
    for key in SECRET_KEYS:
        view[f"{key}.configured"] = bool(str(values.get(key) or "").strip())
    return view


def update(db: Session, changes: dict, actor: str | None = None) -> dict:
    """Write the settings present in `changes` and return the public view."""
    stamp = datetime.now().isoformat(timespec="seconds")
    unknown = sorted(set(changes) - set(SCHEMA))
    if unknown:
        raise ValueError(f"Configuración desconocida: {', '.join(unknown)}.")

    for key, raw in changes.items():
        kind, _ = SCHEMA[key]
        # An untouched secret arrives empty: keep what is stored.
        if kind == "secret" and not str(raw or "").strip():
            continue

        value = _coerce(kind, raw)
        if key == "sync.interval_seconds":
            value = max(MIN_INTERVAL, min(value, MAX_INTERVAL))
        if key == "api.auth_scheme":
            value = value.lower()
            if value not in ("bearer", "api-key"):
                raise ValueError("El esquema de autenticación debe ser «bearer» o «api-key».")
        if key == "sheets.credentials":
            value = _validated_credentials(value)
        if key == "quickbooks.environment":
            value = value.lower()
            if value not in ("sandbox", "production"):
                raise ValueError("El entorno de QuickBooks debe ser «sandbox» o «production».")
        if key == "quickbooks.since" and value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise ValueError("La fecha de inicio de QuickBooks debe ser AAAA-MM-DD.") from None

        row = db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value=value, updated_at=stamp, updated_by=actor))
        else:
            row.value, row.updated_at, row.updated_by = value, stamp, actor

    db.commit()
    return public(db)


def bootstrap_from_env(db: Session) -> list[str]:
    """Seed settings from the environment on a database that lacks them.

    A fresh deploy has no rows in `settings`; without this someone would have
    to type the endpoint into Configuración before the first pull. Each
    variable in SETTINGS_FROM_ENV seeds its key ONLY when that key has never
    been stored, so what an admin later saves always wins over the env.
    Returns the keys it wrote.
    """
    from ..config import SETTINGS_FROM_ENV

    stored = {row.key for row in db.scalars(select(Setting)).all()}
    changes = {key: value for key, value in SETTINGS_FROM_ENV.items() if key not in stored}
    if not changes:
        return []
    update(db, changes, actor="env")
    return sorted(changes)


def _validated_credentials(raw: str) -> str:
    """Check the pasted JSON is a usable service account before storing it."""
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise ValueError("Las credenciales no son un JSON válido.") from None
    missing = [field for field in ("client_email", "private_key") if not parsed.get(field)]
    if missing:
        raise ValueError(
            "El JSON no parece de una cuenta de servicio: le falta "
            f"{', '.join(missing)}.")
    return json.dumps(parsed, separators=(",", ":"))


REPORT_KEY = "sync.last_pull_report"


def save_report(db: Session, report: dict, key: str = REPORT_KEY) -> None:
    """Keep the outcome of the last pull, shown in the settings screen.

    Each source keeps its own report under its own key (the endpoint, QuickBooks).
    """
    row = db.get(Setting, key)
    stamp = datetime.now().isoformat(timespec="seconds")
    report = {**report, "ran_at": stamp}
    if row is None:
        db.add(Setting(key=key, value=report, updated_at=stamp))
    else:
        row.value, row.updated_at = report, stamp
    db.commit()


def last_report(db: Session, key: str = REPORT_KEY) -> dict | None:
    row = db.get(Setting, key)
    return row.value if row is not None else None


def credentials(db: Session) -> dict | None:
    """The stored service account, parsed, or None when not configured."""
    raw = get(db, "sheets.credentials")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None
