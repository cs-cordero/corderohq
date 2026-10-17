"""The disposition of a transaction: how the budget counts it.

- BUDGET — a normal expense or refund. It needs a category, and it counts in the actual of that
  category. An old row has no disposition, and it is a BUDGET row.
- OFF_BUDGET — money that the budget must not count, such as work travel that an employer pays
  back. The category is optional. The summary adds it to one separate Off-budget total.
- SKIP — money that is not an expense, such as a paycheck or a transfer. It has no category, and
  it counts nowhere.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DISPOSITION_BUDGET = "BUDGET"
DISPOSITION_OFF_BUDGET = "OFF_BUDGET"
DISPOSITION_SKIP = "SKIP"
DISPOSITIONS = frozenset({DISPOSITION_BUDGET, DISPOSITION_OFF_BUDGET, DISPOSITION_SKIP})

NOTE_MAX_LENGTH = 200

# The stored attributes that the disposition rules control together.
DISPOSITION_ATTRIBUTES = ("disposition", "categoryId", "accountId", "note")


def disposition_of(txn: Mapping[str, Any]) -> str:
    """Return the disposition of a transaction. An absent value means BUDGET."""
    value = txn.get("disposition")
    return str(value) if value else DISPOSITION_BUDGET


@dataclass(frozen=True)
class DispositionFields:
    """The checked values of the attributes in `DISPOSITION_ATTRIBUTES`. `None` means absent."""

    disposition: str
    category_id: str | None
    account_id: str | None
    note: str | None

    def stored_values(self) -> dict[str, str]:
        """Return the attributes to write. An absent value is left out."""
        values: dict[str, str] = {"disposition": self.disposition}
        if self.category_id is not None:
            values["categoryId"] = self.category_id
        if self.account_id is not None:
            values["accountId"] = self.account_id
        if self.note is not None:
            values["note"] = self.note
        return values

    def absent_attributes(self) -> list[str]:
        """Return the attributes that must not exist on the row."""
        return [name for name in DISPOSITION_ATTRIBUTES if name not in self.stored_values()]


def _optional_str(value: Any, name: str) -> str | None:
    """Return the value without outer white space. `None` and an empty string give `None`."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    stripped = value.strip()
    return stripped or None


def resolve_disposition_fields(
    changes: Mapping[str, Any],
    base: Mapping[str, Any] | None = None,
    require_category: bool = True,
) -> DispositionFields:
    """Merge `changes` over `base`, and check the result against the disposition rules.

    `base` is the stored row for an update, or `None` for a new row. A key in `changes` wins over
    the same key in `base`, even when its value is `None`. Raise `ValueError` if a rule fails.

    With `require_category=False`, a BUDGET row with no category passes. The CSV commit uses this,
    because it reports a missing category as a row issue, and not as an error.
    """
    stored = base or {}

    def pick(key: str) -> Any:
        return changes[key] if key in changes else stored.get(key)

    raw_disposition = pick("disposition")
    if raw_disposition is None or raw_disposition == "":
        disposition = DISPOSITION_BUDGET
    elif isinstance(raw_disposition, str) and raw_disposition in DISPOSITIONS:
        disposition = raw_disposition
    else:
        raise ValueError(f"disposition must be one of {sorted(DISPOSITIONS)}")

    category_id = _optional_str(pick("categoryId"), "categoryId")
    if disposition == DISPOSITION_SKIP:
        # A SKIP row counts nowhere, so a category on it means nothing. Drop it.
        category_id = None
    elif disposition == DISPOSITION_BUDGET and category_id is None and require_category:
        raise ValueError("categoryId is required for a BUDGET transaction")

    account_id = _optional_str(pick("accountId"), "accountId")

    note = _optional_str(pick("note"), "note")
    if note is not None and len(note) > NOTE_MAX_LENGTH:
        raise ValueError(f"note must be at most {NOTE_MAX_LENGTH} characters")

    return DispositionFields(disposition=disposition, category_id=category_id, account_id=account_id, note=note)
