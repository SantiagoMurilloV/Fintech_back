"""Monthly history from a spreadsheet: accounts down, months across.

Built to read the sheets finance teams already keep, not a format of ours:

  months    any column whose header reads as a month («2026-05», «May/2026»,
            «Mayo 2026», «05/2026», «Apr-26», a real date) and holds numbers.
  accounts  each row's label is the last text left of the first month column,
            matched to the plan by name, alias or key, ignoring case and
            accents. Every sheet of the workbook is read.
  figures   «$1,234.56», «1.234,56», «(18.00)» and «-$12,933» are numbers;
            «36.54%» is a ratio the statement recomputes, so it is skipped.

Inputs are stored. Totals are NOT stored, they are CHECKED: when the file says
«Operational Profit = X» and the plan's recipe gives Y, the report says so,
with the difference. That is how a statement with two versions of the same
month gets caught.

A figure two places in the file disagree on is not written at all: picking
one would be a guess, and a gap the report names is better than a guess.
"""
from __future__ import annotations

import io
import math
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime

import pandas as pd
from sqlalchemy.orm import Session

from ..agent import formatting as fmt
from . import plan as plan_service
from . import statement

HEADER_SCAN_ROWS = 40
MATCH_TOLERANCE = 0.01      # money: a cent
ROUNDING_TOLERANCE = 1.0    # a figure typed rounded to the unit

MONTH_WORDS = {
    "ene": 1, "enero": 1, "jan": 1, "january": 1,
    "feb": 2, "febrero": 2, "february": 2,
    "mar": 3, "marzo": 3, "march": 3,
    "abr": 4, "abril": 4, "apr": 4, "april": 4,
    "may": 5, "mayo": 5,
    "jun": 6, "junio": 6, "june": 6,
    "jul": 7, "julio": 7, "july": 7,
    "ago": 8, "agosto": 8, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "set": 9, "septiembre": 9, "september": 9,
    "oct": 10, "octubre": 10, "october": 10,
    "nov": 11, "noviembre": 11, "november": 11,
    "dic": 12, "diciembre": 12, "dec": 12, "december": 12,
}

_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


class ImportError_(ValueError):
    pass


# ------------------------------------------------------------------ parsing

