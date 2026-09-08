"""Excel/CSV imports and downloadable templates."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import excel_import
from .auth import AuthDep

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


@router.post("/api/import/excel")
async def import_excel(kind: str = Form(...), file: UploadFile = None,
                       db: Session = Depends(get_db)):
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail="Indique el tipo: orders o expenses.")
    if file is None:
        raise HTTPException(status_code=400, detail="Adjunte un archivo .xlsx o .csv.")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="El archivo supera 10 MB.")
    try:
        return excel_import.import_file(db, kind, content, file.filename or "file.xlsx")
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
