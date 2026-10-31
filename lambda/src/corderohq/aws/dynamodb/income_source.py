from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE, scan_all_items, update_expression
from corderohq.budget.income import CreateIncomeSourceArgs, UpdateIncomeSourceArgs
from corderohq.common import FieldChange

_LOGGER = logging.getLogger(__name__)


class IncomeSourceTable:
    """An income source for the household.

    This can be a job or a cash-producing investment, or the aether. It merely identifies a source entity that can be
    associated with PlannedIncome events.
    """

    @staticmethod
    def _sorted(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Sort the income sources by sortOrder, and then by label."""

        return sorted(items, key=lambda s: (int(s.get("sortOrder", 0)), str(s["label"])))

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def get_all_income_sources(self) -> list[dict[str, Any]]:
        """Return every income source (active and inactive), sorted by sortOrder, then label."""

        return self._sorted(scan_all_items(self._table))

    def get_active_income_sources(self) -> list[dict[str, Any]]:
        """Return the active income sources, sorted by sortOrder, then label."""

        return self._sorted(
            scan_all_items(self._table, FilterExpression="active = :val", ExpressionAttributeValues={":val": True})
        )

    def get_income_source_by_id(self, income_source_id: str) -> dict[str, Any] | None:
        response = self._table.get_item(Key={"incomeSourceId": income_source_id})
        return response.get("Item")

    def create_income_source(self, args: CreateIncomeSourceArgs) -> dict[str, Any]:
        """Create an active income source at the end of the order. Returns the created item.

        A joint source, such as the interest of a joint account, has two owners. It does not check that each person
        exists.
        """

        existing = self.get_all_income_sources()
        sort_order = max((int(s.get("sortOrder", 0)) for s in existing), default=-1) + 1
        now = datetime.now(tz=UTC).isoformat()
        item: dict[str, Any] = {
            "incomeSourceId": str(ULID()),
            "owners": args.owners,
            "label": args.label,
            "active": True,
            "sortOrder": sort_order,
            "createdAt": now,
            "updatedAt": now,
        }
        self._table.put_item(Item=item)
        _LOGGER.info("Created income source %s", item["incomeSourceId"])
        return item

    def update_income_source(self, income_source_id: str, args: UpdateIncomeSourceArgs) -> dict[str, Any] | None:
        """Apply the checked changes to an income source. Returns the changed item, or None if not found."""

        if self.get_income_source_by_id(income_source_id) is None:
            return None
        changes: dict[str, FieldChange[Any]] = {"label": args.label, "owners": args.owners}
        response = self._table.update_item(
            Key={"incomeSourceId": income_source_id},
            ReturnValues="ALL_NEW",
            **update_expression(changes, always_set={"updatedAt": datetime.now(tz=UTC).isoformat()}),
        )
        return response["Attributes"]

    def set_active(self, income_source_id: str, active: bool) -> dict[str, Any] | None:
        """Deactivate or reactivate an income source. Returns the changed item, or None if not found."""

        if self.get_income_source_by_id(income_source_id) is None:
            return None
        response = self._table.update_item(
            Key={"incomeSourceId": income_source_id},
            UpdateExpression="SET active = :val, updatedAt = :now",
            ExpressionAttributeValues={":val": active, ":now": datetime.now(tz=UTC).isoformat()},
            ReturnValues="ALL_NEW",
        )
        _LOGGER.info("Set income source %s active=%s", income_source_id, active)
        return response["Attributes"]

    def sources_with_owner(self, person_id: str) -> list[dict[str, str]]:
        """Return [{incomeSourceId, label}] for every income source that lists `person_id` as an owner."""

        return [
            {"incomeSourceId": s["incomeSourceId"], "label": s["label"]}
            for s in self.get_all_income_sources()
            if person_id in s.get("owners", [])
        ]

    def reorder(self, income_source_ids_in_order: list[str]) -> None:
        """Write `sortOrder = index` on each income source in the list."""

        now = datetime.now(tz=UTC).isoformat()
        for index, income_source_id in enumerate(income_source_ids_in_order):
            self._table.update_item(
                Key={"incomeSourceId": income_source_id},
                UpdateExpression="SET sortOrder = :o, updatedAt = :now",
                ExpressionAttributeValues={":o": index, ":now": now},
            )
