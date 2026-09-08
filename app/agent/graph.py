"""Agent execution graph.

Fixed node pipeline, executed in order on every turn:

    plan  ->  resolve  ->  execute  ->  render  ->  narrate

  plan     rules first (agent/router.py); then the learning layer
           (agent/learning.py), which replays how a repeated phrasing was
           resolved before; the LLM planner only runs for phrasings neither
           knows, and may only pick a registered tool plus arguments. What it
           decides is remembered, so each phrasing costs one model call ever.
  resolve  validates the arguments against the tool's JSON schema and drops
           anything unknown, so a hallucinated argument can never reach a tool.
  execute  runs the tool — pure Python and SQL. Every figure originates here.
  render   collects the blocks the tool produced (already deterministic).
  narrate  optional one-paragraph prose from the LLM, constrained to the tool
           output. If the LLM is unavailable the deterministic summary is used.

Creations (an expense, an order) are the one case where execute may be held
back: when a required field is missing the turn ends by asking for it and the
half-filled call is kept as a draft on the conversation (agent/drafts.py), so
the next message completes it instead of failing.

Every node appends to a trace that is persisted in `agent_runs`, so any answer
can be replayed and audited.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import AgentRun, Conversation
from ..services import llm
from . import blocks, drafts, learning, parsing, router, skills
from .registry import ToolResult, get as get_tool, schemas
from . import tools as _tools  # noqa: F401 — importing registers every tool

log = logging.getLogger("agent")

# Temperature 0: with the same prompt the planner returns the same tool call.
PLANNER_TEMPERATURE = 0.0
NARRATOR_TEMPERATURE = 0.2

PLANNER_PROMPT = """You are the planner of a financial assistant.

Pick exactly one tool that answers the user's question and return ONLY a JSON
object: {"tool": "<tool_name>", "arguments": {...}}.

Rules:
- Choose only from the provided tools. Never invent a tool or an argument.
- Never compute or guess numbers; the tool produces them.
- Omit arguments you cannot derive from the question — they have defaults.
- Periods use the YYYY-MM format.
- A conceptual finance or crypto question, a greeting or anything conversational
  goes to answer_question; pass the message as its `question` argument.
- If no tool clearly fits, return {"tool": "answer_question", "arguments": {}} —
  it will answer naturally or ask for the missing detail."""

NARRATOR_PROMPT = """You write one short paragraph introducing a financial result.

Hard rules:
- Use ONLY figures present in the JSON given to you. Never add, round or
  extrapolate numbers.
- Write amounts the way the panel does: compact USD equivalents such as
  "USD 512,4 k", percentages as "94,3%", counts as "1.284". Never print a raw
  float like 512400.04.
- Do not repeat the whole table: the user already sees it rendered.
- Two sentences maximum, formal Colombian Spanish — address the reader as
  «usted», never «vos» or «tú» —, no markdown, no bullet points.
