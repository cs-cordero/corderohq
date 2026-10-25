from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE

_LOGGER = logging.getLogger(__name__)


class CategoryGroupTable:
    """CRUD operations for category groups.

    Groups own an `order` int — lower numbers display first. Reordering is
    coarse-grained (write the full new order via `reorder`) since the table is
    bounded in size (typically <20 groups).
    """

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

    def _name_exists(self, name: str, exclude_id: str | None = None) -> bool:
        """Return True if a group uses this name already. Case does not count.

        This check has the same race as CategoryTable.
        """

        lower_name = name.lower()
        return any(g["name"].lower() == lower_name and g["groupId"] != exclude_id for g in self.list_all())

    def _next_order(self) -> int:
        """Return the next free order value for a new group that is not a system group.

        System groups (Unassigned) are excluded from the max computation — they
        sort last by `list_all`'s ordering, so their numeric `order` shouldn't
        push regular groups past them.
        """

        regular = [g for g in self.list_all() if not g.get("system")]
        if not regular:
            return 0
        return max(int(g.get("order", 0)) for g in regular) + 1

    def create(self, name: str, system: bool = False) -> dict[str, Any]:
        """Create a new group.

        `system=True` marks the row as immutable (no rename, no delete). The "Unassigned" sentinel
        uses it.
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
        """Return the (single) `system=True` group, creating it if absent.

        The Unassigned group is a sentinel destination for orphaned categories
        (e.g. deactivated cats whose original group was deleted). It is hidden
        from rename and delete and renders without action buttons in the UI.
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
        """Hard-delete a group by key.

        The empty-group invariant (no active categories still point at it) is
        enforced server-side by the sole caller, the `_delete_category_group`
        handler (a 409 listing blocking categories). It lives there rather than
        here because the check spans the Category table, which this group-scoped
        wrapper has no handle on; the handler is the single API entry point.
        """

        self._table.delete_item(Key={"groupId": group_id})
        _LOGGER.info("Deleted group: %s", group_id)

    def reorder(self, group_ids_in_order: list[str]) -> None:
        """Apply a new total ordering across all groups.

        The caller passes the full ordered list; we write `order = index` on each.
        Groups not present in the list are left untouched (and will display after
        the reordered ones since their order is presumably lower / unchanged).
        """

        now = datetime.now(tz=UTC).isoformat()
        for index, gid in enumerate(group_ids_in_order):
            self._table.update_item(
                Key={"groupId": gid},
                UpdateExpression="SET #o = :o, updatedAt = :now",
                ExpressionAttributeNames={"#o": "order"},
                ExpressionAttributeValues={":o": index, ":now": now},
            )
