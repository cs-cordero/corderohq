"""The Lambda that a schedule calls each month, to make the month dense in the Budget table and in the IncomePlan table.

Invoked by an EventBridge Scheduler at midnight ET on the 1st of every month.
Runs the same `densify` routine that the API handlers use for lazy hydration,
so the cron and the on-demand path share one source of truth. See
docs/personal-finance-architecture.md "Densification" for the eager + lazy split.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from corderohq.aws.dynamodb import (
    BudgetCarryStore,
    BudgetTable,
    CategoryTable,
    IncomePlanCarryStore,
    IncomePlanTable,
    IncomeSourceTable,
    active_category_ids,
    active_income_source_ids,
    current_year_month,
)
from corderohq.budget.densify import densify
from corderohq.util import get_env_var

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

_CATEGORY_TABLE = CategoryTable(get_env_var("CATEGORY_TABLE_NAME"))
_BUDGET_TABLE = BudgetTable(get_env_var("BUDGET_TABLE_NAME"))
_INCOME_SOURCE_TABLE = IncomeSourceTable(get_env_var("INCOME_SOURCE_TABLE_NAME"))
_INCOME_PLAN_TABLE = IncomePlanTable(get_env_var("INCOME_PLAN_TABLE_NAME"))


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Make the current year-month dense.

    Logs a single structured JSON line so "last successful densification" is
    queryable from CloudWatch Insights:
        fields @timestamp, @message
        | filter event = "scheduled_densify_done"
        | sort @timestamp desc
        | limit 20
    """
    current_ym = current_year_month()
    LOGGER.info(json.dumps({"event": "scheduled_densify_start", "currentYearMonth": current_ym}))
    try:
        rows_written = densify(
            BudgetCarryStore(_BUDGET_TABLE),
            active_category_ids(_CATEGORY_TABLE.list_all()),
            current_ym,
            cold_start_value=0,
        )
        rows_written += densify(
            IncomePlanCarryStore(_INCOME_PLAN_TABLE),
            active_income_source_ids(_INCOME_SOURCE_TABLE.list_all()),
            current_ym,
            cold_start_value=None,
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
