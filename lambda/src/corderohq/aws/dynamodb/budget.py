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

    def get_targets(self, year_month: str) -> list[dict[str, Any]]:
        """Get all budget targets for a given month (YYYY-MM)."""

        response = self._table.query(
            KeyConditionExpression="yearMonth = :ym",
            ExpressionAttributeValues={":ym": year_month},
        )
        return response["Items"]

    def put_targets(self, year_month: str, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Set the budget targets for one month. This writes over every target in that month."""

        existing = self.get_targets(year_month)
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

    def scan_all(self) -> list[dict[str, Any]]:
        """Return every row in the table.

        Used by densification to load the full state into memory before walking
        forward through missed months. The table is bounded in size (categories
        x months tracked) so a Scan is cheaper than the equivalent fan-out of
        per-month Queries.
        """

        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {}
        for response in scan_pages(self._table, **params):
            items.extend(response.get("Items", []))
        return items

    def put_single(self, year_month: str, category_id: str, amount: Any) -> None:
        """Write one budget row."""

        self._table.put_item(
            Item={"yearMonth": year_month, "categoryId": category_id, "amount": amount},
        )

    def delete_single(self, year_month: str, category_id: str) -> None:
        """Delete one budget row. /pin calls this when it clears a pin, with amount=null."""

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
        """Return the sorted yearMonth values earlier than before_month that hold this categoryId.

        Used by the hard-delete preview to highlight historical data the delete would erase.
        The caller filters by editability state if it needs only locked vs grace.
        """

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
        """Delete every Budget row that names this categoryId. Return the count.

        Used by hard-delete only — soft-delete (/deactivate) drops just the future pin
        rows. See docs/personal-finance-architecture.md "Hard delete semantics".
        """

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
    """The Budget table as a `DenseSparseStore` for `budget/densify.py`.

    The key of a row is its `categoryId`, and the value is its amount. This is a separate class,
    and not methods on `BudgetTable`, so that a test that replaces `BudgetTable` with a mock still
    drives the densify step through `scan_all()` and `put_single()`.
    """

    def __init__(self, budget_table: BudgetTable) -> None:
        self._budget_table = budget_table

    def scan_carry_rows(self) -> list[CarryRow]:
        """Return every Budget row as `(yearMonth, categoryId, amount)`."""

        return [(r["yearMonth"], r["categoryId"], r["amount"]) for r in self._budget_table.scan_all()]

    def put_carry_row(self, year_month: str, key: str, value: Any) -> None:
        """Write one Budget row."""

        self._budget_table.put_single(year_month, key, value)
