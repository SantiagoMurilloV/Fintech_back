"""Move price data between databases: export to a gzip CSV, import it back.

Production does not collect prices on its own (PRICE_SCHEDULER off), so the
history and readings gathered elsewhere are carried over as a file:

    python -m app.services.rates_transfer export prices.csv.gz     # on the source
    POST /api/rates/import  (admin, the file)                       # on the target

Import is idempotent: a row whose (source, pair, taken_at) already exists is
skipped, so the same file can be sent twice and a later file adds only what
is new.
"""
from __future__ import annotations

import csv
import gzip
import io
import sys

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from ..models import RateSnapshot

FIELDS = ("source", "pair", "price", "bid", "ask", "rate_date", "taken_at")
CHUNK = 5000


def export(db: Session, out) -> int:
    """Write every snapshot as gzip CSV to the binary file `out`; returns rows written."""
    written = 0
    with gzip.open(out, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        for row in db.execute(select(*(getattr(RateSnapshot, f) for f in FIELDS))
                              .order_by(RateSnapshot.id)).yield_per(CHUNK):
            writer.writerow(["" if value is None else value for value in row])
            written += 1
    return written


def _number(text: str):
    return float(text) if text not in ("", None) else None


def _insert_ignoring_duplicates(db: Session, rows: list[dict]) -> None:
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        db.execute(pg_insert(RateSnapshot).values(rows).on_conflict_do_nothing(
            index_elements=["source", "pair", "taken_at"]))
    else:
        db.execute(insert(RateSnapshot).prefix_with("OR IGNORE"), rows)


def import_file(db: Session, content: bytes) -> dict:
    """Load a gzip CSV made by `export`; returns rows read and the table size after."""
    try:
        text = gzip.decompress(content).decode("utf-8")
    except OSError:
        raise ValueError("El archivo no es un .csv.gz exportado por el panel.") from None
    reader = csv.DictReader(io.StringIO(text))
    if tuple(reader.fieldnames or ()) != FIELDS:
        raise ValueError(f"Columnas esperadas: {', '.join(FIELDS)}.")
    from sqlalchemy import func
    before = db.scalar(select(func.count(RateSnapshot.id))) or 0
    read, batch = 0, []
    for record in reader:
        batch.append({"source": record["source"], "pair": record["pair"],
                      "price": float(record["price"]), "bid": _number(record["bid"]),
                      "ask": _number(record["ask"]), "rate_date": record["rate_date"] or None,
                      "taken_at": record["taken_at"]})
        read += 1
        if len(batch) >= CHUNK:
            _insert_ignoring_duplicates(db, batch)
            db.commit()
            batch = []
    if batch:
        _insert_ignoring_duplicates(db, batch)
        db.commit()
    after = db.scalar(select(func.count(RateSnapshot.id))) or 0
    return {"read": read, "added": after - before, "skipped": read - (after - before),
            "total": after}


if __name__ == "__main__":
    from ..database import SessionLocal

    if len(sys.argv) != 3 or sys.argv[1] != "export":
        sys.exit("uso: python -m app.services.rates_transfer export <archivo.csv.gz>")
    with SessionLocal() as session, open(sys.argv[2], "wb") as target:
        print(f"{export(session, target)} filas exportadas a {sys.argv[2]}")
