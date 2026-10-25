from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE, scan_pages

_LOGGER = logging.getLogger(__name__)


class NetWorthSnapshotTable:
    """Monthly per-account balance snapshots.

    Mirrors the BudgetTable pattern: PK yearMonth, SK accountId. Sparse by design —
    a missing row means "no value recorded", never zero. One month = a single
    Query; the whole history = a Scan (trivial at ~20 accounts x 12 months/year).
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
        """Per-class merge upsert of a month's rows; returns the month's stored state.

        Each row is {accountId, classes: {class: millionths | None}, note?}. Semantics
        (read-modify-write per account, so partial payloads never clobber history):
        - a class mapped to a number is set; mapped to None is removed; classes NOT in
          the map are left untouched.
        - if the account's resulting per-class map is empty, the row is deleted.
        - `note` (account-level, month-specific): a present non-empty string replaces
          it, present-but-empty/None clears it, and an absent `note` key leaves any
          existing note untouched.
        Accounts not present in `rows` are untouched. Returns get_month(year_month).
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
