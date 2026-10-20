"""The IncomeSource table: each source of planned income, such as a job, interest, or dividends."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE, scan_pages

_LOGGER = logging.getLogger(__name__)


class IncomeSourceTable:
    """CRUD operations for income sources. An income source is any stream of income, such as a job.

    The money is not here. Each month of the plan holds a payment row in `IncomePlanTable`.
    """

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def list_all(self) -> list[dict[str, Any]]:
        """Return every income source (active and inactive), sorted by sortOrder, then label."""
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {}
        for response in scan_pages(self._table, **params):
            items.extend(response.get("Items", []))
        return sorted(items, key=lambda s: (int(s.get("sortOrder", 0)), str(s["label"])))

    def get(self, income_source_id: str) -> dict[str, Any] | None:
        response = self._table.get_item(Key={"incomeSourceId": income_source_id})
        return response.get("Item")

    def create(self, owners: list[str], label: str, frequency: str) -> dict[str, Any]:
        """Create an active income source at the end of the order. Returns the created item.

        `owners` is one or more personIds, the same as on an account. A joint source, such as the
        interest of a joint account, has two. The caller checks that each person exists.
        """
        existing = self.list_all()
        sort_order = max((int(s.get("sortOrder", 0)) for s in existing), default=-1) + 1
        now = datetime.now(tz=UTC).isoformat()
        item: dict[str, Any] = {
            "incomeSourceId": str(ULID()),
            "owners": owners,
            "label": label,
            "frequency": frequency,
            "active": True,
            "sortOrder": sort_order,
            "createdAt": now,
            "updatedAt": now,
        }
        self._table.put_item(Item=item)
        _LOGGER.info("Created income source %s", item["incomeSourceId"])
        return item

    def update(self, income_source_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        """Change `label`, `frequency`, and/or `owners`. Returns the changed item, or None if not found.

        The caller checks the values first.
        """
        if self.get(income_source_id) is None:
            return None
        set_parts = ["updatedAt = :now"]
        values: dict[str, Any] = {":now": datetime.now(tz=UTC).isoformat()}
        for field in ("label", "frequency", "owners"):
            if field in updates:
                set_parts.append(f"{field} = :{field}")
                values[f":{field}"] = updates[field]
        response = self._table.update_item(
            Key={"incomeSourceId": income_source_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        return response["Attributes"]

    def set_active(self, income_source_id: str, active: bool) -> dict[str, Any] | None:
        """Deactivate or reactivate an income source. Returns the changed item, or None if not found."""
        if self.get(income_source_id) is None:
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
        """Return [{incomeSourceId, label}] for every income source that lists `person_id` as an owner.

        The handler uses this to block the delete of a person who still owns an income source.
        """
        return [
            {"incomeSourceId": s["incomeSourceId"], "label": s["label"]}
            for s in self.list_all()
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


def active_income_source_ids(income_sources: list[dict[str, Any]]) -> list[str]:
    """Return the IDs of the active income sources, in the order given. The densify step uses them."""
    return [s["incomeSourceId"] for s in income_sources if s["active"]]
