"""
Dual-LLM factory (pattern from hotel-booking-agent):
  - Claude    → Computer Use + vision (mandatory, no substitute)
  - DeepSeek  → cheap text nodes (classify unmatched items, draft summaries)

Swap TEXT_MODEL/DEEPSEEK_* in .env to change the text provider later.
"""

import json

import anthropic

from config.settings import ANTHROPIC_API_KEY, DEEPSEEK_API_KEY, TEXT_MODEL


def get_anthropic_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def classify_unmatched(items: list[dict]) -> list[dict]:
    """
    LLM labels unmatched reconciliation items (bank_fee, timing_difference, …).
    Amounts/dates are never modified — validated after the call.
    """
    if not items:
        return []
    if not DEEPSEEK_API_KEY:
        print("⚠️  DEEPSEEK_API_KEY missing — unmatched items left unclassified")
        return items

    from openai import OpenAI

    from agent.prompts import CLASSIFY_DIFFERENCES_PROMPT

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    prompt = CLASSIFY_DIFFERENCES_PROMPT.format(items_json=json.dumps(items, ensure_ascii=False))
    response = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    raw = response.choices[0].message.content.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        labeled = json.loads(raw)
    except json.JSONDecodeError:
        print("⚠️  Classifier returned invalid JSON — items left unclassified")
        return items

    # Hard guard: LLM may only ADD 'category'; any tampering discards its output
    if len(labeled) != len(items):
        return items
    for orig, new in zip(items, labeled):
        if any(new.get(k) != orig.get(k) for k in ("amount", "date", "reference")):
            return items
    return labeled
