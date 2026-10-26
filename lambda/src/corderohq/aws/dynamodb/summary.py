from __future__ import annotations

import calendar
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from corderohq.aws.dynamodb.budget import BudgetDensifyAdapter, BudgetTable
from corderohq.aws.dynamodb.category import CategoryTable
from corderohq.aws.dynamodb.transactions import TransactionsTable
from corderohq.aws.dynamodb.util import BUDGET_TZ
from corderohq.budget.densify import carry_map, value_at_or_before
from corderohq.budget.editability import EditabilityState, editability


def _month_end_utc(year_month: str) -> datetime:
    """Return the last instant of `year_month` in BUDGET_TZ, as a UTC datetime."""

    year, month = int(year_month[:4]), int(year_month[5:7])
    last_day = calendar.monthrange(year, month)[1]
    local_end = datetime(year, month, last_day, 23, 59, 59, 999999, tzinfo=BUDGET_TZ)
    return local_end.astimezone(UTC)


def _resolve_historical_name(category: dict[str, Any], month_end_utc: datetime) -> str | None:
    """Return the name that the category had at `month_end_utc`.

    Return None if the category has the same name now.
    """

    history = category.get("nameHistory") or []
    if not history:
        return None
    # Sort by replacedAt, oldest first. We then take the first entry that comes after the month.
    sorted_history = sorted(history, key=lambda e: e["replacedAt"])
    for entry in sorted_history:
        replaced_at = datetime.fromisoformat(entry["replacedAt"])
        if replaced_at > month_end_utc:
            return str(entry["previousName"])
    return None


def compute_summary(
    budget: BudgetTable,
    transactions: TransactionsTable,
    category_table: CategoryTable,
    year_month: str,
    now: datetime,
) -> dict[str, Any]:
    """Return the budget and the actual amount of each category in one month.

    An editable month shows the active categories, plus each category with a transaction that is not zero. A locked
    month shows each category that has a budget row or a transaction.

    In a locked month, `historicalName` holds the name that the category had then, if the name changed later.

    Amounts are signed: positive is an expense, and negative is income or a refund. A refund makes the actual amount of
    its category smaller.
    """

    txns = transactions.list_for_month(year_month)
    categories_all = category_table.get_all_categories()
    category_by_id = {c["categoryId"]: c for c in categories_all}

    current_ym = f"{now.year:04d}-{now.month:02d}"
    is_future = year_month > current_ym

    # Find the budget target of each category in this month. A target of None means "no
    # target", and the totals count it as $0.
    #
    # A dense month has a stored row for each of its categories, so no value comes from a walk
    # back. The past months and the current month are dense. Each has a row for each category
    # that was active when the month became dense, so the code reads the rows of the month. A
    # future month has rows only for its pins. For each other category, the target is the value
    # of the nearest earlier row. An inactive category with no target is left out.
    budget_by_category: dict[str, Decimal] = {}
    if is_future:
        by_key = carry_map(BudgetDensifyAdapter(budget).scan_carry_rows())
        for cat in categories_all:
            cat_id = cat["categoryId"]
            raw = value_at_or_before(by_key, year_month, cat_id)
            if raw is None and not cat.get("active", False):
                continue
            budget_by_category[cat_id] = Decimal("0") if raw is None else Decimal(str(raw))
    else:
        for target in budget.get_budget_targets(year_month):
            raw = target.get("amount")
            budget_by_category[target["categoryId"]] = Decimal("0") if raw is None else Decimal(str(raw))

    actual_by_category: dict[str, Decimal] = {}
    nonzero_txn_cat_ids: set[str] = set()
    for txn in txns:
        cat_id = txn["categoryId"]
        raw_amount = txn.get("amount")
        amount = Decimal("0") if raw_amount is None else Decimal(str(raw_amount))
        actual_by_category[cat_id] = actual_by_category.get(cat_id, Decimal("0")) + amount
        if amount != 0:
            nonzero_txn_cat_ids.add(cat_id)

    state = editability(year_month, now)
    is_locked = state == EditabilityState.LOCKED

    if is_locked:
        # Locked rule: every category with ANY budget row or ANY transaction (zero or not).
        # Budget rows are never removed on deactivation, so they're the historical signal.
        member_ids = set(budget_by_category.keys()) | {t["categoryId"] for t in txns}
    else:
        # Editable rule: active categories + (inactive with non-zero txn).
        active_ids = {c["categoryId"] for c in categories_all if c.get("active", False)}
        member_ids = active_ids | nonzero_txn_cat_ids
    all_category_ids = sorted(member_ids)

    month_end_utc = _month_end_utc(year_month) if is_locked else None

    categories: list[dict[str, Any]] = []
    total_budgeted = Decimal("0")
    total_actual = Decimal("0")

    for cat_id in all_category_ids:
        budgeted = budget_by_category.get(cat_id, Decimal("0"))
        actual = actual_by_category.get(cat_id, Decimal("0"))
        delta = budgeted - actual

        cat = category_by_id.get(cat_id)
        name = str(cat["name"]) if cat else cat_id
        historical_name: str | None = None
        if is_locked and cat is not None and month_end_utc is not None:
            historical_name = _resolve_historical_name(cat, month_end_utc)

        categories.append(
            {
                "categoryId": cat_id,
                "name": name,
                "historicalName": historical_name,
                "budgeted": int(budgeted),
                "actual": int(actual),
                "delta": int(delta),
            }
        )

        total_budgeted += budgeted
        total_actual += actual

    return {
        "yearMonth": year_month,
        "state": state,
        "categories": categories,
        "totals": {
            "budgeted": int(total_budgeted),
            "actual": int(total_actual),
            "delta": int(total_budgeted - total_actual),
        },
    }
