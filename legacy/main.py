"""
Fintech Close Agent — month-end close via the Tasman pattern
(Playwright over CDP + Claude Computer Use).

Usage:
  python main.py --period 2026-07                      # full run: collect + close
  python main.py --period 2026-07 --skip-collect \
      --bank-file downloads/banco_2026-07.xlsx \
      --books-file downloads/libros_2026-07.xlsx       # close over existing files
  python main.py --period 2026-07 --dry-run            # skip Slack
  python main.py --period 2026-07 --portal bancolombia # collect one portal only

Before a collection run, start Chrome with remote debugging and log in:
  /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222
"""

import argparse
import asyncio
import sys
from pathlib import Path

import yaml

from config.settings import PORTALS_CONFIG
from orchestrator.graph import close_phase, collect_phase
from utils.token_tracker import tracker


def parse_args():
    parser = argparse.ArgumentParser(description="Fintech Close Agent")
    parser.add_argument("--period", required=True, help="Close period, e.g. 2026-07")
    parser.add_argument("--dry-run", action="store_true", help="Skip Slack notification")
    parser.add_argument("--skip-collect", action="store_true", help="Skip browser collection")
    parser.add_argument("--portal", default=None, help="Collect only this portal")
    parser.add_argument("--bank-file", default=None, help="Bank statement (with --skip-collect)")
    parser.add_argument("--books-file", default=None, help="Books export (with --skip-collect)")
    return parser.parse_args()


def load_portals(only: str | None) -> list[dict]:
    if not PORTALS_CONFIG.exists():
        print(f"❌ {PORTALS_CONFIG} not found")
        sys.exit(1)
    portals = yaml.safe_load(PORTALS_CONFIG.read_text())["portals"]
    if only:
        portals = [p for p in portals if p["name"] == only]
        if not portals:
            print(f"❌ Portal '{only}' not in portals.yaml")
            sys.exit(1)
    return portals


async def main():
    args = parse_args()
    print(f"🚀 Fintech Close Agent — período {args.period}")

    if args.skip_collect:
        if not (args.bank_file and args.books_file):
            print("❌ --skip-collect requires --bank-file and --books-file")
            sys.exit(1)
        report = close_phase(
            args.period, "cuenta", Path(args.bank_file), Path(args.books_file),
            dry_run=args.dry_run,
        )
    else:
        from agent.llm_factory import get_anthropic_client

        portals = load_portals(args.portal)
        client = get_anthropic_client()
        downloads = await collect_phase(portals, args.period, client)
        if not downloads:
            print("❌ Nothing collected — aborting close")
            sys.exit(1)

        # Convention: the portal named 'books' (accounting) is the books side;
        # every other portal reconciles against it.
        books_file = downloads.pop("books", None)
        if not books_file:
            print("⚠️  No 'books' portal collected — reports skipped, files kept in downloads/")
            sys.exit(0)
        report = None
        for account, bank_file in downloads.items():
            report = close_phase(args.period, account, bank_file, books_file,
                                 dry_run=args.dry_run)

    print()
    print(tracker.summary())
    if report:
        print(f"\n{'✅ Cierre cuadrado' if report.all_checks_passed else '⚠️ Cierre con pendientes'}"
              f" — diferencia {report.difference:,.2f}")


if __name__ == "__main__":
    asyncio.run(main())
