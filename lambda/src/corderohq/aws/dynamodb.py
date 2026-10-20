"""AWS DynamoDB table classes for the budget app."""

from __future__ import annotations

import calendar
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import boto3
from ulid import ULID

from corderohq.budget.editability import editability
from corderohq.networth.models import (
    HOUSEHOLD_PK,
    PROFILE_SETTINGS_SK,
    account_type_meta,
    resolve_account_type_fields,
    resolve_target_year,
    validate_asset_classes,
    validate_birth_year_month,
    validate_loan_terms,
    validate_owners,
    validate_target_year,
)

_DYNAMO_RESOURCE = boto3.resource("dynamodb")
_LOGGER = logging.getLogger(__name__)
_BUDGET_TZ = ZoneInfo("America/New_York")


def current_year_month() -> str:
    """Return the current YYYY-MM string."""
    now = datetime.now(tz=UTC)
    return f"{now.year}-{now.month:02d}"


def _month_end_utc(year_month: str) -> datetime:
    """Return the last instant of `year_month` in BUDGET_TZ, converted to UTC.

    Per the architecture's "Display names on locked months" section: the cutoff
    must use the DST offset in effect on the last day of the year-month, not a
    fixed one. ZoneInfo handles that automatically — we just construct the local
    end-of-month datetime and call .astimezone(UTC).
    """
    year, month = int(year_month[:4]), int(year_month[5:7])
    last_day = calendar.monthrange(year, month)[1]
    local_end = datetime(year, month, last_day, 23, 59, 59, 999999, tzinfo=_BUDGET_TZ)
    return local_end.astimezone(UTC)


def _prev_year_month(year_month: str) -> str:
    year, month = int(year_month[:4]), int(year_month[5:7])
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"


def _walk_back_amount(by_key: dict[tuple[str, str], Any], year_month: str, cat_id: str, max_steps: int = 240) -> Any:
    """Find the most recent stored amount at or before `year_month` for `cat_id`.

    Mirrors densify.walk_back_for but lives here to avoid a circular import
    between dynamodb.py and budget.densify (densify imports BudgetTable from
    here). Returns None if no row is found within the safety bound.
    """
    if (year_month, cat_id) in by_key:
        return by_key[(year_month, cat_id)]
    cursor = year_month
    for _ in range(max_steps):
        cursor = _prev_year_month(cursor)
        if (cursor, cat_id) in by_key:
            return by_key[(cursor, cat_id)]
    return None


def _resolve_historical_name(category: dict[str, Any], month_end_utc: datetime) -> str | None:
    """Return the category's `previousName` in effect during `month_end_utc`, or None.

    Walks `nameHistory` for the earliest entry whose `replacedAt` is strictly
    greater than `month_end_utc`. If found, the category was renamed AFTER the
    month being summarized — its previousName is the name at the time. Otherwise
    the current name was already in effect and we return None.
    """
    history = category.get("nameHistory") or []
    if not history:
        return None
    # Sort by replacedAt, oldest first. We then take the first entry that comes after the month.
    sorted_history = sorted(history, key=lambda e: e["replacedAt"])
    for entry in sorted_history:
        replaced_at = datetime.fromisoformat(entry["replacedAt"])
        if replaced_at > month_end_utc:
            return str(entry["previousName"])
    return None


