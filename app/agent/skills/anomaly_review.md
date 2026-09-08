# Skill: anomaly review

Applies when the user asks about alerts, anomalies, risks or "what looks wrong".

## Thresholds (fixed, see tools/analytics.py)
- Rejection rate >= 5% of the period's orders.
- >= 50% of rejections concentrated in a single gateway.
- Month-over-month expense growth >= 20%.
- Per-category expense growth >= 30%.
- Any expense without a receipt.

## Sequence
1. `detect_anomalies` for the period.
2. `list_orders` with `status=rejected` when the user wants the detail behind a
   rejection alert.
3. `list_expenses` with `missing_receipt=true` when the alert is about receipts.

## Rules
- State the threshold that triggered each alert so the user can judge severity.
- Do not invent causes: describe the pattern, then suggest what to check.
