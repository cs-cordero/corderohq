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
from typing import Any

from corderohq.budget.csv_import import parse_date
from corderohq.budget.dispositions import DISPOSITION_BUDGET, DISPOSITION_OFF_BUDGET, DISPOSITION_SKIP
from corderohq.networth.models import BUDGET_ROLE_HOLDING, BUDGET_ROLE_SPENDING, budget_role_of

PAIR_WINDOW_DAYS = 4

# The other side of a row.
_OUTSIDE = "OUTSIDE"  # a merchant, an employer, a bank: the outside world
_OWN_UNKNOWN = "OWN_UNKNOWN"  # one of the user's own accounts, but the app cannot tell which
_UNKNOWN = "UNKNOWN"

# Reasons to look at a row first. A reason never blocks the commit.
REVIEW_NO_PAIR_FOUND = "no_pair_found"
REVIEW_LLM_GUESS = "llm_guess"
REVIEW_MANY_PAIRS = "many_pairs"
REVIEW_UNKNOWN_DIRECTION = "unknown_direction"

_FOUR_DIGITS = re.compile(r"(?<!\d)(\d{4})(?!\d)")


@dataclass(frozen=True)
class OtherSide:
    """What the steps found about the other side of one row."""

    kind: str  # _OUTSIDE, _OWN_UNKNOWN, _UNKNOWN, or "ACCOUNT"
    role: str | None = None  # the budget role of the other account, when kind is "ACCOUNT"
    suggested_by: str = "DEFAULT"
    pair_with: dict[str, str] | None = None
    # For _OUTSIDE: "income" or "refund" changes the suggestion for money in.
    # For _OWN_UNKNOWN: "card_payment" or "own_transfer".
    detail: str | None = None
    review: tuple[str, ...] = ()


@dataclass(frozen=True)
class Suggestion:
    disposition: str
    category_id: str
    suggested_by: str
    pair_with: dict[str, str] | None
    review: list[str] = field(default_factory=list)


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
    suggested_by: str,
    pair_with: dict[str, str] | None = None,
    review: tuple[str, ...] = (),
) -> OtherSide:
    """Return the other side for a known account. An account with no budget role is outside the budget.

    For example, a loan or a mortgage account has no role, so a payment to it is an expense.
    """
    role = budget_role_of(dict(account)) if account is not None else None
    kind = _OUTSIDE if role is None else "ACCOUNT"
    return OtherSide(kind=kind, role=role, suggested_by=suggested_by, pair_with=pair_with, review=review)


def find_other_side(
    row: Mapping[str, Any],
    upload_account_id: str,
    accounts_by_id: Mapping[str, Mapping[str, Any]],
    saved: Sequence[Mapping[str, Any]],
) -> OtherSide:
    """Run the steps in the module docstring, and return the first answer."""
    review: tuple[str, ...] = ()

    pairs = _find_pairs(row, upload_account_id, saved)
    if len(pairs) == 1:
        pair = pairs[0]
        pair_with = {"yearMonth": str(pair["yearMonth"]), "sortId": str(pair["sortId"])}
        return _account_side(accounts_by_id.get(str(pair["accountId"])), "PAIR", pair_with=pair_with)
    if len(pairs) > 1:
        review = (REVIEW_MANY_PAIRS,)

    digit_groups = set(_FOUR_DIGITS.findall(str(row["description"])))
    if digit_groups:
        matching = [
            account
            for account_id, account in accounts_by_id.items()
            if account_id != upload_account_id and digit_groups & set(account.get("lastFour") or [])
        ]
        if len(matching) == 1:
            return _account_side(matching[0], "LAST_FOUR", review=review)

    hint = row.get("_bankHint")
    if hint == "INCOME":
        return OtherSide(kind=_OUTSIDE, suggested_by="BANK", detail="income", review=review)
    if hint == "CARD_PAYMENT":
        return OtherSide(kind=_OWN_UNKNOWN, suggested_by="BANK", detail="card_payment", review=review)
    if hint == "TRANSFER":
        return OtherSide(kind=_OWN_UNKNOWN, suggested_by="BANK", detail="own_transfer", review=review)

    kind = row.get("_kind")
    if kind in ("expense", "refund", "income"):
        return OtherSide(kind=_OUTSIDE, suggested_by="LLM", detail=str(kind), review=review)
    if kind in ("card_payment", "own_transfer"):
        return OtherSide(kind=_OWN_UNKNOWN, suggested_by="LLM", detail=str(kind), review=review)

    return OtherSide(kind=_UNKNOWN, suggested_by="DEFAULT", review=review)


def suggest(row: Mapping[str, Any], upload_role: str, side: OtherSide) -> Suggestion:
    """Apply the direction table to one row. See docs/budget-income-design.md "Step 7"."""
    category_id = str(row.get("categoryId") or "")
    money_out = int(row["amount"]) >= 0
    review = list(side.review)

    def result(disposition: str, *reasons: str) -> Suggestion:
        # A SKIP row has no category.
        return Suggestion(
            disposition=disposition,
            category_id="" if disposition == DISPOSITION_SKIP else category_id,
            suggested_by=side.suggested_by,
            pair_with=side.pair_with,
            review=review + [r for r in reasons if r not in review],
        )

    if side.kind == _UNKNOWN:
        return result(DISPOSITION_SKIP, REVIEW_UNKNOWN_DIRECTION)

    own_account = side.kind in ("ACCOUNT", _OWN_UNKNOWN)

    if upload_role == BUDGET_ROLE_HOLDING:
        # Money into a fund is counted on the spending side. Money out of a fund to the outside
        # world already had its job, so it is Off-budget.
        if money_out and not own_account:
            return result(DISPOSITION_OFF_BUDGET)
        return result(DISPOSITION_SKIP)

    # The upload account is a spending account.
    if not money_out:
        if own_account or side.detail == "income":
            return result(DISPOSITION_SKIP)
        return result(DISPOSITION_BUDGET)  # a refund

    if side.kind == _OUTSIDE:
        return result(DISPOSITION_BUDGET)
    if side.kind == "ACCOUNT":
        if side.role == BUDGET_ROLE_SPENDING:
            return result(DISPOSITION_SKIP)  # such as checking pays the card
        return result(DISPOSITION_BUDGET)  # to a holding account: the actual of a savings category
    # One of the user's own accounts, but the app cannot tell which.
    if side.detail == "card_payment":
        return result(DISPOSITION_SKIP)
    if side.detail == "own_transfer" and category_id:
        return result(DISPOSITION_BUDGET, REVIEW_LLM_GUESS)
    return result(DISPOSITION_SKIP, REVIEW_NO_PAIR_FOUND)


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
    upload_role = budget_role_of(dict(upload_account)) or BUDGET_ROLE_SPENDING
    accounts_by_id = {str(a["accountId"]): a for a in accounts}
    return [suggest(row, upload_role, find_other_side(row, upload_account_id, accounts_by_id, saved)) for row in rows]
