"""The BudgetAuditLog table: one entry for each change to a budget target or to the income plan."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE

_LOGGER = logging.getLogger(__name__)


_AUDIT_PARTITION = "AUDIT"


class BudgetAuditLogTable:
    """Budget audit log writes and reads.

    Single-partition design: PK is always the literal string "AUDIT", SK is a ULID
    that sorts chronologically. Recent-N reads are one Query with
    ScanIndexForward=false; no month-walking required.
    """

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def write_entry(
        self,
        effective_year_month: str | None,
        category_id: str | None,
        action: str,
        explanation: str,
        user: dict[str, str],
        changes: dict[str, Any],
        override: bool = False,
        income_source_id: str | None = None,
        income_line_id: str | None = None,
    ) -> dict[str, Any]:
        """Write a single audit log entry. Returns the created item.

        `user` is the JWT-claims snapshot {sub, email, username} captured at request time
        per the architecture doc's "Audit user identity" section.
        `effective_year_month` may be None for actions not scoped to a single month
        (e.g. CATEGORY_HARD_DELETE).
        A budget entry has a `category_id`. An income plan entry has an `income_line_id`, and a
        payment entry also has an `income_source_id`. An ID that is None is not stored.
        """
        now = datetime.now(tz=UTC)

        item: dict[str, Any] = {
            "entityType": _AUDIT_PARTITION,
            "sortId": str(ULID()),
            "changedAt": now.isoformat(),
            "effectiveYearMonth": effective_year_month,
            "action": action,
            "explanation": explanation,
            "user": user,
            "override": override,
            "changes": changes,
        }
        if category_id is not None:
            item["categoryId"] = category_id
        if income_source_id is not None:
            item["incomeSourceId"] = income_source_id
        if income_line_id is not None:
            item["incomeLineId"] = income_line_id

        self._table.put_item(Item=item)
        _LOGGER.info(
            "Audit: %s on %s/%s by %s%s",
            action,
            effective_year_month,
            category_id or income_line_id,
            user.get("email", user.get("sub", "?")),
            " (override)" if override else "",
        )
        return item

    def read_recent_entries(self, limit: int = 50) -> list[dict[str, Any]]:
        """Read the last N audit log entries, newest first.

        Single Query against the AUDIT partition with ScanIndexForward=False so
        DynamoDB walks the sort key in descending ULID order.
        """
        response = self._table.query(
            KeyConditionExpression="entityType = :pk",
            ExpressionAttributeValues={":pk": _AUDIT_PARTITION},
            ScanIndexForward=False,
            Limit=limit,
        )
        return list(response["Items"])
