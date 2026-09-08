"""External endpoint tools.

The endpoint contract is not final yet, so the mapping between its payload and
our models is configuration (EXTERNAL_FIELD_MAP) plus a deterministic
auto-detection of common field names. Nothing is guessed by the LLM.
"""
from __future__ import annotations

import json
import os

from sqlalchemy.orm import Session

from ...models import CURRENCIES, ORDER_STATUSES, Expense, Order
from ...services import external_api, finance
from .. import blocks
from ..formatting import integer
from ..registry import ToolResult, tool

# Candidate source keys per target field, tried in order.
DEFAULT_FIELD_MAP = {
    "orders": {
        "id": ["id", "order_id", "orderId", "reference", "referencia"],
        "customer": ["customer", "client", "cliente", "customer_name", "payer"],
        "amount": ["amount", "monto", "value", "total"],
        "currency": ["currency", "moneda", "currency_code"],
        "status": ["status", "estado", "state"],
        "gateway": ["gateway", "pasarela", "provider", "payment_method"],
        "date": ["date", "fecha", "created_at", "createdAt", "transaction_date"],
    },
    "expenses": {
        "description": ["description", "concepto", "concept", "detail", "name"],
        "category": ["category", "categoria", "type"],
        "vendor": ["vendor", "proveedor", "supplier", "merchant"],
        "amount": ["amount", "monto", "value", "total"],
        "currency": ["currency", "moneda"],
        "owner": ["owner", "responsable", "user"],
        "date": ["date", "fecha", "created_at", "createdAt"],
    },
}

STATUS_MAP = {
    "approved": "approved", "aprobada": "approved", "aprobado": "approved",
    "success": "approved", "succeeded": "approved", "paid": "approved", "completed": "approved",
    "pending": "pending", "pendiente": "pending", "in_progress": "pending", "created": "pending",
    "rejected": "rejected", "rechazada": "rejected", "declined": "rejected", "failed": "rejected",
    "refunded": "refunded", "reembolsada": "refunded", "reversed": "refunded",
}


def _field_map(kind: str) -> dict:
    """Config override merged over the defaults."""
    raw = os.getenv("EXTERNAL_FIELD_MAP")
    mapping = {k: dict(v) for k, v in DEFAULT_FIELD_MAP.items()}
    if raw:
        try:
            override = json.loads(raw)
            for entity, fields in override.items():
                mapping.setdefault(entity, {}).update(fields)
        except json.JSONDecodeError:
            pass  # a malformed override must not break the sync
    return mapping[kind]


def _pick(item: dict, candidates: list[str]):
    lowered = {str(k).lower(): v for k, v in item.items()}
    for candidate in candidates:
        if candidate.lower() in lowered and lowered[candidate.lower()] not in (None, ""):
            return lowered[candidate.lower()]
    return None


