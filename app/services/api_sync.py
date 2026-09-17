"""Pull sync: bring orders and expenses from the external endpoint.

Idempotent by design: every imported record carries the source and its
external id in `extra`, so running the sync twice updates instead of
duplicating. Records the mapping cannot place (no amount, no usable fields)
are skipped and counted, never invented — the report says exactly what came
in, what was updated and what was left out, and that report is what the
settings screen shows after each run.

A paginated source (DRF-style `count`/`next`/`results`) is walked page by
page until the whole feed is in, and each path setting accepts several
comma-separated routes — one feed per route, each with its own report entry.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CustomColumn, Expense, Order, Setting
from . import columns as columns_service
from . import finance, intake
from . import mapping
from . import settings as settings_service
from .external_api import auth_headers

log = logging.getLogger("api_sync")

TIMEOUT = 30
PAGE_SIZE = 200           # asked per request; sources that don't paginate ignore it
MAX_RECORDS = 5000        # a runaway feed must not swallow the panel
WRAPPER_KEYS = ("items", "data", "results", "records", "rows")


class SyncError(RuntimeError):
    pass


def split_paths(value) -> list[str]:
    """The configured path(s): one string, several separated by comma/newline."""
    return [p.strip() for p in re.split(r"[,\n]+", str(value or "")) if p.strip()]


def path_slug(path: str) -> str:
    """Short stable name of a feed, from the last segment of its path."""
    route = path.partition("?")[0].strip().strip("/")
    segment = route.rsplit("/", 1)[-1]
    clean = re.sub(r"[^a-z0-9]+", "-", segment.lower()).strip("-")
    return clean or "feed"


def _fetch_list(base_url: str, token: str, path: str,
                scheme: str = "bearer") -> tuple[list[dict], dict]:
    """All records behind a path, following DRF-style pagination.

    Returns (records, info): info reports the pages walked, the count the
    source declared and whether MAX_RECORDS cut the walk short.
    """
    headers = {"Accept": "application/json", **auth_headers(token, scheme)}
    route, _, query = (path or "").partition("?")
    params = dict(httpx.QueryParams(query))
    params.setdefault("page_size", str(PAGE_SIZE))

    records: list[dict] = []
    info = {"pages": 0, "source_count": None, "truncated": False}
    try:
        with httpx.Client(base_url=base_url, headers=headers, timeout=TIMEOUT) as client:
            while True:
                response = client.get(route, params=params)
                if not response.is_success:
                    raise SyncError(f"{path} respondió {response.status_code}.")
                try:
                    payload = response.json()
                except ValueError:
                    raise SyncError(f"{path} devolvió algo que no es JSON.") from None

                page = _unwrap(payload)
                if page is None:
                    raise SyncError(f"{path} devolvió un objeto sin lista adentro.")
                records.extend(r for r in page if isinstance(r, dict))
                info["pages"] += 1

                next_url = payload.get("next") if isinstance(payload, dict) else None
                if isinstance(payload, dict) and isinstance(payload.get("count"), int):
                    info["source_count"] = payload["count"]
                if not next_url or not page:
                    break
                if len(records) >= MAX_RECORDS:
                    info["truncated"] = True
                    break
                # Follow only the query string of `next`: behind a proxy its
                # scheme/host can disagree with base_url; the params never do.
                params = dict(httpx.URL(str(next_url)).params)
    except httpx.HTTPError as err:
        raise SyncError(f"No pude contactar {path}: {err}") from None
    return records[:MAX_RECORDS], info


def _unwrap(payload):
    """The list of records inside a payload, wherever the API put it.

    Bare list first; then the conventional wrappers (items, data, results…);
    then any key whose value is a list of objects — DummyJSON says
    {"carts": [...]}, Stripe says {"data": [...]}: the name varies, the shape
    does not. With several list keys the longest wins.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in WRAPPER_KEYS:
        if isinstance(payload.get(key), list):
            return payload[key]
    candidates = [value for value in payload.values()
                  if isinstance(value, list) and value and isinstance(value[0], dict)]
    if candidates:
        return max(candidates, key=len)
    return None


