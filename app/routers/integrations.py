"""External integrations: the generic endpoint probe and QuickBooks Online.

QuickBooks is an OAuth round trip, so it has three moments instead of a
token field: `connect` hands the browser Intuit's authorisation URL, Intuit
sends the browser to `callback` with a code, and `callback` exchanges it
and returns the browser to the panel's settings screen. Everything else
(status, sync now, disconnect) is an ordinary authenticated call.
"""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..services import external_api, quickbooks
from .auth import AdminDep, AuthDep

router = APIRouter(tags=["integrations"])


# ----------------------------------------------------------- generic endpoint

@router.get("/api/integrations/status", dependencies=[AuthDep])
def status():
    return external_api.status()


@router.post("/api/integrations/probe", dependencies=[AuthDep])
def probe():
    """Ping the configured endpoint and return a response preview."""
    try:
        return external_api.probe()
    except external_api.ExternalAPIError as err:
        raise HTTPException(status_code=503, detail=str(err))
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"No se pudo conectar: {err}")


@router.post("/api/integrations/sync", dependencies=[AuthDep])
def sync(db: Session = Depends(get_db)):
    """Pull external data into the local models (mapping pending)."""
    try:
        return external_api.sync(db)
    except external_api.ExternalAPIError as err:
        raise HTTPException(status_code=501, detail=str(err))


# ----------------------------------------------------------------- QuickBooks

@router.get("/api/integrations/quickbooks")
def quickbooks_status(db: Session = Depends(get_db), _: User = AuthDep):
    """Connection state for the settings card. Never carries a token."""
    return quickbooks.status(db)


@router.post("/api/integrations/quickbooks/connect")
def quickbooks_connect(admin: User = AdminDep, db: Session = Depends(get_db)):
    """The URL the browser must visit to authorise Mandioca's app."""
    try:
        return {"url": quickbooks.authorization_url(db, actor=admin.email)}
    except quickbooks.QuickBooksError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None


@router.get("/api/integrations/quickbooks/callback", include_in_schema=False)
def quickbooks_callback(code: str = "", realmId: str = "", state: str = "",  # noqa: N803 — Intuit's spelling
                        error: str = "", db: Session = Depends(get_db)):
    """Where Intuit sends the browser back. No session here: `state` is the proof.

    Whatever happens, the browser ends up on the panel's settings screen with
    a `quickbooks=` marker the card turns into a sentence.
    """
    def back(**marks) -> RedirectResponse:
        return RedirectResponse(f"{quickbooks.frontend_url()}/?{urlencode(marks)}#/settings",
                                status_code=303)

    if error:
        # access_denied: the person closed Intuit's screen without authorising.
        return back(quickbooks="cancelled" if error == "access_denied" else "error", reason=error)
    if not code or not realmId or not state:
        return back(quickbooks="error", reason="Faltan datos en la respuesta de Intuit.")
    try:
        conn = quickbooks.complete_connection(db, code=code, realm_id=realmId, state=state)
    except quickbooks.QuickBooksError as err:
        return back(quickbooks="error", reason=str(err))
    return back(quickbooks="connected", company=conn.company_name or conn.realm_id or "")


@router.post("/api/integrations/quickbooks/sync")
def quickbooks_sync(db: Session = Depends(get_db), _: User = AdminDep):
    """Pull from the connected company right now."""
    try:
        return {"report": quickbooks.pull(db)}
    except quickbooks.NotConnected as err:
        raise HTTPException(status_code=409, detail=str(err)) from None
    except quickbooks.QuickBooksError as err:
        raise HTTPException(status_code=502, detail=str(err)) from None


@router.post("/api/integrations/quickbooks/disconnect")
def quickbooks_disconnect(db: Session = Depends(get_db), _: User = AdminDep):
    """Revoke at Intuit and forget the connection. Imported records stay."""
    return quickbooks.disconnect(db)
