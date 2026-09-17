"""ORM models.

Identifiers, columns and enum codes are English by convention. User-facing
content (customer names, expense descriptions, chat messages) is data and may
be in any language.
"""
from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

# Order status codes; the frontend maps them to display labels.
ORDER_STATUSES = ["approved", "pending", "rejected", "refunded"]
GATEWAYS = ["Wompi", "ePayco", "Bold", "Stripe (USD)"]
CURRENCIES = ["USD", "COP", "MXN", "USDT", "USDC"]

# Roles are job titles, not permission tiers: everyone works with the whole
# panel the same way. The only role that carries authority is `admin`, which
# additionally manages who gets in — accounts, roles and sessions.
ADMIN_ROLE = "admin"

ROLES = [
    ADMIN_ROLE, "miembro",
    # Startup
    "ceo", "cofundador", "coo", "cto", "cpo", "cmo", "cro",
    "growth", "product_manager", "ingenieria", "diseno", "datos",
    "ventas", "marketing", "soporte", "customer_success",
    "people", "legal", "operaciones", "compras",
    # Finanzas
    "cfo", "controller", "contabilidad", "tesoreria",
    "cuentas_por_pagar", "cuentas_por_cobrar", "facturacion", "cobranza",
    "nomina", "analista_financiero", "fpa", "presupuesto",
    "auditoria_interna", "compliance", "riesgos", "impuestos", "inversionista",
]

# Entities that accept user-defined columns.
CUSTOM_COLUMN_ENTITIES = ["orders", "expenses"]
CUSTOM_COLUMN_TYPES = ["text", "number", "date", "datetime", "boolean", "formula"]

# JSONB on PostgreSQL, plain JSON elsewhere (keeps tests/SQLite portable).
JsonColumn = JSON().with_variant(JSONB(), "postgresql")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    customer: Mapped[str] = mapped_column(String(120))
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(10), default="COP")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    gateway: Mapped[str | None] = mapped_column(String(30), nullable=True)
    date: Mapped[str] = mapped_column(String(10), index=True)  # ISO YYYY-MM-DD
    # Values for user-defined columns, keyed by CustomColumn.key.
    extra: Mapped[dict] = mapped_column(JsonColumn, default=dict)


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    description: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(60))
    vendor: Mapped[str | None] = mapped_column(String(120), nullable=True)
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(10), default="COP")
    owner: Mapped[str | None] = mapped_column(String(80), nullable=True)  # responsible person
    date: Mapped[str] = mapped_column(String(10), index=True)
    receipt_name: Mapped[str | None] = mapped_column(String(200), nullable=True)  # file label
    receipt_url: Mapped[str | None] = mapped_column(Text, nullable=True)          # stored file URL
    extra: Mapped[dict] = mapped_column(JsonColumn, default=dict)


class CustomColumn(Base):
    """A user-defined column added to orders or expenses.

    `data_type == "formula"` makes it a computed column: `formula` is evaluated
    deterministically per row (see services/formula.py) and never stored.
    """
    __tablename__ = "custom_columns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity: Mapped[str] = mapped_column(String(20), index=True)   # "orders" | "expenses"
    key: Mapped[str] = mapped_column(String(60))                  # snake_case identifier
    label: Mapped[str] = mapped_column(String(80))                # header shown in the UI
    data_type: Mapped[str] = mapped_column(String(20), default="text")
    formula: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(19))


class Report(Base):
    """A generated report: chart spec + rendered SVG + optional PDF file."""
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(40))       # "chart" | "financial" | "invoice" | "receipt"
    period: Mapped[str | None] = mapped_column(String(7), nullable=True)  # YYYY-MM
    spec: Mapped[dict] = mapped_column(JsonColumn, default=dict)  # inputs used to build it
    svg: Mapped[str | None] = mapped_column(Text, nullable=True)  # inline chart for the web
    pdf_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(19), index=True)


class Attachment(Base):
    """A file uploaded through the chat composer, with extracted content."""
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(200))
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(20))        # "pdf" | "image" | "sheet" | "text" | "other"
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict] = mapped_column(JsonColumn, default=dict)  # pages, rows, columns, dimensions…
    created_at: Mapped[str] = mapped_column(String(19))


class Setting(Base):
    """Runtime configuration, editable from the panel.

    What lives here instead of .env is what an operator changes without a
    deploy: the external endpoint, the spreadsheet, and the switches that pause
    each direction of the sync. Secrets are stored here too and never travel
    back to the frontend — see services/settings.py.
    """
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[object] = mapped_column(JsonColumn, nullable=True)
    updated_at: Mapped[str] = mapped_column(String(19))
    updated_by: Mapped[str | None] = mapped_column(String(160), nullable=True)


