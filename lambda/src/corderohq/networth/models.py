from __future__ import annotations

from typing import Any

from corderohq.util import YEAR_MONTH_PATTERN

# The keys for the Profile table. `HOUSEHOLD_PK` is the one partition key, because there
# is one household in practice. `PROFILE_SETTINGS_SK` is a reserved sort key, kept for a
# settings item for the whole household later, such as the horizon age of the simulation.
# The sort key for a person is a ULID, so it never equals that reserved value. These two
# names are the only definitions. The table wrapper imports them, and does not repeat them.
HOUSEHOLD_PK = "HOUSEHOLD"
PROFILE_SETTINGS_SK = "SETTINGS"


def validate_birth_year_month(value: Any) -> str:
    """Return `value` if it is a YYYY-MM string, else raise ValueError."""

    if not isinstance(value, str) or not YEAR_MONTH_PATTERN.match(value):
        raise ValueError("birthYearMonth must be a YYYY-MM string")
    return value