def _key(entity: str, slug: str, external_id) -> str:
    """Identity of an imported record: kind, feed and external id.

    The feed slug is part of it so two feeds reusing the same id
    (deposit 5, withdrawal 5) never collide with each other.
    """
    return f"api:{entity}:{slug}:{external_id}"


def _order_row_id(key: str, slug: str, external_id) -> str:
    """A primary key that fits Order.id (20 chars), readable when it can be."""
    readable = f"EXT-{slug}-{external_id}"
    if len(readable) <= 20:
        return readable
    return "EXT-" + hashlib.sha1(key.encode()).hexdigest()[:16]


# ------------------------------------------------------------ dynamic columns

def raw_record(record: dict):
    """The source record, kept verbatim so nothing is ever lost.

    Capped only when absurdly large; the cap is reported, never silent.
    """
    try:
        if len(json.dumps(record, ensure_ascii=False)) <= 8000:
            return record
    except (TypeError, ValueError):
        pass
    return {"_truncated": True, "_preview": str(record)[:2000]}


def verify_copy(db: Session, entity: str, expected: dict[str, tuple[float, str]]) -> dict:
    """Prove the copy: what the database holds equals what the source sent.

    Compares, record by record and against fresh rows from the database, the
    exact amount and currency that were parsed from the feed. Any difference
    is reported as a failure — with money there is no "close enough".
    """
    db.expire_all()
    model = Order if entity == "orders" else Expense
    mismatches: list[str] = []
    checked = 0
    source_total = 0.0
    stored_total = 0.0

    rows = db.scalars(select(model)).all()
    by_key = {}
    for row in rows:
        if isinstance(row.extra, dict) and row.extra.get("external_key"):
            by_key[row.extra["external_key"]] = row

    for key, (amount, currency) in expected.items():
        row = by_key.get(key)
        if row is None:
            mismatches.append(f"{key}: no quedó guardado")
            continue
        checked += 1
        source_total += amount
        stored_total += row.amount
        if row.amount != amount or row.currency != currency:
            mismatches.append(
                f"{key}: fuente {amount} {currency} ≠ guardado {row.amount} {row.currency}")

    return {
        "rows_checked": checked,
        "source_total": round(source_total, 6),
        "stored_total": round(stored_total, 6),
        "exact": not mismatches and source_total == stored_total,
        "mismatches": mismatches[:10],
    }


# Keys that name a nested object well enough to stand for it in a cell:
# {"name": "United States", "code2": "US", "tld": "us"} is "United States".
LABEL_KEYS = ("name", "label", "title", "display_name", "full_name", "legal_name",
              "description", "code", "code2", "iso", "symbol", "id", "pid", "uuid")

# Shapes a feed uses for typed values it serialised as strings.
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")

# Records inspected to decide the type of a new column.
TYPE_SAMPLE = 50

COLUMN_REGISTRY_KEY = "intake.columns.{entity}"


def _cell_value(value):
    """A feed value as something a table cell can show.

    Scalars pass through. A nested object is reduced to the field that names
    it (`{"name": "United States", "code2": "US"}` -> "United States"); an
    object without a naming field becomes "key: value" pairs and a list is
    joined with commas. The verbatim record is kept in `_raw` regardless, so
    this reduction never loses data — it only decides what the column shows.
    """
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in LABEL_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, (str, int, float)) and not isinstance(candidate, bool) \
                    and str(candidate).strip():
                return str(candidate).strip()
        pairs = [f"{key}: {_cell_value(item)}" for key, item in value.items() if item is not None]
        return ", ".join(pairs)[:200] if pairs else None
    if isinstance(value, list):
        parts = [str(part) for part in (_cell_value(item) for item in value)
                 if part not in (None, "")]
        return ", ".join(parts)[:200] if parts else None
    return json.dumps(value, ensure_ascii=False)[:200]


