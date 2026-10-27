from __future__ import annotations

import json
import logging
from typing import Any

from corderohq.aws.dynamodb import (
    BudgetDensifyAdapter,
    BudgetTable,
    CategoryTable,
    PlannedIncomeTable,
    RecurringIncomeDensifyAdapter,
    current_year_month,
)
from corderohq.budget.densify import densify
from corderohq.util import get_env_var

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

_CATEGORY_TABLE = CategoryTable(get_env_var("CATEGORY_TABLE_NAME"))
_BUDGET_TABLE = BudgetTable(get_env_var("BUDGET_TABLE_NAME"))
_PLANNED_INCOME_TABLE = PlannedIncomeTable(get_env_var("PLANNED_INCOME_TABLE_NAME"))


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Make the current year-month dense.

    An EventBridge schedule calls this at midnight ET on the 1st of each month. It logs one JSON line with `event =
    "scheduled_densify_done"`, for CloudWatch Insights.
    """

    current_ym = current_year_month()
    LOGGER.info(json.dumps({"event": "scheduled_densify_start", "currentYearMonth": current_ym}))
    try:
        rows_written = densify(
            BudgetDensifyAdapter(_BUDGET_TABLE),
            [c["categoryId"] for c in _CATEGORY_TABLE.get_active_categories()],
            current_ym,
            cold_start_value=0,
        )
        recurring_incomes = RecurringIncomeDensifyAdapter(_PLANNED_INCOME_TABLE)
        rows_written += densify(
            recurring_incomes, recurring_incomes.recurring_income_ids(), current_ym, cold_start_value=None
        )
    except Exception:
        LOGGER.exception(json.dumps({"event": "scheduled_densify_failed", "currentYearMonth": current_ym}))
        raise
    LOGGER.info(
        json.dumps(
            {
                "event": "scheduled_densify_done",
                "currentYearMonth": current_ym,
                "rowsWritten": rows_written,
            }
        )
    )
    return {"currentYearMonth": current_ym, "rowsWritten": rows_written}
