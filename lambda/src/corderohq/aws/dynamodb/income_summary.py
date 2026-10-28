from __future__ import annotations

from datetime import datetime
from typing import Any

from corderohq.aws.dynamodb.income_source import IncomeSourceTable
from corderohq.aws.dynamodb.planned_income import PlannedIncomeTable, RecurringIncomeDensifyAdapter
from corderohq.aws.dynamodb.util import BUDGET_TZ
from corderohq.budget.densify import carry_map, value_at_or_before
from corderohq.budget.editability import editability
from corderohq.budget.income import (
    ONE_TIME_INCOME_PREFIX,
    RECURRING_INCOME_PREFIX,
    RecurringIncome,
    parse_one_time_income,
)


def compute_income_plan(
    planned_income: PlannedIncomeTable,
    income_sources: IncomeSourceTable,
    year_month: str,
    now: datetime,
) -> dict[str, Any]:
    """Return the income plan of one month: each recurring income, each one-time income, and the planned net income.

    A current or past month shows each recurring income that it holds. A future month shows each recurring income
    with its own value there, or a value from the walk back. A month shows its pinned value if it has one, else its
    carried value. A month after the end month of a value does not show or count it. Nothing here reads a
    transaction.
    """

    sources_by_id = {s["incomeSourceId"]: s for s in income_sources.get_all_income_sources()}
    now_et = now.astimezone(BUDGET_TZ)
    is_future = year_month > f"{now_et.year:04d}-{now_et.month:02d}"

    month_items = planned_income.get_planned_income_for_month(year_month)
    store = RecurringIncomeDensifyAdapter(planned_income)
    by_key = carry_map(store.scan_stored_values())
    pinned_by_key = carry_map(store.scan_pinned_values())
    if is_future:
        keys = sorted({key for _, key in by_key} | {key for ym, key in pinned_by_key if ym == year_month})
    else:
        keys = sorted(
            str(item["plannedIncomeId"])
            for item in month_items
            if str(item["plannedIncomeId"]).startswith(RECURRING_INCOME_PREFIX)
        )

    recurring: list[dict[str, Any]] = []
    for key in keys:
        carried: RecurringIncome | None = value_at_or_before(by_key, year_month, key)
        if carried is not None and not carried.counts_in(year_month):
            carried = None
        pinned_value: RecurringIncome | None = pinned_by_key.get((year_month, key))
        if pinned_value is not None and not pinned_value.counts_in(year_month):
            pinned_value = None
        income = pinned_value if pinned_value is not None else carried
        if income is None:
            continue
        entry: dict[str, Any] = {
            "plannedIncomeId": key,
            **income.to_item(),
            **_source_fields(sources_by_id, income.income_source_id),
            "net": income.net,
            "monthlyNet": income.monthly_net,
            # The value that later months copy. It differs from the fields above when `hasPinnedRecurringIncome`.
            "carriedRecurringIncome": carried.to_item() if carried is not None else None,
            "hasPinnedRecurringIncome": pinned_value is not None,
        }
        if is_future:
            entry["inherited"] = (year_month, key) not in by_key
        recurring.append(entry)
    recurring.sort(
        key=lambda entry: (entry.get("sourceSortOrder", 0), entry.get("sourceLabel", ""), entry["plannedIncomeId"])
    )

    one_time: list[dict[str, Any]] = []
    for item in sorted(month_items, key=lambda item: str(item["plannedIncomeId"])):
        if not str(item["plannedIncomeId"]).startswith(ONE_TIME_INCOME_PREFIX):
            continue
        one_time_income = parse_one_time_income(item)
        one_time.append(
            {
                "plannedIncomeId": item["plannedIncomeId"],
                **one_time_income.to_item(),
                **(
                    _source_fields(sources_by_id, one_time_income.income_source_id)
                    if one_time_income.income_source_id is not None
                    else {}
                ),
                "net": one_time_income.net,
            }
        )

    planned = sum(entry["monthlyNet"] for entry in recurring) + sum(entry["net"] for entry in one_time)
    return {
        "yearMonth": year_month,
        "state": editability(year_month, now),
        "recurring": recurring,
        "oneTime": one_time,
        "plannedNetIncome": planned,
    }


def _source_fields(sources_by_id: dict[str, dict[str, Any]], income_source_id: str) -> dict[str, Any]:
    """Return the label, the owners, and the sort order of an income source, for the month view."""

    source = sources_by_id.get(income_source_id, {})
    return {
        "sourceLabel": source.get("label", ""),
        "owners": list(source.get("owners", [])),
        "sourceSortOrder": int(source.get("sortOrder", 0)),
    }
