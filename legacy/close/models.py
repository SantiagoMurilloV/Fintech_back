"""Canonical schemas for the close engine. Pure dataclasses — no LLM anywhere."""

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Movement:
    """One transaction, normalized from any source (bank export or books)."""
    source: str          # portal/system name, e.g. "bancolombia", "alegra"
    side: str            # "bank" | "books"
    txn_date: date
    amount: float        # signed: positive = credit/inflow, negative = debit/outflow
    description: str = ""
    reference: str = ""  # document number / transaction id, if any
    matched: bool = False
    match_id: str = ""   # id linking bank↔books rows once matched
    category: str = ""   # filled by the LLM classifier for UNMATCHED items only


@dataclass
class MatchResult:
    matched_pairs: list = field(default_factory=list)   # [(bank Movement, books Movement)]
    unmatched_bank: list = field(default_factory=list)   # Movements only in bank
    unmatched_books: list = field(default_factory=list)  # Movements only in books

    @property
    def match_rate(self) -> float:
        total = len(self.matched_pairs) + len(self.unmatched_bank) + len(self.unmatched_books)
        return len(self.matched_pairs) / total if total else 1.0


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CloseReport:
    period: str                       # "2026-07"
    account: str                      # portal/account name
    match: MatchResult = None
    checks: list = field(default_factory=list)   # [CheckResult]
    bank_total: float = 0.0
    books_total: float = 0.0

    @property
    def difference(self) -> float:
        return round(self.bank_total - self.books_total, 2)

    @property
    def all_checks_passed(self) -> bool:
        return all(c.passed for c in self.checks)