def _infer_type(values: list) -> str:
    """The column type a set of sample values calls for.

    Feeds serialise amounts as "52.090000000" and timestamps as ISO strings;
    typing them lets the panel format and sort them as what they are instead
    of showing the raw text. Only unanimous samples decide: one odd value and
    the column stays text, which never misrepresents anything.
    """
    samples = [value for value in values if value is not None and value != ""]
    if not samples:
        return "text"
    if all(isinstance(value, bool) for value in samples):
        return "boolean"
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in samples):
        return "number"
    if all(isinstance(value, str) for value in samples):
        texts = [value.strip() for value in samples]
        if all(_NUMBER_RE.match(text) for text in texts):
            return "number"
        if all(_DATETIME_RE.match(text) for text in texts):
            return "datetime"
        if all(_DATE_RE.match(text) for text in texts):
            return "date"
    return "text"


def _iso_datetime(value) -> str | None:
    """An ISO 8601 timestamp at second precision, offset kept, or None.

    "2026-09-08T00:20:54.521054-04:00" -> "2026-09-08T00:20:54-04:00". The
    wall-clock time and offset the source sent are preserved verbatim: nothing
    is converted to another zone, so the panel shows what the provider shows.
    """
    text = str(value or "").strip()
    if not _DATETIME_RE.match(text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.isoformat(timespec="seconds")


def _typed_value(value, data_type: str):
    """A feed value coerced to what its column holds; None when it cannot be.

    The coercion is a copy in another representation ("52.090000000" ->
    52.09), never a computation: what does not fit the type is left empty and
    stays readable in `_raw`.
    """
    if value is None or value == "":
        return None
    if data_type == "number":
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return value
        return mapping.to_amount(value)
    if data_type == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "si", "sí"):
            return True
        if text in ("false", "0", "no"):
            return False
        return None
    if data_type == "date":
        return mapping.to_date(value)
    if data_type == "datetime":
        return _iso_datetime(value)
    return _cell_value(value)


def _registered_columns(db: Session, entity: str) -> list[str]:
    """Keys of the columns the sync itself created for this entity."""
    row = db.get(Setting, COLUMN_REGISTRY_KEY.format(entity=entity))
    return list((row.value or {}).get("keys", [])) if row is not None else []


def _remember_column(db: Session, entity: str, key: str) -> None:
    """Record that the sync created this column, so a wipe can undo exactly it."""
    row_key = COLUMN_REGISTRY_KEY.format(entity=entity)
    row = db.get(Setting, row_key)
    stamp = datetime.now().isoformat(timespec="seconds")
    keys = _registered_columns(db, entity)
    if key not in keys:
        keys.append(key)
    if row is None:
        db.add(Setting(key=row_key, value={"keys": keys}, updated_at=stamp))
    else:
        row.value, row.updated_at = {"keys": keys}, stamp


def _ensure_columns(db: Session, entity: str, records: list[dict],
                    used_fields: set[str]) -> dict[str, CustomColumn]:
    """Make every unmapped feed field a visible column. Returns field -> column.

    The tables stay dynamic this way: base columns hold what the panel
    computes with (amounts, dates, states), and whatever else the source
    sends shows up under its own name instead of being silently discarded.

    Each new column is typed from the sample values (number, date, datetime,
    boolean or text). A column this sync created earlier as text is upgraded
    when the samples now show a firmer type — a first page whose values were
    all null could not decide. Columns a person created are never retyped.
    """
    fields = [field for field in records[0].keys() if field not in used_fields]
    if not fields:
        return {}

    existing = {column.key: column for column in columns_service.list_columns(db, entity)}
    ours = set(_registered_columns(db, entity))
    resolved: dict[str, CustomColumn] = {}
    for field in fields:
        try:
            key = columns_service.slugify(str(field))
        except columns_service.ColumnError:
            continue
        inferred = _infer_type([record.get(field) for record in records[:TYPE_SAMPLE]])
        column = existing.get(key)
        if column is None:
            try:
                column = columns_service.create_column(db, entity, str(field), inferred, key=key)
            except columns_service.ColumnError:
                continue
            existing[key] = column
            _remember_column(db, entity, key)
        elif key in ours and column.data_type == "text" and inferred != "text":
            column.data_type = inferred
            db.commit()
        # Formula columns are computed, never written by a feed.
        if column.data_type != "formula":
            resolved[field] = column
    return resolved


