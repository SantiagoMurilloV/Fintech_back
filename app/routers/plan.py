"""Accounting plan, monthly figures and the income statement built from them.

Structure is the admin's job (the plan says what the company's numbers mean);
figures are everyone's, like orders and expenses: the person who closes the
month types or uploads them.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..agent import formatting as fmt
from ..database import get_db
from ..models import Report, User
from ..services import pdf, plan_import, statement, storage
from ..services import plan as plan_service
from .auth import AdminDep, AuthDep

router = APIRouter(tags=["plan"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class PlanBody(BaseModel):
    accounts: list[dict]


class TemplateBody(BaseModel):
    template: str
    replace: bool = False


class ValuesBody(BaseModel):
    values: list[dict]


class StatementBody(BaseModel):
    period: str | None = None
    compare: str | None = "prev"


# ---------------------------------------------------------------------- plan

@router.get("/api/plan", dependencies=[AuthDep])
def get_plan(db: Session = Depends(get_db)):
    return plan_service.public(db)


@router.put("/api/plan")
def put_plan(body: PlanBody, db: Session = Depends(get_db), _: User = AdminDep):
    try:
        return plan_service.replace(db, body.accounts)
    except plan_service.PlanError as err:
        raise HTTPException(status_code=400, detail={"message": "El plan tiene errores.",
                                                     "problems": err.problems}) from None


@router.get("/api/plan/templates", dependencies=[AuthDep])
def list_templates():
    return {"items": plan_service.templates()}


@router.post("/api/plan/template")
def load_template(body: TemplateBody, db: Session = Depends(get_db), _: User = AdminDep):
    try:
        return plan_service.load_template(db, body.template, replace_existing=body.replace)
    except plan_service.PlanError as err:
        status = 409 if "Ya hay un plan" in str(err) else 400
        raise HTTPException(status_code=status, detail=str(err)) from None


# -------------------------------------------------------------------- values

@router.get("/api/plan/values", dependencies=[AuthDep])
def get_values(first: str | None = None, last: str | None = None,
               db: Session = Depends(get_db)):
    try:
        return statement.grid(db, first, last)
    except statement.StatementError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None


@router.put("/api/plan/values")
def put_values(body: ValuesBody, db: Session = Depends(get_db), user: User = AuthDep):
    try:
        return statement.set_values(db, body.values, actor=user.email)
    except statement.StatementError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None


@router.post("/api/plan/values/import")
async def import_values(file: UploadFile, dry_run: bool = Form(False),
                        db: Session = Depends(get_db), user: User = AuthDep):
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="El archivo supera 10 MB.")
    try:
        return plan_import.run(db, content, file.filename or "cifras.xlsx",
                               actor=user.email, dry_run=dry_run)
    except plan_import.ImportError_ as err:
        raise HTTPException(status_code=400, detail=str(err)) from None


@router.get("/api/plan/values/template.xlsx", dependencies=[AuthDep])
def values_template(first: str | None = None, last: str | None = None,
                    db: Session = Depends(get_db)):
    try:
        content = plan_import.template_xlsx(db, first, last)
    except statement.StatementError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None
    return Response(content=content, media_type=XLSX_MIME, headers={
        "Content-Disposition": 'attachment; filename="cifras_plan_de_cuentas.xlsx"'})


# ----------------------------------------------------------------- statement

def _compute(db: Session, period: str | None, compare: str | None) -> dict:
    try:
        return statement.compute(db, period, compare)
    except statement.StatementError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None


@router.get("/api/statement", dependencies=[AuthDep])
def get_statement(period: str | None = None, compare: str | None = "prev",
                  db: Session = Depends(get_db)):
    """Raw numbers per line; the frontend formats them."""
    return _compute(db, period, compare)


@router.get("/api/statement/export.csv", dependencies=[AuthDep])
def export_statement(period: str | None = None, compare: str | None = "prev",
                     db: Session = Depends(get_db)):
    result = _compute(db, period, compare)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["account", "name", "level", "kind", "unit", result["period"],
                     "vertical_pct", result["compare"] or "", "variation", "variation_pct",
                     "status"])
    for line in result["rows"]:
        writer.writerow([line["key"], line["name"], line["level"], line["kind"], line["unit"],
                         line["value"], line["vertical_pct"], line["previous"],
                         line["variation"], line["variation_pct"], line["status"]])
    buffer.seek(0)
    return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="estado_resultados_{result["period"]}.csv"'})


@router.post("/api/statement/report", status_code=201, dependencies=[AuthDep])
def save_statement_report(body: StatementBody, db: Session = Depends(get_db)):
    """Freeze the statement as a PDF in Reportes: same blocks as the screen."""
    result = _compute(db, body.period, body.compare)
    if not result["has_data"]:
        raise HTTPException(status_code=400, detail="Ese mes no tiene cifras.")
    stamp = datetime.now()
    title = f"Estado de resultados · {fmt.period_name(result['period'])}"
    subtitle = (f"Comparado con {fmt.period_name(result['compare'])} · " if result["compare"]
                else "") + f"Generado {stamp.strftime('%Y-%m-%d %H:%M')}"
    blocks = statement.to_blocks(result)
    # The heading repeats the masthead; the document starts at the figures.
    content = pdf.analysis_report(title, subtitle, blocks[1:])
    stored = storage.save(content, f"estado_resultados_{result['period']}.pdf", subfolder="reports")
    saved = Report(title=title, kind="statement", period=result["period"],
                   spec={"compare": result["compare"]}, pdf_url=stored["url"],
                   summary=None, created_at=stamp.isoformat(timespec="seconds"))
    db.add(saved)
    db.commit()
    return {"id": saved.id, "title": saved.title, "pdf_url": saved.pdf_url}