def parse_period(cell) -> str | None:
    """A header cell as YYYY-MM, or None when it is not a month."""
    if isinstance(cell, (datetime, date, pd.Timestamp)):
        return f"{cell.year:04d}-{cell.month:02d}"
    if not isinstance(cell, str):
        return None
    text = unicodedata.normalize("NFD", cell.strip().lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn").replace(" de ", " ")
    patterns = (
        (r"^(\d{4})[-/.](\d{1,2})(?:[-/.]\d{1,2})?(?:[ t].*)?$", ("y", "m")),
        (r"^(\d{1,2})[-/.](\d{4})$", ("m", "y")),
        (r"^([a-z]+)\.?[\s/\-.]*(\d{4}|\d{2})$", ("w", "y")),
        (r"^(\d{4})[\s/\-.]*([a-z]+)\.?$", ("y", "w")),
    )
    for pattern, parts in patterns:
        found = re.match(pattern, text)
        if not found:
            continue
        values = dict(zip(parts, found.groups()))
        month = MONTH_WORDS.get(values["w"]) if "w" in values else int(values["m"])
        year = int(values["y"])
        year = year + 2000 if year < 100 else year
        if month and 1 <= month <= 12 and 1990 <= year <= 2100:
            return f"{year:04d}-{month:02d}"
    return None


def parse_number(cell) -> float | None:
    """A figure cell as a number; None for blanks, text and percentages."""
    if cell is None or isinstance(cell, bool):
        return None
    if isinstance(cell, (int, float)):
        return None if isinstance(cell, float) and math.isnan(cell) else float(cell)
    text = str(cell).strip()
    if not text or text in ("-", "—", "–") or text.endswith("%"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace("$", "").replace("USD", "").replace(" ", "").replace(" ", "")
    if text.startswith("-"):
        negative, text = True, text[1:]
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        text = text.replace(thousands, "").replace(decimal, ".")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        text = text.replace(",", "") if len(tail) == 3 and head else text.replace(",", ".")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    if not _NUMERIC.match(text):
        return None
    value = float(text)
    return -value if negative else value


def _is_text(cell) -> bool:
    return isinstance(cell, str) and cell.strip() != "" and parse_number(cell) is None \
        and not cell.strip().endswith("%")


def read_sheets(content: bytes, filename: str) -> dict[str, list[list]]:
    """Every sheet as a grid of raw cells."""
    try:
        if filename.lower().endswith(".csv"):
            frames = {"CSV": pd.read_csv(io.BytesIO(content), header=None, dtype=object)}
        else:
            frames = pd.read_excel(io.BytesIO(content), header=None, sheet_name=None, dtype=object)
    except Exception:
        raise ImportError_("No pude leer el archivo. Use .xlsx, .xls o .csv.") from None
    grids = {}
    for name, frame in frames.items():
        grid = frame.where(pd.notna(frame), None).values.tolist()
        if grid:
            grids[str(name)] = grid
    if not grids:
        raise ImportError_("El archivo no tiene filas.")
    return grids


def _month_columns(grid: list[list]) -> tuple[int, dict[int, str]]:
    """Header row index and {column: period} for the columns that hold figures."""
    for index, row in enumerate(grid[:HEADER_SCAN_ROWS]):
        candidates = {col: parse_period(cell) for col, cell in enumerate(row)}
        candidates = {col: period for col, period in candidates.items() if period}
        if not candidates:
            continue
        # A header that reads as a month but sits over labels is not a month column.
        holding = {col: period for col, period in candidates.items()
                   if any(parse_number(r[col] if col < len(r) else None) is not None
                          for r in grid[index + 1:])}
        if holding:
            return index, holding
    return -1, {}


# ------------------------------------------------------------------- import

def run(db: Session, content: bytes, filename: str, actor: str | None,
        dry_run: bool = False) -> dict:
    """Read a history workbook; store inputs and check totals. Returns the report."""
    accounts = [plan_service.view(row) for row in plan_service.accounts(db)]
    if not accounts:
        raise ImportError_("Primero cargue un plan de cuentas.")
    lookup: dict[str, dict] = {}
    for account in accounts:
        for label in (account["name"], account["key"], *account["aliases"]):
            lookup.setdefault(plan_service.fold(label), account)

    figures: dict[tuple[str, str], list[tuple[float, str]]] = defaultdict(list)
    unknown: dict[str, str] = {}
    sheets, percent_cells, unlabeled = [], 0, 0

    for sheet, grid in read_sheets(content, filename).items():
        header, months = _month_columns(grid)
        if not months:
            sheets.append({"name": sheet, "months": [], "rows": 0})
            continue
        first_month = min(months)
        rows_read = 0
        for offset, row in enumerate(grid[header + 1:], start=header + 2):
            label = next((cell.strip() for cell in reversed(row[:first_month])
                          if _is_text(cell)), None)
            cells = {col: (row[col] if col < len(row) else None) for col in months}
            numbers = {col: parse_number(cell) for col, cell in cells.items()}
            percent_cells += sum(1 for cell in cells.values()
                                 if isinstance(cell, str) and cell.strip().endswith("%"))
            if not any(value is not None for value in numbers.values()):
                continue
            if not label:
                unlabeled += 1
                continue
            account = lookup.get(plan_service.fold(label))
            if account is None:
                unknown.setdefault(label, f"{sheet}, fila {offset}")
                continue
            if account["kind"] == "group":
                continue
            rows_read += 1
            for col, value in numbers.items():
                if value is not None:
                    figures[(account["key"], months[col])].append((value, f"{sheet}, fila {offset}"))
        sheets.append({"name": sheet, "months": sorted(set(months.values())), "rows": rows_read})

    if not figures:
        raise ImportError_(
            "No encontré cifras: revise que haya una fila de meses (ej. «2026-05» o «May/2026») "
            "y una columna con el nombre de cada cuenta.")

    by_key = {account["key"]: account for account in accounts}
    inputs: dict[tuple[str, str], float] = {}
    checks: dict[tuple[str, str], list[tuple[float, str]]] = {}
    conflicts = []
    rounded_copies = 0
    for (key, period), seen in sorted(figures.items()):
        values = [value for value, _ in seen]
        if by_key[key]["kind"] == "total":
            checks[(key, period)] = seen
        elif max(values) - min(values) <= MATCH_TOLERANCE:
            inputs[(key, period)] = values[0]
        elif max(values) - min(values) <= ROUNDING_TOLERANCE:
            # 164 in one sheet and 164.23 in another is the same figure, one of
            # them rounded for display: keep the precise one, say so.
            inputs[(key, period)] = max(values, key=_precision)
            rounded_copies += 1
        else:
            conflicts.append({"account": by_key[key]["name"], "key": key, "period": period,
                              "values": [{"value": value, "where": where} for value, where in seen]})

    periods = sorted({period for _, period in figures})
    stored = statement.values_for(db, periods)
    written = updated = unchanged = 0
    for (key, period), value in inputs.items():
        before = stored.get(period, {}).get(key)
        if before is None:
            written += 1
        elif abs(before - value) > 1e-9:
            updated += 1
        else:
            unchanged += 1

    matched, rounding, mismatches, unverified = 0, [], [], []
    for period in periods:
        merged = {**stored.get(period, {}),
                  **{key: value for (key, p), value in inputs.items() if p == period}}
        computed = statement.evaluate(accounts, merged)
        for (key, p), seen in checks.items():
            if p != period:
                continue
            ours = computed[key]["value"]
            # A recipe missing some of its figures cannot vouch for a total:
            # comparing would blame the total for the gap.
            if computed[key]["status"] != "ok":
                unverified.append({"account": by_key[key]["name"], "key": key, "period": period})
                continue
            for value, where in seen:
                entry = {"account": by_key[key]["name"], "key": key, "period": period,
                         "file": value, "computed": ours, "where": where,
                         "difference": round(value - ours, 2)}
                if abs(value - ours) <= MATCH_TOLERANCE:
                    matched += 1
                elif abs(value - ours) <= ROUNDING_TOLERANCE:
                    rounding.append(entry)
                else:
                    mismatches.append(entry)

    if not dry_run and inputs:
        statement.set_values(db, [{"account_key": key, "period": period, "amount": value}
                                  for (key, period), value in inputs.items()],
                             actor=actor, origin=f"import:{filename}"[:160])

    return {
        "dry_run": dry_run, "file": filename, "sheets": sheets, "periods": periods,
        "written": written, "updated": updated, "unchanged": unchanged,
        "checked": matched + len(rounding) + len(mismatches), "matched": matched,
        "rounding": rounding[:50], "mismatches": mismatches[:50], "conflicts": conflicts[:50],
        "unverified": unverified[:50],
        "unknown": [{"label": label, "where": where} for label, where in list(unknown.items())[:30]],
        "notes": _notes(percent_cells, unlabeled, rounding, rounded_copies, unverified),
        "problems": _problems(conflicts, mismatches, unknown),
    }


def _precision(value: float) -> int:
    """Decimals a figure was written with: 164.23 beats its rounded copy 164."""
    text = f"{value:.6f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def _notes(percent_cells: int, unlabeled: int, rounding: list, rounded_copies: int,
           unverified: list) -> list[str]:
    notes = []
    if percent_cells:
        notes.append(f"Omití {percent_cells} celdas con porcentaje: el estado los recalcula.")
    if unlabeled:
        notes.append(f"Omití {unlabeled} filas con cifras pero sin nombre de cuenta.")
    if rounding:
        notes.append(f"{len(rounding)} totales del archivo difieren en menos de 1 USD de la "
                     "fórmula: cifras redondeadas.")
    if rounded_copies:
        notes.append(f"{rounded_copies} cifras aparecen redondeadas en otra hoja; usé la más "
                     "precisa.")
    if unverified:
        shown = ", ".join(f"{item['account']} {item['period']}" for item in unverified[:6])
        notes.append(f"No pude verificar {len(unverified)} totales porque les faltan cifras: "
                     f"{shown}" + ("…" if len(unverified) > 6 else "") + ".")
    return notes


def _problems(conflicts: list, mismatches: list, unknown: dict) -> list[str]:
    problems = []
    for item in conflicts[:10]:
        shown = " · ".join(f"{fmt.thousands(v['value'], 2)} ({v['where']})" for v in item["values"])
        problems.append(f"{item['account']} {item['period']}: el archivo trae cifras distintas "
                        f"({shown}). No la guardé; corrija la que no va.")
    for item in mismatches[:10]:
        problems.append(
            f"{item['account']} {item['period']}: el archivo dice {fmt.thousands(item['file'], 2)}, "
            f"la fórmula da {fmt.thousands(item['computed'], 2)} "
            f"(diferencia {fmt.thousands(item['difference'], 2)}; {item['where']}).")
    if unknown:
        problems.append(f"Cuentas que no están en el plan: {', '.join(list(unknown)[:8])}"
                        + ("…" if len(unknown) > 8 else "")
                        + ". Agréguelas al plan o como alias, y vuelva a cargar.")
    return problems


# ----------------------------------------------------------------- template

def template_xlsx(db: Session, first: str | None, last: str | None) -> bytes:
    """Inputs × months, with the figures already stored, ready to fill."""
    data = statement.grid(db, first, last)
    rows = []
    for account in data["accounts"]:
        label = ("  " * account["level"]) + account["name"]
        row = {"Cuenta": label}
        for period in data["periods"]:
            value = data["values"].get(period, {}).get(account["key"])
            row[period] = value if account["kind"] == "input" else None
        rows.append(row)
    frame = pd.DataFrame(rows, columns=["Cuenta", *data["periods"]])
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False, sheet_name="Cifras")
    return buffer.getvalue()
