"""Backend configuration loaded from environment variables (.env)."""
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# --- Database (PostgreSQL) ---
def _sqlalchemy_url(url: str) -> str:
    """Name the psycopg driver in URLs handed out as postgres:// or postgresql://.

    Railway, Heroku and friends provide `postgresql://user:pass@host/db`;
    without the `+psycopg` suffix SQLAlchemy would look for psycopg2, which is
    not installed. Any URL that already names a driver is left untouched.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _sqlalchemy_url(os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://localhost:5432/fintech_agent",
))

# --- Panel auth ---
# Access tokens are signed JWTs; refresh tokens are opaque and revocable.
# Without JWT_SECRET a random one is generated at boot, which logs every
# session out on restart — fine locally, never in production.
JWT_SECRET = os.getenv("JWT_SECRET") or secrets.token_urlsafe(48)
JWT_SECRET_IS_EPHEMERAL = not os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "mandioca"

ACCESS_TOKEN_MINUTES = int(os.getenv("ACCESS_TOKEN_MINUTES", "30"))
REFRESH_TOKEN_DAYS = int(os.getenv("REFRESH_TOKEN_DAYS", "30"))

# First account, created on an empty database. Without a password one is
# generated and printed once at startup.
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "santiago@elhub.co").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or None
ADMIN_NAME = os.getenv("ADMIN_NAME", "Administrador")

# Failed logins tolerated per account before it is locked for a while.
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "8"))
LOGIN_LOCKOUT_MINUTES = int(os.getenv("LOGIN_LOCKOUT_MINUTES", "15"))

MIN_PASSWORD_LENGTH = 8

# --- Demo seed ---
# On an empty database the deterministic mock (May-July 2026) is loaded so a
# fresh clone has something to show. Set to false once real data flows in:
# without the flag, wiping the tables would just bring the demo back on the
# next restart.
SEED_DEMO = os.getenv("SEED_DEMO", "true").strip().lower() in ("true", "1", "yes", "si", "sí")

# --- Chat LLM: DeepSeek (OpenAI-compatible API) ---
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY") or None
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
CHAT_MODEL = os.getenv("CHAT_MODEL", "deepseek-chat")

# --- Cloudinary (media & documents) ---
# The SDK reads CLOUDINARY_URL from the environment (cloudinary://key:secret@cloud_name).
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL") or None
CLOUDINARY_FOLDER = os.getenv("CLOUDINARY_FOLDER", "fintech_agent")

# --- External endpoint (contract to be defined; structure is ready) ---
EXTERNAL_API_URL = os.getenv("EXTERNAL_API_URL") or None
EXTERNAL_API_KEY = os.getenv("EXTERNAL_API_KEY") or None
# "bearer" (Authorization: Bearer <token>) or "api-key" (Authorization: Api-Key <key>,
# the djangorestframework-api-key convention).
EXTERNAL_API_AUTH_SCHEME = os.getenv("EXTERNAL_API_AUTH_SCHEME", "bearer").strip().lower()

# --- Demo financial feed (routers/demo_feed.py) ---
# Key the built-in demo feed expects, mirroring the real provider's Api-Key
# auth. Not a secret: the feed serves deterministic fake data.
DEMO_FEED_KEY = os.getenv("DEMO_FEED_KEY", "demo.mandioca-feed")

# --- FX rates for USD-equivalent consolidation ---
# Dollar-pegged stablecoins are booked at par (1 USDT = 1 USD), the accounting
# convention for them. Mandioca settles its payouts in USDT/USDC, so leaving
# them without a rate would blind every USD total. The peg stays an explicit,
# configurable rate rather than a hard-coded truth.
FX_TO_USD = {
    "USD": 1.0,
    "COP": 1.0 / float(os.getenv("FX_COP_PER_USD", "4000")),
    "MXN": 1.0 / float(os.getenv("FX_MXN_PER_USD", "18")),
    "USDT": 1.0 / float(os.getenv("FX_USDT_PER_USD", "1")),
    "USDC": 1.0 / float(os.getenv("FX_USDC_PER_USD", "1")),
}
# Currencies whose USD rate is a peg assumption; sync reports say so.
STABLECOINS = ("USDT", "USDC")

# --- Public base URL, used to build links to locally stored files ---
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

# --- Company identity printed on generated PDFs ---
COMPANY_NAME = os.getenv("COMPANY_NAME", "Mandioca")
COMPANY_TAX_ID = os.getenv("COMPANY_TAX_ID", "")

# --- CORS: allowed frontend origins ---
CORS_ORIGINS = [o.strip() for o in os.getenv(
    "CORS_ORIGINS", "http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000"
).split(",") if o.strip()]
# Optional regex for origins that change per deploy, e.g. Vercel previews:
# CORS_ORIGIN_REGEX=https://fintech-.*\.vercel\.app
CORS_ORIGIN_REGEX = os.getenv("CORS_ORIGIN_REGEX") or None

# --- Sync bootstrap for a fresh database ---
# The sync is configured from the panel and lives in the `settings` table. A
# fresh deploy (Railway) has none of it, so these variables seed each setting
# ONCE, on the first boot that finds the key unset; whatever an admin saves in
# Configuración afterwards is never overwritten by the environment.
# Setting key -> value from the environment (only the ones actually set).
SETTINGS_FROM_ENV = {key: value.strip() for key, value in {
    "api.base_url": EXTERNAL_API_URL,
    "api.token": EXTERNAL_API_KEY,
    "api.auth_scheme": os.getenv("EXTERNAL_API_AUTH_SCHEME"),
    # Comma-separated feed paths relative to the base URL.
    "api.orders_path": os.getenv("EXTERNAL_API_ORDERS_PATHS"),
    "api.expenses_path": os.getenv("EXTERNAL_API_EXPENSES_PATHS"),
    "api.default_currency": os.getenv("EXTERNAL_API_DEFAULT_CURRENCY"),
    # JSON, our field -> feed field; fixes the mapping instead of re-deciding it.
    "intake.orders_override": os.getenv("EXTERNAL_API_ORDERS_MAPPING"),
    "intake.expenses_override": os.getenv("EXTERNAL_API_EXPENSES_MAPPING"),
    # SYNC_ENABLED switches on both the integration and the periodic pull.
    "api.enabled": os.getenv("SYNC_ENABLED"),
    "sync.pull_enabled": os.getenv("SYNC_ENABLED"),
    "sync.interval_seconds": os.getenv("SYNC_INTERVAL_SECONDS"),
}.items() if value is not None and str(value).strip()}

# Seconds after boot before the first background pull: long enough for the
# server to be answering, short enough that a fresh deploy fills up on its own.
SYNC_FIRST_PULL_SECONDS = int(os.getenv("SYNC_FIRST_PULL_SECONDS", "10"))
