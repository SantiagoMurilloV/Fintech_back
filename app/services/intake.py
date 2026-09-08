"""Intake agent: decides where each external field belongs.

The split of responsibilities is the whole design:

  the agent DECIDES     which source field is the amount, which is the date,
                        which is the customer — looking at field names AND
                        sample values, so `title` beats `description` when the
                        samples show one is a name and the other a paragraph.
  the code COPIES       every value, verbatim. The model never sees the full
                        dataset, never rewrites a number, never fills a gap.

A decision is made once per payload shape (the sorted field list), stored in
settings with who made it and when, and reused deterministically until the
shape changes. If the model is unavailable the synonym table decides alone —
the sync never depends on the LLM being up.

Whatever the mapping, api_sync verifies the copy afterwards, row by row and
sum by sum: a wrong mapping can misplace a column, but it cannot alter money.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Setting
from . import llm, mapping

log = logging.getLogger("intake")

# Targets the agent may assign, with the meaning the model is told.
TARGETS = {
    "orders": {
        "external_id": "unique id of the record in the source",
        "customer": "who paid / the client (short name)",
        "amount": "the monetary amount of the order (number)",
        "currency": "what the AMOUNT is denominated in. NOT the name or ticker "
                    "of an asset/product the record is about: in a crypto price "
                    "feed, `symbol` is the asset, not the denomination.",
        "status": "payment state whose samples read like paid/pending/failed. "
                  "Never a number, a rank or a score.",
        "gateway": "payment processor name (Stripe, Wompi). Never a URL.",
        "date": "when the order happened",
    },
    "expenses": {
        "external_id": "unique id of the record in the source",
        "description": "short name of what was paid for",
        "category": "expense category / type",
        "vendor": "who was paid (provider)",
        "owner": "person responsible",
        "amount": "the monetary amount (number)",
        "currency": "what the AMOUNT is denominated in — never the asset or "
                    "product the record is about",
        "date": "when it was paid",
    },
}

SAMPLE_ROWS = 4
SAMPLE_VALUE_LEN = 80

PROMPT = """You classify the fields of an external financial feed.

Given the target schema and the source fields with sample values, return ONLY
a JSON object: {"mapping": {"<target>": "<source_field>", ...}}.

Rules:
- Map a target only when a source field clearly holds that meaning; omit it
  otherwise. Never guess and never invent field names.
- Use each source field for at most one target.
- Look at the sample VALUES, not just the names: a field whose samples are
  long paragraphs is not a good short name; one whose samples look like
  "PAID"/"PENDING" is a status.
- When unsure, OMIT the target: an unmapped field still shows up as its own
  column, which is safe — a wrong mapping is not. This matters most for
  `currency`: only map it when the field states what the amount is priced in.
- You are classifying columns, not transforming data. No values in your output."""


def _signature(fields: list[str]) -> str:
    return hashlib.sha256("|".join(sorted(map(str, fields))).encode()).hexdigest()[:16]


def _store_key(entity: str, signature: str) -> str:
    # One decision per payload shape: several feeds of the same entity keep
    # their own mapping instead of overwriting a single one.
    return f"intake.map.{entity}.{signature}"


def _valid(proposal: dict, entity: str, fields: list[str]) -> dict[str, str]:
    """Keep only assignments that name real targets and real, unused fields."""
    result: dict[str, str] = {}
    used: set[str] = set()
    for target, source in (proposal or {}).items():
        if target not in TARGETS[entity] or not isinstance(source, str):
            continue
        if source not in fields or source in used:
            continue
        result[target] = source
        used.add(source)
    return result


def _ask_model(entity: str, fields: list[str], records: list[dict]) -> dict[str, str]:
    samples = {
        field: [str(r.get(field))[:SAMPLE_VALUE_LEN] for r in records[:SAMPLE_ROWS]]
        for field in fields
    }
    content = llm.complete(
        PROMPT,
        json.dumps({
            "entity": entity,
            "targets": TARGETS[entity],
            "source_fields": samples,
        }, ensure_ascii=False),
        temperature=0.0, max_tokens=300, json_object=True, caller="intake")
    if not content:
        return {}
    try:
        proposal = json.loads(content).get("mapping", {})
    except ValueError:
        log.warning("Intake model returned invalid JSON; falling back to synonyms.")
        return {}
    return _valid(proposal, entity, fields)


def mapping_for(db: Session, entity: str, records: list[dict]) -> tuple[dict[str, str], str]:
    """The field mapping for this payload shape, plus who decided it.

    Order: a stored decision for the same shape wins (deterministic replay);
    otherwise the model proposes and synonyms fill what it left out; without a
    model, synonyms alone. The decision is persisted so the next sync with the
    same shape does not consult anyone.
    """
    fields = list(records[0].keys())
    signature = _signature(fields)

    # A manual override is the human correcting the agent: it wins, always.
    from . import settings as settings_service
    raw_override = settings_service.get(db, f"intake.{entity}_override")
    if raw_override:
        try:
            override = _valid(json.loads(raw_override), entity, fields)
            if override:
                return override, "manual"
        except ValueError:
            log.warning("Override de %s no es JSON válido; lo ignoro.", entity)

    row = db.get(Setting, _store_key(entity, signature))
    stored = row.value if row is not None else None
    if isinstance(stored, dict) and stored.get("signature") == signature:
        return dict(stored.get("mapping") or {}), stored.get("decided_by", "guardado")

    by_synonyms = mapping.guess_mapping(fields, entity)
    by_model = _ask_model(entity, fields, records)

    # The model saw the sample values, so it wins where both have an opinion;
    # synonyms cover the targets it left unassigned.
    final = dict(by_synonyms)
    final.update(by_model)
    decided_by = "agente" if by_model else ("sinónimos" if by_synonyms else "nadie")

    stamp = datetime.now().isoformat(timespec="seconds")
    decision = {"signature": signature, "fields": sorted(map(str, fields)),
                "mapping": final, "decided_by": decided_by, "decided_at": stamp}
    if row is None:
        db.add(Setting(key=_store_key(entity, signature), value=decision, updated_at=stamp))
    else:
        row.value, row.updated_at = decision, stamp
    db.commit()

    log.info("Intake %s: %s decided %s", entity, decided_by, final)
    return final, decided_by