- If the data shows nothing noteworthy, say so plainly."""


@dataclass
class AgentAnswer:
    blocks: list[dict]
    text: str
    intent: str
    planner: str
    trace: list[dict] = field(default_factory=list)


def run(db: Session, question: str, history: list[dict] | None = None,
        conversation_id: int | None = None, attachment_ids: list[int] | None = None,
        context: dict | None = None) -> AgentAnswer:
    """Execute one agent turn and return its blocks, text and trace.

    `context` describes where the user is in the app (e.g. {"view": "orders"}),
    so questions that point at the current screen resolve without ambiguity.
    """
    trace: list[dict] = []
    conversation = db.get(Conversation, conversation_id) if conversation_id else None
    draft = drafts.load(conversation)
    route = lambda text: router.route(text, context)  # noqa: E731 — passed to drafts
    abandoned, confirmed = None, False

    if draft and draft.get("asked") == drafts.CONFIRM:
        # A deletion is waiting for a yes; nothing else may be inferred here.
        verdict = parsing.decision(question)
        if verdict == "no":
            drafts.clear(db, conversation)
            plan = router.Plan(draft["tool"], {}, "rules", "cancel_draft", "draft.declined")
            _trace_plan(trace, plan)
            return _answer(db, conversation_id, question, plan,
                           drafts.declined(draft["tool"]), trace)
        if verdict == "yes":
            confirmed = True
        elif drafts.continues(draft, question, route):
            plan = router.Plan(draft["tool"], draft.get("arguments") or {}, "rules",
                               draft["tool"], "draft.confirm.again")
            _trace_plan(trace, plan)
            result = drafts.confirm(db, plan.tool, plan.arguments, again=True)
            return _answer(db, conversation_id, question, plan,
                           _node_render(result, trace), trace)
        else:
            drafts.clear(db, conversation)
            abandoned, draft = draft, None
    elif draft and drafts.is_cancel(question):
        # "cancelar" ends a pending operation without touching the database.
        drafts.clear(db, conversation)
        plan = router.Plan(draft["tool"], {}, "rules", "cancel_draft", "draft.cancel")
        _trace_plan(trace, plan)
        return _answer(db, conversation_id, question, plan,
                       drafts.cancelled(draft["tool"]), trace)
    elif draft and not drafts.continues(draft, question, route):
        # A message about something else leaves the draft behind; say so instead
        # of silently dropping data the user already gave.
        drafts.clear(db, conversation)
        abandoned, draft = draft, None

    if confirmed:
        plan = router.Plan(draft["tool"], draft.get("arguments") or {}, "rules",
                           draft["tool"], "draft.confirmed")
        _trace_plan(trace, plan)
    else:
        plan = _node_plan(db, question, trace, attachment_ids, context, draft)

    arguments = _node_resolve(plan, trace, attachment_ids, question)

    missing = [] if confirmed else drafts.pending(plan.tool, arguments)
    awaiting = bool(missing)
    if missing:
        result, failed = _node_ask(db, conversation, plan, arguments, missing, trace), False
    elif not confirmed and drafts.needs_confirmation(plan.tool):
        result, failed = _node_hold(db, conversation, plan, arguments, trace), False
        awaiting = True
    else:
        drafts.clear(db, conversation)
        result, failed = _node_execute(db, plan, arguments, trace)
        # The learning loop: an LLM decision that worked becomes a lookup for
        # the next identical phrasing; a replay that failed is unlearned.
        if plan.source == "llm" and not failed:
            learning.remember(db, question, plan.tool, arguments)
        elif plan.source == "learned" and failed:
            learning.forget(db, question)

    output_blocks = _node_render(result, trace)
    narrative = ("" if failed or awaiting
                 else _node_narrate(question, plan, result, trace, context))

    if narrative:
        output_blocks = [blocks.text(narrative)] + output_blocks
    if abandoned:
        output_blocks = [drafts.discarded(abandoned["tool"])] + output_blocks

    return _answer(db, conversation_id, question, plan, output_blocks, trace)


def _trace_plan(trace: list[dict], plan: router.Plan) -> None:
    trace.append({"node": "plan", "tool": plan.tool, "source": plan.source,
                  "rule": plan.rule, "ok": True, "ms": 0.0})


def _answer(db: Session, conversation_id: int | None, question: str,
            plan: router.Plan, output_blocks: list[dict], trace: list[dict]) -> AgentAnswer:
    _persist(db, conversation_id, question, plan, trace)
    return AgentAnswer(
        blocks=output_blocks,
        text=blocks.plain_text(output_blocks),
        intent=plan.intent or plan.tool,
        planner=plan.source,
        trace=trace,
    )


# --------------------------------------------------------------------- nodes

def _node_plan(db: Session, question: str, trace: list[dict],
               attachment_ids: list[int] | None, context: dict | None = None,
               draft: dict | None = None) -> router.Plan:
    started = time.perf_counter()

    if draft and draft["tool"] == "create_document":
        # «¿Documenta un gasto o una orden?» — con la respuesta se re-planifica
        # la creación real, re-parseando el pedido original más lo nuevo.
        resolved = drafts.resolve_document_kind(question)
        if resolved:
            source = (draft.get("arguments") or {}).get("source_text", "")
            combined = f"{source} {question}".strip()
            arguments, _ = drafts.parse(db, resolved, combined)
            arguments["with_receipt" if resolved == "create_expense"
                      else "with_invoice"] = True
            if resolved == "create_expense" and not arguments.get("description"):
                doc_words = re.finditer(
                    r"(?i)factura\w*|recibo\w*|comprobante\w*|genera\w*|hazme|"
                    r"emite\w*|quiero|necesito", source)
                cleaned = parsing.blank(source, [m.span() for m in doc_words])
                arguments["description"] = parsing.literal(cleaned) or None
                arguments = {key: value for key, value in arguments.items() if value}
            plan = router.Plan(resolved, arguments, "rules", resolved, "draft.document.kind")
        else:
            plan = router.Plan("create_document", draft.get("arguments") or {}, "rules",
                               "create_document", "draft.document.again")
    elif draft:
        # The message answers what the agent asked for: merge it into the values
        # already collected and keep aiming at the same tool.
        arguments = drafts.merge(db, draft["tool"], draft.get("arguments") or {}, question,
                                 draft.get("asked"), draft.get("candidates"))
        plan = router.Plan(draft["tool"], arguments, "rules", draft["tool"], "draft.resume",
                           candidates=draft.get("candidates") or [])
        if arguments.get(_id_field(draft["tool"])):
            plan.candidates = []      # the record is settled; stop offering the list
    else:
        plan = router.route(question, context)
        if plan is None:
            plan = learning.recall(db, question)
        if plan is None:
            plan = _llm_plan(db, question, context)

        # A fresh attachment makes document tools the natural default when the
        # question itself carries no other instruction.
        if attachment_ids and plan.tool == "get_period_summary" and plan.source != "rules":
            plan = router.Plan("read_attachment", {}, "rules", "read_document", "attachment.present")

        if drafts.is_record_tool(plan.tool):
            plan = _hydrate(plan, question, db)

    trace.append({
        "node": "plan", "tool": plan.tool, "source": plan.source, "rule": plan.rule,
        "view": (context or {}).get("view"), "draft": bool(draft),
        "ms": round((time.perf_counter() - started) * 1000, 1), "ok": True,
    })
    return plan


# Fields of a record that are always read from the message by agent/parsing.py.
PARSED_FIELDS = ("amount", "currency", "date", "status", "gateway")


def _id_field(tool: str) -> str:
    from .targets import ID_FIELD
    return ID_FIELD.get(drafts.ENTITY_OF.get(tool, ""), "")


def _hydrate(plan: router.Plan, question: str, db: Session) -> router.Plan:
    """Fill a record operation from the message itself.

    Amounts, currencies, dates, statuses and gateways suggested by the planner
    are discarded and re-extracted by rule: a figure written to the database
    must never originate in the model. Text fields (a concept, a customer) are
    kept when the parser could not find them, and which record is meant is
    resolved against the database, never guessed.
    """
    parsed, candidates = drafts.parse(db, plan.tool, question)
    arguments = {key: value for key, value in (plan.arguments or {}).items()
                 if key not in PARSED_FIELDS}
    for key, value in parsed.items():
        if value and not arguments.get(key):
            arguments[key] = value

    identifier = _id_field(plan.tool)
    # A planner-invented id is worth nothing: only a resolved record counts.
    if identifier and identifier in arguments and identifier not in parsed:
        from . import targets
        if not targets.exists(db, drafts.ENTITY_OF[plan.tool], arguments[identifier]):
            arguments.pop(identifier)

    # «Factura de pago de nómina por…»: el concepto es el pedido menos las
    # palabras de documento y el monto ya entendido.
    if arguments.get("with_receipt") and not arguments.get("description"):
        doc_words = re.finditer(
            r"(?i)factura\w*|recibo\w*|comprobante\w*|genera\w*|hazme|emite\w*|"
            r"quiero|necesito|dame", question)
        cleaned = parsing.blank(question, [m.span() for m in doc_words])
        description = parsing.literal(cleaned)
        if description:
            arguments["description"] = description

    plan.arguments = arguments
    plan.candidates = [] if arguments.get(identifier) else candidates

    # "Marca la orden ORD-4554 con centro de costo Comercial" edits a
    # user-defined column, which is set_cell's job, not an edit of base fields.
    if plan.tool in drafts.EDITABLE and not set(arguments) & drafts.EDITABLE[plan.tool]:
        redirect = _custom_column_plan(db, plan, question)
        if redirect is not None:
            return redirect
    return plan


def _custom_column_plan(db: Session, plan: router.Plan, question: str) -> router.Plan | None:
    """Turn an edit that names a user-defined column into a set_cell call."""
    from ..services import columns as columns_service

    entity = drafts.ENTITY_OF[plan.tool]
    identifier = plan.arguments.get(_id_field(plan.tool))
    if not identifier:
        return None

    normalized = parsing.normalize(question)
    for column in columns_service.list_columns(db, entity):
        if column.data_type == "formula":
            continue                      # computed columns are never written
        for name in (column.label, column.key.replace("_", " "), column.key):
            token = parsing.normalize(name)
            if not re.search(rf"(?<!\w){re.escape(token)}(?!\w)", normalized):
                continue
            value = parsing.capture_after(question, re.escape(name))
            if value:
                return router.Plan(
                    "set_cell",
                    {"entity": entity, "row_id": str(identifier),
                     "key": column.key, "value": value},
                    "rules", "set_cell", "records.set_cell")
    return None


def _llm_plan(db: Session, question: str, context: dict | None = None) -> router.Plan:
    """Ask the LLM to choose a tool. Falls back to the period summary."""
    fallback = router.Plan("answer_question", {}, "fallback", "answer_question", "no_rule")
    if not llm.is_available():
        return fallback

    catalog = [
        {"name": schema["function"]["name"],
         "description": schema["function"]["description"],
         "parameters": schema["function"]["parameters"]}
        for schema in schemas()
    ]
    active_skills = skills.select(question)
    skill_context = skills.content(active_skills)
    # How this company already asks for things: learned routings guide the
    # planner on brand-new phrasings that resemble known ones.
    learned = learning.examples(db)

    content = llm.complete(
        PLANNER_PROMPT,
        f"Tools:\n{json.dumps(catalog, ensure_ascii=False)}\n\n"
        + (f"Playbooks:\n{skill_context}\n\n" if skill_context else "")
        + (f"How this company usually asks (question -> tool already confirmed):\n"
           f"{json.dumps(learned, ensure_ascii=False)}\n\n" if learned else "")
        + f"The user is currently viewing: {router.view_label(context)}.\n"
        + f"Question: {question}",
        temperature=PLANNER_TEMPERATURE, max_tokens=400, json_object=True, caller="planner")
    if not content:
        return fallback
    try:
        payload = json.loads(content)
    except ValueError:
        log.warning("Planner returned invalid JSON; using fallback.")
        return fallback

    name = payload.get("tool")
    if not name or get_tool(name) is None:
        return fallback
    arguments = payload.get("arguments")
    return router.Plan(name, arguments if isinstance(arguments, dict) else {},
                       "llm", payload.get("intent") or name, None)


def _node_resolve(plan: router.Plan, trace: list[dict],
                  attachment_ids: list[int] | None, question: str = "") -> dict:
    """Keep only arguments declared in the tool schema, coerced to their type."""
    started = time.perf_counter()
    tool = get_tool(plan.tool)
    schema = (tool.parameters if tool else {}).get("properties", {})
    resolved: dict = {}

    for key, value in (plan.arguments or {}).items():
        if key not in schema or value is None:
            continue
        resolved[key] = _coerce(value, schema[key])

    # Document tools default to the attachment that came with the message.
    if attachment_ids and "attachment_id" in schema and "attachment_id" not in resolved:
        resolved["attachment_id"] = attachment_ids[-1]

    # The advisor answers what was actually written, not the planner's summary.
    if "question" in schema and not resolved.get("question"):
        resolved["question"] = question

    dropped = sorted(set(plan.arguments or {}) - set(resolved))
    trace.append({
        "node": "resolve", "tool": plan.tool, "args": resolved, "dropped": dropped,
        "ms": round((time.perf_counter() - started) * 1000, 1), "ok": True,
    })
    return resolved


def _coerce(value, spec: dict):
    """Best-effort cast to the schema type; invalid values are left untouched."""
    expected = spec.get("type")
    try:
        if expected == "integer":
            return int(value)
        if expected == "number":
            return float(value)
        if expected == "boolean":
            return value if isinstance(value, bool) else str(value).lower() in ("true", "1", "si", "sí")
        if expected == "string":
            return str(value)
    except (TypeError, ValueError):
        return value
    return value


def _node_execute(db: Session, plan: router.Plan, arguments: dict,
                  trace: list[dict]) -> tuple[ToolResult, bool]:
    started = time.perf_counter()
    tool = get_tool(plan.tool)
    if tool is None:
        trace.append({"node": "execute", "tool": plan.tool, "ok": False,
                      "error": "tool no registrada"})
        return ToolResult(blocks=[blocks.notice(
            f"La herramienta «{plan.tool}» no existe.", tone="danger")]), True

    try:
        result = tool.handler(db, **arguments)
        ok, error = True, None
    except TypeError as err:
        db.rollback()
        ok, error = False, f"argumentos inválidos: {err}"
        # Name what is missing instead of a generic failure.
        required = [name for name in tool.parameters.get("required", []) if name not in arguments]
        detail = (f" Me falta: {', '.join(required)}." if required
                  else " Reformule la petición con más detalle.")
        result = ToolResult(blocks=[blocks.notice(
            f"No pude ejecutar «{tool.name}».{detail}", tone="warning")])
    except Exception as err:  # noqa: BLE001 — surfaced to the user as a notice
        db.rollback()
        ok, error = False, str(err)
        result = ToolResult(blocks=[blocks.notice(str(err), tone="danger")])

    trace.append({
        "node": "execute", "tool": plan.tool, "args": arguments, "ok": ok, "error": error,
        "mutates": tool.mutates, "ms": round((time.perf_counter() - started) * 1000, 1),
    })
    return result, not ok


def _node_ask(db: Session, conversation: Conversation | None, plan: router.Plan,
              arguments: dict, missing: list, trace: list[dict]) -> ToolResult:
    """Hold the operation back and ask for the first thing still missing."""
    started = time.perf_counter()
    drafts.save(db, conversation, plan.tool, arguments, missing[0].name, plan.candidates)
    trace.append({
        "node": "execute", "tool": plan.tool, "args": arguments, "ok": True, "error": None,
        # Nothing was written to the records, so the panel must not refresh.
        "mutates": False, "awaiting": [slot.name for slot in missing],
        "candidates": len(plan.candidates),
        "ms": round((time.perf_counter() - started) * 1000, 1),
    })
    return drafts.ask(plan.tool, arguments, missing, plan.candidates)


def _node_hold(db: Session, conversation: Conversation | None, plan: router.Plan,
               arguments: dict, trace: list[dict]) -> ToolResult:
    """Show what a deletion would remove and wait for an explicit yes."""
    started = time.perf_counter()
    drafts.save(db, conversation, plan.tool, arguments, drafts.CONFIRM)
    trace.append({
        "node": "execute", "tool": plan.tool, "args": arguments, "ok": True, "error": None,
        "mutates": False, "awaiting": [drafts.CONFIRM],
        "ms": round((time.perf_counter() - started) * 1000, 1),
    })
    return drafts.confirm(db, plan.tool, arguments)


def _node_render(result: ToolResult, trace: list[dict]) -> list[dict]:
    started = time.perf_counter()
    output = result.blocks or [blocks.notice("La herramienta no devolvió contenido.", "warning")]
    trace.append({
        "node": "render", "blocks": [block["type"] for block in output],
        "ms": round((time.perf_counter() - started) * 1000, 1), "ok": True,
    })
    return output


def _node_narrate(question: str, plan: router.Plan, result: ToolResult,
                  trace: list[dict], context: dict | None = None) -> str:
    """One short paragraph of prose. Skipped when the LLM is not configured."""
    started = time.perf_counter()
    tool = get_tool(plan.tool)
    # A creation or edit already states its outcome in plain blocks; asking the
    # model to add prose there produced invented figures once — never again.
    if (not llm.is_available() or not result.data
            or (tool is not None and tool.mutates)):
        trace.append({"node": "narrate", "skipped": True, "ok": True, "ms": 0})
        return ""

    narrative = llm.complete(
        NARRATOR_PROMPT,
        f"Pregunta: {question}\n"
        f"Pantalla actual: {router.view_label(context)}\n"
        f"Herramienta: {plan.tool}\n"
        f"Datos: {json.dumps(result.data, ensure_ascii=False, default=str)[:4000]}",
        temperature=NARRATOR_TEMPERATURE, max_tokens=220, caller="narrator") or ""

    trace.append({
        "node": "narrate", "ok": True, "used_llm": bool(narrative),
        "ms": round((time.perf_counter() - started) * 1000, 1),
    })
    return narrative


def _persist(db: Session, conversation_id: int | None, question: str,
             plan: router.Plan, trace: list[dict]) -> None:
    """Store the trace so every answer stays auditable."""
    try:
        db.add(AgentRun(
            conversation_id=conversation_id, question=question,
            intent=plan.intent or plan.tool, planner=plan.source, trace=trace,
            created_at=datetime.now().isoformat(timespec="seconds"),
        ))
        db.commit()
    except Exception as err:  # noqa: BLE001 — telemetry must never break the answer
        db.rollback()
        log.warning("Could not persist the agent trace: %s", err)
