"""The Account table: the financial accounts of the net worth record."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE
from corderohq.networth.models import (
    account_type_meta,
    resolve_account_type_fields,
    resolve_target_year,
    validate_asset_classes,
    validate_budget_role,
    validate_last_four,
    validate_loan_terms,
    validate_owners,
    validate_target_year,
)

_LOGGER = logging.getLogger(__name__)


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
        self._table = DYNAMO_RESOURCE.Table(table_name)

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
        loanTerms, notes, sortOrder, budgetRole, lastFour. `accountType` and `liability` are IMMUTABLE after
        create (the type drives liability, asset-class rules, and loan-term
        eligibility — reclassifying is a deactivate-and-recreate). `assetClasses` is
        editable (a non-empty set, replaced wholesale) only for types that let the
        user choose; on a fixed-class type it raises. `targetYear` is reconciled
        against the effective asset-class set (required with `target_date`, cleared
        without it). `loanTerms` is gated on the type's amortizing flag. `owners` is
        required and replaced wholesale (never removed). A field present with value
        None REMOVEs the optional attribute (loanTerms/notes/budgetRole). An empty
        `lastFour` list removes that attribute.

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
        if "budgetRole" in updates:
            # null removes the role, and the account then uses the default for its type.
            budget_role = validate_budget_role(updates["budgetRole"])
            if budget_role is None:
                remove_parts.append("budgetRole")
            else:
                set_parts.append("budgetRole = :br")
                values[":br"] = budget_role
        if "lastFour" in updates:
            last_four = validate_last_four(updates["lastFour"])
            if not last_four:
                remove_parts.append("lastFour")
            else:
                set_parts.append("lastFour = :lf")
                values[":lf"] = last_four

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
