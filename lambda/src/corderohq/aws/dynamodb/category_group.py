from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE

_LOGGER = logging.getLogger(__name__)


class CategoryGroupTable:
    """Named groups of budget categories. A group with a lower `order` displays first."""

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def list_all(self) -> list[dict[str, Any]]:
        """Return all groups (active + inactive), sorted by order then name."""

        response = self._table.scan()
        items: list[dict[str, Any]] = response["Items"]
        # A system group, such as Unassigned, always sorts last, whatever its number. It
        # therefore stays in place when the user changes the order of the other groups.
        return sorted(items, key=lambda g: (bool(g.get("system", False)), int(g.get("order", 0)), g["name"]))

    def get(self, group_id: str) -> dict[str, Any] | None:
        response = self._table.get_item(Key={"groupId": group_id})
        return response.get("Item")

    def create(self, name: str, system: bool = False) -> dict[str, Any]:
        """Create a group, and return it.

        A `system` group cannot be renamed or deleted. Raise ValueError for an empty or duplicate name.
        """

        name = name.strip()
        if not name:
            raise ValueError("Group name must not be empty")
        if self._name_exists(name):
            raise ValueError(f"A group named '{name}' already exists")

        now = datetime.now(tz=UTC).isoformat()
        item: dict[str, Any] = {
            "groupId": str(ULID()),
            "name": name,
            "order": self._next_order(),
            "createdAt": now,
            "updatedAt": now,
            "system": system,
        }
        self._table.put_item(Item=item)
        _LOGGER.info("Created group: %s (%s)%s", item["name"], item["groupId"], " [system]" if system else "")
        return item

    def get_or_create_unassigned(self) -> dict[str, Any]:
        """Return the system group "Unassigned", and create it if it does not exist.

        It holds the inactive categories whose group was deleted.
        """

        for g in self.list_all():
            if g.get("system"):
                return g
        return self.create("Unassigned", system=True)

    def update(self, group_id: str, name: str) -> dict[str, Any] | None:
        name = name.strip()
        if not name:
            raise ValueError("Group name must not be empty")
        if self._name_exists(name, exclude_id=group_id):
            raise ValueError(f"A group named '{name}' already exists")
        if self.get(group_id) is None:
            return None
        now = datetime.now(tz=UTC).isoformat()
        response = self._table.update_item(
            Key={"groupId": group_id},
            UpdateExpression="SET #n = :name, updatedAt = :now",
            ExpressionAttributeNames={"#n": "name"},
            ExpressionAttributeValues={":name": name, ":now": now},
            ReturnValues="ALL_NEW",
        )
        return response["Attributes"]

    def delete(self, group_id: str) -> None:
        """Delete a group for ever.

        The caller makes sure that no active category is in the group.
        """

        self._table.delete_item(Key={"groupId": group_id})
        _LOGGER.info("Deleted group: %s", group_id)

    def reorder(self, group_ids_in_order: list[str]) -> None:
        """Set the order of each group to its position in the list.

        A group that is not in the list keeps its order.
        """

        now = datetime.now(tz=UTC).isoformat()
        for index, gid in enumerate(group_ids_in_order):
            self._table.update_item(
                Key={"groupId": gid},
                UpdateExpression="SET #o = :o, updatedAt = :now",
                ExpressionAttributeNames={"#o": "order"},
                ExpressionAttributeValues={":o": index, ":now": now},
            )

    def _name_exists(self, name: str, exclude_id: str | None = None) -> bool:
        """Checks whether a name already exists across all groups.

        This check does a full Scan across the table and iteratively checks for name collisions. This therefore does not
        handle concurrent write race conditions.
        """

        lower_name = name.lower()
        return any(g["name"].lower() == lower_name and g["groupId"] != exclude_id for g in self.list_all())

    def _next_order(self) -> int:
        """Return the next order value for a group that is not a system group."""

        regular = [g for g in self.list_all() if not g.get("system")]
        if not regular:
            return 0
        return max(int(g.get("order", 0)) for g in regular) + 1
