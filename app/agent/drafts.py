"""Record operations that take more than one turn.

Creating, editing and deleting all need things the first message may not carry:
the fields of a new record, which record to touch, what to change on it, and —
before anything is destroyed — a yes. Whatever is still missing is kept as a
draft on the conversation, the agent asks for one thing at a time, and each
answer is merged with the same deterministic parser used for a complete
command. The tool call that finally runs is identical to the one an all-in-one
message would have produced, and the draft only ever holds validated values.

Nothing here decides a figure: amounts, dates and statuses come from
agent/parsing.py, and which record is meant comes from agent/targets.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Conversation, Expense, Order
from . import blocks, parsing, targets
from .formatting import amount as amount_text
from .formatting import status_label
from .registry import ToolResult

# Pseudo-slots: not fields of the tool, but things the turn still needs.
CHANGES = "__changes__"      # at least one field to modify
CONFIRM = "__confirm__"      # an explicit yes before destroying data


@dataclass(frozen=True)
class Slot:
    name: str
    label: str          # how the agent names it when asking
    example: str        # shown with the question
    required: bool = True


# Asked in this order; optional slots are never asked for, only displayed when
# the user did provide them.
SLOTS: dict[str, list[Slot]] = {
    "create_expense": [
        Slot("description", "el concepto del gasto", "«Licencias de diseño»"),
        Slot("amount", "el monto", "«200 USD», «800.000» o «1,5 millones»"),
        Slot("category", "la categoría", "«Software», «Marketing», «Operación»"),
        Slot("vendor", "el proveedor", "«Figma»", required=False),
        Slot("owner", "el responsable", "«D. Torres»", required=False),
        Slot("currency", "la moneda", "«USD», «COP», «MXN»", required=False),
        Slot("date", "la fecha", "«hoy», «ayer» o «2026-08-04»", required=False),
    ],
    "create_order": [
        Slot("customer", "el cliente", "«Grupo Andino»"),
        Slot("amount", "el monto", "«500.000 COP» o «1.200 USD»"),
        Slot("status", "el estado", "«aprobada», «pendiente», «rechazada»", required=False),
        Slot("gateway", "la pasarela", "«Wompi», «ePayco», «Bold», «Stripe (USD)»", required=False),
        Slot("currency", "la moneda", "«USD», «COP», «MXN»", required=False),
        Slot("date", "la fecha", "«hoy» o «2026-08-04»", required=False),
    ],
    "update_expense": [
        Slot("expense_id", "cuál gasto", "el ID de la tabla, o «el último»"),
        Slot(CHANGES, "qué desea cambiarle", "«el monto a 250 USD», «categoría Marketing»"),
    ],
    "update_order": [
        Slot("order_id", "cuál orden", "«ORD-4123» o «la última»"),
        Slot(CHANGES, "qué desea cambiarle", "«estado aprobada», «el monto a 800.000»"),
    ],
    "delete_expense": [
        Slot("expense_id", "cuál gasto", "el ID de la tabla, o «el último»"),
    ],
    "delete_order": [
        Slot("order_id", "cuál orden", "«ORD-4123» o «la última»"),
    ],
    # «Una factura por 500 USD» sin decir de qué: primero se resuelve si
    # documenta un gasto o una orden; el grafo re-planifica con la respuesta.
    "create_document": [
        Slot("record_kind", "si esto documenta un gasto o una orden",
             "«es un gasto» o «es una orden para <cliente>»"),
    ],
}

# Fields each edit may write; one of them must be present for the tool to run.
EDITABLE = {
    "update_expense": {"description", "category", "amount", "currency", "vendor", "owner", "date"},
    "update_order": {"customer", "amount", "currency", "status", "gateway", "date"},
}

# Same fields, in the order the agent names them when listing the options.
EDITABLE_ORDER = {
    "update_expense": ["description", "category", "amount", "currency", "vendor", "owner", "date"],
    "update_order": ["customer", "amount", "currency", "status", "gateway", "date"],
}

# Destroying data always waits for a yes.
CONFIRMS = {"delete_expense", "delete_order"}

ENTITY_OF = {
    "create_expense": "expenses", "update_expense": "expenses", "delete_expense": "expenses",
    "create_order": "orders", "update_order": "orders", "delete_order": "orders",
}

# Qué palabra de la respuesta resuelve el tipo del documento ambiguo.
KIND_WORDS = [
    (r"\b(gasto|egreso|pago|compra|nomina|proveedor|sueldo|salario)\w*\b", "create_expense"),
    (r"\b(orden|venta|cliente|pedido|cobro|ingreso)\w*\b", "create_order"),
]


def resolve_document_kind(text: str) -> str | None:
    """Which creation tool the answer to «¿gasto u orden?» points at."""
    normalized = parsing.normalize(text)
    for pattern, tool in KIND_WORDS:
        if re.search(pattern, normalized):
            return tool
    return None

# What each record is called when the agent talks about it.
RECORD_LABEL = {
    "create_expense": "el gasto", "update_expense": "el gasto", "delete_expense": "el gasto",
    "create_order": "la orden", "update_order": "la orden", "delete_order": "la orden",
    "create_document": "el documento",
}

ACTION_LABEL = {
    "create_expense": "registrar", "create_order": "registrar",
    "update_expense": "modificar", "update_order": "modificar",
    "delete_expense": "eliminar", "delete_order": "eliminar",
    "create_document": "emitir",
}

# Free-text answers only fill fields that are plain text; amounts, dates,
# statuses and gateways must come from the parser or they are asked again.
TEXT_SLOTS = {"description", "category", "vendor", "owner", "customer"}

CANCEL_WORDS = (r"\b(?:cancela|cancelar|olvidalo|olvida|dejalo|deja|anula|anular|"
                r"no importa|nada|abortar|aborta|mejor no|ya no)\w*\b")

DEFAULT_CURRENCY = "COP"


def is_record_tool(tool: str) -> bool:
    return tool in SLOTS


def is_creation(tool: str) -> bool:
    return tool in ("create_expense", "create_order")


def needs_confirmation(tool: str) -> bool:
    return tool in CONFIRMS


def is_cancel(text: str) -> bool:
    return bool(re.search(CANCEL_WORDS, parsing.normalize(text)))


def known_values(db: Session) -> dict:
    """Values already stored, so an answer reuses them instead of a variant."""
    return {
        "categories": [v for v in db.scalars(select(Expense.category).distinct()).all() if v],
        "vendors": [v for v in db.scalars(select(Expense.vendor).distinct()).all() if v],
        "owners": [v for v in db.scalars(select(Expense.owner).distinct()).all() if v],
        "customers": [v for v in db.scalars(select(Order.customer).distinct()).all() if v],
    }


def parse(db: Session, tool: str, text: str, known: dict | None = None) -> tuple[dict, list[dict]]:
    """Read a message as arguments for `tool`, plus candidates when ambiguous."""
    known = known if known is not None else known_values(db)
    entity = ENTITY_OF.get(tool)
    if tool == "create_expense":
        return parsing.parse_expense(text, known), []
    if tool == "create_order":
        return parsing.parse_order(text, known), []
    if entity is None:
        return {}, []

    arguments, candidates = targets.parse_edit(db, entity, text, known)
    if tool in CONFIRMS:
        # A deletion takes no field values, only the target.
        identifier = targets.ID_FIELD[entity]
        arguments = {identifier: arguments[identifier]} if identifier in arguments else {}
    return arguments, candidates


def pending(tool: str, arguments: dict) -> list[Slot]:
    """Slots still empty; an empty list means the tool can run."""
    missing = []
    for slot in SLOTS.get(tool, []):
        if slot.name == CHANGES:
            if not set(arguments) & EDITABLE.get(tool, set()):
                missing.append(slot)
        elif slot.required and not str(arguments.get(slot.name) or "").strip():
            missing.append(slot)
    return missing


def merge(db: Session, tool: str, arguments: dict, text: str,
          asked: str | None = None, candidates: list[dict] | None = None) -> dict:
    """Add to a draft everything a new message contributes."""
    merged = dict(arguments)
    entity = ENTITY_OF.get(tool, "")
    identifier = targets.ID_FIELD.get(entity)
    remainder = text

    # "¿cuál gasto?" is answered with an id, which the parser alone would not
    # read: a bare "24" carries no noun to attach it to. Once picked, that
    # number is cut out — otherwise answering "48" would also read as a new
    # amount of 48.
    if asked and asked == identifier and not merged.get(identifier):
        picked = _pick(entity, text, candidates or [])
        if picked is not None:
            merged[identifier], span = picked
            remainder = parsing.blank(text, [span])

    parsed, _ = parse(db, tool, remainder)
    for key, value in parsed.items():
        if value and (not merged.get(key) or (asked == CHANGES and key in EDITABLE.get(tool, ()))):
            merged[key] = value

    # The answer to "¿cuál es el concepto?" is the message itself, minus the
    # amounts and dates already understood.
    if asked in TEXT_SLOTS and not merged.get(asked):
        value = parsing.literal(text)
        if value:
            merged[asked] = value
    return merged


def _pick(entity: str, text: str, candidates: list[dict]):
    """Read the id the user answered with, checked against what was offered.

    Returns the id and the span it occupied, so the caller can cut it out.
    """
    offered = {str(row["id"]).upper() for row in candidates}
    normalized = parsing.normalize(text).upper()

    for match in re.finditer(r"ORD-?\s*\d{1,6}|\d{1,6}", normalized):
        digits = re.sub(r"\D", "", match.group(0))
        value = f"ORD-{digits}" if entity == "orders" else digits
        if not offered or value in offered:
            return (value if entity == "orders" else int(digits)), match.span()
    return None


# ------------------------------------------------------------------- storage

def load(conversation: Conversation | None) -> dict | None:
    draft = getattr(conversation, "draft", None) if conversation is not None else None
    if isinstance(draft, dict) and draft.get("tool") in SLOTS:
        return draft
    return None


def save(db: Session, conversation: Conversation | None, tool: str, arguments: dict,
         asked: str, candidates: list[dict] | None = None) -> None:
    if conversation is None:
        return
    conversation.draft = {
        "tool": tool, "arguments": arguments, "asked": asked,
        "candidates": candidates or [],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    db.commit()


def clear(db: Session, conversation: Conversation | None) -> None:
    if conversation is not None and getattr(conversation, "draft", None):
        conversation.draft = None
        db.commit()


# Rules broad enough to fire on a plain answer: naming "gastos" is not asking
# for the list of them when the agent just asked for a concept.
WEAK_RULES = {"records.expenses.generic", "analytics.summary"}

# Words that make a message a new question rather than an answer.
QUERY_WORDS = (r"\b(?:muestra|muestrame|dame|lista|listame|ver|cual(?:es)?|cuant[oa]s?|cuando|"
               r"donde|como|quien|porque|por\s+que|genera|grafica|exporta|importa|sincroniza|"
               r"resume|resumen|explica|analiza|revisa|compara|calcula|dime)\w*\b")

MAX_ANSWER_WORDS = 8


def continues(draft: dict, question: str, route) -> bool:
    """True when the message keeps filling the draft instead of changing subject.

    `route` is the deterministic router: when it recognises the message as
    another request, the user is no longer answering. Broad rules are the
    exception — a short reply that merely names "gastos" is still an answer.
    """
    tool = parsing.record_tool(question)
    if tool:
        return tool == draft["tool"]
    if "?" in question or "¿" in question or re.search(QUERY_WORDS, parsing.normalize(question)):
        return False
    plan = route(question)
    if plan is None:
        return True
    return plan.rule in WEAK_RULES and len(question.split()) <= MAX_ANSWER_WORDS


# ------------------------------------------------------------------ rendering

def ask(tool: str, arguments: dict, missing: list[Slot],
        candidates: list[dict] | None = None) -> ToolResult:
    """Ask for the first missing field and show what is already captured."""
    slot = missing[0]
    record = RECORD_LABEL.get(tool, "el registro")
    action = ACTION_LABEL.get(tool, "procesar")
    entity = ENTITY_OF.get(tool, "expenses")
    identifier = targets.ID_FIELD.get(entity)

    if slot.name == identifier and candidates:
        return _ask_which(tool, entity, action, record, candidates)

    question = f"Para {action} {record} me falta {slot.label}. Ej.: {slot.example}."
    if slot.name == "amount":
        question += f" Si no indica moneda, uso {DEFAULT_CURRENCY}."
    if slot.name == CHANGES:
        fields = ", ".join(_FIELD_LABELS.get(key, key) for key in EDITABLE_ORDER[tool])
        question += f" Puede cambiar: {fields}."

    output = [blocks.notice(question, tone="info")]
    items = _captured(tool, arguments) + [
        {"title": candidate.label.capitalize(), "detail": "pendiente", "tone": "warning"}
        for candidate in missing]
    if items:
        output.append(blocks.listing(items, title=f"Datos de {record}"))
    if len(missing) > 1:
        rest = ", ".join(candidate.label for candidate in missing[1:])
        output.append(blocks.notice(
            f"Después le pido: {rest}. Puede darme todo junto en un mensaje, "
            f"o escribir «cancelar» para descartarlo.", tone="info"))
    return ToolResult(blocks=output, summary=f"Falta {slot.label} para {action} {record}.")


def _ask_which(tool: str, entity: str, action: str, record: str,
               candidates: list[dict]) -> ToolResult:
    """Show the records the reference matched so the user names one."""
    return ToolResult(
        blocks=[
            blocks.notice(
                f"Encontré {len(candidates)} registros que encajan. ¿Cuál desea {action}? "
                f"Respóndame con el ID.", tone="info"),
            blocks.table(targets.CANDIDATE_COLUMNS[entity], candidates,
                         caption=f"Candidatos para {action} {record}"),
        ],
        summary=f"{len(candidates)} candidatos para {action} {record}.",
    )


def _captured(tool: str, arguments: dict) -> list[dict]:
    """The values already collected, as list items."""
    items = []
    for slot in SLOTS.get(tool, []):
        if slot.name in (CHANGES, CONFIRM):
            continue
        value = arguments.get(slot.name)
        if value:
            items.append({"title": slot.label.capitalize(),
                          "detail": _display(slot.name, value, arguments), "tone": "success"})
    if tool in EDITABLE:
        for key in sorted(set(arguments) & EDITABLE[tool]):
            items.append({"title": _FIELD_LABELS.get(key, key).capitalize(),
                          "detail": _display(key, arguments[key], arguments), "tone": "success"})
    return items


_FIELD_LABELS = {
    "description": "concepto", "category": "categoría", "vendor": "proveedor",
    "owner": "responsable", "amount": "monto", "currency": "moneda", "date": "fecha",
    "customer": "cliente", "status": "estado", "gateway": "pasarela",
    "expense_id": "gasto", "order_id": "orden",
}


def _display(name: str, value, arguments: dict) -> str:
    """Render a captured value the way the panel would."""
    if name == "amount":
        currency = arguments.get("currency") or DEFAULT_CURRENCY
        return f"{amount_text(float(value), currency)} {currency}"
    if name == "status":
        return status_label(str(value))
    return str(value)


def confirm(db: Session, tool: str, arguments: dict, again: bool = False) -> ToolResult:
    """Show what is about to be destroyed and wait for a yes."""
    entity = ENTITY_OF[tool]
    identifier = arguments.get(targets.ID_FIELD[entity])
    model = Expense if entity == "expenses" else Order
    record = db.get(model, identifier)
    label = RECORD_LABEL[tool]

    if record is None:
        return ToolResult(
            blocks=[blocks.notice(f"No encontré {label} {identifier}.", tone="warning")],
            summary="Registro no encontrado.")

    rows = targets._rows(entity, [record])
    warning = ("Necesito un sí o un no para seguir." if again
               else "Esto no se puede deshacer. ¿Lo elimino?")
    return ToolResult(
        blocks=[
            blocks.notice(warning, tone="warning"),
            blocks.table(targets.CANDIDATE_COLUMNS[entity], rows,
                         caption=f"{label.capitalize()} que se eliminaría"),
            blocks.notice("Responda «sí» para eliminarlo o «no» para dejarlo como está.",
                          tone="info"),
        ],
        summary=f"Esperando confirmación para eliminar {label} {identifier}.",
    )


def cancelled(tool: str | None = None) -> list[dict]:
    record = RECORD_LABEL.get(tool or "", "el registro")
    return [blocks.notice(f"Listo, descarté {record} que estábamos armando.", tone="info")]


def declined(tool: str | None = None) -> list[dict]:
    record = RECORD_LABEL.get(tool or "", "el registro")
    return [blocks.notice(f"No eliminé nada: {record} queda como estaba.", tone="info")]


def discarded(tool: str | None = None) -> dict:
    record = RECORD_LABEL.get(tool or "", "el registro")
    action = ACTION_LABEL.get(tool or "", "procesar")
    return blocks.notice(
        f"Dejé de lado {record} que iba a {action} porque cambió de tema. "
        f"Cuando quiera, pídamelo otra vez.", tone="warning")
