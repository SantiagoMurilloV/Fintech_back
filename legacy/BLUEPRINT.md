# Fintech Close Agent — Blueprint (legacy)

> Month-end close agent based on the Tasman architecture:
> **Playwright (CDP) + Claude Computer Use**, applied to fintech operations.
> Draft written for refinement in a meeting. Superseded: the current product
> (Mandioca) replaced this flow with an external endpoint sync; the code in
> `legacy/` is kept for reference only.

---

## 1. Problem

The month-end close in a fintech/accounting operation requires touching systems
that **have no API (or an incomplete one)**: bank portals, payment gateways,
accounting platforms, tax-authority (DIAN) portals. Today that is manual work:
sign in to each portal, download statements, cross them against the books,
chase differences and assemble the close package.

## 2. The solution (the Tasman pattern)

Tasman proved a combination that works in production and that is reused here
as is:

| Layer           | Technology                                    | Role                                                                                                                                                                                      |
| --------------- | --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Session         | **Playwright `connect_over_cdp`**             | Attaches to the user's **real**, already authenticated Chrome. The human signs in (2FA included) once; the agent inherits the session. Credentials are never stored.                      |
| Perception      | **Claude Computer Use** (`computer_20250124`) | Looks at screenshots and decides what to do when the UI is uncertain: assess the portal's state, find the export button, read tables visually. Resilient to UI changes.                   |
| Critical action | **Deterministic Playwright**                  | Where reliability matters (downloading a file, setting a date range), direct selectors: fast, cheap, repeatable. Tasman lesson: CU decides, Playwright executes what is critical.          |
| Memory          | **Self-learned `.md` skills**                 | After each successful run the agent notes coordinates and observations ("the Export button is top right, x=1180,y=140"). Every run is cheaper and more accurate than the previous one.    |
| Guardrails      | Strict system prompt                          | Allow-list of clickable zones per portal. NEVER: transfer, pay, change settings, navigate outside the allowed module. Read/export only.                                                    |
| Cost            | Token tracker                                 | Tokens logged per call (CU is expensive; everything is measured).                                                                                                                         |

### The Computer Use loop

```
1. Screenshot of the page
2. Sent to Claude with the goal + previous skill notes
3. Claude returns actions (click, type, scroll, key…)
4. Playwright executes each action on the real Chrome
5. New screenshot as tool_result
6. Repeat until Claude answers in plain text (goal reached)
```

## 3. Fintech application: month-end close pipeline

```
┌────────────────────────────────────────────────────────────────────┐
│                    main.py --period 2026-07                        │
│                    (LangGraph orchestrator)                        │
└──────┬─────────────────────┬─────────────────────┬─────────────────┘
       ▼                     ▼                     ▼
┌──────────────┐   ┌──────────────────┐   ┌──────────────────┐
│ PHASE 1      │   │ PHASE 2          │   │ PHASE 3          │
│ COLLECTION   │──▶│ RECONCILIATION   │──▶│ CLOSE PACKAGE    │
│ (browser)    │   │ (deterministic)  │   │ (outputs)        │
└──────────────┘   └──────────────────┘   └──────────────────┘

PHASE 1 — Collection (Playwright + Computer Use, Tasman pattern):
  For each portal configured in portals.yaml:
    a. ensure_logged_in()   → CU assesses the state: signed in / needs human login
    b. navigate_to_module() → CU finds the movements/statements section
    c. set_period + export  → deterministic Playwright when a learned selector exists;
                              CU as fallback when the UI changed
    d. download             → Playwright's expect_download() captures the file
    e. update_skill()       → notes what was learned for the next run

PHASE 2 — Reconciliation (100% deterministic, no LLM — money = zero hallucination):
  - ETL: normalises statements (xlsx/csv/pdf) into a canonical movement schema
  - Matching engine: bank vs. books (exact → amount+date±3d → reference)
  - Differences: unreconciled items classified (the LLM only LABELS, never computes)
  - Close checklist: validations (balances match, no date gaps, etc.)

PHASE 3 — Close package:
  - Reconciliation Excel per account (matched / pending bank / pending books)
  - Executive summary PDF of the close
  - Slack notification with totals and the differences that need a human
```

## 4. Project layout

