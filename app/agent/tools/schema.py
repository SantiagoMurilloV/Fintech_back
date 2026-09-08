"""Schema tools: user-defined columns on orders and expenses."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ...models import CUSTOM_COLUMN_ENTITIES, CUSTOM_COLUMN_TYPES
from ...services import columns as columns_service
from .. import blocks
from ..registry import ToolResult, tool

ENTITY_LABELS = {"orders": "órdenes", "expenses": "gastos"}


@tool(
    name="add_column",
    description="Agrega una columna nueva a órdenes o gastos. Con data_type='formula' la columna "
                "es calculada y se recalcula sola en cada consulta (ej. formula='amount * 0.19'). "
                "Campos disponibles en fórmulas de órdenes: amount, usd_eq, currency, status, "
                "gateway, customer, date; en gastos: amount, usd_eq, currency, category, vendor, "
                "owner, description, date.",
    parameters={
        "type": "object",
        "properties": {
            "entity": {"type": "string", "enum": CUSTOM_COLUMN_ENTITIES},
            "label": {"type": "string", "description": "Nombre visible de la columna."},
            "data_type": {"type": "string", "enum": CUSTOM_COLUMN_TYPES, "default": "text"},
            "formula": {"type": "string", "description": "Requerida si data_type='formula'."},
        },
        "required": ["entity", "label"],
    },
    mutates=True,
    examples=["Agrega a gastos una columna calculada IVA = amount * 0.19",
              "Añade la columna 'Centro de costo' a gastos"],
)
def add_column(db: Session, entity: str, label: str, data_type: str = "text",
               formula: str | None = None) -> ToolResult:
    column = columns_service.create_column(db, entity, label, data_type, formula)
    kind = "calculada" if column.data_type == "formula" else column.data_type
    detail = f" con la fórmula `{column.formula}`" if column.formula else ""

    return ToolResult(
        blocks=[
            blocks.notice(
                f"Columna «{column.label}» ({kind}) agregada a {ENTITY_LABELS[entity]}{detail}.",
                tone="success"),
            blocks.table(
                columns=[{"key": "field", "label": "Campo"}, {"key": "value", "label": "Valor"}],
                rows=[
                    {"field": "Entidad", "value": ENTITY_LABELS[entity]},
                    {"field": "Clave", "value": column.key},
                    {"field": "Tipo", "value": kind},
                    {"field": "Fórmula", "value": column.formula or "—"},
                ],
            ),
        ],
        data=columns_service.column_view(column),
        summary=f"Columna {column.key} creada en {entity}.",
    )


@tool(
    name="list_columns",
    description="Muestra las columnas personalizadas configuradas en órdenes y gastos.",
    parameters={
        "type": "object",
        "properties": {"entity": {"type": "string", "enum": CUSTOM_COLUMN_ENTITIES}},
        "required": [],
    },
    examples=["¿Qué columnas personalizadas hay?"],
)
def list_columns(db: Session, entity: str | None = None) -> ToolResult:
    items = columns_service.list_columns(db, entity)
    if not items:
        return ToolResult(
            blocks=[blocks.notice("Todavía no hay columnas personalizadas.", tone="info")],
            data={"count": 0}, summary="Sin columnas personalizadas.")

    rows = [{
        "entity": ENTITY_LABELS.get(column.entity, column.entity),
        "label": column.label,
        "key": column.key,
        "type": "calculada" if column.data_type == "formula" else column.data_type,
        "formula": column.formula or "—",
    } for column in items]

    return ToolResult(
        blocks=[blocks.table(
            columns=[
                {"key": "entity", "label": "Entidad"},
                {"key": "label", "label": "Columna"},
                {"key": "key", "label": "Clave"},
                {"key": "type", "label": "Tipo"},
                {"key": "formula", "label": "Fórmula"},
            ],
            rows=rows,
            caption=f"{len(rows)} columnas personalizadas",
        )],
        data={"count": len(rows)},
        summary=f"{len(rows)} columnas personalizadas.",
    )


@tool(
    name="remove_column",
    description="Elimina una columna personalizada de órdenes o gastos. Se puede identificar por "
                "su nombre visible o por su clave. Los valores guardados en esa columna dejan de "
                "mostrarse; las columnas base (monto, estado, fecha…) no se pueden eliminar.",
    parameters={
        "type": "object",
        "properties": {
            "entity": {"type": "string", "enum": CUSTOM_COLUMN_ENTITIES},
            "column": {"type": "string", "description": "Nombre o clave de la columna."},
        },
        "required": ["column"],
    },
    mutates=True,
    examples=["Elimina la columna IVA de gastos", "Borra la columna centro de costo"],
)
def remove_column(db: Session, column: str, entity: str | None = None) -> ToolResult:
    target = columns_service.find_column(db, column, entity)
    if target is None:
        available = columns_service.list_columns(db, entity)
        if not available:
            return ToolResult(
                blocks=[blocks.notice("No hay columnas personalizadas para eliminar.", "info")],
                data={}, summary="Sin columnas personalizadas.")
        names = ", ".join(f"«{c.label}»" for c in available)
        return ToolResult(
            blocks=[blocks.notice(
                f"No encontré la columna «{column}». Las disponibles son: {names}.", "warning")],
            data={"available": [c.key for c in available]},
            summary="Columna no encontrada.")

    removed = columns_service.column_view(target)
    columns_service.delete_column(db, target.id)
    return ToolResult(
        blocks=[blocks.notice(
            f"Eliminé la columna «{removed['label']}» de {ENTITY_LABELS[removed['entity']]}.",
            tone="success")],
        data=removed,
        summary=f"Columna {removed['key']} eliminada.",
    )


@tool(
    name="set_cell",
    description="Escribe el valor de una columna personalizada en un registro concreto "
                "(no aplica a columnas calculadas).",
    parameters={
        "type": "object",
        "properties": {
            "entity": {"type": "string", "enum": CUSTOM_COLUMN_ENTITIES},
            "row_id": {"type": "string", "description": "ID de la orden (ORD-1234) o del gasto."},
            "key": {"type": "string", "description": "Clave de la columna."},
            "value": {"description": "Valor a guardar."},
        },
        "required": ["entity", "row_id", "key", "value"],
    },
    mutates=True,
    examples=["Marca la orden ORD-4554 con centro de costo 'Comercial'"],
)
def set_cell(db: Session, entity: str, row_id: str, key: str, value) -> ToolResult:
    identifier = int(row_id) if entity == "expenses" else row_id
    result = columns_service.set_value(db, entity, identifier, key, value)
    return ToolResult(
        blocks=[blocks.notice(
            f"Actualicé «{key}» = «{value}» en {ENTITY_LABELS[entity]} {row_id}.", tone="success")],
        data=result,
        summary=f"{entity}.{row_id}.{key} actualizado.",
    )
