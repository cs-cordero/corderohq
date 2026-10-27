from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

# A safety limit on how far the walk back goes. The architecture says that the walk back
# has no fixed limit, and that it continues until it finds a row or reaches the end of the
# history. But an empty table and a request for a future month would then loop for ever.
# 240 months, which is 20 years, is far more than any household budget history.
_MAX_WALKBACK_MONTHS = 240

# One stored value, with its month and its key: (yearMonth, key, value).
StoredValue = tuple[str, str, Any]


class DenseSparseStore(Protocol):
    """A Protocol for a table that the densify step makes dense.

    A month is dense when the table holds a stored value for each of its keys, so no value
    comes from a walk back to an earlier month. A table that must become dense implements this
    Protocol. The densify step uses it to read the stored values and to write each missing value.

    A key in a month can hold a carried value, which later months copy, and a this-month-only
    value, which only its own month uses.
    """

    def scan_stored_values(self) -> list[StoredValue]:
        """Return each carried value as `(yearMonth, key, value)`."""

        ...

    def scan_only_values(self) -> list[StoredValue]:
        """Return each this-month-only value as `(yearMonth, key, value)`."""

        ...

    def put_stored_value(self, year_month: str, key: str, value: Any) -> None:
        """Store the carried value of one key in one month, and keep its this-month-only value.

        The densify step calls this to fill a missing value.
        """

        ...


@dataclass(frozen=True)
class ResolvedValue:
    """One key in a future month.

    `value` is the carried value: the pin at this month, or the value from the walk back. It is
    `None` if neither exists. `pinned` says that this month holds its own carried value. `only` is
    the this-month-only value, if `has_only`. The month shows `only` when `has_only`, else `value`.
    """

    key: str
    value: Any
    pinned: bool
    only: Any = None
    has_only: bool = False

    @property
    def effective(self) -> Any:
        return self.only if self.has_only else self.value


def prev_year_month(year_month: str) -> str:
    """Return the YYYY-MM string for the month immediately before `year_month`."""

    year, month = int(year_month[:4]), int(year_month[5:7])
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"


def next_year_month(year_month: str) -> str:
    """Return the YYYY-MM string for the month immediately after `year_month`."""

    year, month = int(year_month[:4]), int(year_month[5:7])
    if month == 12:
        return f"{year + 1}-01"
    return f"{year}-{month + 1:02d}"


def carry_map(rows: list[StoredValue]) -> dict[tuple[str, str], Any]:
    """Return the rows as a dict from `(yearMonth, key)` to the value."""

    return {(year_month, key): value for year_month, key, value in rows}


def densify(
    store: DenseSparseStore,
    active_keys: list[str],
    current_ym: str,
    cold_start_value: Any,
) -> int:
    """Make every month up to `current_ym` dense. Return the number of rows written.

    Each missing row gets the value of the nearest row before it, or None. An existing row does not change. When the
    table is empty, each active key gets `cold_start_value` in `current_ym` only. A second call writes nothing.
    """

    rows = store.scan_stored_values()
    by_key = carry_map(rows)

    past_months = sorted({year_month for year_month, _, _ in rows if year_month <= current_ym})
    m_last = past_months[-1] if past_months else None

    if m_last is None:
        for key in active_keys:
            store.put_stored_value(current_ym, key, cold_start_value)
        return len(active_keys)

    if m_last >= current_ym:
        return 0

    months_to_fill = _months_between_exclusive(m_last, current_ym)
    rows_written = 0
    for month in months_to_fill:
        for key in active_keys:
            if (month, key) in by_key:
                # A row is here already, such as a pin the user wrote before the month changed.
                continue
            value = walk_back_for(by_key, month, key)
            by_key[(month, key)] = value
            store.put_stored_value(month, key, value)
            rows_written += 1

    return rows_written


def resolve_future(store: DenseSparseStore, active_keys: list[str], year_month: str) -> list[ResolvedValue]:
    """Return each active key of a future month, with its carried and its this-month-only value.

    A pin at `year_month` wins, with `pinned=True`. Else the carried value comes from the nearest
    row before it, with `pinned=False`. The walk back reads carried values only, so a this-month-only
    value never reaches a later month. The this-month-only value is the one at `year_month`, if it
    exists. A key with no carried value (or None) and no this-month-only value is left out.
    `year_month` must be after the current month.
    """

    by_key = carry_map(store.scan_stored_values())
    only_by_key = carry_map(store.scan_only_values())
    result: list[ResolvedValue] = []
    for key in active_keys:
        pinned = (year_month, key) in by_key
        value = by_key[(year_month, key)] if pinned else walk_back_for(by_key, year_month, key)
        has_only = (year_month, key) in only_by_key
        if value is None and not has_only:
            continue
        only = only_by_key.get((year_month, key))
        result.append(ResolvedValue(key=key, value=value, pinned=pinned, only=only, has_only=has_only))
    return result


def resolve_future_targets(
    budget_store: DenseSparseStore,
    active_category_ids: list[str],
    year_month: str,
) -> list[dict[str, Any]]:
    """Return the budget targets of a future month, in the shape that the API sends.

    Each target is `{yearMonth, categoryId, carriedAmount, pinned}`, plus `pinnedAmount` when the month has a pinned
    amount. The month shows `pinnedAmount` if it is present, else `carriedAmount`.
    """

    targets: list[dict[str, Any]] = []
    for resolved in resolve_future(budget_store, active_category_ids, year_month):
        target: dict[str, Any] = {
            "yearMonth": year_month,
            "categoryId": resolved.key,
            "carriedAmount": resolved.value,
            "pinned": resolved.pinned,
        }
        if resolved.has_only:
            target["pinnedAmount"] = resolved.only
        targets.append(target)
    return targets


def walk_back_for(by_key: dict[tuple[str, str], Any], target: str, key: str) -> Any:
    """Return the value of the nearest row for `key` before `target`, or None.

    `by_key` is the full state, from `carry_map`.
    """

    cursor = prev_year_month(target)
    for _ in range(_MAX_WALKBACK_MONTHS):
        if (cursor, key) in by_key:
            return by_key[(cursor, key)]
        cursor = prev_year_month(cursor)
    return None


def value_at_or_before(by_key: dict[tuple[str, str], Any], year_month: str, key: str) -> Any:
    """Return the value at `(year_month, key)` if a row exists there. Else walk back from it."""

    if (year_month, key) in by_key:
        return by_key[(year_month, key)]
    return walk_back_for(by_key, year_month, key)


def effective_value(
    by_key: dict[tuple[str, str], Any], only_by_key: dict[tuple[str, str], Any], year_month: str, key: str
) -> Any:
    """Return what a month shows: its this-month-only value if it has one, else its carried value."""

    if (year_month, key) in only_by_key:
        return only_by_key[(year_month, key)]
    return value_at_or_before(by_key, year_month, key)


def _months_between_exclusive(start_ym: str, end_ym: str) -> list[str]:
    """Return the months after `start_ym`, up to and including `end_ym`."""

    result: list[str] = []
    cursor = next_year_month(start_ym)
    while cursor <= end_ym:
        result.append(cursor)
        cursor = next_year_month(cursor)
    return result
