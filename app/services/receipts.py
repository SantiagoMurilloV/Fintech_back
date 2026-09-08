"""Expense voucher generation.

One place builds the PDF from the row's own data, keeps it in the report
library and attaches it to the expense, so the agent tool and the REST
endpoint behind the expenses table (the magic pencil) produce exactly the
same document.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..agent.formatting import amount, short_date, usd_compact
from ..models import Expense, Report
from . import finance, pdf, storage


def generate(db: Session, expense: Expense) -> dict:
    """Build the voucher and return raw facts only — no presentation.

    The PDF lands in the report library; an expense without a receipt adopts
    it as its official one, so it shows up in the table column too.
    """
    usd = finance.usd_eq(expense.amount, expense.currency)
    filename = f"comprobante_{expense.id}.pdf"
    content = pdf.receipt({
        "title": f"Comprobante de gasto #{expense.id}",
        "subtitle": f"{expense.description} · {short_date(expense.date)}",
        "fields": [
            ["Concepto", expense.description],
            ["Categoría", expense.category],
            ["Proveedor", expense.vendor or "—"],
            ["Responsable", expense.owner or "—"],
            ["Monto", f"{amount(expense.amount, expense.currency)} {expense.currency}"],
            ["Equivalente", usd_compact(usd)],
            ["Fecha", expense.date],
        ],
        "file_url": expense.receipt_url,
    })

    stored = storage.save(content, filename, subfolder="receipts")
    saved = Report(title=f"Comprobante #{expense.id}", kind="receipt", period=expense.date[:7],
                   spec={"expense_id": expense.id}, pdf_url=stored["url"],
                   summary=expense.description,
                   created_at=datetime.now().isoformat(timespec="seconds"))
    db.add(saved)
    # Un gasto sin comprobante recibe este PDF como su comprobante oficial:
    # queda visible en la columna de la tabla, no solo en la biblioteca.
    attached = False
    if not expense.receipt_url:
        expense.receipt_name = filename
        expense.receipt_url = stored["url"]
        attached = True
    db.commit()

    return {"report_id": saved.id, "url": stored["url"], "filename": filename,
            "attached": attached}
