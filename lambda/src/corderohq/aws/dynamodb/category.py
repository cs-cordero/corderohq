from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE

_LOGGER = logging.getLogger(__name__)


class CategoryTable:
    """CRUD operations for budget categories."""

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def get_active_categories(self) -> list[dict[str, Any]]:
        """Return all active categories, sorted by name."""

        response = self._table.scan(
            FilterExpression="active = :val",
            ExpressionAttributeValues={":val": True},
        )
        items: list[dict[str, Any]] = response["Items"]
        return sorted(items, key=lambda c: c["name"])

    def get_all_categories(self) -> list[dict[str, Any]]:
        """Return all categories (active and inactive), sorted by name."""

        response = self._table.scan()
        items: list[dict[str, Any]] = response["Items"]
        return sorted(items, key=lambda c: c["name"])

    def get_category_by_id(self, category_id: str) -> dict[str, Any] | None:
        """Return a single category by ID, or None if not found."""

        response = self._table.get_item(Key={"categoryId": category_id})
        return response.get("Item")

    def create_category(self, name: str, group_id: str) -> dict[str, Any]:
        """Create a new category in `group_id`. Returns the created item."""

        name = name.strip()
        if not name:
            raise ValueError("Category name must not be empty")
        if not group_id:
            raise ValueError("group_id is required")
        if self._name_exists(name):
            raise ValueError(f"A category named '{name}' already exists")

        now = datetime.now(tz=UTC).isoformat()
        item: dict[str, Any] = {
            "categoryId": str(ULID()),
            "name": name,
            "active": True,
            "createdAt": now,
            "updatedAt": now,
            "nameHistory": [],
            "groupId": group_id,
            "orderInGroup": self._next_order_in_group(group_id),
        }

        self._table.put_item(Item=item)
        _LOGGER.info("Created category: %s (%s) in group %s", item["name"], item["categoryId"], group_id)
        return item

    def update(self, category_id: str, name: str) -> dict[str, Any] | None:
        """Rename a category. Return the updated item, or None if it does not exist.

        A changed name adds the old name to `nameHistory`.
        """

        name = name.strip()
        if not name:
            raise ValueError("Category name must not be empty")
        if self._name_exists(name, exclude_id=category_id):
            raise ValueError(f"A category named '{name}' already exists")

        existing = self.get_category_by_id(category_id)
        if existing is None:
            return None

        now = datetime.now(tz=UTC).isoformat()
        if existing["name"] == name:
            # The name did not change. Set updatedAt only, and add nothing to nameHistory.
            response = self._table.update_item(
                Key={"categoryId": category_id},
                UpdateExpression="SET updatedAt = :now",
                ExpressionAttributeValues={":now": now},
                ReturnValues="ALL_NEW",
            )
            return response["Attributes"]

        # One history entry, as the architecture spec states: previousName and a replacedAt
        # time. A summary of a locked month reads this, and it then shows the name that the
        # category had at that time.
        history_entry = {"previousName": existing["name"], "replacedAt": now}
        response = self._table.update_item(
            Key={"categoryId": category_id},
            UpdateExpression=(
                "SET #n = :name, updatedAt = :now, "
                "nameHistory = list_append(if_not_exists(nameHistory, :empty), :entry)"
            ),
            ExpressionAttributeNames={"#n": "name"},
            ExpressionAttributeValues={
                ":name": name,
                ":now": now,
                ":empty": [],
                ":entry": [history_entry],
            },
            ReturnValues="ALL_NEW",
        )
        _LOGGER.info("Renamed category %s: %s -> %s", category_id, existing["name"], name)
        return response["Attributes"]

    def move_to_group(self, category_id: str, new_group_id: str) -> dict[str, Any] | None:
        """Move a category to a new group, appending it to the end of that group's order."""

        existing = self.get_category_by_id(category_id)
        if existing is None:
            return None
        if existing.get("groupId") == new_group_id:
            return existing
        now = datetime.now(tz=UTC).isoformat()
        response = self._table.update_item(
            Key={"categoryId": category_id},
            UpdateExpression="SET groupId = :g, orderInGroup = :o, updatedAt = :now",
            ExpressionAttributeValues={
                ":g": new_group_id,
                ":o": self._next_order_in_group(new_group_id),
                ":now": now,
            },
            ReturnValues="ALL_NEW",
        )
        return response["Attributes"]

    def reorder_in_group(self, group_id: str, category_ids_in_order: list[str]) -> None:
        """Put the given categories in `group_id`, in the given order.

        A category from a different group moves to `group_id`.
        """

        now = datetime.now(tz=UTC).isoformat()
        for index, cid in enumerate(category_ids_in_order):
            self._table.update_item(
                Key={"categoryId": cid},
                UpdateExpression="SET groupId = :g, orderInGroup = :o, updatedAt = :now",
                ExpressionAttributeValues={":g": group_id, ":o": index, ":now": now},
            )

    def deactivate(self, category_id: str) -> dict[str, Any] | None:
        """Set active=False on a category. Return the changed item, or None if it does not exist."""

        if self.get_category_by_id(category_id) is None:
            return None

        now = datetime.now(tz=UTC).isoformat()
        response = self._table.update_item(
            Key={"categoryId": category_id},
            UpdateExpression="SET active = :val, updatedAt = :now",
            ExpressionAttributeValues={":val": False, ":now": now},
            ReturnValues="ALL_NEW",
        )
        _LOGGER.info("Deactivated category %s", category_id)
        return response["Attributes"]

    def delete(self, category_id: str) -> None:
        """Delete the Category row for ever."""

        self._table.delete_item(Key={"categoryId": category_id})

    def reactivate(self, category_id: str) -> dict[str, Any] | None:
        """Re-activate a deactivated category. Returns the updated item, or None if not found."""

        if self.get_category_by_id(category_id) is None:
            return None

        now = datetime.now(tz=UTC).isoformat()
        response = self._table.update_item(
            Key={"categoryId": category_id},
            UpdateExpression="SET active = :val, updatedAt = :now",
            ExpressionAttributeValues={":val": True, ":now": now},
            ReturnValues="ALL_NEW",
        )
        _LOGGER.info("Reactivated category %s", category_id)
        return response["Attributes"]

    def _name_exists(self, name: str, exclude_id: str | None = None) -> bool:
        """Checks whether a name already exists across all active and inactive categories.

        This check does a full Scan across the table and iteratively checks for name collisions. This therefore does not
        handle concurrent write race conditions.
        """

        lower_name = name.lower()
        return any(
            cat["name"].lower() == lower_name and cat["categoryId"] != exclude_id for cat in self.get_all_categories()
        )

    def _next_order_in_group(self, group_id: str) -> int:
        """Next available orderInGroup, placing new categories at the end of the group."""

        cats_in_group = [c for c in self.get_all_categories() if c.get("groupId") == group_id]
        if not cats_in_group:
            return 0
        return max(int(c.get("orderInGroup", 0)) for c in cats_in_group) + 1


def active_category_ids(categories: list[dict[str, Any]]) -> list[str]:
    """Return the IDs of the active categories, in the order given. The densify step uses them."""

    return [c["categoryId"] for c in categories if c["active"]]
