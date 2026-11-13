"""Excel/CSV imports and downloadable templates."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..services import excel_import, otc_import
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


def _otc_as_import_result(report: dict) -> dict:
    """The OTC report in the shape the import dialog shows."""
    errors = [f"Fila {item['fila']}: {item['motivo']}" for item in report["omitidas_detalle"]]
    errors += [f"Fila {item['fila']}: año corregido {item['original']} → {item['corregida']}"
               for item in report["fechas_corregidas_detalle"]]
    return {
        "inserted": report["nuevas"] + report["reemplazadas"],
        "skipped": report["omitidas"],
        "errors": errors[:20],
        "message": (f"Libro OTC {report['anio']} detectado. {report['message']} "
                    f"Omitidas: {report['omitidas']} · años corregidos: "
                    f"{report['fechas_corregidas']} · posibles duplicados: "
                    f"{report['posibles_duplicados']}."),
        "otc": report,
    }


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
    # An OTC trade log (Date, Client, Direction, Wire USD Sent…) goes to its own
    # importer, which keeps every trade field: the generic one would find no
    # «amount» column and reject every row.
    if kind == "orders" and otc_import.looks_like_otc(content):
        if user.role != "admin":
            raise HTTPException(status_code=403,
                                detail="Cargar un libro OTC es solo para administradores.")
        try:
            return _otc_as_import_result(otc_import.run(db, content))
        except otc_import.OTCImportError as err:
            raise HTTPException(status_code=400, detail=str(err)) from None
    try:
        return excel_import.import_file(db, kind, content, file.filename or "file.xlsx")
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