class CategoryGroupTable:
    """CRUD operations for category groups.

    Groups own an `order` int — lower numbers display first. Reordering is
    coarse-grained (write the full new order via `reorder`) since the table is
    bounded in size (typically <20 groups).
    """

    def __init__(self, table_name: str) -> None:
        self._table = _DYNAMO_RESOURCE.Table(table_name)

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
        """Create a new group. `system=True` marks the row as immutable
        (no rename, no delete) — used by the "Unassigned" sentinel.
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


class CategoryTable:
    """CRUD operations for budget categories."""

    def __init__(self, table_name: str) -> None:
        self._table = _DYNAMO_RESOURCE.Table(table_name)

    def list_active(self) -> list[dict[str, Any]]:
        """Return all active categories, sorted by name."""
        response = self._table.scan(
            FilterExpression="active = :val",
            ExpressionAttributeValues={":val": True},
        )
        items: list[dict[str, Any]] = response["Items"]
        return sorted(items, key=lambda c: c["name"])

    def list_all(self) -> list[dict[str, Any]]:
        """Return all categories (active and inactive), sorted by name."""
        response = self._table.scan()
        items: list[dict[str, Any]] = response["Items"]
        return sorted(items, key=lambda c: c["name"])

    def get(self, category_id: str) -> dict[str, Any] | None:
        """Return a single category by ID, or None if not found."""
        response = self._table.get_item(Key={"categoryId": category_id})
        return response.get("Item")

    def _name_exists(self, name: str, exclude_id: str | None = None) -> bool:
        """Return True if a category uses this name already. Case does not count.

        This applies to an active category and to an inactive one.

        Race condition: this is a Scan-then-write pattern with no DynamoDB-level
        unique constraint on `name`. Two simultaneous category creates with the same
        name can both pass this check and both write. Accepted because (a) this is a
        single-household app, so concurrent writers are vanishingly rare; (b) adding
        a name-keyed GSI or sentinel item to enforce uniqueness would add complexity
        out of proportion to the risk. If a duplicate ever slips through, rename or
        merge manually. See docs/personal-finance-architecture.md "Category name uniqueness".
        """
        lower_name = name.lower()
        return any(cat["name"].lower() == lower_name and cat["categoryId"] != exclude_id for cat in self.list_all())

    def _next_order_in_group(self, group_id: str) -> int:
        """Next available orderInGroup, placing new categories at the end of the group."""
        cats_in_group = [c for c in self.list_all() if c.get("groupId") == group_id]
        if not cats_in_group:
            return 0
        return max(int(c.get("orderInGroup", 0)) for c in cats_in_group) + 1

    def create(self, name: str, group_id: str) -> dict[str, Any]:
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
        """Update a category's name. Appends to nameHistory if the name actually changed.

        Returns the updated item, or None if not found.
        """
        name = name.strip()
        if not name:
            raise ValueError("Category name must not be empty")
        if self._name_exists(name, exclude_id=category_id):
            raise ValueError(f"A category named '{name}' already exists")

        existing = self.get(category_id)
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
        existing = self.get(category_id)
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
        """Apply a new total ordering for categories within `group_id`.

        Each id in `category_ids_in_order` is updated with its `orderInGroup`
        set to its index, and its `groupId` set to `group_id` (so this also
        functions as a bulk move when called with categories from other groups).
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
        if self.get(category_id) is None:
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
        """Delete the Category row for ever. This has no soft-delete step.

        Only the admin hard delete calls this.
        """
        self._table.delete_item(Key={"categoryId": category_id})

    def reactivate(self, category_id: str) -> dict[str, Any] | None:
        """Re-activate a deactivated category. Returns the updated item, or None if not found."""
        if self.get(category_id) is None:
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


