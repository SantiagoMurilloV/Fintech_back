"""External endpoint integration (contract pending definition)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import external_api
from .auth import AuthDep

router = APIRouter(tags=["integrations"], dependencies=[AuthDep])


@router.get("/api/integrations/status")
def status():
    return external_api.status()


@router.post("/api/integrations/probe")
def probe():
    """Ping the configured endpoint and return a response preview."""
    try:
        return external_api.probe()
    except external_api.ExternalAPIError as err:
        raise HTTPException(status_code=503, detail=str(err))
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"No se pudo conectar: {err}")


@router.post("/api/integrations/sync")
def sync(db: Session = Depends(get_db)):
    """Pull external data into the local models (mapping pending)."""
    try:
        return external_api.sync(db)
    except external_api.ExternalAPIError as err:
        raise HTTPException(status_code=501, detail=str(err))
