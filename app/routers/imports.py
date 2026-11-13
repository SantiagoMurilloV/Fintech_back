"""Excel/CSV imports and downloadable templates."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..services import excel_import
from ..services import import_batches as batches
from .auth import AuthDep, current_user

router = APIRouter(tags=["imports"], dependencies=[AuthDep])

KINDS = {"orders", "expenses"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/api/import/template/{kind}")
def template(kind: str):
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail="Tipo inválido.")
    return Response(
        content=excel_import.template_xlsx(kind),
        media_type=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="template_{kind}.xlsx"'},
    )


def _as_import_result(batch) -> dict:
    """A batch in the shape the old one-click import answered."""
    rejected = (batch.exceptions or {}).get("rechazadas") or []
    corrected = (batch.exceptions or {}).get("fechas_corregidas") or []
    errors = [f"Fila {item['fila']}: {item['motivo']}" for item in rejected]
    errors += [f"Fila {item['fila']}: año corregido {item['original']} → {item['corregida']}"
               for item in corrected]
    label = f"{batch.label} detectado. " if batch.kind == "otc" else ""
    return {"inserted": batch.rows_loaded, "skipped": batch.rows_rejected, "errors": errors[:20],
            "message": f"{label}{batch.message or ''} Importación #{batch.id}.".strip(),
            "import_id": batch.id}


@router.post("/api/import/excel")
async def import_excel(kind: str = Form(...), file: UploadFile = None,
                       db: Session = Depends(get_db), user: User = Depends(current_user)):
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail="Indique el tipo: orders o expenses.")
    if file is None:
        raise HTTPException(status_code=400, detail="Adjunte un archivo .xlsx o .csv.")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="El archivo supera 10 MB.")
    # Every upload goes through the import history (preview + confirm in one
    # step here), so it can be reconciled later. An OTC log is recognised by its
    # columns and loaded by the OTC importer.
    try:
        return _as_import_result(batches.apply_now(db, kind, file.filename or "file.xlsx",
                                                   content, user))
    except PermissionError as err:
        raise HTTPException(status_code=403, detail=str(err)) from None
    except batches.ImportError_ as err:
        raise HTTPException(status_code=400, detail=str(err))
