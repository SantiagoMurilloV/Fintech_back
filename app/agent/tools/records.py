"""Record tools: query and create orders and expenses."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import CURRENCIES, GATEWAYS, ORDER_STATUSES, Expense, Order
from ...services import columns as columns_service
from ...services import finance
from ...services import records as records_service
from .. import blocks
from ..formatting import amount, integer, short_date, status_label, usd_compact
from ..registry import ToolResult, tool

MAX_ROWS = 50


def _custom_columns(db: Session, entity: str):
    return columns_service.list_columns(db, entity)


def _order_payload(order: Order) -> dict:
    return {
        "id": order.id, "customer": order.customer, "amount": order.amount,
        "currency": order.currency, "status": order.status, "gateway": order.gateway,
        "date": order.date, "usd_eq": finance.usd_eq(order.amount, order.currency),
    }


def _expense_payload(expense: Expense) -> dict:
    return {
        "id": expense.id, "description": expense.description, "category": expense.category,
        "vendor": expense.vendor, "amount": expense.amount, "currency": expense.currency,
        "owner": expense.owner, "date": expense.date,
        "usd_eq": finance.usd_eq(expense.amount, expense.currency),
    }


@tool(
    name="list_orders",
    description="Lista órdenes con filtros opcionales por estado, gateway, cliente o periodo. "
                "Úsala para «muéstrame las órdenes», «órdenes rechazadas», «órdenes de X cliente».",
    parameters={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ORDER_STATUSES},
            "gateway": {"type": "string", "enum": GATEWAYS},
            "customer": {"type": "string", "description": "Coincidencia parcial del nombre."},
            "period": {"type": "string", "pattern": r"^\d{4}-\d{2}$"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS, "default": 10},
        },
        "required": [],
    },
    examples=["Muéstrame las órdenes rechazadas", "Órdenes de Wompi este mes"],
)
def list_orders(db: Session, status: str | None = None, gateway: str | None = None,
                customer: str | None = None, period: str | None = None,
                limit: int = 10) -> ToolResult:
    limit = max(1, min(int(limit), MAX_ROWS))
    query = select(Order)
    filters = []
    if status in ORDER_STATUSES:
        query = query.where(Order.status == status)
        filters.append(f"estado {status_label(status)}")
    if gateway:
        query = query.where(Order.gateway == gateway)
        filters.append(f"gateway {gateway}")
    if customer:
        query = query.where(Order.customer.ilike(f"%{customer}%"))
        filters.append(f'cliente ~ "{customer}"')
    if period:
        low, high = finance.month_bounds(period)
        query = query.where(Order.date >= low, Order.date < high)
        filters.append(f"periodo {period}")

    rows = list(db.scalars(query.order_by(Order.date.desc(), Order.id.desc()).limit(limit)).all())
    custom = _custom_columns(db, "orders")

    table_columns = [
        {"key": "id", "label": "ID"},
        {"key": "customer", "label": "Cliente"},
        {"key": "amount", "label": "Monto", "align": "right"},
        {"key": "status", "label": "Estado", "kind": "badge"},
        {"key": "date", "label": "Fecha", "align": "right"},
    ] + [{"key": column.key, "label": column.label} for column in custom]

    table_rows = []
    for order in rows:
        payload = _order_payload(order)
        extra = columns_service.apply_to_row(payload, custom, order.extra)
        table_rows.append({
            "id": order.id,
            "customer": order.customer,
            "amount": f"{amount(order.amount, order.currency)} {order.currency}",
            "status": {"label": status_label(order.status), "tone": _tone(order.status)},
            "date": short_date(order.date),
            **{key: _cell(value) for key, value in extra.items()},
        })

    total_usd = sum(finance.usd_eq(o.amount, o.currency) for o in rows)
    caption = f"{integer(len(rows))} órdenes · {usd_compact(total_usd)} equivalente"
    if filters:
        caption += f" · filtros: {', '.join(filters)}"

    return ToolResult(
        blocks=[blocks.table(table_columns, table_rows, caption)] if rows
        else [blocks.notice("No hay órdenes que cumplan ese filtro.", tone="info")],
        data={"count": len(rows), "total_usd": round(total_usd, 2)},
        summary=f"{len(rows)} órdenes listadas.",
    )


def _tone(status: str) -> str:
    from ..formatting import status_tone
    return status_tone(status)


def _cell(value):
    """Custom column values are rendered as-is; numbers get grouped."""
    if isinstance(value, float):
        return amount(value, "USD")
    return "" if value is None else value


@tool(
    name="list_expenses",
    description="Lista gastos con filtros por categoría, proveedor, responsable, periodo o "
                "faltantes de comprobante. Úsala para «gastos de marketing», «gastos sin comprobante».",
    parameters={
        "type": "object",
        "properties": {
            "category": {"type": "string"},
            "vendor": {"type": "string"},
            "owner": {"type": "string"},
            "period": {"type": "string", "pattern": r"^\d{4}-\d{2}$"},
            "missing_receipt": {"type": "boolean", "description": "Solo gastos sin comprobante."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS, "default": 10},
        },
        "required": [],
    },
    examples=["Gastos sin comprobante", "Gastos de infraestructura de julio"],
)
def list_expenses(db: Session, category: str | None = None, vendor: str | None = None,
                  owner: str | None = None, period: str | None = None,
                  missing_receipt: bool = False, limit: int = 10) -> ToolResult:
    limit = max(1, min(int(limit), MAX_ROWS))
    query = select(Expense)
    filters = []
    if category:
        query = query.where(Expense.category.ilike(f"%{category}%"))
        filters.append(f"categoría ~ {category}")
    if vendor:
        query = query.where(Expense.vendor.ilike(f"%{vendor}%"))
        filters.append(f"proveedor ~ {vendor}")
    if owner:
        query = query.where(Expense.owner.ilike(f"%{owner}%"))
        filters.append(f"responsable ~ {owner}")
    if period:
        low, high = finance.month_bounds(period)
        query = query.where(Expense.date >= low, Expense.date < high)
        filters.append(f"periodo {period}")
    if missing_receipt:
        query = query.where(Expense.receipt_name.is_(None), Expense.receipt_url.is_(None))
        filters.append("sin comprobante")

    rows = list(db.scalars(query.order_by(Expense.date.desc(), Expense.id.desc()).limit(limit)).all())
    custom = _custom_columns(db, "expenses")

    table_columns = [
        {"key": "description", "label": "Concepto"},
        {"key": "category", "label": "Categoría", "kind": "badge"},
        {"key": "vendor", "label": "Proveedor"},
        {"key": "amount", "label": "Monto", "align": "right"},
        {"key": "date", "label": "Fecha", "align": "right"},
        {"key": "receipt", "label": "Comprobante"},
    ] + [{"key": column.key, "label": column.label} for column in custom]

    table_rows = []
    for expense in rows:
        payload = _expense_payload(expense)
        extra = columns_service.apply_to_row(payload, custom, expense.extra)
        table_rows.append({
            "description": expense.description,
            "category": {"label": expense.category, "tone": "neutral"},
            "vendor": expense.vendor or "—",
            "amount": f"{amount(expense.amount, expense.currency)} {expense.currency}",
            "date": short_date(expense.date),
            "receipt": ({"label": "Ver", "url": expense.receipt_url} if expense.receipt_url
                        else (expense.receipt_name or "— Falta")),
            **{key: _cell(value) for key, value in extra.items()},
        })

    total_usd = sum(finance.usd_eq(e.amount, e.currency) for e in rows)
    caption = f"{integer(len(rows))} gastos · {usd_compact(total_usd)} equivalente"
    if filters:
        caption += f" · filtros: {', '.join(filters)}"

    return ToolResult(
        blocks=[blocks.table(table_columns, table_rows, caption)] if rows
        else [blocks.notice("No hay gastos que cumplan ese filtro.", tone="info")],
        data={"count": len(rows), "total_usd": round(total_usd, 2)},
        summary=f"{len(rows)} gastos listados.",
    )


@tool(
    name="create_order",
    description="Crea una orden nueva. Requiere cliente, monto y moneda; el ID se asigna solo.",
    parameters={
        "type": "object",
        "properties": {
            "customer": {"type": "string"},
            "amount": {"type": "number", "exclusiveMinimum": 0},
            "currency": {"type": "string", "enum": CURRENCIES, "default": "COP"},
            "status": {"type": "string", "enum": ORDER_STATUSES, "default": "pending"},
            "gateway": {"type": "string", "enum": GATEWAYS},
            "date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
            "with_invoice": {"type": "boolean", "default": False,
                             "description": "Además genera la factura PDF de la orden."},
        },
        "required": ["customer", "amount"],
    },
    mutates=True,
    examples=["Crea una orden de 500.000 COP para Grupo Andino",
              "Hazme una factura para el cliente Acme por 2.000 USD"],
)
def create_order(db: Session, customer: str, amount: float, currency: str = "COP",
                 status: str = "pending", gateway: str | None = None,
                 date: str | None = None, with_invoice: bool = False) -> ToolResult:
    if amount is None or float(amount) <= 0:
        raise ValueError("El monto debe ser mayor a 0.")
    if currency not in CURRENCIES:
        raise ValueError(f"currency debe ser una de: {', '.join(CURRENCIES)}")
    if status not in ORDER_STATUSES:
        raise ValueError(f"status debe ser uno de: {', '.join(ORDER_STATUSES)}")

    order = Order(id=finance.next_order_id(db), customer=customer.strip(), amount=float(amount),
                  currency=currency, status=status, gateway=gateway,
                  date=date or _today(), extra={})
    db.add(order)
    db.commit()

    invoice_blocks = []
    order_data = {"id": order.id}
    if with_invoice:
        from .reporting import generate_invoice
        documented = generate_invoice(db, order_id=order.id)
        invoice_blocks = documented.blocks
        order_data.update(documented.data)

    return ToolResult(
        blocks=[
            blocks.notice(f"Orden {order.id} creada.", tone="success"),
            blocks.table(
                columns=[{"key": "field", "label": "Campo"}, {"key": "value", "label": "Valor"}],
                rows=[
                    {"field": "ID", "value": order.id},
                    {"field": "Cliente", "value": order.customer},
                    {"field": "Monto", "value": f"{amount_fmt(order)} {order.currency}"},
                    {"field": "Estado", "value": status_label(order.status)},
                    {"field": "Gateway", "value": order.gateway or "—"},
                    {"field": "Fecha", "value": order.date},
                ],
            ),
        ] + invoice_blocks,
        data=order_data,
        summary=f"Orden {order.id} creada"
                + (" con su factura." if with_invoice else "."),
    )


@tool(
    name="create_expense",
    description="Registra un gasto nuevo. Requiere concepto, categoría, monto y moneda.",
    parameters={
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "category": {"type": "string"},
            "amount": {"type": "number", "exclusiveMinimum": 0},
            "currency": {"type": "string", "enum": CURRENCIES, "default": "COP"},
            "vendor": {"type": "string"},
            "owner": {"type": "string"},
            "date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
            "with_receipt": {"type": "boolean", "default": False,
                             "description": "Además genera el comprobante PDF y lo "
                                            "adjunta al gasto."},
        },
        "required": ["description", "category", "amount"],
    },
    mutates=True,
    examples=["Registra un gasto de 200 USD en software, proveedor Figma",
              "Generame una factura de pago de nómina por 1.000 USD"],
)
def create_expense(db: Session, description: str, category: str, amount: float,
                   currency: str = "COP", vendor: str | None = None,
                   owner: str | None = None, date: str | None = None,
                   with_receipt: bool = False) -> ToolResult:
    if float(amount) <= 0:
        raise ValueError("El monto debe ser mayor a 0.")
    if currency not in CURRENCIES:
        raise ValueError(f"currency debe ser una de: {', '.join(CURRENCIES)}")

    expense = Expense(description=description.strip(), category=category.strip(),
                      amount=float(amount), currency=currency, vendor=vendor, owner=owner,
                      date=date or _today(), extra={})
    db.add(expense)
    db.commit()

    result_blocks = []
    result_data = {"id": expense.id}
    if with_receipt:
        # El pedido fue «facturame X»: el registro sin su documento está a
        # medias. El comprobante queda adjunto en la columna de la tabla.
        from .reporting import generate_receipt
        documented = generate_receipt(db, expense_id=expense.id)
        result_blocks = documented.blocks
        result_data.update(documented.data)

    return ToolResult(
        blocks=[
            blocks.notice(f"Gasto «{expense.description}» registrado.", tone="success"),
            blocks.table(
                columns=[{"key": "field", "label": "Campo"}, {"key": "value", "label": "Valor"}],
                rows=[
                    {"field": "ID", "value": str(expense.id)},
                    {"field": "Concepto", "value": expense.description},
                    {"field": "Categoría", "value": expense.category},
                    {"field": "Monto", "value": f"{amount_fmt(expense)} {expense.currency}"},
                    {"field": "Proveedor", "value": expense.vendor or "—"},
                    {"field": "Fecha", "value": expense.date},
                ],
            ),
        ] + result_blocks,
        data=result_data,
        summary=f"Gasto {expense.id} registrado"
                + (" con su comprobante." if with_receipt else "."),
    )


@tool(
    name="create_document",
    description="Paso intermedio cuando piden «una factura/recibo por X» sin decir si "
                "documenta un gasto o una orden: pregunta cuál de los dos es. No uses "
                "esta herramienta si el mensaje ya lo dice.",
    parameters={
        "type": "object",
        "properties": {
            "record_kind": {"type": "string", "enum": ["expense", "order"]},
            "source_text": {"type": "string",
                            "description": "El pedido original, para no repreguntar."},
        },
        "required": ["record_kind"],
    },
    examples=["Generame una factura por 500 USD"],
)
def create_document(db: Session, record_kind: str = "", source_text: str = "") -> ToolResult:
    """Never runs in practice: the draft flow resolves the kind first and the
    graph re-plans as create_expense/create_order. Kept as a safe landing."""
    return ToolResult(blocks=[blocks.notice(
        "¿Ese documento respalda un gasto que hizo o una orden/venta a un "
        "cliente? Dígame «es un gasto» o «es una orden».", tone="info")],
        summary="Falta saber si es gasto u orden.")


@tool(
    name="update_expense",
    description="Modifica un gasto que ya existe. Solo cambia los campos que se indiquen; "
                "el resto queda igual. Úsala para «cambia el monto del gasto 12 a 250 USD», "
                "«reclasifica el gasto 8 a Marketing», «corrige el proveedor del gasto 3».",
    parameters={
        "type": "object",
        "properties": {
            "expense_id": {"type": "integer", "description": "ID del gasto a modificar."},
            "description": {"type": "string"},
            "category": {"type": "string"},
            "amount": {"type": "number", "exclusiveMinimum": 0},
            "currency": {"type": "string", "enum": CURRENCIES},
            "vendor": {"type": "string"},
            "owner": {"type": "string"},
            "date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        },
        "required": ["expense_id"],
    },
    mutates=True,
    examples=["Cambia el monto del gasto 12 a 250 USD",
              "Reclasifica el gasto 8 a la categoría Marketing"],
)
def update_expense(db: Session, expense_id: int, **changes) -> ToolResult:
    expense = db.get(Expense, int(expense_id))
    if expense is None:
        raise ValueError(f"No existe el gasto {expense_id}.")
    applied = records_service.apply(db, expense, changes, "expenses")
    if not applied:
        raise ValueError("No me indicó ningún cambio para ese gasto.")

    return ToolResult(
        blocks=[
            blocks.notice(f"Gasto {expense.id} actualizado.", tone="success"),
            _changes_table(applied, expense.currency),
            blocks.table(
                columns=[{"key": "field", "label": "Campo"}, {"key": "value", "label": "Valor"}],
                rows=[
                    {"field": "Concepto", "value": expense.description},
                    {"field": "Categoría", "value": expense.category},
                    {"field": "Monto", "value": f"{amount_fmt(expense)} {expense.currency}"},
                    {"field": "Proveedor", "value": expense.vendor or "—"},
                    {"field": "Responsable", "value": expense.owner or "—"},
                    {"field": "Fecha", "value": expense.date},
                ],
                caption="Cómo queda el gasto",
            ),
        ],
        data={"id": expense.id, "changed": list(applied)},
        summary=f"Gasto {expense.id} actualizado ({', '.join(applied)}).",
    )


@tool(
    name="update_order",
    description="Modifica una orden que ya existe. Solo cambia los campos que se indiquen. "
                "Úsala para «marca la orden ORD-4123 como aprobada», «cambia el cliente de "
                "ORD-4123», «corrige el monto de la orden ORD-4123 a 800.000».",
    parameters={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "ID de la orden, formato ORD-1234."},
            "customer": {"type": "string"},
            "amount": {"type": "number", "exclusiveMinimum": 0},
            "currency": {"type": "string", "enum": CURRENCIES},
            "status": {"type": "string", "enum": ORDER_STATUSES},
            "gateway": {"type": "string", "enum": GATEWAYS},
            "date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        },
        "required": ["order_id"],
    },
    mutates=True,
    examples=["Marca la orden ORD-4123 como aprobada",
              "Cambia el monto de la orden ORD-4123 a 800.000 COP"],
)
def update_order(db: Session, order_id: str, **changes) -> ToolResult:
    order = db.get(Order, str(order_id).upper())
    if order is None:
        raise ValueError(f"No existe la orden {order_id}.")
    applied = records_service.apply(db, order, changes, "orders")
    if not applied:
        raise ValueError("No me indicó ningún cambio para esa orden.")

    return ToolResult(
        blocks=[
            blocks.notice(f"Orden {order.id} actualizada.", tone="success"),
            _changes_table(applied, order.currency),
            blocks.table(
                columns=[{"key": "field", "label": "Campo"}, {"key": "value", "label": "Valor"}],
                rows=[
                    {"field": "Cliente", "value": order.customer},
                    {"field": "Monto", "value": f"{amount_fmt(order)} {order.currency}"},
                    {"field": "Estado", "value": status_label(order.status)},
                    {"field": "Gateway", "value": order.gateway or "—"},
                    {"field": "Fecha", "value": order.date},
                ],
                caption="Cómo queda la orden",
            ),
        ],
        data={"id": order.id, "changed": list(applied)},
        summary=f"Orden {order.id} actualizada ({', '.join(applied)}).",
    )


@tool(
    name="delete_expense",
    description="Elimina un gasto. La acción no se puede deshacer, así que el agente pide "
                "confirmación antes de ejecutarla.",
    parameters={
        "type": "object",
        "properties": {"expense_id": {"type": "integer"}},
        "required": ["expense_id"],
    },
    mutates=True,
    examples=["Elimina el gasto 12", "Borra el último gasto"],
)
def delete_expense(db: Session, expense_id: int) -> ToolResult:
    expense = db.get(Expense, int(expense_id))
    if expense is None:
        raise ValueError(f"No existe el gasto {expense_id}.")

    removed = {"id": expense.id, "description": expense.description,
               "amount": f"{amount_fmt(expense)} {expense.currency}", "date": expense.date}
    db.delete(expense)
    db.commit()

    return ToolResult(
        blocks=[blocks.notice(
            f"Eliminé el gasto {removed['id']} «{removed['description']}» "
            f"por {removed['amount']}.", tone="success")],
        data=removed,
        summary=f"Gasto {removed['id']} eliminado.",
    )


@tool(
    name="delete_order",
    description="Elimina una orden. La acción no se puede deshacer, así que el agente pide "
                "confirmación antes de ejecutarla.",
    parameters={
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"],
    },
    mutates=True,
    examples=["Elimina la orden ORD-4123"],
)
def delete_order(db: Session, order_id: str) -> ToolResult:
    order = db.get(Order, str(order_id).upper())
    if order is None:
        raise ValueError(f"No existe la orden {order_id}.")

    removed = {"id": order.id, "customer": order.customer,
               "amount": f"{amount_fmt(order)} {order.currency}", "date": order.date}
    db.delete(order)
    db.commit()

    return ToolResult(
        blocks=[blocks.notice(
            f"Eliminé la orden {removed['id']} de {removed['customer']} "
            f"por {removed['amount']}.", tone="success")],
        data=removed,
        summary=f"Orden {removed['id']} eliminada.",
    )


def _changes_table(applied: dict, currency: str) -> dict:
    """Before/after of every field that moved, so the edit is auditable."""
    return blocks.table(
        columns=[{"key": "field", "label": "Campo"},
                 {"key": "before", "label": "Antes"},
                 {"key": "after", "label": "Ahora"}],
        rows=[{"field": label,
               "before": _cell_value(key, previous, currency),
               "after": _cell_value(key, current, currency)}
              for key, (label, previous, current) in applied.items()],
        caption=f"{len(applied)} campo{'s' if len(applied) != 1 else ''} modificado"
                f"{'s' if len(applied) != 1 else ''}",
    )


def _cell_value(key: str, value, currency: str) -> str:
    if value is None or value == "":
        return "—"
    if key == "status":
        return status_label(str(value))
    if key == "amount":
        return f"{amount(float(value), currency)} {currency}"
    return str(value)


def amount_fmt(row) -> str:
    return amount(row.amount, row.currency)


def _today() -> str:
    return date.today().isoformat()
