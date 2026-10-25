from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo


class EditabilityState(StrEnum):
    """Whether a year-month can change. GRACE is the previous month in its grace period."""

    EDITABLE = "EDITABLE"
    GRACE = "GRACE"
    LOCKED = "LOCKED"


_N_FUTURE_MONTHS = 12
_GRACE_PERIOD_DAYS = 7
_BUDGET_TZ = "America/New_York"

_YEAR_MONTH_LEN = 7  # "YYYY-MM"


def _to_month_ordinal(year: int, month: int) -> int:
    """Convert (year, month) to a single integer for easy month-distance math."""

    return year * 12 + (month - 1)


def _from_month_ordinal(ordinal: int) -> tuple[int, int]:
    return (ordinal // 12, (ordinal % 12) + 1)


def _parse_year_month(year_month: str) -> tuple[int, int]:
    """Parse "YYYY-MM" into (year, month). Raises ValueError on malformed input."""

    if len(year_month) != _YEAR_MONTH_LEN or year_month[4] != "-":
        raise ValueError(f"Invalid year-month: {year_month!r}, expected YYYY-MM")
    return int(year_month[:4]), int(year_month[5:])


def editability(year_month: str, now_utc: datetime) -> EditabilityState:
    """Return the editability state of `year_month` at the instant `now_utc`.

    `now_utc` must be a timezone-aware datetime.

    - The current month (in ET) and the next _N_FUTURE_MONTHS months are EDITABLE.
    - The previous month is GRACE until midnight ET at the start of day 1 + _GRACE_PERIOD_DAYS of the current month.
      After that, it is LOCKED.
    - All other months are LOCKED.

    The user can change a GRACE month, the same as an EDITABLE month.
    """

    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")

    tz = ZoneInfo(_BUDGET_TZ)
    now_et = now_utc.astimezone(tz)

    target_year, target_month = _parse_year_month(year_month)
    target_ord = _to_month_ordinal(target_year, target_month)
    current_ord = _to_month_ordinal(now_et.year, now_et.month)
    diff = target_ord - current_ord

    if diff == 0 or 1 <= diff <= _N_FUTURE_MONTHS:
        return EditabilityState.EDITABLE

    if diff == -1:
        grace_cutoff_et = datetime(
            now_et.year,
            now_et.month,
            1 + _GRACE_PERIOD_DAYS,
            0,
            0,
            0,
            tzinfo=tz,
        )
        if now_et < grace_cutoff_et:
            return EditabilityState.GRACE

    return EditabilityState.LOCKED
