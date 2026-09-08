"""Reconciliation workbook: one sheet per section, plain pandas/openpyxl."""

from pathlib import Path

import pandas as pd

from close.models import CloseReport
from config.settings import OUTPUT_DIR


def _movs_to_df(movs) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "fecha": m.txn_date,
                "valor": m.amount,
                "descripcion": m.description,
                "referencia": m.reference,
                "categoria": m.category,
                "match_id": m.match_id,
            }
            for m in movs
        ]
    )


def write_reconciliation_xlsx(report: CloseReport) -> Path:
    dest = OUTPUT_DIR / f"conciliacion_{report.account}_{report.period}.xlsx"

    pairs = report.match.matched_pairs
    matched_rows = []
    for bank_m, books_m in pairs:
        matched_rows.append(
            {
                "match_id": bank_m.match_id,
                "fecha_banco": bank_m.txn_date,
                "fecha_libros": books_m.txn_date,
                "valor": bank_m.amount,
                "descripcion_banco": bank_m.description,
                "descripcion_libros": books_m.description,
                "referencia": bank_m.reference or books_m.reference,
            }
        )

    summary = pd.DataFrame(
        [
            {"concepto": "Período", "valor": report.period},
            {"concepto": "Cuenta", "valor": report.account},
            {"concepto": "Total banco", "valor": f"{report.bank_total:,.2f}"},
            {"concepto": "Total libros", "valor": f"{report.books_total:,.2f}"},
            {"concepto": "Diferencia", "valor": f"{report.difference:,.2f}"},
            {"concepto": "Conciliados", "valor": len(pairs)},
            {"concepto": "Pendientes banco", "valor": len(report.match.unmatched_bank)},
            {"concepto": "Pendientes libros", "valor": len(report.match.unmatched_books)},
            {"concepto": "% conciliación", "valor": f"{report.match.match_rate:.0%}"},
        ]
        + [
            {"concepto": f"Check: {c.name}", "valor": ("OK — " if c.passed else "FALLA — ") + c.detail}
            for c in report.checks
        ]
    )

    with pd.ExcelWriter(dest, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Resumen", index=False)
        pd.DataFrame(matched_rows).to_excel(writer, sheet_name="Conciliados", index=False)
        _movs_to_df(report.match.unmatched_bank).to_excel(
            writer, sheet_name="Pendientes Banco", index=False
        )
        _movs_to_df(report.match.unmatched_books).to_excel(
            writer, sheet_name="Pendientes Libros", index=False
        )

    print(f"📊 Excel de conciliación: {dest}")
    return dest
