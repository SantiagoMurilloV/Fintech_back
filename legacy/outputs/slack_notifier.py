"""Slack notification with close totals + files (same pattern as Tasman/hotel agent)."""

from pathlib import Path

from close.models import CloseReport
from config.settings import SLACK_BOT_TOKEN, SLACK_CHANNEL_ID


def notify_close(report: CloseReport, files: list[Path]) -> bool:
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID:
        print("⚠️  Slack not configured (SLACK_BOT_TOKEN/SLACK_CHANNEL_ID) — skipping")
        return False

    from slack_sdk import WebClient

    client = WebClient(token=SLACK_BOT_TOKEN)
    status = "✅ Cierre cuadrado" if report.all_checks_passed else "⚠️ Cierre con pendientes"
    text = (
        f"{status} — *{report.account}* período *{report.period}*\n"
        f"• Banco: {report.bank_total:,.2f} · Libros: {report.books_total:,.2f} "
        f"· Diferencia: {report.difference:,.2f}\n"
        f"• Conciliación: {report.match.match_rate:.0%} "
        f"({len(report.match.unmatched_bank)} pendientes banco, "
        f"{len(report.match.unmatched_books)} pendientes libros)"
    )
    client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=text)
    for f in files:
        client.files_upload_v2(channel=SLACK_CHANNEL_ID, file=str(f), title=f.name)
    print(f"💬 Slack notified ({len(files)} files)")
    return True
