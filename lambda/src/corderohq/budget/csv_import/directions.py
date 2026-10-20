"""Suggest a disposition for each CSV row, from the direction of the money.

Each upload belongs to one account, and that account has a budget role: SPENDING (checking, a
credit card) or HOLDING (a savings fund, a brokerage, a 529). Money counts in the budget only when
it crosses the boundary between the outside world, the spending accounts, and the holding
accounts. So each transfer between two of the user's own accounts counts one time at most, on the
side where the money leaves a spending account.

The sign of an amount gives the direction for the upload account: positive is money out, and
negative is money in. The only unknown is the other side. These steps find it, in order, and the
first step that gives an answer wins:

1. PAIR — a saved transaction in a different account, with the opposite amount, at most 4 days
   away. Exactly one match gives that account. More than one gives the reason "many_pairs".
2. LAST_FOUR — a 4-digit group in the description that is in the `lastFour` of exactly one other
   account.
3. BANK — the bank's own category (see `bank_category_hint` in `csv_import.py`).
4. LLM — the kind from the categorization call.
5. DEFAULT — nothing found.

When the import is not sure, it suggests SKIP and marks the row. A missed savings transfer shows as
a $0 actual in a savings category, so it is easy to see. A double count is not.

Nothing here reads a table. The handler loads the rows and passes them in.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import Enum, StrEnum, auto
from typing import Any

from corderohq.budget.csv_import.llm import TransactionKind
from corderohq.budget.csv_import.local_checks import BankHint, parse_date
from corderohq.budget.dispositions import Disposition
from corderohq.networth.models import BudgetRole, budget_role_of

PAIR_WINDOW_DAYS = 4



class SideKind(Enum):
    """The other side of a row. It stays inside the upload, and is never sent or stored."""

    OUTSIDE = auto()  # a merchant, an employer, a bank: the outside world
    ACCOUNT = auto()  # one of the user's own accounts, and the app knows which
    OWN_UNKNOWN = auto()  # one of the user's own accounts, but the app cannot tell which
    UNKNOWN = auto()


class SuggestedBy(StrEnum):
    """The step that found the other side of a row. The upload response sends the value."""

    PAIR = "PAIR"
    LAST_FOUR = "LAST_FOUR"
    BANK = "BANK"
    LLM = "LLM"
    DEFAULT = "DEFAULT"


class ReviewReason(StrEnum):
    """A reason to look at a row first. It never blocks the commit. The upload response sends it."""

    NO_PAIR_FOUND = "no_pair_found"
    LLM_GUESS = "llm_guess"
    MANY_PAIRS = "many_pairs"
    UNKNOWN_DIRECTION = "unknown_direction"


# For a bank hint: the other side, and the kind of money.
_BANK_HINT_SIDES: dict[BankHint, tuple[SideKind, TransactionKind]] = {
    BankHint.INCOME: (SideKind.OUTSIDE, TransactionKind.INCOME),
    BankHint.CARD_PAYMENT: (SideKind.OWN_UNKNOWN, TransactionKind.CARD_PAYMENT),
    BankHint.TRANSFER: (SideKind.OWN_UNKNOWN, TransactionKind.OWN_TRANSFER),
}
_OUTSIDE_KINDS = (TransactionKind.EXPENSE, TransactionKind.REFUND, TransactionKind.INCOME)
_OWN_ACCOUNT_KINDS = (TransactionKind.CARD_PAYMENT, TransactionKind.OWN_TRANSFER)

_FOUR_DIGITS = re.compile(r"(?<!\d)(\d{4})(?!\d)")


@dataclass(frozen=True)
class OtherSide:
    """What the steps found about the other side of one row."""

    kind: SideKind
    role: BudgetRole | None = None  # the budget role of the other account, when kind is ACCOUNT
    suggested_by: SuggestedBy = SuggestedBy.DEFAULT
    pair_with: dict[str, str] | None = None
    # For OUTSIDE: INCOME or REFUND changes the suggestion for money in.
    # For OWN_UNKNOWN: CARD_PAYMENT or OWN_TRANSFER.
    detail: TransactionKind | None = None
    review: tuple[ReviewReason, ...] = ()


@dataclass(frozen=True)
class Suggestion:
    disposition: Disposition
    category_id: str
    suggested_by: SuggestedBy
    pair_with: dict[str, str] | None
    review: list[ReviewReason] = field(default_factory=list)


def _days_apart(first: date | None, second: date | None) -> int | None:
    if first is None or second is None:
        return None
    return abs((first - second).days)


def _find_pairs(
    row: Mapping[str, Any], upload_account_id: str, saved: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """Return the saved transactions that look like the other side of `row`."""
    row_date = parse_date(str(row["transactionDate"]))
    amount = int(row["amount"])
    if row_date is None or amount == 0:
        return []
    matches: list[Mapping[str, Any]] = []
    for txn in saved:
        account_id = txn.get("accountId")
        if not account_id or account_id == upload_account_id:
            continue
        if int(txn.get("amount", 0)) != -amount:
            continue
        days = _days_apart(row_date, parse_date(str(txn.get("transactionDate", ""))))
        if days is not None and days <= PAIR_WINDOW_DAYS:
            matches.append(txn)
    return matches


def _account_side(
    account: Mapping[str, Any] | None,
    suggested_by: SuggestedBy,
    pair_with: dict[str, str] | None = None,
    review: tuple[ReviewReason, ...] = (),
) -> OtherSide:
    """Return the other side for a known account. An account with no budget role is outside the budget.

    For example, a loan or a mortgage account has no role, so a payment to it is an expense.
    """
    role = budget_role_of(dict(account)) if account is not None else None
    kind = SideKind.OUTSIDE if role is None else SideKind.ACCOUNT
    return OtherSide(kind=kind, role=role, suggested_by=suggested_by, pair_with=pair_with, review=review)


def find_other_side(
    row: Mapping[str, Any],
    upload_account_id: str,
    accounts_by_id: Mapping[str, Mapping[str, Any]],
    saved: Sequence[Mapping[str, Any]],
) -> OtherSide:
    """Run the steps in the module docstring, and return the first answer."""
    review: tuple[ReviewReason, ...] = ()

    pairs = _find_pairs(row, upload_account_id, saved)
    if len(pairs) == 1:
        pair = pairs[0]
        pair_with = {"yearMonth": str(pair["yearMonth"]), "sortId": str(pair["sortId"])}
        return _account_side(accounts_by_id.get(str(pair["accountId"])), SuggestedBy.PAIR, pair_with=pair_with)
    if len(pairs) > 1:
        review = (ReviewReason.MANY_PAIRS,)

    digit_groups = set(_FOUR_DIGITS.findall(str(row["description"])))
    if digit_groups:
        matching = [
            account
            for account_id, account in accounts_by_id.items()
            if account_id != upload_account_id and digit_groups & set(account.get("lastFour") or [])
        ]
        if len(matching) == 1:
            return _account_side(matching[0], SuggestedBy.LAST_FOUR, review=review)

    hint = row.get("_bankHint")
    if isinstance(hint, BankHint):
        side_kind, detail = _BANK_HINT_SIDES[hint]
        return OtherSide(kind=side_kind, suggested_by=SuggestedBy.BANK, detail=detail, review=review)

    kind = row.get("_kind")
    if kind in _OUTSIDE_KINDS:
        detail = TransactionKind(kind)
        return OtherSide(kind=SideKind.OUTSIDE, suggested_by=SuggestedBy.LLM, detail=detail, review=review)
    if kind in _OWN_ACCOUNT_KINDS:
        detail = TransactionKind(kind)
        return OtherSide(kind=SideKind.OWN_UNKNOWN, suggested_by=SuggestedBy.LLM, detail=detail, review=review)

    return OtherSide(kind=SideKind.UNKNOWN, suggested_by=SuggestedBy.DEFAULT, review=review)


def suggest(row: Mapping[str, Any], upload_role: BudgetRole, side: OtherSide) -> Suggestion:
    """Apply the direction table to one row. See docs/budget-income-design.md "Step 7"."""
    category_id = str(row.get("categoryId") or "")
    money_out = int(row["amount"]) >= 0
    review = list(side.review)

    def result(disposition: Disposition, *reasons: ReviewReason) -> Suggestion:
        # A SKIP row has no category.
        return Suggestion(
            disposition=disposition,
            category_id="" if disposition == Disposition.SKIP else category_id,
            suggested_by=side.suggested_by,
            pair_with=side.pair_with,
            review=review + [r for r in reasons if r not in review],
        )

    if side.kind == SideKind.UNKNOWN:
        return result(Disposition.SKIP, ReviewReason.UNKNOWN_DIRECTION)

    own_account = side.kind in (SideKind.ACCOUNT, SideKind.OWN_UNKNOWN)

    if upload_role == BudgetRole.HOLDING:
        # Money into a fund is counted on the spending side. Money out of a fund to the outside
        # world already had its job, so it is Off-budget.
        if money_out and not own_account:
            return result(Disposition.OFF_BUDGET)
        return result(Disposition.SKIP)

    # The upload account is a spending account.
    if not money_out:
        if own_account or side.detail == TransactionKind.INCOME:
            return result(Disposition.SKIP)
        return result(Disposition.BUDGET)  # a refund

    if side.kind == SideKind.OUTSIDE:
        return result(Disposition.BUDGET)
    if side.kind == SideKind.ACCOUNT:
        if side.role == BudgetRole.SPENDING:
            return result(Disposition.SKIP)  # such as checking pays the card
        return result(Disposition.BUDGET)  # to a holding account: the actual of a savings category
    # One of the user's own accounts, but the app cannot tell which.
    if side.detail == TransactionKind.CARD_PAYMENT:
        return result(Disposition.SKIP)
    if side.detail == TransactionKind.OWN_TRANSFER and category_id:
        return result(Disposition.BUDGET, ReviewReason.LLM_GUESS)
    return result(Disposition.SKIP, ReviewReason.NO_PAIR_FOUND)


def suggest_dispositions(
    rows: Sequence[Mapping[str, Any]],
    upload_account: Mapping[str, Any],
    accounts: Sequence[Mapping[str, Any]],
    saved: Sequence[Mapping[str, Any]],
) -> list[Suggestion]:
    """Return one suggestion for each row, in order.

    Each row has transactionDate, description, amount (positive is money out), categoryId (the
    LLM's choice, or ""), and optionally `_bankHint` and `_kind`. `saved` is the saved
    transactions of the months around the upload, in every account.
    """
    upload_account_id = str(upload_account["accountId"])
    upload_role = budget_role_of(dict(upload_account)) or BudgetRole.SPENDING
    accounts_by_id = {str(a["accountId"]): a for a in accounts}
    return [suggest(row, upload_role, find_other_side(row, upload_account_id, accounts_by_id, saved)) for row in rows]
