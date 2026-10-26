from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.accounts import (
    CreateAccountArgs,
    get_metadata_for_account_type,
    parse_loan_terms,
    validate_asset_classes,
    validate_owners,
    validate_target_year,
)
from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE

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

        response = self._table.scan(
            FilterExpression="active = :val",
            ExpressionAttributeValues={":val": True},
        )
        return self._sorted(response["Items"])

    def get_all_accounts(self) -> list[dict[str, Any]]:
        """Return all accounts, active and inactive, sorted by sortOrder and then by name."""

        response = self._table.scan()
        return self._sorted(response["Items"])

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

    def update(self, account_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        """Change the given fields of an account. Return the updated item, or None if it does not exist.

        The editable fields are name, assetClasses, owners, excludedFromNetWorth, targetYear,
        loanTerms, notes, and sortOrder. The account type cannot change. A value of None removes
        loanTerms or notes. Raise ValueError for invalid input.
        """

        existing = self.get_account_by_id(account_id)
        if existing is None:
            return None
        meta = get_metadata_for_account_type(existing["accountType"])

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
            # The list is replaced as a whole. It is required, so it cannot be removed.
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
                terms = parse_loan_terms(updates["loanTerms"], amortizing=meta.amortizing)
                if terms is not None:  # always true for a non-None input; narrows the type
                    set_parts.append("loanTerms = :lt")
                    values[":lt"] = terms.to_item()
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
