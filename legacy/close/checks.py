"""Month-end close checklist — deterministic validations over the reconciliation."""

from calendar import monthrange
from datetime import date

from close.models import CheckResult, CloseReport, MatchResult, Movement


def run_checks(report: CloseReport, bank: list[Movement], books: list[Movement]) -> list[CheckResult]:
    checks = [
        _check_totals_difference(report),
        _check_match_rate(report.match),
        _check_period_coverage(bank, report.period, "bank"),
        _check_period_coverage(books, report.period, "books"),
        _check_duplicates(bank, "bank"),
        _check_duplicates(books, "books"),
    ]
    report.checks = checks
    for c in checks:
        print(f"   {'✅' if c.passed else '❌'} {c.name}: {c.detail}")
    return checks


def _check_totals_difference(report: CloseReport) -> CheckResult:
    diff = report.difference
    return CheckResult(
        name="totals_match",
        passed=abs(diff) < 1.0,
        detail=f"bank {report.bank_total:,.2f} vs books {report.books_total:,.2f} → diff {diff:,.2f}",
    )


def _check_match_rate(match: MatchResult, threshold: float = 0.90) -> CheckResult:
    rate = match.match_rate if match else 0.0
    return CheckResult(
        name="match_rate",
        passed=rate >= threshold,
        detail=f"{rate:.0%} (threshold {threshold:.0%})",
    )


def _check_period_coverage(movs: list[Movement], period: str, side: str) -> CheckResult:
    """All movements must fall inside the close period (YYYY-MM)."""
    year, month = map(int, period.split("-"))
    first = date(year, month, 1)
    last = date(year, month, monthrange(year, month)[1])
    outside = [m for m in movs if not (first <= m.txn_date <= last)]
    return CheckResult(
        name=f"period_coverage_{side}",
        passed=len(outside) == 0,
        detail=f"{len(outside)} movements outside {period}" if outside else f"all inside {period}",
    )


def _check_duplicates(movs: list[Movement], side: str) -> CheckResult:
    seen, dups = set(), 0
    for m in movs:
        key = (m.txn_date, m.amount, m.reference or m.description)
        if key in seen:
            dups += 1
        seen.add(key)
    return CheckResult(
        name=f"duplicates_{side}",
        passed=dups == 0,
        detail=f"{dups} suspected duplicates" if dups else "none",
    )
