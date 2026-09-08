# Skill: monthly close

Applies when the user asks about closing a month, an executive summary, or how
the business performed.

## Sequence
1. `get_period_summary` — headline KPIs and order mix.
2. `detect_anomalies` — anything that needs attention before closing.
3. `generate_financial_report` — only when the user asks for a PDF or a document.

## Rules
- Never restate a figure that is not in the tool output.
- Always name the comparison month when quoting a delta.
- If expenses are missing receipts, mention it before declaring the month closed.
