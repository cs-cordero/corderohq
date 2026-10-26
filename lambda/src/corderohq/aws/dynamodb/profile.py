from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE, query_all_items
from corderohq.networth.models import HOUSEHOLD_PK, PROFILE_SETTINGS_SK, validate_birth_year_month

_LOGGER = logging.getLogger(__name__)


class ProfileTable:
    """The people of the household."""

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def list_profiles(self) -> list[dict[str, Any]]:
        """Return every person in the household, sorted by name.

        The app currently assumes only a single household is present in the data, and therefore the HOUSEHOLD_PK is
        hardcoded.
        """

        items = query_all_items(
            self._table, KeyConditionExpression="householdId = :pk", ExpressionAttributeValues={":pk": HOUSEHOLD_PK}
        )
        people = [p for p in items if p["personId"] != PROFILE_SETTINGS_SK]
        return sorted(people, key=lambda p: (str(p.get("name", "")).lower(), p["personId"]))

    def get_profile(self, person_id: str) -> dict[str, Any] | None:
        response = self._table.get_item(Key={"householdId": HOUSEHOLD_PK, "personId": person_id})
        return response.get("Item")

    def create_profile(self, name: str, birth_year_month: str) -> dict[str, Any]:
        """Create a profile, and return it. Raise ValueError for an empty name or a bad birthYearMonth.

        The new profile belongs to the one household of the app, HOUSEHOLD_PK.
        """

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

    def update_profile(
        self, person_id: str, name: str | None = None, birth_year_month: str | None = None
    ) -> dict[str, Any] | None:
        """Update a person's name and/or birthYearMonth. Returns the item, or None if absent."""

        if self.get_profile(person_id) is None:
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

    def delete_profile(self, person_id: str) -> None:
        """Delete a person for ever.

        It does not check whether an account still names the person as an owner.
        """

        self._table.delete_item(Key={"householdId": HOUSEHOLD_PK, "personId": person_id})
        _LOGGER.info("Deleted person: %s", person_id)
