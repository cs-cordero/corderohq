from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.accounts import CreateAccountArgs, UpdateAccountArgs
from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE, scan_all_items, update_expression
from corderohq.field_changes import FieldChange, Keep, Remove, SetTo

_LOGGER = logging.getLogger(__name__)


class AccountTable:
    """Financial accounts for a household."""

    @staticmethod
    def _sorted(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Sort the accounts by sortOrder, and then by name. Case does not count."""

        return sorted(items, key=lambda a: (int(a.get("sortOrder", 0)), str(a.get("name", "")).lower()))

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def get_active_accounts(self) -> list[dict[str, Any]]:
        """Return the active accounts, sorted by sortOrder and then by name."""

        items = scan_all_items(self._table, FilterExpression="active = :val", ExpressionAttributeValues={":val": True})
        return self._sorted(items)

    def get_all_accounts(self) -> list[dict[str, Any]]:
        """Return all accounts, active and inactive, sorted by sortOrder and then by name."""

        return self._sorted(scan_all_items(self._table))

    def get_account_by_id(self, account_id: str) -> dict[str, Any] | None:
        """Return the account, or None if it does not exist."""

        response = self._table.get_item(Key={"accountId": account_id})
        return response.get("Item")

    def create_account(self, args: CreateAccountArgs) -> dict[str, Any]:
        """Create an account, and return the stored item. Raise ValueError for a duplicate name."""

        if self._name_exists(args.name):
            raise ValueError(f"An account named '{args.name}' already exists")

        now = datetime.now(tz=UTC).isoformat()
        item: dict[str, Any] = {
            "accountId": str(ULID()),
            "name": args.name,
            "owners": args.owners,
            "excludedFromNetWorth": args.excluded_from_net_worth,
            "active": True,
            "sortOrder": self._next_sort_order(),
            "createdAt": now,
            "updatedAt": now,
        }
        if args.notes is not None:
            item["notes"] = args.notes
        item.update(args.account.to_item())

        self._table.put_item(Item=item)
        _LOGGER.info("Created account: %s (%s)", item["name"], item["accountId"])
        return item

    def update_account(self, account_id: str, args: UpdateAccountArgs) -> dict[str, Any]:
        """Apply the checked changes to an account, and return the updated item.

        Raise ValueError for a duplicate name. The caller reads the account first, and parses the
        changes against it.
        """

        match args.name:
            case SetTo(value=name):
                if self._name_exists(name, exclude_id=account_id):
                    raise ValueError(f"An account named '{name}' already exists")
            case Keep():
                pass

        # Store loan terms in their DynamoDB form.
        loan_terms: FieldChange[dict[str, Any]]
        match args.loan_terms:
            case SetTo(value=terms):
                loan_terms = SetTo(terms.to_item())
            case Keep() | Remove() as unchanged:
                loan_terms = unchanged
        changes: dict[str, FieldChange[Any]] = {
            "name": args.name,
            "owners": args.owners,
            "excludedFromNetWorth": args.excluded_from_net_worth,
            "sortOrder": args.sort_order,
            "notes": args.notes,
            "assetClasses": args.asset_classes,
            "targetYear": args.target_year,
            "loanTerms": loan_terms,
            "budgetRole": args.budget_role,
            "lastFour": args.last_four,
        }
        response = self._table.update_item(
            Key={"accountId": account_id},
            ReturnValues="ALL_NEW",
            **update_expression(changes, always_set={"updatedAt": datetime.now(tz=UTC).isoformat()}),
        )
        return response["Attributes"]

    def deactivate(self, account_id: str) -> dict[str, Any] | None:
        """Close an account, and keep its history. Return the item, or None if it does not exist."""

        return self._set_active(account_id, False)

    def reactivate(self, account_id: str) -> dict[str, Any] | None:
        """Open a closed account again. Return the item, or None if it does not exist."""

        return self._set_active(account_id, True)

    def reorder(self, account_ids_in_order: list[str]) -> None:
        """Set the sortOrder of each account to its position in the list."""

        now = datetime.now(tz=UTC).isoformat()
        for index, account_id in enumerate(account_ids_in_order):
            self._table.update_item(
                Key={"accountId": account_id},
                UpdateExpression="SET sortOrder = :o, updatedAt = :now",
                ExpressionAttributeValues={":o": index, ":now": now},
            )

    def accounts_with_owner(self, person_id: str) -> list[dict[str, str]]:
        """Return the accountId and the name of each account that lists `person_id` as an owner."""

        return [
            {"accountId": a["accountId"], "name": a["name"]}
            for a in self.get_all_accounts()
            if person_id in a.get("owners", [])
        ]

    def _name_exists(self, name: str, exclude_id: str | None = None) -> bool:
        """Checks whether a name already exists across all active and inactive accounts.

        This check does a full Scan across the table and iteratively checks for name collisions.
        This therefore does not handle concurrent write race conditions.
        """

        lower_name = name.lower()
        return any(a["name"].lower() == lower_name and a["accountId"] != exclude_id for a in self.get_all_accounts())

    def _next_sort_order(self) -> int:
        accounts = self.get_all_accounts()
        if not accounts:
            return 0
        return max(int(a.get("sortOrder", 0)) for a in accounts) + 1

    def _set_active(self, account_id: str, active: bool) -> dict[str, Any] | None:
        if self.get_account_by_id(account_id) is None:
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
