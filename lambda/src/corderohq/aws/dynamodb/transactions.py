from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE, query_all_items, scan_all_items, update_expression
from corderohq.budget.transaction_args import CreateTransactionArgs, UpdateTransactionArgs
from corderohq.common import FieldChange

_LOGGER = logging.getLogger(__name__)


class TransactionsTable:
    """Actual transactions data. These data are applied against budget categories."""

    @staticmethod
    def _new_item(new: CreateTransactionArgs, created_at: str) -> dict[str, Any]:
        """Return the stored item of a new transaction. The sort key sorts by date, and a ULID makes it unique."""

        return {**new.to_item(), "sortId": f"{new.transaction_date}#{ULID()}", "createdAt": created_at}

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def list_transactions_for_month(self, year_month: str) -> list[dict[str, Any]]:
        """Return every transaction for one month (YYYY-MM), sorted by sortId."""

        return query_all_items(
            self._table, KeyConditionExpression="yearMonth = :ym", ExpressionAttributeValues={":ym": year_month}
        )

    def create_transaction(self, new: CreateTransactionArgs) -> dict[str, Any]:
        """Create a single transaction. Returns the created item."""

        item = self._new_item(new, datetime.now(tz=UTC).isoformat())
        self._table.put_item(Item=item)
        _LOGGER.info("Created transaction %s in %s", item["sortId"], item["yearMonth"])
        return item

    def batch_create_transaction(self, rows: list[CreateTransactionArgs]) -> list[dict[str, Any]]:
        """Create multiple transactions in a batch. Returns the created items."""

        now = datetime.now(tz=UTC).isoformat()
        items = [self._new_item(new, now) for new in rows]
        with self._table.batch_writer() as batch:
            for item in items:
                batch.put_item(Item=item)
        _LOGGER.info("Batch created %d transactions", len(items))
        return items

    def get_transaction(self, year_month: str, sort_id: str) -> dict[str, Any] | None:
        """Return one transaction, or None if it does not exist."""

        response = self._table.get_item(Key={"yearMonth": year_month, "sortId": sort_id})
        return response.get("Item")

    def update_transaction(self, year_month: str, sort_id: str, args: UpdateTransactionArgs) -> dict[str, Any]:
        """Apply the checked changes to a transaction, and return the updated item.

        Raise ValueError if the changes change nothing.
        """

        changes: dict[str, FieldChange[Any]] = {
            "description": args.description,
            "amount": args.amount,
            "budgetTreatment": args.budget_treatment,
            "categoryId": args.category_id,
            "accountId": args.account_id,
            "note": args.note,
        }
        response = self._table.update_item(
            Key={"yearMonth": year_month, "sortId": sort_id}, ReturnValues="ALL_NEW", **update_expression(changes)
        )
        _LOGGER.info("Updated transaction %s/%s", year_month, sort_id)
        return response["Attributes"]

    def delete_transaction(self, year_month: str, sort_id: str) -> bool:
        """Delete a transaction. Returns True if deleted, False if not found."""

        response = self._table.get_item(Key={"yearMonth": year_month, "sortId": sort_id})
        if "Item" not in response:
            return False

        self._table.delete_item(Key={"yearMonth": year_month, "sortId": sort_id})
        _LOGGER.info("Deleted transaction %s/%s", year_month, sort_id)
        return True

    def batch_delete_transaction(self, items: list[dict[str, str]]) -> int:
        """Delete multiple transactions. Return the number of deleted items.

        Each item must have the keys `yearMonth` and `sortId`.
        """

        count = 0
        with self._table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={"yearMonth": item["yearMonth"], "sortId": item["sortId"]})
                count += 1
        _LOGGER.info("Batch deleted %d transactions", count)
        return count

    def get_rows_for_category(self, category_id: str) -> list[dict[str, Any]]:
        """Return the key of every transaction that names this categoryId, in all months."""

        return scan_all_items(
            self._table,
            FilterExpression="categoryId = :cid",
            ExpressionAttributeValues={":cid": category_id},
            ProjectionExpression="yearMonth, sortId",
        )

    def delete_all_transactions_for_category(self, category_id: str) -> int:
        """Delete every transaction that names this categoryId. Return the number of deleted items."""

        keys = [
            {"yearMonth": str(row["yearMonth"]), "sortId": str(row["sortId"])}
            for row in self.get_rows_for_category(category_id)
        ]
        if not keys:
            return 0
        with self._table.batch_writer() as batch:
            for key in keys:
                batch.delete_item(Key=key)
        _LOGGER.info("Hard-deleted %d Transactions for category %s", len(keys), category_id)
        return len(keys)
