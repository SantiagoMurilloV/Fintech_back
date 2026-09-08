"""
Close pipeline orchestrator.

F0: deterministic sequential pipeline (collect → ETL → reconcile → checks →
outputs). Migrate to LangGraph in F1 if the flow gains branching/human-in-
the-loop nodes — the phase functions below map 1:1 to graph nodes.
"""

from calendar import monthrange
from pathlib import Path

from close.checks import run_checks
from close.etl import load_statement
from close.models import CloseReport
from close.reconciliation import reconcile
from outputs.excel_writer import write_reconciliation_xlsx
from outputs.slack_notifier import notify_close
from outputs.summary_report import write_summary


def period_bounds(period: str) -> tuple[str, str]:
    year, month = map(int, period.split("-"))
    last_day = monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"


async def collect_phase(portal_configs: list[dict], period: str, client) -> dict:
    """FASE 1 — browser collection via Tasman pattern. Returns {portal: file}."""
    from playwright.async_api import async_playwright

    from browser.portals.generic_bank import GenericBankPortal
    from browser.session import get_browser_context, get_page

    date_from, date_to = period_bounds(period)
    downloads = {}

    async with async_playwright() as p:
        context = await get_browser_context(p)
        for cfg in portal_configs:
            page = await get_page(context, cfg.get("url_hint", ""))
            portal = GenericBankPortal(page, client, cfg)
            print(f"\n━━ Collecting {portal.name} ━━")
            path = await portal.collect(date_from, date_to, period)
            if path:
                downloads[portal.name] = path
    return downloads


def close_phase(
    period: str,
    account: str,
    bank_file: Path,
    books_file: Path,
    dry_run: bool = False,
) -> CloseReport:
    """FASE 2+3 — deterministic close over already-downloaded files."""
    bank = load_statement(bank_file, source=account, side="bank")
    books = load_statement(books_file, source="books", side="books")

    report = CloseReport(period=period, account=account)
    report.bank_total = round(sum(m.amount for m in bank), 2)
    report.books_total = round(sum(m.amount for m in books), 2)
    report.match = reconcile(bank, books)

    # LLM only LABELS unmatched items — never computes
    from agent.llm_factory import classify_unmatched

    unmatched_payload = [
        {"side": m.side, "date": str(m.txn_date), "amount": m.amount,
         "description": m.description, "reference": m.reference}
        for m in report.match.unmatched_bank + report.match.unmatched_books
    ]
    labeled = classify_unmatched(unmatched_payload)
    for mov, lab in zip(report.match.unmatched_bank + report.match.unmatched_books, labeled):
        mov.category = lab.get("category", "")

    run_checks(report, bank, books)

    xlsx = write_reconciliation_xlsx(report)
    summary = write_summary(report)
    if not dry_run:
        notify_close(report, [xlsx, summary])
    return report
