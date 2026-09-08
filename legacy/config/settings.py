"""Central configuration. Secrets come from .env — never hardcode them."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).parent.parent
DOWNLOADS_DIR = PROJECT_ROOT / "downloads"
OUTPUT_DIR = PROJECT_ROOT / "output"
DATA_DIR = PROJECT_ROOT / "data"
PORTALS_CONFIG = PROJECT_ROOT / "portals.yaml"

for _d in (DOWNLOADS_DIR, OUTPUT_DIR, DATA_DIR):
    _d.mkdir(exist_ok=True)

# ── LLMs (dual-provider pattern from hotel-booking-agent) ────────────────────
# Claude is mandatory for Computer Use / vision; DeepSeek handles cheap text
# nodes (classification, summaries) and can be swapped via llm_factory.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CU_MODEL = os.getenv("CU_MODEL", "claude-opus-4-5")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
TEXT_MODEL = os.getenv("TEXT_MODEL", "deepseek-chat")

# ── Reconciliation ───────────────────────────────────────────────────────────
MATCH_DATE_TOLERANCE_DAYS = int(os.getenv("MATCH_DATE_TOLERANCE_DAYS", "3"))
MATCH_AMOUNT_TOLERANCE = float(os.getenv("MATCH_AMOUNT_TOLERANCE", "0.01"))

# ── Outputs ──────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID", "")
COMPANY_NAME = os.getenv("COMPANY_NAME", "Mi Empresa")
