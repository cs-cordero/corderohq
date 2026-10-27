from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE, query_all_items, scan_all_items
from corderohq.budget.densify import DenseSparseStore, StoredValue

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BudgetTargetAmounts:
    """The amounts of one budget target in one month. At least one of them is set.

    Later months copy the carried amount, until a later month changes it. Only this month uses the pinned amount, and
    the month shows it in place of the carried amount. Each amount is in millionths of a dollar.
    """

    carried_amount: int | None = None
    pinned_amount: int | None = None

    def __post_init__(self) -> None:
        if self.carried_amount is None and self.pinned_amount is None:
            raise ValueError("a budget target needs a carried amount, a pinned amount, or both")

    @staticmethod
    def from_item(item: Mapping[str, Any]) -> BudgetTargetAmounts:
        """Return the amounts of a stored Budget row."""

        return BudgetTargetAmounts(
            carried_amount=_as_int(item.get("carriedAmount")), pinned_amount=_as_int(item.get("pinnedAmount"))
        )

    @property
    def effective_amount(self) -> int:
        """The amount that the month shows: the pinned amount if it is set, else the carried amount."""

        return self.pinned_amount if self.pinned_amount is not None else self.carried_amount or 0

    def to_item(self) -> dict[str, Any]:
        """Converts an instance of BudgetTargetAmounts to how it is stored in DynamoDB."""

        item: dict[str, Any] = {}
        if self.carried_amount is not None:
            item["carriedAmount"] = self.carried_amount
        if self.pinned_amount is not None:
            item["pinnedAmount"] = self.pinned_amount
        return item


class BudgetTable:
    """Monthly budget target management."""

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def get_budget_targets(self, year_month: str) -> list[dict[str, Any]]:
        """Get all budget targets for a given month (YYYY-MM)."""

        return query_all_items(
            self._table, KeyConditionExpression="yearMonth = :ym", ExpressionAttributeValues={":ym": year_month}
        )

    def set_all_budget_targets(
        self, year_month: str, targets: Mapping[str, BudgetTargetAmounts]
    ) -> list[dict[str, Any]]:
        """Set the budget targets for one month, by categoryId. This writes over every target in that month."""

        existing = self.get_budget_targets(year_month)
        with self._table.batch_writer() as batch:
            for item in existing:
                batch.delete_item(Key={"yearMonth": year_month, "categoryId": item["categoryId"]})

        items: list[dict[str, Any]] = []
        with self._table.batch_writer() as batch:
            for category_id, amounts in targets.items():
                item = {"yearMonth": year_month, "categoryId": category_id, **amounts.to_item()}
                batch.put_item(Item=item)
                items.append(item)

        _LOGGER.info("Set %d targets for %s", len(items), year_month)
        return items

    def scan(self) -> list[dict[str, Any]]:
        """Return every row in the table."""

        return scan_all_items(self._table)

    def set_single_budget_target(self, year_month: str, category_id: str, amounts: BudgetTargetAmounts) -> None:
        """Write one budget row. It replaces the row."""

        self._table.put_item(Item={"yearMonth": year_month, "categoryId": category_id, **amounts.to_item()})

    def delete_single_budget_target(self, year_month: str, category_id: str) -> None:
        """Delete one budget row."""

        self._table.delete_item(Key={"yearMonth": year_month, "categoryId": category_id})

    def get_rows_for_category(self, category_id: str) -> list[dict[str, Any]]:
        """Return the key of every Budget row that names this categoryId, in all months."""

        return scan_all_items(
            self._table,
            FilterExpression="categoryId = :cid",
            ExpressionAttributeValues={":cid": category_id},
            ProjectionExpression="yearMonth, categoryId",
        )

    def delete_all_for_category(self, category_id: str) -> int:
        """Delete every Budget row that names this categoryId. Return the number of deleted rows."""

        keys = [
            {"yearMonth": str(row["yearMonth"]), "categoryId": str(row["categoryId"])}
            for row in self.get_rows_for_category(category_id)
        ]
        if not keys:
            return 0
        with self._table.batch_writer() as batch:
            for key in keys:
                batch.delete_item(Key=key)
        _LOGGER.info("Hard-deleted %d Budget rows for category %s", len(keys), category_id)
        return len(keys)


class BudgetDensifyAdapter(DenseSparseStore):
    """Implements the DenseSparseStore protocol for the BudgetTable class."""

    def __init__(self, budget_table: BudgetTable) -> None:
        self._budget_table = budget_table
        self._rows: list[dict[str, Any]] | None = None

    def scan_stored_values(self) -> list[StoredValue]:
        """Return each Budget row with a carried amount, as `(yearMonth, categoryId, carriedAmount)`."""

        return [
            (r["yearMonth"], r["categoryId"], _as_int(r["carriedAmount"])) for r in self._scan() if "carriedAmount" in r
        ]

    def scan_only_values(self) -> list[StoredValue]:
        """Return each Budget row with a pinned amount, as `(yearMonth, categoryId, pinnedAmount)`."""

        return [
            (r["yearMonth"], r["categoryId"], _as_int(r["pinnedAmount"])) for r in self._scan() if "pinnedAmount" in r
        ]

    def put_stored_value(self, year_month: str, key: str, value: Any) -> None:
        """Write the carried amount of one row, and keep its pinned amount. A None value writes $0."""

        pinned = next(
            (
                _as_int(r.get("pinnedAmount"))
                for r in self._scan()
                if r["yearMonth"] == year_month and r["categoryId"] == key
            ),
            None,
        )
        carried = 0 if value is None else value
        self._budget_table.set_single_budget_target(
            year_month, key, BudgetTargetAmounts(carried_amount=carried, pinned_amount=pinned)
        )

    def _scan(self) -> list[dict[str, Any]]:
        # One scan serves both lists, and put_stored_value reads it to keep a pinned amount.
        if self._rows is None:
            self._rows = self._budget_table.scan()
        return self._rows


def _as_int(value: Any) -> int | None:
    """Return a stored amount as an int. DynamoDB returns a number as a Decimal."""

    if value is None:
        return None
    return int(value) if isinstance(value, Decimal) else value
