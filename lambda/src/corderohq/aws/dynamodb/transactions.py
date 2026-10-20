"""The Transactions table: the ledger of each month."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE, scan_pages
from corderohq.budget.dispositions import DISPOSITION_ATTRIBUTES, Disposition, resolve_disposition_fields

_LOGGER = logging.getLogger(__name__)


class TransactionsTable:
    """Transaction storage and querying."""

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def list_for_month(self, year_month: str) -> list[dict[str, Any]]:
        """Return every transaction for one month (YYYY-MM), sorted by sortId."""
        response = self._table.query(
            KeyConditionExpression="yearMonth = :ym",
            ExpressionAttributeValues={":ym": year_month},
        )
        return response["Items"]

    def create(
        self,
        year_month: str,
        transaction_date: str,
        description: str,
        amount: int,
        category_id: str | None,
        disposition: Disposition = Disposition.BUDGET,
        account_id: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Create a single transaction. Returns the created item.

        The caller checks the disposition rules first (`resolve_disposition_fields`). An optional
        value that is `None` is not stored.
        """
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
            "disposition": disposition,
            "createdAt": now,
        }
        if category_id is not None:
            item["categoryId"] = category_id
        if account_id is not None:
            item["accountId"] = account_id
        if note is not None:
            item["note"] = note

        self._table.put_item(Item=item)
        _LOGGER.info("Created transaction %s in %s", sort_id, year_month)
        return item

    def batch_create(self, transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Create multiple transactions in a batch. Returns created items.

        Each dict must have yearMonth, transactionDate, description, and amount. The keys
        disposition, categoryId, accountId, note, and fingerprint are copied when they are
        present. The caller checks the disposition rules first.
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
                    "createdAt": now,
                }
                for name in (*DISPOSITION_ATTRIBUTES, "fingerprint"):
                    if txn.get(name) is not None:
                        item[name] = txn[name]
                batch.put_item(Item=item)
                items.append(item)

        _LOGGER.info("Batch created %d transactions", len(items))
        return items

    def update(self, year_month: str, sort_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        """Update the mutable fields of a transaction. Returns the updated item, or None if not found.

        The mutable fields are description, amount, and the attributes in
        `DISPOSITION_ATTRIBUTES`. If `updates` names any disposition attribute, the method merges
        the change over the stored row, checks the disposition rules, and then writes all four
        attributes: it sets each value that exists, and removes each one that does not. Raise
        `ValueError` if a rule fails.
        """
        response = self._table.get_item(Key={"yearMonth": year_month, "sortId": sort_id})
        if "Item" not in response:
            return None
        existing = response["Item"]

        set_values: dict[str, Any] = {field: updates[field] for field in ("description", "amount") if field in updates}
        remove_names: list[str] = []
        if any(name in updates for name in DISPOSITION_ATTRIBUTES):
            fields = resolve_disposition_fields(updates, base=existing)
            set_values.update(fields.stored_values())
            remove_names = fields.absent_attributes()

        if not set_values and not remove_names:
            return existing

        attr_names: dict[str, str] = {}
        attr_values: dict[str, Any] = {}
        set_parts: list[str] = []
        for field, value in set_values.items():
            attr_names[f"#{field}"] = field
            attr_values[f":{field}"] = value
            set_parts.append(f"#{field} = :{field}")
        for field in remove_names:
            attr_names[f"#{field}"] = field

        expression = "SET " + ", ".join(set_parts)
        if remove_names:
            expression += " REMOVE " + ", ".join(f"#{field}" for field in remove_names)

        result = self._table.update_item(
            Key={"yearMonth": year_month, "sortId": sort_id},
            UpdateExpression=expression,
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
            ReturnValues="ALL_NEW",
        )
        _LOGGER.info("Updated transaction %s/%s", year_month, sort_id)
        return result["Attributes"]

    def delete(self, year_month: str, sort_id: str) -> bool:
        """Delete a transaction. Returns True if deleted, False if not found."""
        response = self._table.get_item(Key={"yearMonth": year_month, "sortId": sort_id})
        if "Item" not in response:
            return False

        self._table.delete_item(Key={"yearMonth": year_month, "sortId": sort_id})
        _LOGGER.info("Deleted transaction %s/%s", year_month, sort_id)
        return True

    def batch_delete(self, items: list[dict[str, str]]) -> int:
        """Delete multiple transactions. Returns the number of items deleted.

        Each item must have 'yearMonth' and 'sortId' keys. Uses DynamoDB
        batch_writer which handles chunking into 25-item batches automatically.
        """
        count = 0
        with self._table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={"yearMonth": item["yearMonth"], "sortId": item["sortId"]})
                count += 1
        _LOGGER.info("Batch deleted %d transactions", count)
        return count

    def count_rows_for_category(self, category_id: str) -> int:
        """Count each Transactions row that names this categoryId. The hard-delete preview calls this."""
        count = 0
        params: dict[str, Any] = {
            "FilterExpression": "categoryId = :cid",
            "ExpressionAttributeValues": {":cid": category_id},
            "Select": "COUNT",
        }
        for response in scan_pages(self._table, **params):
            count += response.get("Count", 0)
        return count

    def delete_all_for_category(self, category_id: str) -> int:
        """Delete every Transactions row that names this categoryId. Return the count.

        Hard-delete only — there's no soft-delete equivalent for transactions.
        See docs/personal-finance-architecture.md "Hard delete semantics".
        """
        keys: list[dict[str, str]] = []
        params: dict[str, Any] = {
            "FilterExpression": "categoryId = :cid",
            "ExpressionAttributeValues": {":cid": category_id},
            "ProjectionExpression": "yearMonth, sortId",
        }
        for response in scan_pages(self._table, **params):
            keys.extend(
                {"yearMonth": str(item["yearMonth"]), "sortId": str(item["sortId"])} for item in response["Items"]
            )

        if not keys:
            return 0
        with self._table.batch_writer() as batch:
            for key in keys:
                batch.delete_item(Key=key)
        _LOGGER.info("Hard-deleted %d Transactions for category %s", len(keys), category_id)
        return len(keys)
