"""Make months dense, and resolve a value by a walk back.

Storage model (see docs/personal-finance-architecture.md "Carry-Forward"):

- Past months, the current month, and the grace month are **dense**: one row per
  key that was active when the month became dense, plus any later explicit edits.
- Future months are **sparse**: only explicit pin rows.
- Effective values for future months are resolved at read time by walking back
  key-by-key through the rows that do exist, with explicit pins taking precedence.

The code here does not know which table it works on. A table takes part through the
`DenseSparseStore` protocol, where each row is `(yearMonth, key, value)`. For the Budget
table the key is a `categoryId` and the value is the amount.

`densify` is the entry point invoked by every handler that needs a dense
current month before serving a request. It is idempotent and cheap when the
table is already current.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

# A safety limit on how far the walk back goes. The architecture says that the walk back
# has no fixed limit, and that it continues until it finds a row or reaches the end of the
# history. But an empty table and a request for a future month would then loop for ever.
# 240 months, which is 20 years, is far more than any household budget history.
_MAX_WALKBACK_MONTHS = 240

# One stored row: (yearMonth, key, value).
CarryRow = tuple[str, str, Any]


class DenseSparseStore(Protocol):
    """A table whose rows obey the dense-past, sparse-future rules."""

    def scan_carry_rows(self) -> list[CarryRow]:
        """Return every stored row as `(yearMonth, key, value)`."""
        ...

    def put_carry_row(self, year_month: str, key: str, value: Any) -> None:
        """Write one row. The densify step calls this to fill a cell that is absent."""
        ...


@dataclass(frozen=True)
class ResolvedValue:
    """The effective value of one key in a future month."""

    key: str
    value: Any
    pinned: bool


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


def carry_map(rows: list[CarryRow]) -> dict[tuple[str, str], Any]:
    """Return the rows as a dict from `(yearMonth, key)` to the value."""
    return {(year_month, key): value for year_month, key, value in rows}


def densify(
    store: DenseSparseStore,
    active_keys: list[str],
    current_ym: str,
    cold_start_value: Any,
) -> int:
    """Move the table forward. Every month up to `current_ym` then becomes dense.

    Walks forward from `M_last + 1` through `current_ym` chronologically, writing
    a row per active key for each month. The value copies from the most recent
    prior row (per key), or is `None` if no prior row exists. Pin rows in the gap
    are preserved — densification fills missing cells only.

    Cold-start (table is empty): writes `cold_start_value` per active key at
    `current_ym` only. No prior months get materialized because there's no
    history to walk back to.

    Returns the number of rows written. Idempotent — re-runs after the table is
    current return 0 with no writes.
    """
    rows = store.scan_carry_rows()
    by_key = carry_map(rows)

    past_months = sorted({year_month for year_month, _, _ in rows if year_month <= current_ym})
    m_last = past_months[-1] if past_months else None

    if m_last is None:
        for key in active_keys:
            store.put_carry_row(current_ym, key, cold_start_value)
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
            store.put_carry_row(month, key, value)
            rows_written += 1

    return rows_written


def resolve_future(store: DenseSparseStore, active_keys: list[str], year_month: str) -> list[ResolvedValue]:
    """Return the effective value of each active key in a future month.

    For each active key:
    - If an explicit pin exists at `(year_month, key)`, use it with `pinned=True`.
    - Otherwise walk back month-by-month; the first row found is the effective
      value with `pinned=False`.
    - If no row is ever found, or the row found holds `None`, the key is omitted.

    Callers must ensure `year_month > current_ym` before calling.
    """
    by_key = carry_map(store.scan_carry_rows())
    result: list[ResolvedValue] = []
    for key in active_keys:
        if (year_month, key) in by_key:
            result.append(ResolvedValue(key=key, value=by_key[(year_month, key)], pinned=True))
            continue
        value = walk_back_for(by_key, year_month, key)
        if value is not None:
            result.append(ResolvedValue(key=key, value=value, pinned=False))
    return result


def resolve_future_targets(
    budget_store: DenseSparseStore,
    active_category_ids: list[str],
    year_month: str,
) -> list[dict[str, Any]]:
    """Return the budget targets of a future month, in the shape that the API sends.

    Each target is `{yearMonth, categoryId, amount, pinned}`. See `resolve_future`.
    """
    return [
        {"yearMonth": year_month, "categoryId": resolved.key, "amount": resolved.value, "pinned": resolved.pinned}
        for resolved in resolve_future(budget_store, active_category_ids, year_month)
    ]


def walk_back_for(by_key: dict[tuple[str, str], Any], target: str, key: str) -> Any:
    """Walk back from the month before `target`, and find the most recent row for `key`.

    The `by_key` dict must hold the full in-memory state to walk through (see
    `carry_map`). Returns the value of the nearest prior row, or `None` if no row
    exists within the safety bound.
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


def _months_between_exclusive(start_ym: str, end_ym: str) -> list[str]:
    """Return the months after `start_ym`, up to and including `end_ym`."""
    result: list[str] = []
    cursor = next_year_month(start_ym)
    while cursor <= end_ym:
        result.append(cursor)
        cursor = next_year_month(cursor)
    return result
