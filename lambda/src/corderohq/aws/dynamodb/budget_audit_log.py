from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE

_LOGGER = logging.getLogger(__name__)


_AUDIT_PARTITION = "AUDIT"


class AuditAction(StrEnum):
    """The action of an audit log entry. The value is stored in `BudgetAuditLog.action`."""

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    PIN = "PIN"
    UNPIN = "UNPIN"
    CATEGORY_HARD_DELETE = "CATEGORY_HARD_DELETE"


class BudgetAuditLogTable:
    """The audit log of the budget, with one entry for each change."""

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def write_entry(
        self,
        effective_year_month: str | None,
        category_id: str,
        action: AuditAction,
        explanation: str,
        user: dict[str, str],
        changes: dict[str, Any],
        override: bool = False,
    ) -> dict[str, Any]:
        """Write one audit log entry, and return it.

        `user` holds the `sub`, `email`, and `username` claims of the request. `effective_year_month` is None for an
        action that applies to no single month, such as CATEGORY_HARD_DELETE.
        """

        now = datetime.now(tz=UTC)

        item: dict[str, Any] = {
            "entityType": _AUDIT_PARTITION,
            "sortId": str(ULID()),
            "changedAt": now.isoformat(),
            "effectiveYearMonth": effective_year_month,
            "categoryId": category_id,
            "action": action,
            "explanation": explanation,
            "user": user,
            "override": override,
            "changes": changes,
        }

        self._table.put_item(Item=item)
        _LOGGER.info(
            "Audit: %s on %s/%s by %s%s",
            action,
            effective_year_month,
            category_id,
            user.get("email", user.get("sub", "?")),
            " (override)" if override else "",
        )
        return item

    def read_recent_entries(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent `limit` entries, newest first."""

        response = self._table.query(
            KeyConditionExpression="entityType = :pk",
            ExpressionAttributeValues={":pk": _AUDIT_PARTITION},
            ScanIndexForward=False,
            Limit=limit,
        )
        return list(response["Items"])
