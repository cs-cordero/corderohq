from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE, query_all_items, scan_all_items

_LOGGER = logging.getLogger(__name__)


class TransactionsTable:
    """Actual transactions data. These data are applied against budget categories."""

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def list_transactions_for_month(self, year_month: str) -> list[dict[str, Any]]:
        """Return every transaction for one month (YYYY-MM), sorted by sortId."""

        return query_all_items(
            self._table, KeyConditionExpression="yearMonth = :ym", ExpressionAttributeValues={":ym": year_month}
        )

    def create_transaction(
        self,
        year_month: str,
        transaction_date: str,
        description: str,
        amount: int,
        category_id: str,
    ) -> dict[str, Any]:
        """Create a single transaction. Returns the created item."""

        description = description.strip()
        if not description:
            raise ValueError("Transaction description must not be empty")

        now = datetime.now(tz=UTC).isoformat()
        sort_id = f"{transaction_date}#{ULID()}"

        item: dict[str, Any] = {
            "yearMonth": year_month,
            "sortId": sort_id,
            "transactionDate": transaction_date,
            "description": description,
            "amount": amount,
            "categoryId": category_id,
            "createdAt": now,
        }

        self._table.put_item(Item=item)
        _LOGGER.info("Created transaction %s in %s", sort_id, year_month)
        return item

    def batch_create_transaction(self, transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Create multiple transactions in a batch. Returns the created items.

        Each dict must have yearMonth, transactionDate, description, amount, and categoryId.
        """

        now = datetime.now(tz=UTC).isoformat()
        items: list[dict[str, Any]] = []

        with self._table.batch_writer() as batch:
            for txn in transactions:
                sort_id = f"{txn['transactionDate']}#{ULID()}"
                item: dict[str, Any] = {
                    "yearMonth": txn["yearMonth"],
                    "sortId": sort_id,
                    "transactionDate": txn["transactionDate"],
                    "description": txn["description"].strip(),
                    "amount": txn["amount"],
                    "categoryId": txn["categoryId"],
                    "createdAt": now,
                }
                batch.put_item(Item=item)
                items.append(item)

        _LOGGER.info("Batch created %d transactions", len(items))
        return items

    def update_transaction(self, year_month: str, sort_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        """Update the mutable fields of a transaction. Returns the updated item, or None if not found.

        The mutable fields are description, amount, and categoryId.
        """

        response = self._table.get_item(Key={"yearMonth": year_month, "sortId": sort_id})
        if "Item" not in response:
            return None

        update_parts: list[str] = []
        attr_values: dict[str, Any] = {}
        attr_names: dict[str, str] = {}

        for field in ("description", "amount", "categoryId"):
            if field in updates:
                placeholder = f":{field}"
                update_parts.append(f"#{field} = {placeholder}")
                attr_values[placeholder] = updates[field]
                attr_names[f"#{field}"] = field

        if not update_parts:
            return response["Item"]

        result = self._table.update_item(
            Key={"yearMonth": year_month, "sortId": sort_id},
            UpdateExpression="SET " + ", ".join(update_parts),
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
            ReturnValues="ALL_NEW",
        )
        _LOGGER.info("Updated transaction %s/%s", year_month, sort_id)
        return result["Attributes"]

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
