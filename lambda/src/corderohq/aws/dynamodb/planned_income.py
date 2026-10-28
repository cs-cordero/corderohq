from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE, query_all_items, scan_all_items, update_expression
from corderohq.budget.densify import DenseSparseStore, StoredValue
from corderohq.budget.income import (
    ONE_TIME_INCOME_PREFIX,
    RECURRING_INCOME_PREFIX,
    OneTimeIncome,
    PlannedIncomeKind,
    RecurringIncome,
    parse_recurring_income,
)
from corderohq.common import Remove, SetTo


def new_recurring_income_id() -> str:
    """Return a new `plannedIncomeId` for a recurring income."""

    return f"{RECURRING_INCOME_PREFIX}{ULID()}"


def has_carried_value(item: dict[str, Any]) -> bool:
    """Return True if a stored recurring income holds a carried value or a stop.

    A future month can hold an item with only a pinned value (`pinnedRecurringIncome`), and no carried value.
    """

    return item.get("stopped") is True or "gross" in item


def get_recurring_income(item: dict[str, Any]) -> RecurringIncome | None:
    """Return the carried value of a stored recurring income, or None if it is a stop or has no carried value."""

    if not has_carried_value(item) or item.get("stopped") is True:
        return None
    return parse_recurring_income(item)


def get_pinned_recurring_income(item: dict[str, Any]) -> RecurringIncome | None:
    """Return the pinned value of a stored recurring income, or None if it has none."""

    if "pinnedRecurringIncome" not in item:
        return None
    return parse_recurring_income(item["pinnedRecurringIncome"])


class PlannedIncomeTable:
    """An expected income amount for a particular year-month that the household is planning on receiving.

    Planned income comes in two flavors: RECURRING and ONE_TIME. Recurring income follows densification rules and is
    assumed to carry from one month to the next. One-time income is an expected future inflow that is not recurring,
    such as a bonus payment, RSU award, some kind of windfall, etc.
    """

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def get_planned_income_for_month(self, year_month: str) -> list[dict[str, Any]]:
        """Return every planned income of one month: recurring incomes, stops, and one-time incomes."""

        return query_all_items(
            self._table, KeyConditionExpression="yearMonth = :ym", ExpressionAttributeValues={":ym": year_month}
        )

    def get_planned_income(self, year_month: str, planned_income_id: str) -> dict[str, Any] | None:
        """Return one planned income, or None if it does not exist."""

        response = self._table.get_item(Key={"yearMonth": year_month, "plannedIncomeId": planned_income_id})
        return response.get("Item")

    def scan_recurring_incomes(self) -> list[dict[str, Any]]:
        """Return every recurring income and every stop in the table. The one-time incomes are left out."""

        items = scan_all_items(
            self._table,
            FilterExpression="begins_with(plannedIncomeId, :prefix)",
            ExpressionAttributeValues={":prefix": RECURRING_INCOME_PREFIX},
        )
        return [item for item in items if str(item["plannedIncomeId"]).startswith(RECURRING_INCOME_PREFIX)]

    def put_recurring_income(
        self,
        year_month: str,
        planned_income_id: str,
        income: RecurringIncome | None,
        pinned: RecurringIncome | None = None,
    ) -> dict[str, Any]:
        """Write the carried value of a recurring income in one month, and its pinned value if it has one.

        An income of None writes a stop. It replaces the item.
        """

        item: dict[str, Any] = {
            "yearMonth": year_month,
            "plannedIncomeId": planned_income_id,
            "kind": PlannedIncomeKind.RECURRING,
        }
        if income is None:
            item["stopped"] = True
        else:
            item.update(income.to_item())
        if pinned is not None:
            item["pinnedRecurringIncome"] = pinned.to_item()
        self._table.put_item(Item=item)
        return item

    def put_pinned_recurring_income(self, year_month: str, planned_income_id: str, pinned: RecurringIncome) -> None:
        """Write a recurring income in one month with only a pinned value, and no carried value."""

        self._table.put_item(
            Item={
                "yearMonth": year_month,
                "plannedIncomeId": planned_income_id,
                "kind": PlannedIncomeKind.RECURRING,
                "pinnedRecurringIncome": pinned.to_item(),
            }
        )

    def delete_recurring_income(self, year_month: str, planned_income_id: str) -> None:
        """Delete the entry of a recurring income in one month, such as a pin or a stop."""

        self._table.delete_item(Key={"yearMonth": year_month, "plannedIncomeId": planned_income_id})

    def create_one_time_income(self, year_month: str, income: OneTimeIncome) -> dict[str, Any]:
        """Create a one-time income in one month. Returns the created item."""

        now = datetime.now(tz=UTC).isoformat()
        item: dict[str, Any] = {
            "yearMonth": year_month,
            "plannedIncomeId": f"{ONE_TIME_INCOME_PREFIX}{ULID()}",
            "kind": PlannedIncomeKind.ONE_TIME,
            **income.to_item(),
            "createdAt": now,
            "updatedAt": now,
        }
        self._table.put_item(Item=item)
        return item

    def update_one_time_income(self, year_month: str, planned_income_id: str, income: OneTimeIncome) -> dict[str, Any]:
        """Set the new value of a one-time income. Returns the updated item."""

        item = income.to_item()
        response = self._table.update_item(
            Key={"yearMonth": year_month, "plannedIncomeId": planned_income_id},
            ReturnValues="ALL_NEW",
            **update_expression(
                {
                    "label": SetTo(item["label"]),
                    "gross": SetTo(item["gross"]),
                    "deductions": SetTo(item["deductions"]),
                    "incomeSourceId": SetTo(item["incomeSourceId"]) if "incomeSourceId" in item else Remove(),
                },
                always_set={"updatedAt": datetime.now(tz=UTC).isoformat()},
            ),
        )
        return response["Attributes"]

    def delete_one_time_income(self, year_month: str, planned_income_id: str) -> None:
        """Delete a one-time income."""

        self._table.delete_item(Key={"yearMonth": year_month, "plannedIncomeId": planned_income_id})


