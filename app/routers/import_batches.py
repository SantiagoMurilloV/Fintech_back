"""Import history: preview a file, confirm it, and reconcile it later."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ImportBatch, User
from ..services import import_batches as batches
from .auth import AuthDep, current_user

router = APIRouter(tags=["imports"], dependencies=[AuthDep])
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _batch(db: Session, batch_id: int) -> ImportBatch:
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Importación no encontrada.")
    return batch


def _guard(call):
    try:
        return call()
    except PermissionError as err:
        raise HTTPException(status_code=403, detail=str(err)) from None
    except batches.ImportError_ as err:
        raise HTTPException(status_code=400, detail=str(err)) from None


@router.post("/api/imports/preview", status_code=201)
async def preview(kind: str = Form(...), file: UploadFile = None,
                  db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Read and check a file; nothing is written until it is confirmed."""
    if file is None:
        raise HTTPException(status_code=400, detail="Adjunte un archivo .xlsx o .csv.")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="El archivo supera 25 MB.")
    batch = _guard(lambda: batches.preview(db, kind, file.filename or "archivo.xlsx", content, user))
    return batches.view(batch, detail=True)


@router.post("/api/imports/{batch_id}/confirm")
def confirm(batch_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    batch = _guard(lambda: batches.confirm(db, _batch(db, batch_id), user))
    return {**batches.view(batch, detail=True), "reconciliation": batches.reconcile(db, batch)}


@router.post("/api/imports/{batch_id}/discard")
def discard(batch_id: int, db: Session = Depends(get_db)):
    return batches.view(_guard(lambda: batches.discard(db, _batch(db, batch_id))))


@router.get("/api/imports")
def history(limit: int = 50, db: Session = Depends(get_db)):
    items = []
    for batch in batches.history(db, limit):
        result = batches.reconcile(db, batch)
        items.append({**batches.view(batch), "ok": result.get("ok"),
                      "changed": len(result.get("changed") or []),
                      "missing": result.get("missing", 0)})
    return {"items": items}


@router.get("/api/imports/{batch_id}")
def detail(batch_id: int, db: Session = Depends(get_db)):
    batch = _batch(db, batch_id)
    return {**batches.view(batch, detail=True), "reconciliation": batches.reconcile(db, batch)}


@router.get("/api/imports/{batch_id}/exceptions.xlsx")
def exceptions_file(batch_id: int, db: Session = Depends(get_db)):
    batch = _batch(db, batch_id)
    return Response(content=batches.exceptions_xlsx(db, batch), media_type=XLSX_MIME,
                    headers={"Content-Disposition":
                             f'attachment; filename="importacion_{batch.id}.xlsx"'})
