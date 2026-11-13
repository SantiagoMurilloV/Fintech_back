"""Import history and reconciliation: prove a file landed as it was.

    preview(file)   read and check the file, write NOTHING; store an ImportBatch
                    «preview» with its control totals (from the file) and every
                    exception (rejected rows, corrected dates, repeated rows)
    confirm(batch)  load it; every row is tagged with the batch, its row in the
                    file and a fingerprint of its values
    reconcile(batch)
                    recompute, live from the panel, the same totals per month
                    and compare: File | Panel | ✓/✗, plus the rows edited or
                    deleted since the import

Follows the usual controls for financial data loads: row counts (read =
loaded + rejected), control totals per period (volume, spread, provider cost,
profit), row-level fingerprints, an exceptions list and an audit trail (who,
when, which file — by its SHA-256).
"""
from __future__ import annotations

import hashlib
import io
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Expense, ImportBatch, Order
from . import excel_import, finance, fingerprint, otc_import

MONEY = ("volume", "spread", "cost", "profit")
TOLERANCE = 0.01
SHOWN = 50


class ImportError_(ValueError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ preview

def preview(db: Session, kind: str, filename: str, content: bytes, user) -> ImportBatch:
    """Check a file and keep it as a preview. Nothing is loaded yet."""
    if kind not in ("orders", "expenses"):
        raise ImportError_("Indique el tipo: orders o expenses.")
    is_otc = kind == "orders" and otc_import.looks_like_otc(content)
    if is_otc and user.role != "admin":
        raise PermissionError("Cargar un libro OTC es solo para administradores.")
    batch = ImportBatch(kind="otc" if is_otc else kind, filename=filename[:200],
                        file_sha256=hashlib.sha256(content).hexdigest(), status="preview",
                        uploaded_by=user.email, uploaded_at=_now(), content=content)
    try:
        if is_otc:
            report = otc_import.run(db, content, dry_run=True)
            batch.label = f"Libro OTC {report['anio']}"
            batch.rows_read = report["filas_leidas"]
            batch.rows_valid = report["operaciones"]
            batch.rows_rejected = report["omitidas"]
            batch.totals = {m["mes"]: {"rows": m["operaciones"],
                                       "volume": round(m["off_ramp_usd"] + m["on_ramp_usd"], 2),
                                       "spread": m["spread_bruto"], "cost": m["costo_proveedor"],
                                       "profit": m["utilidad"]} for m in report["por_mes"]}
            batch.exceptions = report["excepciones"]
        else:
            report = excel_import.import_file(db, kind, content, filename, dry_run=True)
            batch.label = "Órdenes desde Excel" if kind == "orders" else "Gastos desde Excel"
            batch.rows_read = report["rows_read"]
            batch.rows_valid = report["inserted"]
            batch.rows_rejected = report["skipped"]
            batch.totals = report["totals"]
            batch.exceptions = {"rechazadas": report["rejected"], "fechas_corregidas": [],
                                "posibles_duplicados": []}
    except (otc_import.OTCImportError, ValueError) as err:
        raise ImportError_(str(err)) from None
    same = db.scalars(select(ImportBatch).where(ImportBatch.file_sha256 == batch.file_sha256,
                                                ImportBatch.status == "applied")).first()
    batch.message = (f"Este mismo archivo ya se importó el {same.applied_at} (importación "
                     f"#{same.id})." if same else None)
    db.add(batch)
    db.commit()
    return batch


def confirm(db: Session, batch: ImportBatch, user) -> ImportBatch:
    """Load a previewed file and record what happened."""
    if batch.status != "preview" or not batch.content:
        raise ImportError_("Esta importación ya no está en vista previa.")
    if batch.kind == "otc" and user.role != "admin":
        raise PermissionError("Cargar un libro OTC es solo para administradores.")
    if batch.kind == "otc":
        report = otc_import.run(db, batch.content, batch_id=batch.id)
        batch.rows_loaded = report["nuevas"] + report["reemplazadas"]
        batch.rows_replaced = report["reemplazadas"]
        batch.rows_removed = report["eliminadas"]
        batch.message = report["message"]
        # The year was replaced as a whole: earlier loads of it no longer hold rows.
        for older in db.scalars(select(ImportBatch).where(
                ImportBatch.kind == "otc", ImportBatch.label == batch.label,
                ImportBatch.status == "applied", ImportBatch.id != batch.id)):
            older.status = "superseded"
            older.message = f"Reemplazada por la importación #{batch.id}."
    else:
        report = excel_import.import_file(db, batch.kind, batch.content, batch.filename,
                                          batch_id=batch.id)
        batch.rows_loaded = report["inserted"]
        batch.message = report["message"]
    batch.status, batch.applied_at, batch.content = "applied", _now(), None
    db.commit()
    return batch


def discard(db: Session, batch: ImportBatch) -> ImportBatch:
    if batch.status != "preview":
        raise ImportError_("Solo se descarta una vista previa.")
    batch.status, batch.content = "discarded", None
    db.commit()
    return batch


def apply_now(db: Session, kind: str, filename: str, content: bytes, user) -> ImportBatch:
    """Preview and confirm in one step (the old one-click import), with history."""
    return confirm(db, preview(db, kind, filename, content, user), user)


# ------------------------------------------------------------- reconcile

def _rows(db: Session, batch: ImportBatch) -> list:
    model = Expense if batch.kind == "expenses" else Order
    return list(db.scalars(select(model).where(
        model.extra["import_batch"].as_integer() == batch.id)).all())


def _now_fingerprint(batch: ImportBatch, row) -> str:
    extra = row.extra if isinstance(row.extra, dict) else {}
    if batch.kind == "otc":
        return fingerprint.order_otc(row.date, row.customer, row.amount, row.gateway, extra)
    if batch.kind == "orders":
        return fingerprint.order_plain(row.date, row.customer, row.amount, row.currency, row.status)
    return fingerprint.expense_plain(row.date, row.description, row.amount, row.currency,
                                     row.category)


def _f(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def reconcile(db: Session, batch: ImportBatch) -> dict:
    """File totals vs the panel now, month by month, plus rows changed since."""
    if batch.status not in ("applied", "superseded"):
        return {"status": batch.status, "months": [], "changed": [], "ok": None}
    rows = _rows(db, batch)
    panel: dict[str, dict] = defaultdict(lambda: {"rows": 0, "volume": 0.0, "spread": 0.0,
                                                  "cost": 0.0, "profit": 0.0})
    changed = []
    for row in rows:
        extra = row.extra if isinstance(row.extra, dict) else {}
        month = panel[row.date[:7]]
        month["rows"] += 1
        if batch.kind == "otc":
            month["volume"] += _f(row.amount)
            month["spread"] += _f(extra.get("spread_bruto"))
            month["cost"] += _f(extra.get("costo_proveedor"))
            month["profit"] += _f(extra.get("utilidad"))
        else:
            month["volume"] += finance.usd_eq(row.amount, row.currency)
        if extra.get("huella") and extra["huella"] != _now_fingerprint(batch, row):
            changed.append({"id": row.id, "fila": extra.get("fila_hoja") or extra.get("fila"),
                            "fecha": row.date})
    metrics = ("rows",) + (MONEY if batch.kind == "otc" else ("volume",))
    months, all_ok = [], True
    for month in sorted(set(batch.totals or {}) | set(panel)):
        file_side = (batch.totals or {}).get(month, {})
        panel_side = panel.get(month, {})
        line = {"month": month}
        for metric in metrics:
            expected, found = _f(file_side.get(metric)), round(_f(panel_side.get(metric)), 2)
            ok = (expected == found) if metric == "rows" else abs(expected - found) <= TOLERANCE
            line[metric] = {"file": round(expected, 2), "panel": found, "ok": ok}
            all_ok = all_ok and ok
        months.append(line)
    file_rows = sum(int(_f(t.get("rows"))) for t in (batch.totals or {}).values())
    return {
        "status": batch.status, "metrics": list(metrics), "months": months,
        "rows_file": file_rows, "rows_panel": len(rows),
        "missing": max(file_rows - len(rows), 0), "changed": changed,
        "ok": all_ok and not changed if batch.status == "applied" else None,
    }


# ------------------------------------------------------------------- views

def view(batch: ImportBatch, detail: bool = False) -> dict:
    exceptions = batch.exceptions or {}
    out = {
        "id": batch.id, "kind": batch.kind, "label": batch.label, "filename": batch.filename,
        "file_sha256": batch.file_sha256, "status": batch.status,
        "uploaded_by": batch.uploaded_by, "uploaded_at": batch.uploaded_at,
        "applied_at": batch.applied_at, "rows_read": batch.rows_read,
        "rows_valid": batch.rows_valid, "rows_rejected": batch.rows_rejected,
        "rows_loaded": batch.rows_loaded, "rows_replaced": batch.rows_replaced,
        "rows_removed": batch.rows_removed, "message": batch.message,
        "exception_counts": {key: len(value) for key, value in exceptions.items()},
    }
    if detail:
        out["totals"] = batch.totals or {}
        out["exceptions"] = {key: value[:SHOWN] for key, value in exceptions.items()}
    return out


def history(db: Session, limit: int = 50) -> list[ImportBatch]:
    return list(db.scalars(select(ImportBatch).where(ImportBatch.status != "discarded")
                           .order_by(ImportBatch.id.desc()).limit(max(1, min(limit, 200)))).all())


def exceptions_xlsx(db: Session, batch: ImportBatch) -> bytes:
    """Reconciliation and every exception as a workbook for the finance team."""
    import pandas as pd

    result = reconcile(db, batch)
    exceptions = batch.exceptions or {}
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        summary = [{"Campo": k, "Valor": v} for k, v in (
            ("Importación", f"#{batch.id} · {batch.label or batch.kind}"),
            ("Archivo", batch.filename), ("SHA-256", batch.file_sha256),
            ("Subido por", batch.uploaded_by), ("Subido", batch.uploaded_at),
            ("Aplicado", batch.applied_at), ("Estado", batch.status),
            ("Filas leídas", batch.rows_read), ("Filas válidas", batch.rows_valid),
            ("Filas rechazadas", batch.rows_rejected), ("Filas cargadas", batch.rows_loaded),
            ("Reemplazadas", batch.rows_replaced), ("Eliminadas", batch.rows_removed),
            ("Cuadra", {True: "Sí", False: "No", None: "—"}[result.get("ok")]))]
        pd.DataFrame(summary).to_excel(writer, sheet_name="Resumen", index=False)
        lines = []
        for month in result.get("months", []):
            line = {"Mes": month["month"]}
            for metric in result.get("metrics", []):
                line[f"{metric} archivo"] = month[metric]["file"]
                line[f"{metric} panel"] = month[metric]["panel"]
                line[f"{metric} ok"] = "✓" if month[metric]["ok"] else "✗"
            lines.append(line)
        pd.DataFrame(lines or [{"Mes": "—"}]).to_excel(writer, sheet_name="Conciliación", index=False)
        for key, title in (("rechazadas", "Rechazadas"), ("fechas_corregidas", "Fechas corregidas"),
                           ("posibles_duplicados", "Posibles duplicados")):
            frame = pd.DataFrame(exceptions.get(key) or [{"sin registros": ""}])
            frame.to_excel(writer, sheet_name=title, index=False)
        pd.DataFrame(result.get("changed") or [{"sin cambios": ""}]).to_excel(
            writer, sheet_name="Cambiadas después", index=False)
    return buffer.getvalue()
