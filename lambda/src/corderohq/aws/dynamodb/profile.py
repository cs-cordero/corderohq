from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE
from corderohq.networth.models import HOUSEHOLD_PK, PROFILE_SETTINGS_SK, validate_birth_year_month

_LOGGER = logging.getLogger(__name__)


class ProfileTable:
    """The people of the household. `Account.owners` refers to a personId from here."""

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def list_people(self) -> list[dict[str, Any]]:
        """Return every person in the household, sorted by name. Case does not count."""

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
        """Delete a person for ever.

        The caller makes sure that no account names the person as an owner.
        """

        self._table.delete_item(Key={"householdId": HOUSEHOLD_PK, "personId": person_id})
        _LOGGER.info("Deleted person: %s", person_id)
