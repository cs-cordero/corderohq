from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from corderohq.budget.dispositions import DISPOSITION_ATTRIBUTES, Disposition, resolve_disposition_fields
from corderohq.common import FieldChange, Keep, Remove, SetTo


@dataclass(frozen=True)
class UpdateTransactionArgs:
    """The checked changes to a transaction. Each field that the request did not name is Keep()."""

    description: Keep | SetTo[str]
    amount: Keep | SetTo[int]
    disposition: Keep | SetTo[Disposition]
    category_id: FieldChange[str]
    account_id: FieldChange[str]
    note: FieldChange[str]

    def changes_nothing(self) -> bool:
        """Return True if every field is Keep()."""

        return all(isinstance(getattr(self, field.name), Keep) for field in fields(self))


def parse_update_transaction_args(payload: Mapping[str, Any], existing: Mapping[str, Any]) -> UpdateTransactionArgs:
    """Convert the body of an update request into checked changes. Raise ValueError for bad input.

    `existing` is the stored transaction. A change to any disposition attribute is checked together
    with the stored values of the others. Then each disposition attribute is set to its new value,
    or removed when it no longer applies. The caller checks that the account exists.
    """

    description: Keep | SetTo[str] = Keep()
    if "description" in payload:
        value = payload["description"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("description must not be empty")
        description = SetTo(value.strip())

    amount: Keep | SetTo[int] = Keep()
    if "amount" in payload:
        value = payload["amount"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("amount must be an integer (millionths of a dollar)")
        amount = SetTo(value)

    if not any(name in payload for name in DISPOSITION_ATTRIBUTES):
        return UpdateTransactionArgs(description, amount, Keep(), Keep(), Keep(), Keep())

    resolved = resolve_disposition_fields(payload, base=existing)
    return UpdateTransactionArgs(
        description=description,
        amount=amount,
        disposition=SetTo(resolved.disposition),
        category_id=_set_or_remove(resolved.category_id),
        account_id=_set_or_remove(resolved.account_id),
        note=_set_or_remove(resolved.note),
    )


def _set_or_remove(value: str | None) -> FieldChange[str]:
    """Return SetTo(value), or Remove() for None."""

    return Remove() if value is None else SetTo(value)
