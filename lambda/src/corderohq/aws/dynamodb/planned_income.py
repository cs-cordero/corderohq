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


def get_recurring_income(item: dict[str, Any]) -> RecurringIncome | None:
    """Return the value of a stored recurring income, or None if it is a stop."""

    if item.get("stopped") is True:
        return None
    return parse_recurring_income(item)


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
        self, year_month: str, planned_income_id: str, income: RecurringIncome | None
    ) -> dict[str, Any]:
        """Write a recurring income in one month. An income of None writes a stop."""

        item: dict[str, Any] = {
            "yearMonth": year_month,
            "plannedIncomeId": planned_income_id,
            "kind": PlannedIncomeKind.RECURRING,
        }
        if income is None:
            item["stopped"] = True
        else:
            item.update(income.to_item())
        self._table.put_item(Item=item)
        return item

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

    The key is the `plannedIncomeId`, and the value is the RecurringIncome, or None for a stop. The one-time incomes
    are not part of it, so they never carry forward. A None value writes nothing, because the stop that it comes from
    stays in effect for the walk back.
    """

    def __init__(self, planned_income_table: PlannedIncomeTable) -> None:
        self._planned_income_table = planned_income_table

    def scan_stored_values(self) -> list[StoredValue]:
        """Return each recurring income and each stop as `(yearMonth, plannedIncomeId, RecurringIncome | None)`."""

        return [
            (str(item["yearMonth"]), str(item["plannedIncomeId"]), get_recurring_income(item))
            for item in self._planned_income_table.scan_recurring_incomes()
        ]

    def put_stored_value(self, year_month: str, key: str, value: Any) -> None:
        """Write a recurring income in one month. A None value writes nothing."""

        if value is None:
            return
        self._planned_income_table.put_recurring_income(year_month, key, value)

    def recurring_income_ids(self) -> list[str]:
        """Return the id of each recurring income in the table, also of a stopped one."""

        return sorted({key for _, key, _ in self.scan_stored_values()})
