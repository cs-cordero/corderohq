from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from corderohq.common import parse_optional_str


class BudgetTreatment(StrEnum):
    """Determines how a transaction is treated when rolling up against the budget.

    In general, this is determined in an automated manner and largely relies on the CashFlowRole of the source and
    target accounts involved in a transaction. However, when transactions are saved, users are able to override and
    give it a different treatment.
    """

    # A normal, default transaction that is counted towards the actuals on a budget. All BUDGET_IMPACTING
    # transactions require a budget Category be set.
    BUDGET_IMPACTING = "BUDGET_IMPACTING"
    # A transaction that is not counted towards the budget for whatever reason. The transaction is still saved and
    # recorded, but the summaries for actual expenditures avoid using it. An example of an OFF_BUDGET transaction
    # might be a transaction that was incurred as a work expense and so the household expects to be reimbursed for
    # that amount. Another example might be the transaction that happens when the household sends payment to a
    # credit card account from a checking account. At that point, the expenditures are already counted once the
    # transactions were made on the credit card. Treating this as a BUDGET_IMPACTING transaction would only double
    # count the money. OFF_BUDGET transactions do not require a budget Category to be set.
    OFF_BUDGET = "OFF_BUDGET"
    # Indicates a transaction that should effectively be ignored and unused.
    SKIP = "SKIP"


@dataclass(frozen=True)
class Classification:
    """How the budget counts a transaction: its budget treatment, and its category if it has one."""

    treatment: BudgetTreatment
    category_id: str | None


def parse_classification(budget_treatment: Any, category_id: Any, require_category: bool = True) -> Classification:
    """Check a budget treatment and a category together, and return them. Raise ValueError if a rule fails.

    An absent or empty budget treatment means BUDGET_IMPACTING. A BUDGET_IMPACTING transaction needs a category,
    unless `require_category` is False. A SKIP transaction has no category, so a category that is given is dropped.
    An OFF_BUDGET transaction can have a category or not.
    """

    if budget_treatment is None or budget_treatment == "":
        parsed = BudgetTreatment.BUDGET_IMPACTING
    else:
        try:
            parsed = BudgetTreatment(budget_treatment)
        except ValueError:
            raise ValueError(f"budgetTreatment must be one of {[d.value for d in BudgetTreatment]}") from None

    category = parse_optional_str(category_id, "categoryId")
    if parsed == BudgetTreatment.SKIP:
        category = None
    elif parsed == BudgetTreatment.BUDGET_IMPACTING and category is None and require_category:
        raise ValueError("categoryId is required for a BUDGET_IMPACTING transaction")
    return Classification(treatment=parsed, category_id=category)
