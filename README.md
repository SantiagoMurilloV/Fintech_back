# Mandioca — B2B financial panel (API)

A standalone web application (no WhatsApp, no Telegram): a financial panel
with a conversational agent, installable as a **PWA**.

**Architecture: backend and frontend are fully separated, each in its own
repository.**

| Repository | Contents | Deployed on |
|---|---|---|
| [`Fintech_back`](https://github.com/SantiagoMurilloV/Fintech_back) (this one) | Python API (FastAPI) + PostgreSQL — data and logic | Railway |
| [`Fintech`](https://github.com/SantiagoMurilloV/Fintech) | Presentation PWA (ES modules) — everything visual | Vercel |

**Convention: code, routes, schema, prompts, comments, commit messages and
documentation are written in English.** Everything the end user reads (UI
labels, agent replies, data) is in Spanish.

## Separation of responsibilities

| | `Fintech_back` | `Fintech` |
|---|---|---|
| Returns / consumes | Raw numbers, ISO dates, codes (`approved`) | Formats them: `USD 512,4 k`, `31 jul`, `Aprobada` |
| Financial computation | Deterministic aggregation (`services/finance.py`) | None |
| Presentation | None | KPIs, bar heights, meter widths, printable PDF |
| Excel and PDF parsing | Yes (pandas, pypdf, row-by-row validation) | No |
| LLM agent | Deterministic node graph + tools | Only renders the blocks |

## The deterministic agent

The LLM **never computes**. A fixed node graph decides and executes:

```
plan  →  resolve  →  execute  →  render  →  narrate
```

- **plan** — a rule-based router (`agent/router.py`) maps the question to a
  tool. Only when no rule applies is the LLM consulted, and it may only pick a
  registered tool and its arguments (temperature 0).
- **resolve** — validates the arguments against the tool's JSON Schema and
  drops any invented field.
- **execute** — pure Python and SQL. Every figure is born here.
- **render** — the tool returns typed blocks (table, KPIs, chart, file, list,
  notice) that the frontend paints as real UI.
- **narrate** — an optional paragraph of prose, restricted to the tool's data.
  Without `DEEPSEEK_API_KEY` the agent still works end to end.

Every turn stores its trace in `agent_runs` (nodes, tool, arguments,
duration), so any answer can be audited and replayed. *Skills*
(`agent/skills/*.md`) are playbooks that fix thresholds and sequences.

### It also converses

Questions the database cannot answer — «¿qué es el EBITDA?», «¿cómo funciona
una stablecoin?», «¿me conviene facturar en dólares?» — are answered in prose
by `answer_question`, the way a colleague would. It is the only tool that
computes nothing, and it has two hard limits:

- **It stays within finance and crypto.** Outside that it replies in one
  sentence that the topic is not its job and comes back.
- **It never quotes company figures.** Those live in the database and belong
  to the data tools; if the question is about the company's own orders or
  expenses, it points to the panel instead of estimating.

The router tells the two families apart before the LLM is involved: a
conceptual question («¿qué riesgo tiene cobrar en USDT?») is not confused with
a data question («¿cuánto facturamos en julio?») even when they share words.

### Creating, editing and deleting records from the chat

Touching a record is the only case in which `execute` may wait. If the command
lacks a required value, if it is not clear **which** record it refers to, or if
it is going to delete something, the turn ends with a question and the
half-built call is kept as a draft in `conversations.draft`; the next message
completes it (`agent/drafts.py`). The agent asks for one thing at a time, shows
what it already has and accepts «cancelar».

```
— Registra un gasto
— Para registrar el gasto me falta el concepto. Ej.: «Licencias de diseño».
— Licencias de diseño
— Ahora el monto. Ej.: «200 USD». Si no indica moneda, uso COP.
— 200 USD
— Falta la categoría. Ej.: «Software», «Marketing», «Operación».
— software        →  Gasto «Licencias de diseño» registrado.
```

Editing works the same way, and when the reference matches several records the
agent lists the candidates instead of guessing: **a change on the wrong row is
worse than a question.**

```
— Edita el gasto de AWS
— Encontré 3 registros que encajan. ¿Cuál quiere modificar? Respóndame con el ID.
  [table: ID · Concepto · Categoría · Proveedor · Monto · Fecha]
— 21
— ¿Qué quiere cambiarle? Puede cambiar: concepto, categoría, monto, moneda,
  proveedor, responsable, fecha.
— el monto a 6.100 USD   →  Gasto 21 actualizado.  [antes 5.950 · ahora 6.100]
```

Deleting always goes through an explicit confirmation: the agent shows the row
that would disappear and waits for a «sí». A «no» never touches the database.

Every hard value is extracted by `agent/parsing.py` by rule, never by the LLM:
«500.000», «1,5 millones», «$800.000» and «2,5k USD» become numbers through the
same code every time, and dates («hoy», «ayer», «3 de julio»), status and
gateway are mapped to their codes. If the LLM planner proposes an amount, it is
discarded and read again from the message. Categories, vendors, owners and
customers are matched against the existing ones so the exact value is reused
instead of creating variants.

### Tools (26)

| Family | Tools |
|---|---|
| Analytics | `get_period_summary`, `get_kpi_dashboard`, `detect_anomalies` |
| Records | `list_orders`, `list_expenses`, `create_order`, `create_expense`, `update_order`, `update_expense`, `delete_order`, `delete_expense` |
| Schema | `add_column`, `remove_column`, `list_columns`, `set_cell` |
| Reports | `generate_chart`, `generate_financial_report`, `generate_invoice`, `generate_receipt`, `list_saved_reports` |
| Documents | `read_attachment`, `import_attachment`, `attach_receipt_to_expense` |
| External | `check_external_endpoint`, `sync_external_data` |
| Conversation | `answer_question` |

## Settings

At the bottom of the sidebar (**Configuración**). Any user can see it; **only an
administrator saves** data sources. Four blocks:

- **Appearance** — light or dark theme. A browser preference (localStorage):
  applies instantly, does not go through «Guardar» and needs no admin.
- **Data from the endpoint** — base URL, token and the orders and expenses
  paths. The **Probar** button calls the endpoint and shows what it returns:
  the payload shape, the fields of the first record and a sample.
- **Spreadsheet (Google Sheets)** — the sheet ID, the JSON of a service
  account (which must be shared with the sheet as editor) and the orders and
  expenses tabs. Its **Probar** verifies access and lists the columns and
  sample rows of each tab.
- **Sync** — both directions pause independently: pulling from the sheet and
  writing to it are separate switches, plus the polling interval.

### Sync from the endpoint

**Sincronizar ahora** (or the background loop, when the source is enabled and
the direction is not paused) brings orders and expenses from the endpoint:

- The mapping is decided by the **intake agent** (`services/intake.py`): the
  LLM looks at field names AND sample values and assigns each feed field to one
  of our columns. The decision is stored per payload shape (who made it and
  when) and replayed deterministically; without an LLM the synonym table
  decides alone. **The model only classifies columns: it never sees the full
  dataset nor touches a value.**
- A person has the last word: the **«Mapeo manual (JSON)»** fields in Settings
  override the agent's decision when it needs correcting (e.g. a crypto price
  feed where `symbol` is the asset, not the currency).
- **The copy is verified**: at the end of every run the records are re-read
  from the database and compared one by one against the source — exact amount,
  exact currency, total against total. The report carries the reconciliation
  (`source 4651.2921 · stored 4651.2921 · exact: true`); any difference is
  declared a failure. With money there is no "close enough".
- **Currencies and crypto**: the currency code is stored exactly as it arrives
  (USD, COP, USDT, WBTC…); only a known alias of a standard code is unified
  (`TETHER` → `USDT`, the same asset). Dollar-pegged stablecoins (USDT, USDC)
  count in USD totals at their peg — `FX_USDT_PER_USD` / `FX_USDC_PER_USD`,
  1 by default — and the report says so. A currency without a configured rate
  keeps its exact amount but **adds zero to USD totals** — an invented rate is
  worse than an explicit gap, and the report says that too.
- **The table follows the data, not a template**: the feed's own status text
  («Rejected signature to middleware») is kept on the row and shown in the
  table; our four codes only colour the badge, drive the filters and the
  totals. A status word the mapping does not know waits as `pending` — never
  as revenue — and the report names it. Each base column shows the source
  field it came from (`Cliente ← legal_name`), and an optional column no row
  fills (`gateway`, for a feed without one) is hidden instead of showing dashes.
- **Tables are dynamic**: every feed field that does not map to a base column
  becomes a visible column under its original name, created automatically and
  **typed from its values**: `"52.090000000"` is stored as a number,
  `"2026-09-08T00:20:54.521054-04:00"` as a datetime (seconds, offset kept),
  and a nested object such as `{"name": "United States", "code2": "US"}` is
  reduced to its name. The complete raw record is also kept in `extra._raw`.
  Nothing the source sends is silently discarded. With many columns the page
  switches to wide mode (less margin, no max width) and the table scrolls
  inside its card.
- It is **idempotent**: every record stores its external id, so re-syncing
  updates instead of duplicating.
- The **report** of each run stays in Settings: received, new, updated,
  skipped, the mapping applied and the problems (a row without an amount is
  not imported; it is counted and explained).

The inspectors («Probar») exist to look at the contract before syncing.
Values live in the `settings` table (one per row; a new option is a line in
`services/settings.py`, not a migration). Secrets are written but **never sent
back to the browser** — the API only says whether they are configured, and
saving the form without touching them does not erase them.

## Analysis derived from the data

The **Análisis** screen assumes no shape: it profiles what is there and builds
the indicators from it (`services/insights.py` → `GET /api/insights`). It is
laid out like a statistical report, each section with its title:

1. **Overview** — the totals of the dataset.
2. **Descriptive statistics** — mean, median, standard deviation, coefficient
   of variation, minimum, 25th/75th percentiles and maximum of the amounts,
   per entity; plus the numeric columns the source brought.
3. **Readings and inferences** — outliers by the IQR rule (Tukey),
   concentration (Pareto 80/20 over customers), high dispersion (CV ≥ 100 %
   with the recommendation to use the median), and data quality.
4. **Interpretation** — 3-4 points explaining what the figures mean, written
   by the model strictly over the values already computed.
5. **Distributions** — each chart with the type that answers its question:
   **box plots** (the five numbers of the table, drawn; one per entity, each
   with its own scale), **histograms** (the shape of the amounts), a **time
   line** (daily totals, only when dates vary), a **pie** for few categories
   and **horizontal bars** for many. Descriptive titles on top and 13–15 px
   type, readable without zoom.

The **Guardar reporte** button freezes the on-screen analysis as a PDF inside
**Reportes** (`POST /api/insights/report`): screen and document come from the
same list of blocks, so what is saved is exactly what was read.

The four PDFs (analysis, financial report, invoice and receipt) share one print
identity (`services/pdf.py`): a header with the brand's green band, serif type
for titles over sans for body, **justified** prose, horizontally ruled zebra
tables, KPI cards with an accent rule, and **real vector charts** (pies, bars
and lines drawn with the panel's palette) — every chart travels with its
numeric series and is redrawn in the document.

- **Discovers dimensions**: status, gateway, currency, customer, category,
  vendor, owner — and also **the columns the source brought** (numeric →
  sum/average; categorical → distribution). A dimension with a single value
  produces no chart.
- **Computed readings**: concentrations (top ≥ 40 %), outlier orders (≥ 5× the
  median), currencies without a rate with their exact sum set apart, expenses
  without a receipt, single-day records (an import footprint), imported vs.
  manual.
- The LLM contributes at most the **opening paragraph**, restricted to the
  figures already computed — without it the analysis is the same, just without
  prose.
- The backend returns the same **typed blocks** as the chat and the view paints
  them with the same renderer: if the data changes shape, the analysis changes
  with it without touching the frontend.

## Editing from the table

Every value in Orders and Expenses is edited where it is read: a click turns
it into a field, leaving the field (or Enter) saves, Escape discards. There is
no save button — **the row is the form**.

- Optimistic saving: the new value stays on screen while the request runs and
  **rolls back if the backend rejects it**, with the reason in the cell. It
  never keeps showing something the database does not have.
- The table does not reload on save: the row returned by the `PATCH` is
  applied (`hooks/useRowEdits.js`), so nothing blinks and the place is kept.
- Computed columns are read-only: their value comes from the formula.
- Validations are the same ones the agent uses — `services/records.py` is the
  single source of what each field accepts, whether the chat or the table
  writes it.

## Document viewer

PDFs (reports, invoices, receipts) open **inside the app**, in a modal, so the
conversation or the table that produced them stays in view. Images are shown
the same way; whatever cannot be previewed keeps its link. The viewer
authenticates with `?token=` because an `iframe` cannot send the
`Authorization` header.

## Authentication

Real login: accounts in the database, **bcrypt** passwords and a signed **JWT**
(HS256) for the API. There is no public sign-up — **accounts are created by an
administrator**, so the panel is never open to whoever has the URL.

| | What it is | Lives in | Used for |
|---|---|---|---|
| **access token** | Signed JWT (30 min) | localStorage | Every request; the API verifies it without touching the database |
| **refresh token** | Opaque string (30 days) | localStorage + SHA-256 hash in the database | Renewing the access token without signing in again |

The refresh token is deliberately **not** a JWT: since only its hash is stored,
a database leak hands out no sessions, and revoking it is updating a row
instead of waiting for a signature to expire. Every renewal **rotates** the
token; if an already rotated one shows up — the sign of a stolen copy — every
session of that account is closed.

Details that make it real rather than decorative:

- **Deactivating an account cuts access instantly**, without waiting for the
  JWT to expire: every request confirms the user is still active.
- **Same error for an unknown email and for a wrong password**, and the
  password is verified in both cases, so the endpoint cannot be used to find
  out who has an account.
- **Lockout on attempts**: 8 failures pause the account for 15 minutes.
- **The panel can never be left without an admin**: the last active
  administrator cannot be demoted, deactivated or deleted.
- System-generated passwords **are shown once** and must be changed on first
  sign-in. Only the hash is stored.
- Generated files (invoices, receipts, reports) **also require a session**.
  Since an `<iframe>` cannot send headers, the viewer passes the same JWT as
  `?token=`.

### First boot

On an empty database the administrator account is created from `.env`
(`ADMIN_EMAIL`, `ADMIN_NAME`). Without `ADMIN_PASSWORD` one is generated and
**printed once** at startup:

```
Admin account created: santiago@elhub.co
  Temporary password: 7yKq2mVt9pLxRn4B
  Change it on first sign-in.
```

Set `JWT_SECRET` in `.env`; otherwise a new one is generated on every boot and
every session drops on restart (the backend warns about it).

### User management

The **Usuarios** screen is only shown to administrators. From there an account
is created — the system returns the temporary password to hand to the person —,
signed out of every device, or given a new password.

Everything that is account data is edited in the same table, with a click:
**email, name, role, status and whether the password must be changed on
sign-in**. What the system writes — creation date, last access, open
sessions — is shown in grey and is not editable. The password appears nowhere:
only its hash lives in the database, and it is replaced by generating a new one.

### Roles

The role says **what the person does in the company, not what they may touch**:
everyone works with the same screens — orders, expenses, analysis, reports and
the agent. The only one with authority is **Administrador**, who also manages
who gets in: accounts, positions, passwords and sessions.

| Group | Positions |
|---|---|
| Access | Administrador · Miembro |
| Leadership and team | CEO · Cofundador · COO · CTO · CPO · CMO · CRO · Product Manager · Growth · Ingeniería · Diseño · Data · Ventas · Marketing · Soporte · Customer Success · People · Legal · Operaciones · Compras |
| Finance | CFO · Controller · Contabilidad · Tesorería · Cuentas por pagar · Cuentas por cobrar · Facturación · Cobranza · Nómina · Analista fin. · FP&A · Presupuesto · Auditoría · Cumplimiento · Riesgos · Impuestos · Inversionista |

Codes live in `models.ROLES`; visible names in the frontend's
`src/lib/labels.js` — the same split order statuses already use. Adding a
position is one line on each side.

## Stack

- **Backend**: FastAPI · PostgreSQL (SQLAlchemy) · **Cloudinary** or local disk ·
  **reportlab** (PDF) · **pypdf/Pillow** (attachments) · **DeepSeek** for
  planning and narration · a module ready for the **external endpoint**.
- **Frontend** (repository `Fintech`): PWA with no build step — its own
  micro-runtime with components and hooks (`src/core/runtime.js`), native ES
  modules, service worker and manifest. The service worker serves the code
  **network-first**: the app is a graph of modules that must all come from the
  same version, and serving them cache-first could hand out a new module next
  to an old one. Offline it still opens with the last good copy.
- Playwright + Computer Use are out of scope (earlier code in `legacy/`).

## Backend layout

```
app/
├── agent/
│   ├── graph.py      node graph (plan → resolve → execute → render → narrate)
│   ├── router.py     rule-based router (deterministic, no LLM)
│   ├── parsing.py    rule-based reading of amounts, currencies, dates and fields
│   ├── drafts.py     half-built records: asks for what is missing and keeps it
│   ├── targets.py    which record a message points at (or lists candidates)
│   ├── registry.py   tool registry with JSON Schema
│   ├── blocks.py     block contract consumed by the frontend
│   ├── tools/        analytics · records · schema · reporting · documents · external
│   └── skills/       .md playbooks (thresholds and sequences)
├── services/         finance · records (editing) · security (JWT + bcrypt) · settings
│                     api_sync (pull + typed columns) · intake · mapping
│                     sheets (Google Sheets) · charts (SVG) · insights
│                     pdf (reportlab) · documents · columns
│                     formula (safe evaluator) · storage · excel_import · media
├── routers/          auth · users · settings · orders · expenses · reports · columns
│                     imports · chat · files · integrations · demo_feed
└── models.py · seed.py · migrations.py · config.py · database.py · main.py
```

## Running locally

```bash
# 1. Database (local PostgreSQL)
createdb fintech_agent          # first time only

# 2. Backend (port 8000) — this repository
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env            # DEEPSEEK_API_KEY, CLOUDINARY_URL, EXTERNAL_API_KEY, etc.
./.venv/bin/python -m uvicorn app.main:app --port 8000 --reload

# 3. Frontend (port 3000) — repository Fintech
git clone https://github.com/SantiagoMurilloV/Fintech.git && cd Fintech
python3 -m http.server 3000     # or any static server
# open http://localhost:3000
```

With `SEED_DEMO=true` (the default), on first boot the backend creates the
tables and seeds the **deterministic demo mock** (May–July 2026: 3,554 orders,
July ≈ USD 512.4 k, +8.2 % vs. June). With `SEED_DEMO=false` an empty database
stays empty — the mode for working with real data.

The mock is deterministic: the same figures on every machine. To regenerate it,
set `SEED_DEMO=true`, empty the tables (or `dropdb && createdb`) and restart.

On first boot the administrator account is created and its temporary password
printed (see **Authentication**). The rest of the users are created from the
**Usuarios** screen.

## Automatic sync

While it runs, the backend makes a **periodic GET** to the external endpoint:
the first one `SYNC_FIRST_PULL_SECONDS` after boot and then every
`sync.interval_seconds` (15 s to 1 h, editable in **Configuración** without a
restart). The `api.enabled` and `sync.pull_enabled` switches pause it.

The sync configuration lives in the `settings` table. On an **empty** database
(first boot of a fresh deploy) it is seeded from the environment, once, and
only for keys that do not exist yet:

| Variable | Setting it seeds |
|---|---|
| `EXTERNAL_API_URL` | `api.base_url` |
| `EXTERNAL_API_KEY` | `api.token` |
| `EXTERNAL_API_AUTH_SCHEME` (`bearer` \| `api-key`) | `api.auth_scheme` |
| `EXTERNAL_API_ORDERS_PATHS` (comma-separated paths) | `api.orders_path` |
| `EXTERNAL_API_EXPENSES_PATHS` | `api.expenses_path` |
| `EXTERNAL_API_DEFAULT_CURRENCY` | `api.default_currency` |
| `EXTERNAL_API_ORDERS_MAPPING` (JSON, our field → feed field) | `intake.orders_override` |
| `EXTERNAL_API_EXPENSES_MAPPING` | `intake.expenses_override` |
| `SYNC_ENABLED` | `api.enabled` and `sync.pull_enabled` |
| `SYNC_INTERVAL_SECONDS` | `sync.interval_seconds` |

Whatever an administrator saves later in **Configuración** wins over the
environment. The sync is idempotent: every record carries its external id and
a second run updates instead of duplicating.

## Deploying on Railway

The repository ships `railway.json` (Railpack, `uvicorn` on `$PORT`, health
check on `/api/health`), a `Procfile` and `.python-version` (3.12).

1. In Railway: **New Project → Deploy from GitHub repo →** `Fintech_back`.
2. Add a **PostgreSQL** service and, on the backend service, the variable
   `DATABASE_URL=${{Postgres.DATABASE_URL}}` (a `postgresql://` URL is
   adapted to the `psycopg` driver automatically).
3. Backend variables (minimum):

   ```
   SEED_DEMO=false
   JWT_SECRET=<48+ random characters>
   ADMIN_EMAIL=...            ADMIN_PASSWORD=...
   DEEPSEEK_API_KEY=...       CLOUDINARY_URL=cloudinary://...
   CORS_ORIGINS=https://<frontend-domain>.vercel.app
   PUBLIC_BASE_URL=https://<backend-domain>.up.railway.app

   EXTERNAL_API_URL=https://api-dev.mandioca.global/api/v1/accountant/
   EXTERNAL_API_KEY=<AUXO key>
   EXTERNAL_API_AUTH_SCHEME=api-key
   EXTERNAL_API_ORDERS_PATHS=order-payout/
   EXTERNAL_API_DEFAULT_CURRENCY=USD
   EXTERNAL_API_ORDERS_MAPPING={"external_id":"pid","customer":"legal_name","amount":"amount_in","currency":"currency_in","status":"status","date":"created_at"}
   SYNC_ENABLED=true
   SYNC_INTERVAL_SECONDS=3600
   ```

4. Generate the service's public domain (**Settings → Networking**) and put it
   in `PUBLIC_BASE_URL` here and in the frontend's `API_BASE` on Vercel.

At startup the backend creates the schema, seeds the sync configuration from
those variables, creates the administrator account and makes the first GET
after 10 seconds; from there it repeats every hour. `storage/` is ephemeral on
Railway: receipts go to Cloudinary.

## What to ask the agent

| You ask | It does |
|---|---|
| «¿Cómo cerró el mes?» | KPIs + table of orders by status |
| «Muéstrame el dashboard de KPIs» | KPIs + 3 charts (week, gateway, categories) |
| «¿Hay alertas o anomalías?» | Fixed rules: rejections, gateway concentration, expense growth, missing receipts |
| «Muéstrame 5 órdenes rechazadas» | Filtered table |
| «Registra un gasto» · «Quiero ingresar una operación» | Asks for the missing values one by one and creates the record |
| «Registra un gasto de 200 USD en software, proveedor Figma» | Creates it in one go, no questions |
| «Cambia el monto del gasto 12 a 250 USD» · «Marca ORD-4123 como aprobada» | Edits the field and shows before/after |
| «Edita el gasto de AWS» | Lists the candidates and asks which one |
| «Elimina el gasto 12» | Shows the row and waits for a «sí» before deleting |
| «Crea una orden de 2,5 millones COP para Tienda Norte aprobada por Wompi» | Order with customer, amount, status and gateway |
| «Agrega a gastos una columna calculada IVA con la fórmula `amount * 0.19`» | Computed column, visible instantly in the Expenses table |
| «Elimina la columna IVA de gastos» | Removes the custom column (base columns are untouched) |
| *(in Orders)* «¿Y esto?» · «Las rechazadas» | Resolves against the screen being viewed |
| «Grafica el volumen por gateway en torta» | Chart saved in Reports |
| «Genera el reporte financiero en PDF» | PDF with KPIs, charts and tables |
| «Genera la factura de la orden ORD-4554» | Invoice PDF |
| «Sincroniza las órdenes del endpoint» | Imports from the external endpoint with configurable mapping |
| *(attach a PDF)* «¿Qué dice este documento?» | Extracts and shows the text |
| *(attach an Excel)* «Impórtalo como gastos» | Validates row by row and loads |
| *(attach an image)* «Adjunta este comprobante al gasto 12» | Links the file to the expense |

## API (raw data)

| Method | Route | Returns |
|---|---|---|
| `POST` | `/api/login` · `/api/refresh` · `/api/logout` | Session: JWT + rotating refresh |
| `GET` | `/api/me` · `POST /api/me/password` | Own account and password change |
| `GET/POST/PATCH/DELETE` | `/api/users…` | User management (admin only) |
| `GET/PUT` | `/api/settings` · `POST /api/settings/probe/{api,sheets}` | Settings and inspectors (save/probe: admin) |
| `POST` | `/api/settings/sync/pull` | Run one pull now |
| `GET` | `/api/orders?status&q&filters&date_from&date_to&limit&offset` | Orders + catalogs (`statuses`, `gateways`, `currencies`, `columns`), `source_fields`, `empty_fields` and `facets`. `q` searches every field of the row (base columns, feed columns, the raw record); `filters` is a JSON object `{column: value \| [values]}` over base or feed columns; each facet lists a column's existing values with counts |
| `POST` | `/api/orders` | Created order |
| `PATCH` | `/api/orders/{id}` · `/api/expenses/{id}` | Partial edit (editable table) |
| `GET` | `/api/orders/export.csv` | CSV of the rows the same `status`, `q`, `filters` and date range select |
| `GET` | `/api/expenses` | Expenses + numeric `stats` + `cloudinary` flag |
| `POST` | `/api/expenses` · `/api/expenses/{id}/receipt` | Expense / upload to Cloudinary |
| `GET` | `/api/reports?period` | Month aggregates + `deltas` |
| `GET` | `/api/reports/export.csv` | CSV |
| `GET` | `/api/insights` · `POST /api/insights/report` | Derived analysis and its PDF |
| `POST` | `/api/import/excel` · `GET /api/import/template/{kind}` | Import and templates |
| `GET` | `/api/conversations` · `POST /api/chat` | History and agent reply |
| `GET` | `/api/integrations/status` · `POST /probe` · `POST /sync` | External endpoint |
| `GET` | `/api/health` | Liveness (used by Railway) |

Interactive docs at `http://localhost:8000/docs`.

## PWA

Installable (manifest + maskable icons), works offline for the app shell (the
service worker never caches `/api`, so stale figures are never shown),
persistent light/dark theme and a mobile layout with a drawer.

## Design decisions

- **Money is never computed by the LLM**: deterministic aggregation in the
  backend; the chat receives a JSON of figures already computed and is
  forbidden from inventing numbers.
- **The frontend processes no data**: it only formats for display.
- Multi-currency consolidation to USD equivalent with rates configurable in
  `.env`.
