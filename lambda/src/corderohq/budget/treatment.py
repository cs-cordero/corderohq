from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from corderohq.common import parse_optional_str


class BudgetTreatment(StrEnum):
    """How the budget counts a transaction."""

    # A normal expense or refund. It needs a category, and it counts in the actual of that category.
    # A row with no budget treatment is a BUDGET row.
    BUDGET = "BUDGET"
    # Money that the budget must not count, such as work travel that an employer pays back. The
    # category is optional. The summary adds it to one separate Off-budget total.
    OFF_BUDGET = "OFF_BUDGET"
    # Money that is not an expense, such as a paycheck or a transfer. It has no category, and it
    # counts nowhere.
    SKIP = "SKIP"


@dataclass(frozen=True)
class Classification:
    """How the budget counts a transaction: its budget treatment, and its category if it has one."""

    budget_treatment: BudgetTreatment
    category_id: str | None


def budget_treatment_of(transaction: Mapping[str, Any]) -> BudgetTreatment:
    """Return the budget treatment of a stored transaction. An absent value means BUDGET."""

    value = transaction.get("budgetTreatment")
    return BudgetTreatment(value) if value else BudgetTreatment.BUDGET


def parse_classification(budget_treatment: Any, category_id: Any, require_category: bool = True) -> Classification:
    """Check a budget treatment and a category together, and return them. Raise ValueError if a rule fails.

    An absent or empty budget treatment means BUDGET. A BUDGET transaction needs a category, unless
    `require_category` is False. A SKIP transaction has no category, so a category that is given is
    dropped. An OFF_BUDGET transaction can have a category or not.
    """

    if budget_treatment is None or budget_treatment == "":
        parsed = BudgetTreatment.BUDGET
    else:
        try:
            parsed = BudgetTreatment(budget_treatment)
        except ValueError:
            raise ValueError(f"budgetTreatment must be one of {[d.value for d in BudgetTreatment]}") from None

    category = parse_optional_str(category_id, "categoryId")
    if parsed == BudgetTreatment.SKIP:
        category = None
    elif parsed == BudgetTreatment.BUDGET and category is None and require_category:
        raise ValueError("categoryId is required for a BUDGET transaction")
    return Classification(budget_treatment=parsed, category_id=category)