class RecurringIncomeDensifyAdapter(DenseSparseStore):
    """Implements the DenseSparseStore protocol for the recurring incomes of the PlannedIncomeTable class.

    The key is the `plannedIncomeId`. The carried value is the RecurringIncome, or None for a stop, and the pinned
    value is `pinnedRecurringIncome`. The one-time incomes are not part of it, so they never carry forward. A None
    value writes nothing, because the stop that it comes from stays in effect for the walk back.
    """

    def __init__(self, planned_income_table: PlannedIncomeTable) -> None:
        self._planned_income_table = planned_income_table
        self._items: list[dict[str, Any]] | None = None

    def scan_stored_values(self) -> list[StoredValue]:
        """Return each carried value and each stop as `(yearMonth, plannedIncomeId, RecurringIncome | None)`."""

        return [
            (str(item["yearMonth"]), str(item["plannedIncomeId"]), get_recurring_income(item))
            for item in self._scan()
            if has_carried_value(item)
        ]

    def scan_pinned_values(self) -> list[StoredValue]:
        """Return each pinned value as `(yearMonth, plannedIncomeId, RecurringIncome)`."""

        values: list[StoredValue] = []
        for item in self._scan():
            pinned = get_pinned_recurring_income(item)
            if pinned is not None:
                values.append((str(item["yearMonth"]), str(item["plannedIncomeId"]), pinned))
        return values

    def put_stored_value(self, year_month: str, key: str, value: Any) -> None:
        """Write the carried value of a recurring income, and keep its pinned value. None writes nothing."""

        if value is None:
            return
        pinned = next(
            (
                get_pinned_recurring_income(item)
                for item in self._scan()
                if item["yearMonth"] == year_month and item["plannedIncomeId"] == key
            ),
            None,
        )
        self._planned_income_table.put_recurring_income(year_month, key, value, pinned=pinned)

    def recurring_income_ids(self) -> list[str]:
        """Return the id of each recurring income in the table, also of a stopped one."""

        return sorted({key for _, key, _ in self.scan_stored_values()})

    def _scan(self) -> list[dict[str, Any]]:
        # One scan serves both lists, and put_stored_value reads it to keep a pinned value.
        if self._items is None:
            self._items = self._planned_income_table.scan_recurring_incomes()
        return self._items
