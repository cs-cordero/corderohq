from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE, scan_pages

_LOGGER = logging.getLogger(__name__)


class NetWorthSnapshotTable:
    """The balance of each account in each month.

    A missing row means that no value was recorded. It does not mean zero.
    """

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def get_month(self, year_month: str) -> list[dict[str, Any]]:
        """Return every recorded row for a month (one per account with a value)."""

        response = self._table.query(
            KeyConditionExpression="yearMonth = :ym",
            ExpressionAttributeValues={":ym": year_month},
        )
        return response["Items"]

    def scan_all(self) -> list[dict[str, Any]]:
        """Return every snapshot row across all months, paginating the Scan."""

        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {}
        for response in scan_pages(self._table, **params):
            items.extend(response.get("Items", []))
        return items

    def delete_single(self, year_month: str, account_id: str) -> None:
        """Delete a single snapshot row (undo a mistaken entry)."""

        self._table.delete_item(Key={"yearMonth": year_month, "accountId": account_id})

    def upsert_month(self, year_month: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge rows into one month, and return the stored state of that month.

        Each row is `{accountId, classes: {assetClass: millionths | None}, note?}`.

        - A class with a number is set. A class with None is removed. A class that is not in the map does not change.
        - An account with no classes left loses its row.
        - A non-empty `note` replaces the note. An empty or None `note` removes it. An absent `note` does not change it.
        - An account that is not in `rows` does not change.
        """

        now = datetime.now(tz=UTC).isoformat()
        for row in rows:
            account_id = row["accountId"]
            existing = self._table.get_item(Key={"yearMonth": year_month, "accountId": account_id}).get("Item") or {}
            stored_classes = existing.get("byAssetClass")
            by_class: dict[str, Any] = dict(stored_classes) if isinstance(stored_classes, dict) else {}
            for cls, value in (row.get("classes") or {}).items():
                if value is None:
                    by_class.pop(cls, None)
                else:
                    by_class[cls] = value

            if not by_class:
                self._table.delete_item(Key={"yearMonth": year_month, "accountId": account_id})
                continue

            item: dict[str, Any] = {
                "yearMonth": year_month,
                "accountId": account_id,
                "byAssetClass": by_class,
                "updatedAt": now,
            }
            # Note: absent key → preserve existing; present → replace/clear.
            if "note" in row:
                note = row["note"]
                if isinstance(note, str) and note.strip():
                    item["note"] = note.strip()
            elif "note" in existing:
                item["note"] = existing["note"]
            self._table.put_item(Item=item)
        _LOGGER.info("Upserted %d snapshot rows for %s", len(rows), year_month)
        return self.get_month(year_month)
