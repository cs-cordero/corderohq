from __future__ import annotations

import logging
from typing import Any

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE, scan_pages
from corderohq.budget.densify import CarryRow

_LOGGER = logging.getLogger(__name__)


class BudgetTable:
    """Monthly budget target management."""

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def get_budget_targets(self, year_month: str) -> list[dict[str, Any]]:
        """Get all budget targets for a given month (YYYY-MM)."""

        response = self._table.query(
            KeyConditionExpression="yearMonth = :ym",
            ExpressionAttributeValues={":ym": year_month},
        )
        return response["Items"]

    def set_all_budget_targets(self, year_month: str, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Set the budget targets for one month. This writes over every target in that month."""

        existing = self.get_budget_targets(year_month)
        with self._table.batch_writer() as batch:
            for item in existing:
                batch.delete_item(Key={"yearMonth": year_month, "categoryId": item["categoryId"]})

        items: list[dict[str, Any]] = []
        with self._table.batch_writer() as batch:
            for target in targets:
                item: dict[str, Any] = {
                    "yearMonth": year_month,
                    "categoryId": target["categoryId"],
                    "amount": target["amount"],
                }
                batch.put_item(Item=item)
                items.append(item)

        _LOGGER.info("Set %d targets for %s", len(items), year_month)
        return items

    def scan(self) -> list[dict[str, Any]]:
        """Return every row in the table."""

        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {}
        for response in scan_pages(self._table, **params):
            items.extend(response.get("Items", []))
        return items

    def set_single_budget_target(self, year_month: str, category_id: str, amount: Any) -> None:
        """Write one budget row."""

        self._table.put_item(
            Item={"yearMonth": year_month, "categoryId": category_id, "amount": amount},
        )

    def delete_single_budget_target(self, year_month: str, category_id: str) -> None:
        """Delete one budget row."""

        self._table.delete_item(Key={"yearMonth": year_month, "categoryId": category_id})

    def find_future_months_with_category(self, category_id: str, after_month: str) -> list[str]:
        """Return the sorted yearMonth values later than after_month that hold this categoryId."""

        months: set[str] = set()
        params: dict[str, Any] = {
            "FilterExpression": "categoryId = :cid AND yearMonth > :ym",
            "ExpressionAttributeValues": {":cid": category_id, ":ym": after_month},
            "ProjectionExpression": "yearMonth",
        }
        for response in scan_pages(self._table, **params):
            for item in response["Items"]:
                months.add(str(item["yearMonth"]))
        return sorted(months)

    def count_rows_for_category(self, category_id: str) -> int:
        """Count each Budget row that names this categoryId. The hard-delete preview calls this."""

        count = 0
        params: dict[str, Any] = {
            "FilterExpression": "categoryId = :cid",
            "ExpressionAttributeValues": {":cid": category_id},
            "Select": "COUNT",
        }
        for response in scan_pages(self._table, **params):
            count += response.get("Count", 0)
        return count

    def find_past_months_with_category(self, category_id: str, before_month: str) -> list[str]:
        """Return the sorted yearMonth values before `before_month` that hold this categoryId."""

        months: set[str] = set()
        params: dict[str, Any] = {
            "FilterExpression": "categoryId = :cid AND yearMonth < :ym",
            "ExpressionAttributeValues": {":cid": category_id, ":ym": before_month},
            "ProjectionExpression": "yearMonth",
        }
        for response in scan_pages(self._table, **params):
            for item in response["Items"]:
                months.add(str(item["yearMonth"]))
        return sorted(months)

    def delete_all_for_category(self, category_id: str) -> int:
        """Delete every Budget row that names this categoryId. Return the number of deleted rows."""

        keys: list[dict[str, str]] = []
        params: dict[str, Any] = {
            "FilterExpression": "categoryId = :cid",
            "ExpressionAttributeValues": {":cid": category_id},
            "ProjectionExpression": "yearMonth, categoryId",
        }
        for response in scan_pages(self._table, **params):
            keys.extend(
                {"yearMonth": str(item["yearMonth"]), "categoryId": str(item["categoryId"])}
                for item in response["Items"]
            )

        if not keys:
            return 0
        with self._table.batch_writer() as batch:
            for key in keys:
                batch.delete_item(Key=key)
        _LOGGER.info("Hard-deleted %d Budget rows for category %s", len(keys), category_id)
        return len(keys)


class BudgetCarryStore:
    """The Budget table as a `DenseSparseStore`. The key of a row is its `categoryId`, and the value is its amount."""

    def __init__(self, budget_table: BudgetTable) -> None:
        self._budget_table = budget_table

    def scan_carry_rows(self) -> list[CarryRow]:
        """Return every Budget row as `(yearMonth, categoryId, amount)`."""

        return [(r["yearMonth"], r["categoryId"], r["amount"]) for r in self._budget_table.scan()]

    def put_carry_row(self, year_month: str, key: str, value: Any) -> None:
        """Write one Budget row."""

        self._budget_table.set_single_budget_target(year_month, key, value)