class BudgetTable:
    """Monthly budget target management."""

    def __init__(self, table_name: str) -> None:
        self._table = _DYNAMO_RESOURCE.Table(table_name)

    def get_targets(self, year_month: str) -> list[dict[str, Any]]:
        """Get all budget targets for a given month (YYYY-MM)."""
        response = self._table.query(
            KeyConditionExpression="yearMonth = :ym",
            ExpressionAttributeValues={":ym": year_month},
        )
        return response["Items"]

    def put_targets(self, year_month: str, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Set the budget targets for one month. This writes over every target in that month."""
        existing = self.get_targets(year_month)
        with self._table.batch_writer() as batch:
            for item in existing:
                batch.delete_item(Key={"yearMonth": year_month, "categoryId": item["categoryId"]})

        items: list[dict[str, Any]] = []
        with self._table.batch_writer() as batch:
            for target in targets:
                item: dict[str, Any] = {
                    "yearMonth": year_month,
                    "categoryId": target["categoryId"],
                    "amount": target["amount"],
                }
                batch.put_item(Item=item)
                items.append(item)

        _LOGGER.info("Set %d targets for %s", len(items), year_month)
        return items

    def scan_all(self) -> list[dict[str, Any]]:
        """Return every row in the table.

        Used by densification to load the full state into memory before walking
        forward through missed months. The table is bounded in size (categories
        x months tracked) so a Scan is cheaper than the equivalent fan-out of
        per-month Queries.
        """
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {}
        while True:
            response = self._table.scan(**params)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return items

    def put_single(self, year_month: str, category_id: str, amount: Any) -> None:
        """Write one budget row. The densify step calls this to fill a cell that is absent."""
        self._table.put_item(
            Item={"yearMonth": year_month, "categoryId": category_id, "amount": amount},
        )

    def delete_single(self, year_month: str, category_id: str) -> None:
        """Delete one budget row. /pin calls this when it clears a pin, with amount=null."""
        self._table.delete_item(Key={"yearMonth": year_month, "categoryId": category_id})

    def find_future_months_with_category(self, category_id: str, after_month: str) -> list[str]:
        """Return the sorted yearMonth values later than after_month that hold this categoryId."""
        months: set[str] = set()
        params: dict[str, Any] = {
            "FilterExpression": "categoryId = :cid AND yearMonth > :ym",
            "ExpressionAttributeValues": {":cid": category_id, ":ym": after_month},
            "ProjectionExpression": "yearMonth",
        }
        while True:
            response = self._table.scan(**params)
            for item in response["Items"]:
                months.add(str(item["yearMonth"]))
            if "LastEvaluatedKey" not in response:
                break
            params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return sorted(months)

    def count_rows_for_category(self, category_id: str) -> int:
        """Count each Budget row that names this categoryId. The hard-delete preview calls this."""
        count = 0
        params: dict[str, Any] = {
            "FilterExpression": "categoryId = :cid",
            "ExpressionAttributeValues": {":cid": category_id},
            "Select": "COUNT",
        }
        while True:
            response = self._table.scan(**params)
            count += response.get("Count", 0)
            if "LastEvaluatedKey" not in response:
                break
            params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return count

    def find_past_months_with_category(self, category_id: str, before_month: str) -> list[str]:
        """Return the sorted yearMonth values earlier than before_month that hold this categoryId.

        Used by the hard-delete preview to highlight historical data the delete would erase.
        The caller filters by editability state if it needs only locked vs grace.
        """
        months: set[str] = set()
        params: dict[str, Any] = {
            "FilterExpression": "categoryId = :cid AND yearMonth < :ym",
            "ExpressionAttributeValues": {":cid": category_id, ":ym": before_month},
            "ProjectionExpression": "yearMonth",
        }
        while True:
            response = self._table.scan(**params)
            for item in response["Items"]:
                months.add(str(item["yearMonth"]))
            if "LastEvaluatedKey" not in response:
                break
            params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return sorted(months)

    def delete_all_for_category(self, category_id: str) -> int:
        """Delete every Budget row that names this categoryId. Return the count.

        Used by hard-delete only — soft-delete (/deactivate) drops just the future pin
        rows. See docs/personal-finance-architecture.md "Hard delete semantics".
        """
        keys: list[dict[str, str]] = []
        params: dict[str, Any] = {
            "FilterExpression": "categoryId = :cid",
            "ExpressionAttributeValues": {":cid": category_id},
            "ProjectionExpression": "yearMonth, categoryId",
        }
        while True:
            response = self._table.scan(**params)
            keys.extend(
                {"yearMonth": str(item["yearMonth"]), "categoryId": str(item["categoryId"])}
                for item in response["Items"]
            )
            if "LastEvaluatedKey" not in response:
                break
            params["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        if not keys:
            return 0
        with self._table.batch_writer() as batch:
            for key in keys:
                batch.delete_item(Key=key)
        _LOGGER.info("Hard-deleted %d Budget rows for category %s", len(keys), category_id)
        return len(keys)


class TransactionsTable:
    """Transaction storage and querying."""

    def __init__(self, table_name: str) -> None:
        self._table = _DYNAMO_RESOURCE.Table(table_name)

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
        category_id: str,
    ) -> dict[str, Any]:
        """Create a single transaction. Returns the created item."""
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
            "categoryId": category_id,
            "createdAt": now,
        }

        self._table.put_item(Item=item)
        _LOGGER.info("Created transaction %s in %s", sort_id, year_month)
        return item

    def batch_create(self, transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Create multiple transactions in a batch. Each dict must have yearMonth,
        transactionDate, description, amount, categoryId. Returns created items."""
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
                    "categoryId": txn["categoryId"],
                    "createdAt": now,
                }
                batch.put_item(Item=item)
                items.append(item)

        _LOGGER.info("Batch created %d transactions", len(items))
        return items

    def update(self, year_month: str, sort_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        """Update a transaction's mutable fields (description, amount, categoryId).
        Returns the updated item, or None if not found."""
        response = self._table.get_item(Key={"yearMonth": year_month, "sortId": sort_id})
        if "Item" not in response:
            return None

        update_parts: list[str] = []
        attr_values: dict[str, Any] = {}
        attr_names: dict[str, str] = {}

        for field in ("description", "amount", "categoryId"):
            if field in updates:
                placeholder = f":{field}"
                update_parts.append(f"#{field} = {placeholder}")
                attr_values[placeholder] = updates[field]
                attr_names[f"#{field}"] = field

        if not update_parts:
            return response["Item"]

        result = self._table.update_item(
            Key={"yearMonth": year_month, "sortId": sort_id},
            UpdateExpression="SET " + ", ".join(update_parts),
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
        while True:
            response = self._table.scan(**params)
            count += response.get("Count", 0)
            if "LastEvaluatedKey" not in response:
                break
            params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
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
        while True:
            response = self._table.scan(**params)
            keys.extend(
                {"yearMonth": str(item["yearMonth"]), "sortId": str(item["sortId"])} for item in response["Items"]
            )
            if "LastEvaluatedKey" not in response:
                break
            params["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        if not keys:
            return 0
        with self._table.batch_writer() as batch:
            for key in keys:
                batch.delete_item(Key=key)
        _LOGGER.info("Hard-deleted %d Transactions for category %s", len(keys), category_id)
        return len(keys)


_AUDIT_PARTITION = "AUDIT"


class AuditLogTable:
    """Budget audit log writes and reads.

    Single-partition design: PK is always the literal string "AUDIT", SK is a ULID
    that sorts chronologically. Recent-N reads are one Query with
    ScanIndexForward=false; no month-walking required.
    """

    def __init__(self, table_name: str) -> None:
        self._table = _DYNAMO_RESOURCE.Table(table_name)

    def write_entry(
        self,
        effective_year_month: str | None,
        category_id: str,
        action: str,
        explanation: str,
        user: dict[str, str],
        changes: dict[str, Any],
        override: bool = False,
    ) -> dict[str, Any]:
        """Write a single audit log entry. Returns the created item.

        `user` is the JWT-claims snapshot {sub, email, username} captured at request time
        per the architecture doc's "Audit user identity" section.
        `effective_year_month` may be None for actions not scoped to a single month
        (e.g. CATEGORY_HARD_DELETE).
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


def compute_summary(
    budget: BudgetTable,
    transactions: TransactionsTable,
    category_table: CategoryTable,
    year_month: str,
    now: datetime,
) -> dict[str, Any]:
    """Compute budget vs actuals for a given month per Spec 5.

    Membership rules (architecture: "Summary view membership rules"):
    - **Editable months** — currently active categories, plus any category
      (active or inactive) with at least one transaction with amount != 0 in this month.
    - **Locked months** — every category referenced by a budget row (any amount,
      including $0) OR at least one transaction, regardless of current `active` flag.

    Each row carries `name` (current) and `historicalName` (set only for locked
    months when the category was renamed AFTER this year-month — see
    "Display names on locked months"). The frontend renders the pair as
    `historicalName (renamed to: name)`.

    Amounts are signed (positive=expense, negative=income/return). Aggregation is
    a straight sum — no `abs()`, no sign flips. A refund or income row reduces the
    category's actual, which makes delta (= budgeted - actual) larger.
    """
    txns = transactions.list_for_month(year_month)
    categories_all = category_table.list_all()
    category_by_id = {c["categoryId"]: c for c in categories_all}

    current_ym = f"{now.year:04d}-{now.month:02d}"
    is_future = year_month > current_ym

    # The densify step writes `amount=null` when the walk back finds no earlier row.
    # (architecture: "New category, no prior history"). Null means "no target" —
    # for aggregation we treat it as 0 since there's nothing to subtract from.
    #
    # For dense months (year_month <= current_ym) every active category has a
    # stored row; a direct query is sufficient. For future months the table is
    # sparse — only explicit pins live there — so the effective budgeted value
    # is whatever walk-back resolves to, per architecture's "Display of a future
    # month is resolved per category" rule.
    budget_by_category: dict[str, Decimal] = {}
    if is_future:
        rows = budget.scan_all()
        by_key: dict[tuple[str, str], Any] = {(r["yearMonth"], r["categoryId"]): r["amount"] for r in rows}
        for cat in categories_all:
            cat_id = cat["categoryId"]
            raw = _walk_back_amount(by_key, year_month, cat_id)
            if raw is not None:
                budget_by_category[cat_id] = Decimal("0") if raw is None else Decimal(str(raw))
    else:
        for target in budget.get_targets(year_month):
            raw = target.get("amount")
            budget_by_category[target["categoryId"]] = Decimal("0") if raw is None else Decimal(str(raw))

    actual_by_category: dict[str, Decimal] = {}
    nonzero_txn_cat_ids: set[str] = set()
    for txn in txns:
        cat_id = txn["categoryId"]
        raw_amount = txn.get("amount")
        amount = Decimal("0") if raw_amount is None else Decimal(str(raw_amount))
        actual_by_category[cat_id] = actual_by_category.get(cat_id, Decimal("0")) + amount
        if amount != 0:
            nonzero_txn_cat_ids.add(cat_id)

    state = editability(year_month, now)
    is_locked = state == "LOCKED"

    if is_locked:
        # Locked rule: every category with ANY budget row or ANY transaction (zero or not).
        # Budget rows are never removed on deactivation, so they're the historical signal.
        member_ids = set(budget_by_category.keys()) | {t["categoryId"] for t in txns}
    else:
        # Editable rule: active categories + (inactive with non-zero txn).
        active_ids = {c["categoryId"] for c in categories_all if c.get("active", False)}
        member_ids = active_ids | nonzero_txn_cat_ids
    all_category_ids = sorted(member_ids)

    month_end_utc = _month_end_utc(year_month) if is_locked else None

    categories: list[dict[str, Any]] = []
    total_budgeted = Decimal("0")
    total_actual = Decimal("0")

    for cat_id in all_category_ids:
        budgeted = budget_by_category.get(cat_id, Decimal("0"))
        actual = actual_by_category.get(cat_id, Decimal("0"))
        delta = budgeted - actual

        cat = category_by_id.get(cat_id)
        name = str(cat["name"]) if cat else cat_id
        historical_name: str | None = None
        if is_locked and cat is not None and month_end_utc is not None:
            historical_name = _resolve_historical_name(cat, month_end_utc)

        categories.append(
            {
                "categoryId": cat_id,
                "name": name,
                "historicalName": historical_name,
                "budgeted": int(budgeted),
                "actual": int(actual),
                "delta": int(delta),
            }
        )

        total_budgeted += budgeted
        total_actual += actual

    return {
        "yearMonth": year_month,
        "state": state,
        "categories": categories,
        "totals": {
            "budgeted": int(total_budgeted),
            "actual": int(total_actual),
            "delta": int(total_budgeted - total_actual),
        },
    }


# --- The net worth record ---
#
# HOUSEHOLD_PK and PROFILE_SETTINGS_SK are imported from networth.models (the
# authoritative home for the Profile table's key constants); list_people skips the
# reserved SETTINGS sentinel so a future household-settings item can't masquerade
# as a person.


class ProfileTable:
    """The household's people.

    Item-collection design: PK is the literal "HOUSEHOLD", one item per person
    keyed by a ULID sort key. `list_people` is a single Query on the partition —
    the only read pattern anything needs. `Account.owner` references a personId
    from here, so age-based rules resolve owner -> person -> birthYearMonth from
    data rather than any hardcoded string.
    """

    def __init__(self, table_name: str) -> None:
        self._table = _DYNAMO_RESOURCE.Table(table_name)

    def list_people(self) -> list[dict[str, Any]]:
        """Return every person in the household, sorted by name (case-insensitive).

        The reserved SETTINGS sentinel SK (if it ever exists) is excluded — it is
        not a person.
        """
        response = self._table.query(
            KeyConditionExpression="householdId = :pk",
            ExpressionAttributeValues={":pk": HOUSEHOLD_PK},
        )
        people = [p for p in response["Items"] if p["personId"] != PROFILE_SETTINGS_SK]
        return sorted(people, key=lambda p: (str(p.get("name", "")).lower(), p["personId"]))

    def get_person(self, person_id: str) -> dict[str, Any] | None:
        response = self._table.get_item(Key={"householdId": HOUSEHOLD_PK, "personId": person_id})
        return response.get("Item")

    def create_person(self, name: str, birth_year_month: str) -> dict[str, Any]:
        """Create a person. Validates name non-empty and birthYearMonth as YYYY-MM."""
        name = name.strip()
        if not name:
            raise ValueError("Person name must not be empty")
        birth_year_month = validate_birth_year_month(birth_year_month)

        now = datetime.now(tz=UTC).isoformat()
        item: dict[str, Any] = {
            "householdId": HOUSEHOLD_PK,
            "personId": str(ULID()),
            "name": name,
            "birthYearMonth": birth_year_month,
            "createdAt": now,
            "updatedAt": now,
        }
        self._table.put_item(Item=item)
        _LOGGER.info("Created person: %s (%s)", item["name"], item["personId"])
        return item

    def update_person(
        self, person_id: str, name: str | None = None, birth_year_month: str | None = None
    ) -> dict[str, Any] | None:
        """Update a person's name and/or birthYearMonth. Returns the item, or None if absent."""
        if self.get_person(person_id) is None:
            return None

        set_parts: list[str] = ["updatedAt = :now"]
        values: dict[str, Any] = {":now": datetime.now(tz=UTC).isoformat()}
        names: dict[str, str] = {}

        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("Person name must not be empty")
            set_parts.append("#n = :name")
            names["#n"] = "name"
            values[":name"] = name
        if birth_year_month is not None:
            values[":bym"] = validate_birth_year_month(birth_year_month)
            set_parts.append("birthYearMonth = :bym")

        kwargs: dict[str, Any] = {
            "Key": {"householdId": HOUSEHOLD_PK, "personId": person_id},
            "UpdateExpression": "SET " + ", ".join(set_parts),
            "ExpressionAttributeValues": values,
            "ReturnValues": "ALL_NEW",
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names
        response = self._table.update_item(**kwargs)
        return response["Attributes"]

    def delete_person(self, person_id: str) -> None:
        """Hard-delete a person by key.

        The no-referencing-accounts invariant is enforced server-side by the sole
        caller, the `_delete_person` handler (a 409 when `accounts_with_owner` is
        non-empty). It lives there rather than here because the check spans the
        Account table, which this Profile-scoped wrapper has no handle on; the
        handler is the single API entry point, so the invariant always holds.
        """
        self._table.delete_item(Key={"householdId": HOUSEHOLD_PK, "personId": person_id})
        _LOGGER.info("Deleted person: %s", person_id)


def _loan_terms_for_storage(loan_terms: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of loanTerms with `interestRate` as a Decimal for DynamoDB.

    The boto3 resource client rejects Python floats ("Float types are not
    supported. Use Decimal types instead"). `interestRate` arrives as a float
    (e.g. 0.04875); convert via str() so we store the exact decimal the user
    entered rather than a binary-float approximation. `monthlyPayment` is already
    an int (millionths). The handler's JSON encoder turns the Decimal back into a
    float on read, so the API contract is unchanged.
    """
    stored = dict(loan_terms)
    stored["interestRate"] = Decimal(str(stored["interestRate"]))
    return stored


class AccountTable:
    """Financial accounts (assets and liabilities).

    Mirrors the CategoryTable pattern: PK-only, soft-deactivation, small item
    count so Scan is fine. Enum + loanTerms validation is delegated to
    networth.models so the same rules apply on create and edit.
    """

    def __init__(self, table_name: str) -> None:
        self._table = _DYNAMO_RESOURCE.Table(table_name)

    def list_active(self) -> list[dict[str, Any]]:
        """Return active accounts, sorted by sortOrder then name."""
        response = self._table.scan(
            FilterExpression="active = :val",
            ExpressionAttributeValues={":val": True},
        )
        return self._sorted(response["Items"])

    def list_all(self) -> list[dict[str, Any]]:
        """Return all accounts (active and inactive), sorted by sortOrder then name."""
        response = self._table.scan()
        return self._sorted(response["Items"])

    @staticmethod
    def _sorted(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(items, key=lambda a: (int(a.get("sortOrder", 0)), str(a.get("name", "")).lower()))

    def get(self, account_id: str) -> dict[str, Any] | None:
        response = self._table.get_item(Key={"accountId": account_id})
        return response.get("Item")

    def _name_exists(self, name: str, exclude_id: str | None = None) -> bool:
        """Case-insensitive name uniqueness check across active + inactive accounts.

        Race condition: Scan-then-write with no DynamoDB-level unique constraint on
        `name`, identical to CategoryTable._name_exists. Two simultaneous creates
        with the same name can both pass and both write. Accepted for the same
        reason — single-household app, concurrent writers vanishingly rare; a
        name-keyed GSI would be complexity out of proportion to the risk. Rename or
        merge manually if a duplicate ever slips through.
        """
        lower_name = name.lower()
        return any(a["name"].lower() == lower_name and a["accountId"] != exclude_id for a in self.list_all())

    def _next_sort_order(self) -> int:
        accounts = self.list_all()
        if not accounts:
            return 0
        return max(int(a.get("sortOrder", 0)) for a in accounts) + 1

    def create(
        self,
        *,
        name: str,
        account_type: str,
        asset_classes: list[str] | None,
        owners: list[str],
        excluded_from_net_worth: bool = False,
        target_year: int | None = None,
        loan_terms: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Create an account. The account TYPE is authoritative.

        `liability`, the effective `assetClasses` (a single forced value when the
        type fixes it), and whether `loanTerms` are permitted are all derived from
        the type via `resolve_account_type_fields` — the client cannot assert a
        contradictory combination (no US-equity mortgage, no liability without a
        liability type).

        `owners` is a required non-empty list of personIds, stored as-is; each id's
        existence in the household is validated by the handler (a Profile lookup),
        the same way category groupId existence is checked at the handler layer
        rather than in the table wrapper.

        `excluded_from_net_worth` marks an account that is tracked (its balances are
        still recorded and displayed) but left out of the Total Assets / Total
        Liabilities / Net Worth sums — e.g. a 529 or custodial account you follow
        but that isn't part of the household's own net worth.
        """
        name = name.strip()
        if not name:
            raise ValueError("Account name must not be empty")
        account_type, liability, asset_classes, normalized_loan_terms = resolve_account_type_fields(
            account_type, asset_classes, loan_terms
        )
        normalized_owners = validate_owners(owners)
        if not isinstance(excluded_from_net_worth, bool):
            raise ValueError("excludedFromNetWorth must be a boolean")
        resolved_target_year = resolve_target_year(asset_classes, target_year)
        if self._name_exists(name):
            raise ValueError(f"An account named '{name}' already exists")

        now = datetime.now(tz=UTC).isoformat()
        item: dict[str, Any] = {
            "accountId": str(ULID()),
            "name": name,
            "accountType": account_type,
            "assetClasses": asset_classes,
            "liability": liability,
            "owners": normalized_owners,
            "excludedFromNetWorth": excluded_from_net_worth,
            "active": True,
            "sortOrder": self._next_sort_order(),
            "createdAt": now,
            "updatedAt": now,
        }
        # Optional fields are omitted (not stored as null) when absent, so a row
        # only carries what was actually asserted.
        if resolved_target_year is not None:
            item["targetYear"] = resolved_target_year
        if normalized_loan_terms is not None:
            item["loanTerms"] = _loan_terms_for_storage(normalized_loan_terms)
        if notes is not None:
            item["notes"] = notes

        self._table.put_item(Item=item)
        _LOGGER.info("Created account: %s (%s)", item["name"], item["accountId"])
        return item

    def update(self, account_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        """Partial edit of a mutable account field set.

        Editable fields: name, assetClasses, owners, excludedFromNetWorth, targetYear,
        loanTerms, notes, sortOrder. `accountType` and `liability` are IMMUTABLE after
        create (the type drives liability, asset-class rules, and loan-term
        eligibility — reclassifying is a deactivate-and-recreate). `assetClasses` is
        editable (a non-empty set, replaced wholesale) only for types that let the
        user choose; on a fixed-class type it raises. `targetYear` is reconciled
        against the effective asset-class set (required with `target_date`, cleared
        without it). `loanTerms` is gated on the type's amortizing flag. `owners` is
        required and replaced wholesale (never removed). A field present with value
        None REMOVEs the optional attribute (loanTerms/notes).

        Returns the updated item, or None if the account does not exist.
        """
        existing = self.get(account_id)
        if existing is None:
            return None
        meta = account_type_meta(existing["accountType"])

        set_parts: list[str] = ["updatedAt = :now"]
        remove_parts: list[str] = []
        values: dict[str, Any] = {":now": datetime.now(tz=UTC).isoformat()}
        names: dict[str, str] = {}

        if "name" in updates:
            name = str(updates["name"]).strip()
            if not name:
                raise ValueError("Account name must not be empty")
            if self._name_exists(name, exclude_id=account_id):
                raise ValueError(f"An account named '{name}' already exists")
            set_parts.append("#n = :name")
            names["#n"] = "name"
            values[":name"] = name
        if "assetClasses" in updates:
            if meta.fixed_asset_class is not None:
                raise ValueError(f"assetClasses is fixed for {existing['accountType']} accounts and cannot be changed")
            values[":ac"] = validate_asset_classes(updates["assetClasses"])
            set_parts.append("assetClasses = :ac")
        if "sortOrder" in updates:
            sort_order = updates["sortOrder"]
            if isinstance(sort_order, bool) or not isinstance(sort_order, int):
                raise ValueError("sortOrder must be an integer")
            values[":so"] = sort_order
            set_parts.append("sortOrder = :so")
        if "owners" in updates:
            # Wholesale replace; owners is required so there is no removal path.
            # Aliased via ExpressionAttributeNames for consistency with other fields.
            owners = validate_owners(updates["owners"])
            set_parts.append("#ow = :owners")
            names["#ow"] = "owners"
            values[":owners"] = owners
        if "excludedFromNetWorth" in updates:
            excluded = updates["excludedFromNetWorth"]
            if not isinstance(excluded, bool):
                raise ValueError("excludedFromNetWorth must be a boolean")
            values[":exc"] = excluded
            set_parts.append("excludedFromNetWorth = :exc")
        if "loanTerms" in updates:
            if updates["loanTerms"] is None:
                remove_parts.append("loanTerms")
            else:
                normalized = validate_loan_terms(updates["loanTerms"], amortizing=meta.amortizing)
                if normalized is not None:  # always true for a non-None input; narrows the type
                    set_parts.append("loanTerms = :lt")
                    values[":lt"] = _loan_terms_for_storage(normalized)
        if "notes" in updates:
            if updates["notes"] is None:
                remove_parts.append("notes")
            else:
                set_parts.append("notes = :notes")
                values[":notes"] = str(updates["notes"])

        # Reconcile targetYear against the effective (post-update) asset-class set, so
        # adding target_date requires a year and removing it clears a stale one — even
        # when only one of the two fields is in `updates`.
        effective_classes = values.get(":ac", existing.get("assetClasses", []))
        proposed_year = updates.get("targetYear", existing.get("targetYear"))
        if "target_date" in effective_classes:
            if proposed_year is None:
                raise ValueError("targetYear is required when target_date is one of the asset classes")
            values[":ty"] = validate_target_year(proposed_year)
            set_parts.append("targetYear = :ty")
        else:
            if updates.get("targetYear") is not None:
                raise ValueError("targetYear is only allowed when target_date is an asset class")
            if "targetYear" in existing:
                remove_parts.append("targetYear")

        expression = "SET " + ", ".join(set_parts)
        if remove_parts:
            expression += " REMOVE " + ", ".join(remove_parts)

        kwargs: dict[str, Any] = {
            "Key": {"accountId": account_id},
            "UpdateExpression": expression,
            "ExpressionAttributeValues": values,
            "ReturnValues": "ALL_NEW",
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names
        response = self._table.update_item(**kwargs)
        return response["Attributes"]

    def deactivate(self, account_id: str) -> dict[str, Any] | None:
        """Soft-close an account. History rows are untouched. Returns the item, or None."""
        return self._set_active(account_id, False)

    def reactivate(self, account_id: str) -> dict[str, Any] | None:
        """Reopen a soft-closed account. Returns the item, or None if absent."""
        return self._set_active(account_id, True)

    def _set_active(self, account_id: str, active: bool) -> dict[str, Any] | None:
        if self.get(account_id) is None:
            return None
        now = datetime.now(tz=UTC).isoformat()
        response = self._table.update_item(
            Key={"accountId": account_id},
            UpdateExpression="SET active = :val, updatedAt = :now",
            ExpressionAttributeValues={":val": active, ":now": now},
            ReturnValues="ALL_NEW",
        )
        _LOGGER.info("%s account %s", "Reactivated" if active else "Deactivated", account_id)
        return response["Attributes"]

    def reorder(self, account_ids_in_order: list[str]) -> None:
        """Apply a new total ordering: write sortOrder = index for each id in the list."""
        now = datetime.now(tz=UTC).isoformat()
        for index, account_id in enumerate(account_ids_in_order):
            self._table.update_item(
                Key={"accountId": account_id},
                UpdateExpression="SET sortOrder = :o, updatedAt = :now",
                ExpressionAttributeValues={":o": index, ":now": now},
            )

    def accounts_with_owner(self, person_id: str) -> list[dict[str, str]]:
        """Return [{accountId, name}] for every account that lists `person_id` as an owner.

        Used by the handler to block deleting a person still referenced as an
        account owner (409).
        """
        return [
            {"accountId": a["accountId"], "name": a["name"]}
            for a in self.list_all()
            if person_id in a.get("owners", [])
        ]


class NetWorthSnapshotTable:
    """Monthly per-account balance snapshots.

    Mirrors the BudgetTable pattern: PK yearMonth, SK accountId. Sparse by design —
    a missing row means "no value recorded", never zero. One month = a single
    Query; the whole history = a Scan (trivial at ~20 accounts x 12 months/year).
    """

    def __init__(self, table_name: str) -> None:
        self._table = _DYNAMO_RESOURCE.Table(table_name)

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
        while True:
            response = self._table.scan(**params)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
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
            by_class: dict[str, Any] = dict(existing.get("byAssetClass") or {})
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
