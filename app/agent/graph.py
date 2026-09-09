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

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgentRun, Conversation
from ..services import llm
from . import blocks, conversation, drafts, formatting, learning, parsing, router, skills
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
- The recent conversation may be given. A message that corrects or refines the
  previous request («no, con el monto más grande», «¿y en agosto?») is the
  previous request with that change applied: plan it that way.
- If no tool clearly fits, return {"tool": "answer_question", "arguments": {}} —
  it will answer naturally or ask for the missing detail."""

NARRATOR_PROMPT = """You are the voice of a financial assistant in a chat. You receive the
recent conversation, the user's latest message and the data the panel computed
for it (JSON). Write the reply that goes above the rendered result.

Hard rules:
- Answer the question that was asked, directly, as a knowledgeable colleague
  would. Formal Colombian Spanish: «usted», never «vos» or «tú». Two or three
  sentences, no markdown, no bullet points.
- If the user greeted or thanked you, acknowledge it briefly first.
- Use ONLY figures present in the JSON. Every figure there is already written
  exactly as the panel shows it ("USD 139,00", "USD 512,4 k", "94,3%",
  "1.284"): copy figures character by character. Never convert, abbreviate or
  re-scale them, never add or drop a suffix such as "k", "mil" or "millones",
  and never derive a new number of your own (no sums, differences, ranks or
  percentages).
- If the question asks for something the data does not contain (a ranking,
  a cause, another period), say plainly what the data does show and what is
  missing, and suggest how to ask for it. Never answer beyond the data.
- You may add one short interpretation grounded in the panel's criteria below,
  and relate the result to the previous turn when that helps. Never invent
  causes.
- When a computed answer sentence is given to you, say it in your own words as
  your first sentence: the user does not see the tool's sentence when yours
  exists. Never write «el resultado ya lo indica» or refer to the table as
  already saying it. Then add at most two sentences: an acknowledgement, one
  interpretation or a next step.
- Only suggest actions the panel offers (listed below). Never mention modules
  or reports that do not exist.