class IntegrationConnection(Base):
    """A live OAuth link to an external system (today: QuickBooks Online).

    One row per provider: the panel is one company, so "connected" means one
    QuickBooks company (realm). Tokens are stored encrypted (services/
    quickbooks.py) and never leave the server — the frontend only sees the
    status, the company name and the dates.

    `status`: connected | needs_reconnect | disconnected. The second one is
    set when Intuit refuses our refresh token (revoked by the user, rotated
    elsewhere, expired): the only fix is a person clicking "Reconectar".
    """
    __tablename__ = "integration_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    environment: Mapped[str] = mapped_column(String(20), default="sandbox")
    # Intuit's company id; every API URL carries it.
    realm_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # ISO 4217 code the books are kept in; stamped on records without one.
    home_currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)      # encrypted
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)     # encrypted
    access_expires_at: Mapped[str | None] = mapped_column(String(25), nullable=True)   # UTC ISO
    refresh_expires_at: Mapped[str | None] = mapped_column(String(25), nullable=True)  # UTC ISO
    status: Mapped[str] = mapped_column(String(20), default="connected")
    connected_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    connected_at: Mapped[str | None] = mapped_column(String(19), nullable=True)
    last_sync_at: Mapped[str | None] = mapped_column(String(19), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Incremental sync watermark: records updated after this instant are
    # fetched on the next pull. Null means "start from the configured date".
    cursor: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[str | None] = mapped_column(String(19), nullable=True)


class User(Base):
    """Someone who can sign in. Accounts are created by an admin, never
    self-registered, so the panel is never open to whoever finds the URL."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # bcrypt hash — the password itself is never stored, logged or returned.
    password_hash: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20), default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Set when an admin creates or resets the account: the user must choose a
    # new password before doing anything else.
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(19))
    last_login_at: Mapped[str | None] = mapped_column(String(19), nullable=True)


class RefreshToken(Base):
    """A live session.

    The token is opaque (not a JWT) and stored hashed: a leaked database gives
    no usable session, and revoking one is a row update instead of waiting for
    a signature to expire.
    """
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[str] = mapped_column(String(19))
    created_at: Mapped[str] = mapped_column(String(19))
    revoked_at: Mapped[str | None] = mapped_column(String(19), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[str] = mapped_column(String(19))
    # Creation being filled in over several turns: {tool, arguments, asked}.
    # Holds validated values only — see agent/drafts.py.
    draft: Mapped[dict | None] = mapped_column(JsonColumn, nullable=True, default=None)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.id"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(10))  # "user" | "agent"
    content: Mapped[str] = mapped_column(Text)
    # Structured render blocks (tables, KPIs, charts, files) for the web chat.
    blocks: Mapped[list] = mapped_column(JsonColumn, default=list)
    created_at: Mapped[str] = mapped_column(String(19))

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Learning(Base):
    """A learned routing: a question shape that already resolved to a tool.

    The learning layer is how the agent gets faster with use. The first time a
    phrasing needs the LLM planner, the outcome is recorded here; from then on
    the same phrasing routes by lookup — instant and deterministic. Entries
    count their hits (the most useful ones feed the planner as examples) and
    are forgotten the moment they mislead. See agent/learning.py.
    """
    __tablename__ = "agent_learnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # The normalized question text; exact-match lookup keeps replay safe.
    pattern: Mapped[str] = mapped_column(String(300), unique=True, index=True)
    tool: Mapped[str] = mapped_column(String(60))
    # Replayable arguments only: time-relative values are recomputed on use.
    arguments: Mapped[dict] = mapped_column(JsonColumn, default=dict)
    hits: Mapped[int] = mapped_column(Integer, default=1)
    last_used_at: Mapped[str] = mapped_column(String(19), index=True)
    created_at: Mapped[str] = mapped_column(String(19))


class AgentRun(Base):
    """Execution trace of one agent turn: which nodes and tools ran, with what.

    Kept so every answer is auditable and reproducible — the core of the
    deterministic design.
    """
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    question: Mapped[str] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(String(60))
    planner: Mapped[str] = mapped_column(String(20))   # "rules" | "llm"
    trace: Mapped[list] = mapped_column(JsonColumn, default=list)  # [{node, tool, args, ms, ok}]
    created_at: Mapped[str] = mapped_column(String(19))