def _records(payload) -> list[dict]:
    """Find the list of records in a response of unknown shape."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "items", "records", "orders", "transactions"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        # A single record object.
        return [payload] if payload else []
    return []


@tool(
    name="check_external_endpoint",
    description="Consulta el estado del endpoint externo configurado y muestra una vista previa "
                "de su respuesta, para conocer su contrato.",
    parameters={"type": "object", "properties": {}, "required": []},
    examples=["¿Está conectado el endpoint externo?", "Prueba la integración"],
)
def check_external_endpoint(db: Session) -> ToolResult:
    status = external_api.status()
    if not status["configured"]:
        return ToolResult(
            blocks=[blocks.notice(
                "El endpoint externo no está configurado. Defina EXTERNAL_API_URL en "
                "fintech_back/.env para habilitarlo.", tone="warning")],
            data=status, summary="Endpoint externo sin configurar.")

    try:
        probe = external_api.probe()
    except Exception as err:  # noqa: BLE001 — network failures are expected here
        return ToolResult(
            blocks=[blocks.notice(f"No pude conectar: {err}", tone="danger")],
            data=status, summary="Endpoint externo inaccesible.")

    return ToolResult(
        blocks=[
            blocks.table(
                columns=[{"key": "field", "label": "Campo"}, {"key": "value", "label": "Valor"}],
                rows=[
                    {"field": "URL", "value": status["url"]},
                    {"field": "Autenticado", "value": "sí" if status["has_api_key"] else "no"},
                    {"field": "Código HTTP", "value": str(probe["status_code"])},
                    {"field": "Content-Type", "value": probe.get("content_type") or "—"},
                ],
            ),
            blocks.text(probe["preview"][:800] or "(respuesta vacía)"),
        ],
        data={**status, **probe},
        summary="Endpoint externo consultado.",
    )


@tool(
    name="sync_external_data",
    description="Trae registros del endpoint externo y los inserta como órdenes o gastos, "
                "mapeando los campos de forma determinista. Úsala para «sincroniza», "
                "«trae los datos del endpoint».",
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["orders", "expenses"], "default": "orders"},
            "path": {"type": "string", "description": "Ruta relativa dentro del endpoint."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        },
        "required": [],
    },
    mutates=True,
    examples=["Sincroniza las órdenes del endpoint"],
)
def sync_external_data(db: Session, kind: str = "orders", path: str = "",
                       limit: int = 100) -> ToolResult:
    if not external_api.is_configured():
        return ToolResult(
            blocks=[blocks.notice(
                "Falta configurar EXTERNAL_API_URL en fintech_back/.env.", tone="warning")],
            data={}, summary="Endpoint externo sin configurar.")

    payload = external_api.fetch(path)
    records = _records(payload)[: max(1, min(int(limit), 500))]
    if not records:
        return ToolResult(
            blocks=[blocks.notice("El endpoint no devolvió registros.", tone="info")],
            data={"fetched": 0}, summary="Sin registros externos.")

    mapping = _field_map(kind)
    inserted, skipped, errors = 0, 0, []

    for index, item in enumerate(records, start=1):
        try:
            amount = float(_pick(item, mapping["amount"]) or 0)
            if amount <= 0:
                raise ValueError("monto ausente o no positivo")
            currency = str(_pick(item, mapping["currency"]) or "COP").upper()
            if currency not in CURRENCIES:
                raise ValueError(f"moneda «{currency}» no soportada")
            raw_date = str(_pick(item, mapping["date"]) or "")[:10]
            if len(raw_date) != 10:
                raise ValueError("fecha ausente o con formato inesperado")

            if kind == "orders":
                status = STATUS_MAP.get(str(_pick(item, mapping["status"]) or "pending").lower())
                if status not in ORDER_STATUSES:
                    raise ValueError("estado no reconocido")
                external_id = _pick(item, mapping["id"])
                order_id = str(external_id) if external_id else finance.next_order_id(db)
                if db.get(Order, order_id):
                    skipped += 1
                    continue
                db.add(Order(
                    id=order_id,
                    customer=str(_pick(item, mapping["customer"]) or "Sin nombre"),
                    amount=amount, currency=currency, status=status,
                    gateway=(str(_pick(item, mapping["gateway"])) if _pick(item, mapping["gateway"]) else None),
                    date=raw_date, extra={"source": "external"},
                ))
                db.flush()
            else:
                db.add(Expense(
                    description=str(_pick(item, mapping["description"]) or "Gasto externo"),
                    category=str(_pick(item, mapping["category"]) or "Sin categoría"),
                    vendor=(str(_pick(item, mapping["vendor"])) if _pick(item, mapping["vendor"]) else None),
                    amount=amount, currency=currency,
                    owner=(str(_pick(item, mapping["owner"])) if _pick(item, mapping["owner"]) else None),
                    date=raw_date, extra={"source": "external"},
                ))
            inserted += 1
        except Exception as err:  # noqa: BLE001 — per-record validation
            errors.append(f"Registro {index}: {err}")

    db.commit()

    result_blocks = [blocks.notice(
        f"Sincronicé {integer(inserted)} de {integer(len(records))} registros en "
        f"{'órdenes' if kind == 'orders' else 'gastos'}"
        + (f" ({integer(skipped)} ya existían)." if skipped else "."),
        tone="success" if inserted else "warning")]
    if errors:
        result_blocks.append(blocks.listing(
            [{"title": error, "tone": "warning"} for error in errors[:10]],
            title="Registros omitidos"))

    return ToolResult(
        blocks=result_blocks,
        data={"fetched": len(records), "inserted": inserted, "skipped": skipped,
              "errors": errors[:10]},
        summary=f"{inserted} registros externos importados.",
    )
