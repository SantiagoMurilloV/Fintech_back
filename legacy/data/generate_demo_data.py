"""
Synthetic demo data for the E2E close pipeline (no browser, no APIs).

Generates a bank statement and a books export for 2026-07 with realistic
discrepancies: bank fees not in books, a timing difference, an invoice
recorded in books but not yet in the bank, and one duplicate suspect.
"""

import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DOWNLOADS_DIR  # noqa: E402

random.seed(42)

PERIOD = "2026-07"
FIRST = date(2026, 7, 1)


def _rows():
    bank, books = [], []
    ref_counter = 1000

    # 40 clean matched transactions
    for i in range(40):
        d = FIRST + timedelta(days=random.randint(0, 29))
        amount = round(random.choice([1, -1]) * random.uniform(50_000, 8_000_000), 2)
        ref = f"TRX{ref_counter}"
        ref_counter += 1
        desc = random.choice(
            ["Pago cliente", "Pago proveedor", "Nómina", "Transferencia recibida", "PSE recaudo"]
        )
        bank.append((d, amount, desc, ref))
        books.append((d, amount, desc, ref))

    # 3 timing differences: books records 2 days before bank settles
    for i in range(3):
        d = FIRST + timedelta(days=random.randint(5, 25))
        amount = round(-random.uniform(200_000, 900_000), 2)
        ref = f"TRX{ref_counter}"
        ref_counter += 1
        bank.append((d + timedelta(days=2), amount, "Pago proveedor (compensado)", ref))
        books.append((d, amount, "Pago proveedor", ref))

    # 4 bank fees not recorded in books
    for i in range(4):
        d = FIRST + timedelta(days=random.randint(0, 29))
        bank.append((d, round(-random.uniform(8_000, 45_000), 2), "Comisión manejo cuenta", ""))

    # 2 invoices in books not yet collected in bank
    for i in range(2):
        d = FIRST + timedelta(days=random.randint(20, 29))
        ref = f"FV-{ref_counter}"
        ref_counter += 1
        books.append((d, round(random.uniform(1_000_000, 4_000_000), 2), "Factura de venta", ref))

    # 1 duplicate suspect in books
    d = FIRST + timedelta(days=12)
    books.append((d, -350_000.00, "Pago servicios", "TRX-DUP"))
    books.append((d, -350_000.00, "Pago servicios", "TRX-DUP"))
    bank.append((d, -350_000.00, "Pago servicios", "TRX-DUP"))

    return bank, books


def main():
    bank, books = _rows()

    bank_df = pd.DataFrame(
        [{"Fecha": d, "Valor": v, "Descripcion": desc, "Referencia": r} for d, v, desc, r in bank]
    ).sort_values("Fecha")
    books_df = pd.DataFrame(
        [{"fecha": d, "valor": v, "concepto": desc, "documento": r} for d, v, desc, r in books]
    ).sort_values("fecha")

    bank_path = DOWNLOADS_DIR / f"demo_banco_{PERIOD}.xlsx"
    books_path = DOWNLOADS_DIR / f"demo_libros_{PERIOD}.xlsx"
    bank_df.to_excel(bank_path, index=False)
    books_df.to_excel(books_path, index=False)
    print(f"✅ {bank_path} ({len(bank_df)} rows)")
    print(f"✅ {books_path} ({len(books_df)} rows)")


if __name__ == "__main__":
    main()