```
fintech_agent/
├── main.py                    # CLI: python main.py --period 2026-07 [--dry-run] [--portal X]
├── BLUEPRINT.md               # this document
├── README.md
├── requirements.txt
├── .env.example
├── portals.yaml               # portals to collect: type, URL, module, export format
├── config/
│   └── settings.py
├── browser/
│   ├── session.py             # CDP attach to the real Chrome (exact Tasman pattern)
│   ├── cu_client.py           # generic Computer Use loop (screenshot→action→repeat)
│   └── portals/
│       ├── base_portal.py     # hybrid base class: CU perception + Playwright action
│       ├── generic_bank.py    # generic skill-guided driver (any bank)
│       └── alegra_portal.py   # accounting (API when available, browser otherwise)
├── skills/
│   ├── skills_manager.py      # load/update of .md skills (Tasman)
│   └── <portal>/*.md          # skills per portal: find_export_button.md, set_date_range.md…
├── agent/
│   ├── llm_factory.py         # Claude = vision/CU · DeepSeek = cheap text nodes
│   └── prompts.py             # system prompts with per-portal guardrails
├── close/                     # close engine — deterministic-first
│   ├── etl.py                 # raw statement → canonical movements
│   ├── reconciliation.py      # bank vs. books matching engine
│   ├── checks.py              # close validation checklist
│   └── models.py              # schemas (Movement, MatchResult, CloseReport)
├── orchestrator/
│   └── graph.py               # LangGraph: collect → reconcile → report
├── outputs/
│   ├── excel_writer.py        # reconciliation .xlsx
│   ├── pdf_generator.py       # executive summary
│   └── slack_notifier.py
├── utils/
│   └── token_tracker.py       # (copied from Tasman)
├── downloads/                 # statements downloaded per run
└── output/                    # generated close package
```

## 5. Design decisions

1. **Money never goes through the LLM.** Reconciliation is deterministic
   arithmetic (pandas). The LLM only: perceives the UI (CU), extracts text from
   screenshots, classifies/labels differences and writes the summary. Same as
   financial-agent: deterministic-first.
2. **CU decides, Playwright executes.** Rule inherited from Tasman
   (`send_message`): actions with effect (clicking export, downloading) run
   through a Playwright selector when a learned skill exists; CU only when there
   is no known route. This lowers cost and removes the risk of CU
   "improvising".
3. **Read-only.** The agent never performs operations with financial effect on
   the portals. Hard guardrail in the system prompt + list of forbidden modules
   per portal.
4. **Human login.** As in Tasman: Chrome with `--remote-debugging-port=9222`,
   the human signs in (2FA included), the agent attaches over CDP. Zero bank
   credentials in code.
5. **Skills per portal.** Every portal has its own skills folder. A new bank =
   an entry in `portals.yaml` + letting the agent learn the UI in the first
   assisted run.
6. **Dual LLM** (hotel-booking-agent pattern): Claude for everything visual
   (required for CU), DeepSeek through `llm_factory` for text nodes
   (classifying differences, writing the summary).

## 6. Candidate portals (to be defined in the meeting)

| Portal                            | Via                          | What is extracted                        |
| --------------------------------- | ---------------------------- | ---------------------------------------- |
| Bancolombia / bank X              | Browser (CU+PW)              | Movement statement for the period        |
| Payment gateway (Wompi/PayU/…)    | Browser or API               | Settlements and fees                     |
| Alegra / Siigo                    | API when available, browser fallback | Bank ledger, invoices            |
| DIAN                              | Browser (CU+PW)              | Status of obligations (optional, phase 2)|

## 7. Implementation phases

- **F0 (today, autonomous):** full scaffolding + generic CU loop + skills
  manager + reconciliation engine over synthetic data + outputs. Everything
  compiles and the pipeline runs end to end against a local demo portal.
- **F1 (after the meeting):** define the real portals, first assisted run per
  portal (skill learning), adapt the ETL to the real statement formats.
- **F2:** full close checklist, DIAN, automatic monthly scheduling.

## 8. Questions for the meeting

1. Which portals exactly? How many bank accounts?
2. Is accounting on Alegra/Siigo/other? Is an API key available?
3. What close-package format does the accountant expect? Who receives it
   (Slack/email)?
4. Matching tolerances (days of offset, cents for fees)?
5. Which machine runs it: the analyst's (own Chrome) or a dedicated one?
