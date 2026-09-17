"""Mandioca API — FastAPI + PostgreSQL.

House rule: ALL processing (Excel parsing, aggregation, charts, PDFs, agent
reasoning) happens here and is exposed as raw data or render blocks. The
frontend (fintech_front/) owns every visual concern.
"""
import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import migrations
from .config import (
    CORS_ORIGIN_REGEX, CORS_ORIGINS, DEEPSEEK_API_KEY, JWT_SECRET_IS_EPHEMERAL, SEED_DEMO,
    SYNC_FIRST_PULL_SECONDS,
)
from .database import Base, SessionLocal, engine
from .routers import (
    auth, chat, columns, demo_feed, expenses, files, imports, integrations, orders,
    reports, settings as settings_router, users,
)
from .seed import ensure_admin, seed_if_empty
from .services import media
from .services import settings as settings_service


def _pull_tick() -> int:
    """One background-sync heartbeat; returns the seconds until the next one.

    Runs in a worker thread with its own session. The interval is re-read on
    every tick, so changing it in Configuración applies without a restart, and
    the api.enabled / sync.pull_enabled switches pause it the same way.
    """
    from .services import api_sync, quickbooks
    from .services import settings as settings_service

    log = logging.getLogger("sync")
    with SessionLocal() as db:
        values = settings_service.all_values(db)
        interval = int(values.get("sync.interval_seconds") or 60)
        pull_enabled = values.get("sync.pull_enabled")
        should_run = (values.get("api.enabled") and pull_enabled
                      and str(values.get("api.base_url") or "").strip())
        if should_run:
            try:
                api_sync.pull(db)
            except Exception as err:  # noqa: BLE001 — one source failing must not block the other
                log.warning("Endpoint pull failed: %s", err)
        # QuickBooks pulls on the same clock, only while connected and switched on.
        if pull_enabled and values.get("quickbooks.enabled") and quickbooks.is_connected(db):
            try:
                quickbooks.pull(db)
            except Exception as err:  # noqa: BLE001
                log.warning("QuickBooks pull failed: %s", err)
    return max(15, interval)


async def _sync_loop():
    """The periodic GET against the external endpoint, for the life of the app.

    The first tick comes shortly after boot so a fresh deploy fills itself
    from the feed; afterwards each tick decides the next delay from the
    configured interval. A failing feed is logged and retried, never fatal.
    """
    log = logging.getLogger("sync")
    delay = SYNC_FIRST_PULL_SECONDS
    while True:
        await asyncio.sleep(delay)
        try:
            delay = await asyncio.to_thread(_pull_tick)
        except Exception as err:  # noqa: BLE001 — the loop must survive bad feeds
            log.warning("Background pull failed: %s", err)
            delay = 60


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Create tables, top up columns added after the first deploy, then seed.
    Base.metadata.create_all(engine)
    applied = migrations.apply(engine)
    if applied:
        print(f"Schema updated: {', '.join(applied)}")
    with SessionLocal() as db:
        if SEED_DEMO and seed_if_empty(db):
            print("Demo seed loaded (deterministic mock).")
        created = ensure_admin(db)
        if created:
            print(f"Admin account created: {created['email']}")
            if created["password"]:
                # Shown once. Set ADMIN_PASSWORD in .env to choose it yourself.
                print(f"  Temporary password: {created['password']}")
                print("  Change it on first sign-in.")
        # A fresh database takes the sync configuration from the environment.
        try:
            seeded = settings_service.bootstrap_from_env(db)
        except ValueError as err:
            seeded = []
            print(f"WARNING: sync settings from env were rejected: {err}")
        if seeded:
            print(f"Sync settings seeded from env: {', '.join(seeded)}")

    if JWT_SECRET_IS_EPHEMERAL:
        print("WARNING: JWT_SECRET is not set — sessions end on every restart.")
    print(f"Chat: {'DeepSeek' if DEEPSEEK_API_KEY else 'deterministic only (DEEPSEEK_API_KEY missing)'}")
    print(f"Cloudinary: {'configured' if media.is_configured() else 'NOT configured (local storage)'}")

    # The periodic pull runs as long as the app does; it stops with it.
    sync_task = asyncio.create_task(_sync_loop())
    yield
    sync_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sync_task


app = FastAPI(title="Mandioca API", version="0.4.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (auth.router, users.router, settings_router.router, orders.router, expenses.router, reports.router,
               imports.router, chat.router, integrations.router, columns.router,
               files.router, demo_feed.router):
    app.include_router(router)


@app.get("/api/health")
def health():
    from .agent import learning
    from .agent.registry import all_tools

    with SessionLocal() as db:
        learned = learning.stats(db)
    return {
        "ok": True,
        "chat": "deepseek" if DEEPSEEK_API_KEY else "deterministic-only",
        "cloudinary": media.is_configured(),
        # Tool count doubles as a smoke check that the agent registry loaded.
        "agent_tools": len(all_tools()),
        # How much the agent has learned from this company's own phrasing.
        "agent_learning": learned,
    }