- Do not repeat the whole table: the user already sees it rendered.
- If the data shows nothing noteworthy, say so plainly."""

# Three sentences is the ceiling the prompt asks for; the cut enforces it.
NARRATOR_MAX_SENTENCES = 3


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
        plan = _node_plan(db, question, trace, attachment_ids, context, draft, history,
                          conversation_id)

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
        result, failed = _node_execute(db, plan, arguments, trace, history, context)
        # The learning loop: an LLM decision that worked becomes a lookup for
        # the next identical phrasing; a replay that failed is unlearned.
        if plan.source == "llm" and not failed and not _looks_like_refinement(question):
            learning.remember(db, question, plan.tool, arguments)
        elif plan.source == "learned" and failed:
            learning.forget(db, question)

    output_blocks = _node_render(result, trace)
    narrative = ("" if failed or awaiting
                 else _node_narrate(question, plan, result, trace, context, history))

    if narrative:
        # A tool that opens with its own answer sentence (ranking tools mark it
        # as `summary`) hands that line to the narrator, who has just said it in
        # the conversation's own words. One voice, not two saying the same.
        if (output_blocks and output_blocks[0].get("type") == "text"
                and output_blocks[0].get("content") == result.summary):
            output_blocks = output_blocks[1:]
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

# «¿Y en agosto?», «¿y las rechazadas?», «y el top 10»: a short message that
# only changes one thing about the previous request.
REFINEMENT_MAX_WORDS = 7
_REFINEMENT_OPENER = re.compile(r"^[\s¿¡]*(y|no|mejor|ahora|pero)\b")


def _looks_like_refinement(question: str) -> bool:
    normalized = router.normalize(question)
    return (len(normalized.split()) <= REFINEMENT_MAX_WORDS
            and bool(_REFINEMENT_OPENER.search(normalized)))


def _previous_call(db: Session, conversation_id: int | None) -> tuple[str, dict] | None:
    """Tool and arguments of the last executed turn in this conversation."""
    if not conversation_id:
        return None
    last = db.scalars(select(AgentRun).where(AgentRun.conversation_id == conversation_id)
                      .order_by(AgentRun.id.desc()).limit(1)).first()
    if last is None:
        return None
    step = next((s for s in (last.trace or [])
                 if s.get("node") == "execute" and s.get("ok") and s.get("tool")), None)
    return (step["tool"], dict(step.get("args") or {})) if step else None


def _refinement(db: Session, conversation_id: int | None, question: str) -> router.Plan | None:
    """The previous request with one argument changed. No model involved."""
    if not _looks_like_refinement(question):
        return None
    normalized = router.normalize(question)
    changes: dict = {}
    period = router.extract_period(question)
    if period:
        changes["period"] = period
    status = router._status(normalized)
    if status:
        changes["status"] = status
    if re.search(r"\b(top|primer[oa]s|ultim[oa]s|las|los)\s+\d{1,3}\b", normalized):
        changes["limit"] = router._limit(question)
    if not changes:
        return None
    previous = _previous_call(db, conversation_id)
    if previous is None:
        return None
    tool_name, arguments = previous
    tool = get_tool(tool_name)
    if tool is None or tool.mutates or tool.conversational:
        return None
    accepted = tool.parameters.get("properties", {})
    if any(key not in accepted for key in changes):
        return None
    return router.Plan(tool_name, {**arguments, **changes}, "rules", tool_name, "refine.previous")


def _node_plan(db: Session, question: str, trace: list[dict],
               attachment_ids: list[int] | None, context: dict | None = None,
               draft: dict | None = None, history: list[dict] | None = None,
               conversation_id: int | None = None) -> router.Plan:
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
            plan = _refinement(db, conversation_id, question)
        if plan is None and not _looks_like_refinement(question):
            # A refinement depends on the previous turn: it is never a
            # phrasing worth remembering on its own.
            plan = learning.recall(db, question)
        if plan is None:
            plan = _llm_plan(db, question, context, history)

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


def _llm_plan(db: Session, question: str, context: dict | None = None,
              history: list[dict] | None = None) -> router.Plan:
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
        + f"Today is {datetime.now().date().isoformat()}; a month named without a year is "
          f"its most recent occurrence that is not in the future.\n"
        + f"The user is currently viewing: {router.view_label(context)}.\n"
        + (f"Recent conversation (oldest first):\n"
           f"{json.dumps(conversation.recent_turns(history, 4, 400), ensure_ascii=False)}\n\n"
           if history else "")
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
    arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
    # A month named in the question is resolved by the rule-based extractor,
    # never by the model: it does not know which year «agosto» is.
    period = router.extract_period(question)
    if period and "period" in get_tool(name).parameters.get("properties", {}):
        arguments["period"] = period
    return router.Plan(name, arguments, "llm", payload.get("intent") or name, None)


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


def _node_execute(db: Session, plan: router.Plan, arguments: dict, trace: list[dict],
                  history: list[dict] | None = None,
                  context: dict | None = None) -> tuple[ToolResult, bool]:
    started = time.perf_counter()
    tool = get_tool(plan.tool)
    if tool is None:
        trace.append({"node": "execute", "tool": plan.tool, "ok": False,
                      "error": "tool no registrada"})
        return ToolResult(blocks=[blocks.notice(
            f"La herramienta «{plan.tool}» no existe.", tone="danger")]), True

    call = dict(arguments)
    if tool.conversational:
        # A talking tool needs what was said before and where the user is.
        call.update(history=history, view=router.view_label(context))

    try:
        result = tool.handler(db, **call)
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
                  trace: list[dict], context: dict | None = None,
                  history: list[dict] | None = None) -> str:
    """The assistant's reply above the result. Skipped when the LLM is not configured."""
    started = time.perf_counter()
    tool = get_tool(plan.tool)
    # A creation or edit already states its outcome in plain blocks; asking the
    # model to add prose there produced invented figures once — never again.
    if (not llm.is_available() or not result.data
            or (tool is not None and tool.mutates)):
        trace.append({"node": "narrate", "skipped": True, "ok": True, "ms": 0})
        return ""

    # Money, percentages and counts reach the model already formatted; it only
    # has to copy them. Formatting is arithmetic, and arithmetic is not its job.
    presented = formatting.present(result.data)
    criteria = skills.content(skills.select(question) or ["monthly_close"])
    stated = " ".join(block["content"] for block in result.blocks if block.get("type") == "text")
    capabilities = "\n".join(f"- {registered.examples[0]}" for registered in _tools_with_examples())
    messages = [
        *conversation.recent_turns(history, 6, 1200),
        {"role": "user", "content":
            f"{question}\n\n"
            f"[Pantalla actual: {router.view_label(context)} · Herramienta: {plan.tool}]\n"
            + (f"[Respuesta calculada por la herramienta: {stated}]\n" if stated else "")
            + f"[Datos calculados: {json.dumps(presented, ensure_ascii=False, default=str)[:4000]}]"},
    ]
    narrative = llm.chat(
        NARRATOR_PROMPT
        + (f"\n\nPanel criteria:\n{criteria}" if criteria else "")
        + f"\n\nWhat the panel can do:\n{capabilities}",
        messages, temperature=NARRATOR_TEMPERATURE, max_tokens=260, caller="narrator") or ""
    if narrative:
        narrative = formatting.first_sentences(formatting.strip_markdown(narrative),
                                               NARRATOR_MAX_SENTENCES)

    # A money figure that neither the data nor the conversation contains,
    # verbatim, is an invention — «USD 139,0 k» for 139 dollars once reached a
    # reader. The prose is dropped and the blocks, which are computed, stand
    # alone. Figures from earlier turns are allowed: comparing with them is
    # what a conversation does.
    backing = json.dumps(presented, ensure_ascii=False, default=str) + "\n" \
        + conversation.transcript(history)
    unbacked = formatting.unbacked_figures(narrative, backing) if narrative else []
    if unbacked:
        log.warning("narrator dropped: figures not in data %s", unbacked)
        narrative = ""

    trace.append({
        "node": "narrate", "ok": True, "used_llm": bool(narrative),
        "rejected_figures": unbacked or None,
        "ms": round((time.perf_counter() - started) * 1000, 1),
    })
    return narrative


def _tools_with_examples():
    from .registry import all_tools
    return [registered for registered in all_tools()
            if registered.examples and registered.name != "answer_question"]


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
