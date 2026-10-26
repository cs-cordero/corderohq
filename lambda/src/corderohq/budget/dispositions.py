from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from corderohq.common import parse_optional_str


class Disposition(StrEnum):
    """How the budget counts a transaction."""

    # A normal expense or refund. It needs a category, and it counts in the actual of that category.
    # A row with no disposition is a BUDGET row.
    BUDGET = "BUDGET"
    # Money that the budget must not count, such as work travel that an employer pays back. The
    # category is optional. The summary adds it to one separate Off-budget total.
    OFF_BUDGET = "OFF_BUDGET"
    # Money that is not an expense, such as a paycheck or a transfer. It has no category, and it
    # counts nowhere.
    SKIP = "SKIP"


@dataclass(frozen=True)
class Classification:
    """How the budget counts a transaction: its disposition, and its category if it has one."""

    disposition: Disposition
    category_id: str | None


def disposition_of(transaction: Mapping[str, Any]) -> Disposition:
    """Return the disposition of a stored transaction. An absent value means BUDGET."""

    value = transaction.get("disposition")
    return Disposition(value) if value else Disposition.BUDGET


def parse_classification(disposition: Any, category_id: Any, require_category: bool = True) -> Classification:
    """Check a disposition and a category together, and return them. Raise ValueError if a rule fails.

    An absent or empty disposition means BUDGET. A BUDGET transaction needs a category, unless
    `require_category` is False. A SKIP transaction has no category, so a category that is given is
    dropped. An OFF_BUDGET transaction can have a category or not.
    """

    if disposition is None or disposition == "":
        parsed = Disposition.BUDGET
    else:
        try:
            parsed = Disposition(disposition)
        except ValueError:
            raise ValueError(f"disposition must be one of {[d.value for d in Disposition]}") from None

    category = parse_optional_str(category_id, "categoryId")
    if parsed == Disposition.SKIP:
        category = None
    elif parsed == Disposition.BUDGET and category is None and require_category:
        raise ValueError("categoryId is required for a BUDGET transaction")
    return Classification(disposition=parsed, category_id=category)