def refresh_extras(db: Session, entity: str) -> dict:
    """Recompute the feed columns of every imported row from its `_raw` copy.

    Used when the typing or reduction rules change: the verbatim record is
    still there, so every cell is re-derived with the current rules instead
    of waiting for the next pull to touch each row. Columns the sync created
    are retyped from the stored records the same way a pull would type them.
    """
    model = Order if entity == "orders" else Expense
    rows = [row for row in db.scalars(select(model)).all()
            if isinstance(row.extra, dict) and isinstance(row.extra.get("_raw"), dict)
            and not row.extra["_raw"].get("_truncated")]
    if not rows:
        return {"rows": 0, "changed": 0, "retyped": {}}

    existing = {column.key: column for column in columns_service.list_columns(db, entity)}
    ours = set(_registered_columns(db, entity))

    # Feed field -> column, matched by the same slug the sync used to create it.
    by_field: dict[str, CustomColumn] = {}
    for row in rows:
        for field in row.extra["_raw"]:
            if field in by_field:
                continue
            try:
                key = columns_service.slugify(str(field))
            except columns_service.ColumnError:
                continue
            column = existing.get(key)
            if column is not None and column.data_type != "formula":
                by_field[field] = column

    retyped: dict[str, str] = {}
    for field, column in by_field.items():
        if column.key not in ours or column.data_type != "text":
            continue
        inferred = _infer_type([row.extra["_raw"].get(field) for row in rows])
        if inferred != "text":
            column.data_type = inferred
            retyped[column.key] = inferred

    changed = 0
    for row in rows:
        raw = row.extra["_raw"]
        updates = {column.key: _typed_value(raw.get(field), column.data_type)
                   for field, column in by_field.items() if field in raw}
        if any(row.extra.get(key) != value for key, value in updates.items()):
            row.extra = {**row.extra, **updates}
            changed += 1
    db.commit()
    return {"rows": len(rows), "changed": changed, "retyped": retyped}


def wipe(db: Session, scope: str) -> dict:
    """Erase data from OUR tables only — the external source is never touched.

    scope "imported": records that arrived through a sync (they carry an
    external key), the columns the sync created, the stored mapping decisions
    and the last report. Hand-entered records survive.

    scope "all": every order, expense and custom column. Users, sessions,
    conversations and configuration stay.
    """
    from sqlalchemy import delete, or_

    if scope not in ("imported", "all"):
        raise SyncError("El alcance debe ser «imported» o «all».")

    removed = {"orders": 0, "expenses": 0, "columns": 0}
    if scope == "all":
        removed["orders"] = db.query(Order).delete()
        removed["expenses"] = db.query(Expense).delete()
        removed["columns"] = db.query(CustomColumn).delete()
        registries = [COLUMN_REGISTRY_KEY.format(entity=e) for e in ("orders", "expenses")]
        db.execute(delete(Setting).where(Setting.key.in_(registries)))
    else:
        for model, name in ((Order, "orders"), (Expense, "expenses")):
            rows = db.scalars(select(model)).all()
            for row in rows:
                key = (row.extra or {}).get("external_key") if isinstance(row.extra, dict) else None
                # Anything a sync brought in: the endpoint ("api:") or QuickBooks ("qbo:").
                if key and str(key).startswith(("api:", "qbo:")):
                    db.delete(row)
                    removed[name] += 1
            registry = db.get(Setting, COLUMN_REGISTRY_KEY.format(entity=name))
            keys = list((registry.value or {}).get("keys", [])) if registry is not None else []
            if keys:
                removed["columns"] += db.query(CustomColumn).filter(
                    CustomColumn.entity == name, CustomColumn.key.in_(keys)).delete(
                    synchronize_session=False)
            if registry is not None:
                db.delete(registry)

    # Mapping decisions, the last report and the cached analysis describe data
    # that no longer exists.
    from .insights import CACHE_KEY as INSIGHTS_CACHE_KEY
    db.execute(delete(Setting).where(or_(
        Setting.key.like("intake.map.%"),
        Setting.key.in_(("sync.last_pull_report", "quickbooks.last_pull_report",
                         INSIGHTS_CACHE_KEY)))))
    # QuickBooks' incremental watermark points at data that is gone: the next
    # pull starts over from the configured date, without touching the connection.
    from ..models import IntegrationConnection
    for conn in db.scalars(select(IntegrationConnection)).all():
        conn.cursor = None
    db.commit()
    return removed


