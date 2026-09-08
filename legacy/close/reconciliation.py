"""
Bank-vs-books matching engine. Deterministic cascade, zero LLM:

  Pass 1 — exact:     same amount + same date + same reference (when both have one)
  Pass 2 — reference: same amount + same reference, date within tolerance
  Pass 3 — fuzzy:     same amount, date within ±MATCH_DATE_TOLERANCE_DAYS

Each bank movement matches at most one books movement (greedy, earliest first).
"""

import uuid

from close.models import MatchResult, Movement
from config.settings import MATCH_AMOUNT_TOLERANCE, MATCH_DATE_TOLERANCE_DAYS


def _amounts_equal(a: float, b: float) -> bool:
    return abs(a - b) <= MATCH_AMOUNT_TOLERANCE


def _pair(bank: Movement, books: Movement, result: MatchResult):
    match_id = uuid.uuid4().hex[:8]
    bank.matched = books.matched = True
    bank.match_id = books.match_id = match_id
    result.matched_pairs.append((bank, books))


def reconcile(bank_movs: list[Movement], books_movs: list[Movement]) -> MatchResult:
    result = MatchResult()
    bank = sorted(bank_movs, key=lambda m: m.txn_date)
    books = sorted(books_movs, key=lambda m: m.txn_date)

    # Pass 1: exact (amount + date + reference)
    for b in bank:
        if b.matched:
            continue
        for k in books:
            if k.matched:
                continue
            if (
                _amounts_equal(b.amount, k.amount)
                and b.txn_date == k.txn_date
                and b.reference and k.reference and b.reference == k.reference
            ):
                _pair(b, k, result)
                break

    # Pass 2: amount + reference, date within tolerance
    for b in bank:
        if b.matched:
            continue
        for k in books:
            if k.matched:
                continue
            if (
                _amounts_equal(b.amount, k.amount)
                and b.reference and k.reference and b.reference == k.reference
                and abs((b.txn_date - k.txn_date).days) <= MATCH_DATE_TOLERANCE_DAYS
            ):
                _pair(b, k, result)
                break

    # Pass 3: amount + date within tolerance (no reference requirement)
    for b in bank:
        if b.matched:
            continue
        candidates = [
            k for k in books
            if not k.matched
            and _amounts_equal(b.amount, k.amount)
            and abs((b.txn_date - k.txn_date).days) <= MATCH_DATE_TOLERANCE_DAYS
        ]
        if candidates:
            # closest date wins
            best = min(candidates, key=lambda k: abs((b.txn_date - k.txn_date).days))
            _pair(b, best, result)

    result.unmatched_bank = [m for m in bank if not m.matched]
    result.unmatched_books = [m for m in books if not m.matched]

    print(
        f"🔗 Reconciliation: {len(result.matched_pairs)} matched · "
        f"{len(result.unmatched_bank)} pending in bank · "
        f"{len(result.unmatched_books)} pending in books "
        f"({result.match_rate:.0%} match rate)"
    )
    return result
