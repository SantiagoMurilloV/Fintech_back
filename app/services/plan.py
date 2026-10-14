"""The company's accounting plan (plan de cuentas): validation, storage, templates.

A plan is a tree of accounts. Inputs are boxes someone fills; totals are
recipes of other accounts; groups only organise. Nothing here knows any
company's accounts: a template is a JSON file in app/plans/, and a plan edited
on screen is just rows. The statement engine (services/statement.py) reads
whatever plan is stored.

Every plan is validated whole before it is written, so the stored tree can
always be evaluated: keys are unique, parents exist, totals carry a recipe
that only names known accounts, and no recipe depends on itself.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import Account, AccountValue
from . import formula as formula_service

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "plans"

KINDS = ("input", "total", "group")
UNITS = ("usd", "pct", "bps")
NATURES = ("income", "cost", "result")
SOURCES = ("manual",)

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,59}$")
MAX_ACCOUNTS = 500

FIELDS = ("key", "name", "parent_key", "position", "kind", "formula", "unit", "nature",
          "percent_of", "headline", "definition", "aliases", "source", "rule")


class PlanError(ValueError):
    """The plan cannot be stored; `problems` names every reason, in Spanish."""

    def __init__(self, problems: list[str]):
        super().__init__(" ".join(problems))
        self.problems = problems


# ------------------------------------------------------------------- reading

def accounts(db: Session) -> list[Account]:
    """Every account, in tree order: parents before children, by position."""
    rows = db.scalars(select(Account)).all()
    return tree_order(rows)


def tree_order(rows: list) -> list:
    """Depth-first order of a flat list of accounts (models or dicts)."""
    def attr(row, name):
        return row[name] if isinstance(row, dict) else getattr(row, name)

    children: dict[str | None, list] = {}
    keys = {attr(row, "key") for row in rows}
    for row in rows:
        parent = attr(row, "parent_key")
        children.setdefault(parent if parent in keys else None, []).append(row)
    for siblings in children.values():
        siblings.sort(key=lambda row: (attr(row, "position") or 0, attr(row, "key")))

    ordered: list = []

    def walk(parent):
        for row in children.get(parent, []):
            ordered.append(row)
            walk(attr(row, "key"))

    walk(None)
    return ordered


def view(account: Account, level: int = 0) -> dict:
    data = {field: getattr(account, field) for field in FIELDS}
    data["aliases"] = list(account.aliases or [])
    data["level"] = level
    return data


def levels(rows: list) -> dict[str, int]:
    """Depth of each account in the tree (roots are 0)."""
    def attr(row, name):
        return row[name] if isinstance(row, dict) else getattr(row, name)

    parents = {attr(row, "key"): attr(row, "parent_key") for row in rows}
    depth: dict[str, int] = {}
    for key in parents:
        level, current, seen = 0, parents.get(key), {key}
        while current in parents and current not in seen:
            seen.add(current)
            level += 1
            current = parents.get(current)
        depth[key] = level
    return depth


def public(db: Session) -> dict:
    rows = accounts(db)
    depth = levels(rows)
    return {
        "accounts": [view(row, depth.get(row.key, 0)) for row in rows],
        "inputs": sum(1 for row in rows if row.kind == "input"),
        "totals": sum(1 for row in rows if row.kind == "total"),
    }


# ---------------------------------------------------------------- validation

def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def fold(label: str) -> str:
    """A label as imports compare it: no case, accents, punctuation or extra
    spaces, so «Other Costs & Expenses», «other costs/expenses» and the key
    «other_costs_expenses» are the same line."""
    text = unicodedata.normalize("NFD", str(label or "").strip().lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def normalise(items: list[dict]) -> list[dict]:
    """Clean every account and reject the plan if any rule is broken."""
    if not isinstance(items, list):
        raise PlanError(["El plan debe ser una lista de cuentas."])
    if len(items) > MAX_ACCOUNTS:
        raise PlanError([f"El plan admite como máximo {MAX_ACCOUNTS} cuentas."])

    problems: list[str] = []
    cleaned: list[dict] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            problems.append(f"La cuenta #{index + 1} no es válida.")
            continue
        key = _text(raw.get("key"), 60).lower()
        name = _text(raw.get("name"), 120)
        label = f"«{name or key or f'#{index + 1}'}»"
        if not KEY_RE.fullmatch(key):
            problems.append(f"{label}: la clave debe empezar con letra y usar solo minúsculas, "
                            "números y guion bajo (ej. «gastos_fijos»).")
        elif key in formula_service._FUNCTIONS:
            problems.append(f"{label}: «{key}» es el nombre de una función; use otra clave.")
        if not name:
            problems.append(f"{label}: falta el nombre.")
        kind = _text(raw.get("kind") or "input", 10)
        if kind not in KINDS:
            problems.append(f"{label}: el tipo debe ser entrada, total o grupo.")
        unit = _text(raw.get("unit") or "usd", 10)
        if unit not in UNITS:
            problems.append(f"{label}: la unidad debe ser USD, % o pb.")
        nature = _text(raw.get("nature") or ("result" if kind == "total" else "cost"), 10)
        if nature not in NATURES:
            problems.append(f"{label}: la naturaleza debe ser ingreso, costo o resultado.")
        source = _text(raw.get("source") or "manual", 20)
        if source not in SOURCES:
            problems.append(f"{label}: por ahora la cifra se carga a mano o por archivo.")
        aliases = raw.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [part for part in aliases.split(",")]
        aliases = [alias for alias in (_text(a, 120) for a in aliases) if alias][:20]
        try:
            position = int(raw.get("position") if raw.get("position") is not None else index)
        except (TypeError, ValueError):
            position = index
        cleaned.append({
            "key": key, "name": name, "kind": kind, "unit": unit, "nature": nature,
            "parent_key": _text(raw.get("parent_key"), 60).lower() or None,
            "position": position,
            "formula": _text(raw.get("formula"), formula_service.MAX_LENGTH) or None,
            "percent_of": _text(raw.get("percent_of"), 60).lower() or None,
            "headline": bool(raw.get("headline")),
            "definition": _text(raw.get("definition"), 2000) or None,
            "aliases": aliases, "source": source, "rule": None,
        })

    names = {row["key"]: row["name"] for row in cleaned}
    seen: set[str] = set()
    for row in cleaned:
        label = f"«{row['name'] or row['key']}»"
        if row["key"] in seen:
            problems.append(f"{label}: la clave «{row['key']}» está repetida.")
        seen.add(row["key"])
        if row["parent_key"] and row["parent_key"] not in names:
            problems.append(f"{label}: la cuenta madre «{row['parent_key']}» no existe.")
        if row["parent_key"] == row["key"]:
            problems.append(f"{label}: una cuenta no puede ser madre de sí misma.")
        if row["percent_of"] and row["percent_of"] not in names:
            problems.append(f"{label}: «% de» apunta a «{row['percent_of']}», que no existe.")
        if row["kind"] == "total":
            if not row["formula"]:
                problems.append(f"{label}: un total necesita su fórmula (ej. «renta + nomina»).")
                continue
            try:
                formula_service.validate(row["formula"])
                used = formula_service.names(row["formula"])
            except formula_service.FormulaError as err:
                problems.append(f"{label}: fórmula inválida. {err}")
                continue
            unknown = sorted(used - set(names))
            if unknown:
                problems.append(f"{label}: la fórmula usa cuentas que no existen: "
                                f"{', '.join(unknown)}.")
            groups = sorted(key for key in used & set(names)
                            if next(r for r in cleaned if r["key"] == key)["kind"] == "group")
            if groups:
                problems.append(f"{label}: un grupo no tiene valor; la fórmula no puede usar "
                                f"{', '.join(groups)}.")
        elif row["formula"]:
            what = "una entrada se llena con cifras" if row["kind"] == "input" else "un grupo solo organiza"
            problems.append(f"{label}: {what}; quite la fórmula o cámbiela a total.")

    # An import finds each line by its name or an alias: two accounts answering
    # to the same label would make a figure land on a guess.
    owners: dict[str, str] = {}
    for row in cleaned:
        for label in {fold(row["name"]), *(fold(alias) for alias in row["aliases"])} - {""}:
            other = owners.setdefault(label, row["key"])
            if other != row["key"]:
                problems.append(f"«{row['name']}» y «{names.get(other, other)}» se llaman igual "
                                f"(«{label}»); cambie el nombre o el alias de una.")

    problems.extend(_cycles(cleaned))
    problems.extend(_parent_loops(cleaned))
    if problems:
        raise PlanError(problems)
    return cleaned


def dependencies(rows: list[dict]) -> dict[str, set[str]]:
    """Total -> the accounts its recipe reads."""
    deps: dict[str, set[str]] = {}
    for row in rows:
        if row["kind"] == "total" and row.get("formula"):
            try:
                deps[row["key"]] = formula_service.names(row["formula"])
            except SyntaxError:
                deps[row["key"]] = set()
    return deps


def evaluation_order(rows: list[dict]) -> list[str]:
    """Totals sorted so each one comes after everything its recipe reads."""
    deps = dependencies(rows)
    order: list[str] = []
    state: dict[str, int] = {}          # 1 = visiting, 2 = done

    def visit(key: str):
        if state.get(key) == 2 or key not in deps:
            return
        if state.get(key) == 1:
            raise _Cycle(key)
        state[key] = 1
        for name in deps[key]:
            visit(name)
        state[key] = 2
        order.append(key)

    for key in deps:
        visit(key)
    return order


class _Cycle(PlanError):
    def __init__(self, key: str):
        super().__init__([f"La fórmula de «{key}» termina dependiendo de sí misma."])
        self.key = key


def _cycles(rows: list[dict]) -> list[str]:
    names = {row["key"]: row["name"] for row in rows}
    try:
        evaluation_order(rows)
    except _Cycle as cycle:
        return [f"«{names.get(cycle.key, cycle.key)}»: su fórmula termina dependiendo "
                "de sí misma."]
    return []


def _parent_loops(rows: list[dict]) -> list[str]:
    parents = {row["key"]: row["parent_key"] for row in rows}
    problems = []
    for key in parents:
        current, seen = parents[key], {key}
        while current:
            if current in seen:
                problems.append(f"«{key}»: la jerarquía da la vuelta sobre sí misma.")
                break
            seen.add(current)
            current = parents.get(current)
    return problems


# ------------------------------------------------------------------ writing

def replace(db: Session, items: list[dict]) -> dict:
    """Store a whole plan. Figures of inputs that disappear are dropped too."""
    cleaned = normalise(items)
    kept_inputs = {row["key"] for row in cleaned if row["kind"] == "input"}

    db.execute(delete(Account))
    for row in cleaned:
        db.add(Account(**row))
    query = select(AccountValue)
    if kept_inputs:
        query = query.where(AccountValue.account_key.not_in(kept_inputs))
    orphan = db.scalars(query).all()
    for value in orphan:
        db.delete(value)
    db.commit()
    return {**public(db), "dropped_values": len(orphan)}


# ---------------------------------------------------------------- templates

def templates() -> list[dict]:
    """The plans shipped as starting points, one JSON file each."""
    found = []
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        found.append({"id": path.stem, "name": data.get("name") or path.stem,
                      "description": data.get("description") or "",
                      "accounts": len(data.get("accounts") or [])})
    return found


def load_template(db: Session, template_id: str, replace_existing: bool = False) -> dict:
    if not KEY_RE.fullmatch(template_id or ""):
        raise PlanError(["Plantilla desconocida."])
    path = TEMPLATES_DIR / f"{template_id}.json"
    if not path.is_file():
        raise PlanError(["Plantilla desconocida."])
    if db.scalars(select(Account).limit(1)).first() and not replace_existing:
        raise PlanError(["Ya hay un plan cargado. Confirme que quiere reemplazarlo."])
    data = json.loads(path.read_text(encoding="utf-8"))
    return replace(db, data.get("accounts") or [])