def source_fields(db: Session, entity: str) -> dict[str, str]:
    """Our field -> feed field, as decided in the last pull for this entity.

    Lets a table say where each base column came from («Cliente ← legal_name»)
    instead of presenting the feed's data under names it never used.
    """
    report = settings_service.last_report(db) or {}
    fields: dict[str, str] = {}
    for name, entry in (report.get("entities") or {}).items():
        if name.split(":")[0].rstrip("·") != entity:
            continue
        for ours, theirs in (entry.get("mapping") or {}).items():
            fields.setdefault(ours, theirs)
    return fields


def collectors(result: dict):
    """Two lists in the report, deduplicated: `problems` is what needs a
    person (a substitution, a gap in the totals, an unknown state);
    `notes` records a criterion applied on purpose, so nothing is silent."""
    def note(problem: str):
        if problem not in result["problems"]:
            result["problems"].append(problem)

    def remark(text: str):
        if text not in result["notes"]:
            result["notes"].append(text)

    return note, remark


def resolve_currency(raw, fallback_currency: str, note, remark) -> str:
    """Normalise a feed currency and report anything the USD totals will
    treat specially: a fallback, an alias, a peg or a missing rate."""
    currency, ok = mapping.to_currency(raw, fallback_currency)
    if not ok:
        note(f"Moneda no reconocida en algunas filas; usé {fallback_currency}.")
    original = str(raw or "").strip().upper()
    if ok and original and original != currency:
        remark(f"Moneda «{original}» normalizada a {currency}: es el mismo activo.")
    rate = finance.usd_rate(currency)
    if rate is None:
        note(f"Sin tasa USD para {currency}: esas filas guardan su monto exacto "
             "pero no suman en los totales USD.")
    elif finance.is_stablecoin(currency):
        if rate == 1.0:
            remark(f"{currency} contabilizado a la par: 1 {currency} = 1 USD en los totales.")
        else:
            shown = f"{rate:.4f}".replace(".", ",")
            remark(f"{currency} contabilizado a {shown} USD por unidad en los totales.")
    return currency


def existing_expenses(db: Session) -> dict[str, Expense]:
    rows = db.scalars(select(Expense)).all()
    return {row.extra["external_key"]: row
            for row in rows if isinstance(row.extra, dict) and row.extra.get("external_key")}


def pull(db: Session) -> dict:
    """Run one pull from the endpoint. Returns the report it stores."""
    values = settings_service.all_values(db)
    base_url = (values.get("api.base_url") or "").strip()
    if not base_url:
        raise SyncError("Falta la URL del endpoint en Configuración.")
    token = values.get("api.token") or ""
    scheme = values.get("api.auth_scheme") or "bearer"
    fallback_currency = (values.get("api.default_currency") or "COP").upper()

    report = {"ran_at": mapping.today(), "source": base_url, "entities": {}}

    def label(kind: str, paths: list[str], path: str) -> str:
        name = kind if len(paths) == 1 else f"{kind}:{path_slug(path)}"
        while name in report["entities"]:  # two paths, same last segment
            name += "·"
        return name

    order_paths = split_paths(values.get("api.orders_path"))
    expense_paths = split_paths(values.get("api.expenses_path"))
    for path in order_paths:
        report["entities"][label("orders", order_paths, path)] = _pull_orders(
            db, base_url, token, path, fallback_currency, scheme)
    for path in expense_paths:
        report["entities"][label("expenses", expense_paths, path)] = _pull_expenses(
            db, base_url, token, path, fallback_currency, scheme)

    if not report["entities"]:
        raise SyncError("No hay rutas de órdenes ni de gastos configuradas.")

    settings_service.save_report(db, report)
    return report


