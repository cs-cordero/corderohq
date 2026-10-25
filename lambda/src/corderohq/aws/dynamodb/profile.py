from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE
from corderohq.networth.models import HOUSEHOLD_PK, PROFILE_SETTINGS_SK, validate_birth_year_month

_LOGGER = logging.getLogger(__name__)


class ProfileTable:
    """The household's people.

    Item-collection design: PK is the literal "HOUSEHOLD", one item per person
    keyed by a ULID sort key. `list_people` is a single Query on the partition —
    the only read pattern anything needs. `Account.owner` references a personId
    from here, so age-based rules resolve owner -> person -> birthYearMonth from
    data rather than any hardcoded string.

    The key constants HOUSEHOLD_PK and PROFILE_SETTINGS_SK come from networth.models. `list_people`
    skips the reserved SETTINGS item, so that a future household settings item never looks like a
    person.
    """

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

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
