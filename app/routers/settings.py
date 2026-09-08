"""Panel configuration, and the two inspectors that show what each source brings.

Reading the configuration is open to any signed-in user — the panel needs to
know whether the sync is paused. Writing it is admin only, like managing users.

The probes exist because the mapping between the external API, the spreadsheet
and our own tables has not been decided yet: they report what each side
actually returns (fields, columns, a sample) so that decision is made looking
at real data instead of guessing.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..services import api_sync, external_api, sheets
from ..services import settings as settings_service
from .auth import AdminDep, AuthDep

router = APIRouter(tags=["settings"])


class SettingsBody(BaseModel):
    """Only the keys present are written; a secret sent empty is left alone."""
    values: dict


@router.get("/api/settings")
def read_settings(db: Session = Depends(get_db), _: User = AuthDep):
    return {"values": settings_service.public(db),
            "interval_bounds": [settings_service.MIN_INTERVAL, settings_service.MAX_INTERVAL],
            "last_pull": settings_service.last_report(db)}


@router.put("/api/settings")
def write_settings(body: SettingsBody, admin: User = AdminDep,
                   db: Session = Depends(get_db)):
    try:
        values = settings_service.update(db, body.values or {}, actor=admin.email)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None
    return {"values": values}


@router.post("/api/settings/sync/pull")
def sync_pull(db: Session = Depends(get_db), _: User = AdminDep):
    """Pull orders and expenses from the endpoint right now.

    The button is explicit intent, so it only needs the URL configured; the
    enabled/paused switches govern the background loop, not this call.
    """
    try:
        return {"report": api_sync.pull(db)}
    except api_sync.SyncError as err:
        raise HTTPException(status_code=502, detail=str(err)) from None


class WipeBody(BaseModel):
    scope: str = "imported"


@router.post("/api/settings/data/clear")
def clear_data(body: WipeBody, db: Session = Depends(get_db), _: User = AdminDep):
    """Danger zone: erase OUR data (never the external source)."""
    try:
        return {"removed": api_sync.wipe(db, body.scope)}
    except api_sync.SyncError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None


@router.post("/api/settings/probe/api")
def probe_api(db: Session = Depends(get_db), _: User = AdminDep):
    """Call the configured endpoint and describe what came back."""
    values = settings_service.all_values(db)
    base_url = (values.get("api.base_url") or "").strip()
    if not base_url:
        raise HTTPException(status_code=400, detail="Falta la URL del endpoint.")

    targets = []
    for kind, key in (("orders", "api.orders_path"), ("expenses", "api.expenses_path")):
        paths = api_sync.split_paths(values.get(key))
        for path in paths:
            name = kind if len(paths) == 1 else f"{kind}:{api_sync.path_slug(path)}"
            targets.append((name, path))
    try:
        return external_api.inspect(base_url, values.get("api.token") or "",
                                    targets, values.get("api.auth_scheme") or "bearer")
    except external_api.ExternalAPIError as err:
        raise HTTPException(status_code=502, detail=str(err)) from None


@router.post("/api/settings/probe/sheets")
def probe_sheets(db: Session = Depends(get_db), _: User = AdminDep):
    """Check access to the spreadsheet and report its columns and a sample."""
    values = settings_service.all_values(db)
    try:
        return sheets.probe(
            settings_service.credentials(db),
            (values.get("sheets.spreadsheet_id") or "").strip(),
            {"orders": values.get("sheets.orders_tab") or "",
             "expenses": values.get("sheets.expenses_tab") or ""},
        )
    except sheets.SheetsError as err:
        raise HTTPException(status_code=502, detail=str(err)) from None