def _pull_orders(db: Session, base_url: str, token: str, path: str,
                 fallback_currency: str, scheme: str = "bearer") -> dict:
    records, fetched = _fetch_list(base_url, token, path, scheme)
    slug = path_slug(path)
    result = {"path": path, "received": len(records), "pages": fetched["pages"],
              "imported": 0, "updated": 0, "skipped": 0, "problems": [], "notes": []}
    if fetched["truncated"]:
        result["problems"].append(
            f"La fuente declara {fetched['source_count']} registros; traje los "
            f"primeros {MAX_RECORDS}.")
    if not records:
        return result

    fields = list(records[0].keys())
    field_map, decided_by = intake.mapping_for(db, "orders", records)
    result["mapping"] = field_map
    result["mapping_decided_by"] = decided_by

    note, remark = collectors(result)

    if "amount" not in field_map:
        note(f"Ningún campo parece un monto (vi: {', '.join(fields)}). "
             "Sin monto no se importa una orden.")
        result["skipped"] = len(records)
        return result

    extra_columns = _ensure_columns(db, "orders", records, set(field_map.values()))
    result["extra_columns"] = sorted(column.key for column in extra_columns.values())

    expected: dict[str, tuple[float, str]] = {}
    unknown_statuses: dict[str, int] = {}
    for index, record in enumerate(records):
        external = record.get(field_map.get("external_id", ""), index)
        amount = mapping.to_amount(record.get(field_map["amount"]))
        if amount is None or amount <= 0:
            result["skipped"] += 1
            note("Algunas filas no tienen un monto usable.")
            continue

        currency = resolve_currency(
            record.get(field_map.get("currency", "")), fallback_currency, note, remark)
        source_status = str(record.get(field_map.get("status", ""), "") or "").strip()
        status = mapping.to_status(source_status)
        if status is None:
            if field_map.get("status"):
                # A lifecycle word we do not know is not revenue: it waits as
                # pending, is named in the report and keeps its exact text.
                status = "pending"
                word = source_status or "(vacío)"
                unknown_statuses[word] = unknown_statuses.get(word, 0) + 1
            else:
                # A feed without a status column is a ledger of settled
                # transactions; the report says so instead of assuming quietly.
                status = "approved"
                remark("El feed no trae estado: las importé como aprobadas.")
        day = mapping.to_date(record.get(field_map.get("date", ""))) or mapping.today()
        customer = str(record.get(field_map.get("customer", ""), "") or "").strip() \
            or f"Cliente externo {external}"

        key = _key("orders", slug, external)
        order_id = _order_row_id(key, slug, external)
        existing = db.get(Order, order_id)
        payload = dict(customer=customer[:120], amount=amount, currency=currency,
                       status=status, date=day,
                       gateway=str(record.get(field_map.get("gateway", ""), "") or "")[:30] or None)
        expected[key] = (amount, currency)
        extra = {"source": "api", "external_key": key, "_raw": raw_record(record)}
        if field_map.get("status"):
            # The feed's own words for the state. The table shows them; `status`
            # stays the category behind filters and totals.
            extra["source_status"] = source_status or None
        for field, column in extra_columns.items():
            extra[column.key] = _typed_value(record.get(field), column.data_type)
        if existing is None:
            db.add(Order(id=order_id, extra=extra, **payload))
            result["imported"] += 1
        else:
            for field, value in payload.items():
                setattr(existing, field, value)
            existing.extra = {**(existing.extra or {}), **extra}
            result["updated"] += 1

    if unknown_statuses:
        listed = ", ".join(
            f"«{value}» ({count} {'orden' if count == 1 else 'órdenes'})"
            for value, count in sorted(unknown_statuses.items()))
        note(f"Estados no reconocidos: {listed}. Quedaron como pendientes, fuera del "
             "ingreso, con su texto original en la tabla. Confirme con la fuente qué significan.")
    db.commit()
    result["verification"] = verify_copy(db, "orders", expected)
    if not result["verification"]["exact"]:
        result["problems"].append(
            "VERIFICACIÓN FALLIDA: lo guardado no coincide con la fuente. "
            + "; ".join(result["verification"]["mismatches"]))
    return result


def _pull_expenses(db: Session, base_url: str, token: str, path: str,
                   fallback_currency: str, scheme: str = "bearer") -> dict:
    records, fetched = _fetch_list(base_url, token, path, scheme)
    slug = path_slug(path)
    result = {"path": path, "received": len(records), "pages": fetched["pages"],
              "imported": 0, "updated": 0, "skipped": 0, "problems": [], "notes": []}
    if fetched["truncated"]:
        result["problems"].append(
            f"La fuente declara {fetched['source_count']} registros; traje los "
            f"primeros {MAX_RECORDS}.")
    if not records:
        return result

    fields = list(records[0].keys())
    field_map, decided_by = intake.mapping_for(db, "expenses", records)
    result["mapping"] = field_map
    result["mapping_decided_by"] = decided_by

    note, remark = collectors(result)

    if "amount" not in field_map or "description" not in field_map:
        missing = [name for name in ("amount", "description") if name not in field_map]
        note(f"No encontré campo para: {', '.join(missing)} (vi: {', '.join(fields)}).")
        result["skipped"] = len(records)
        return result

    extra_columns = _ensure_columns(db, "expenses", records, set(field_map.values()))
    result["extra_columns"] = sorted(column.key for column in extra_columns.values())

    known = existing_expenses(db)
    expected: dict[str, tuple[float, str]] = {}
    for index, record in enumerate(records):
        external = record.get(field_map.get("external_id", ""), index)
        amount = mapping.to_amount(record.get(field_map["amount"]))
        description = str(record.get(field_map["description"], "") or "").strip()
        if amount is None or amount <= 0 or not description:
            result["skipped"] += 1
            note("Algunas filas no tienen monto o concepto usables.")
            continue

        currency = resolve_currency(
            record.get(field_map.get("currency", "")), fallback_currency, note, remark)
        day = mapping.to_date(record.get(field_map.get("date", "")))
        if day is None:
            day = mapping.today()
            if not field_map.get("date"):
                note("El feed no trae fecha: usé la de hoy.")

        payload = dict(
            description=description[:160],
            category=str(record.get(field_map.get("category", ""), "") or "").strip()[:60] or "Sin categoría",
            vendor=str(record.get(field_map.get("vendor", ""), "") or "").strip()[:120] or None,
            owner=str(record.get(field_map.get("owner", ""), "") or "").strip()[:80] or None,
            amount=amount, currency=currency, date=day,
        )
        key = _key("expenses", slug, external)
        expected[key] = (amount, currency)
        extra = {"source": "api", "external_key": key, "_raw": raw_record(record)}
        for field, column in extra_columns.items():
            extra[column.key] = _typed_value(record.get(field), column.data_type)
        existing = known.get(key)
        if existing is None:
            db.add(Expense(extra=extra, **payload))
            result["imported"] += 1
        else:
            for field, value in payload.items():
                setattr(existing, field, value)
            existing.extra = {**(existing.extra or {}), **extra}
            result["updated"] += 1

    db.commit()
    result["verification"] = verify_copy(db, "expenses", expected)
    if not result["verification"]["exact"]:
        result["problems"].append(
            "VERIFICACIÓN FALLIDA: lo guardado no coincide con la fuente. "
            + "; ".join(result["verification"]["mismatches"]))
    return result
