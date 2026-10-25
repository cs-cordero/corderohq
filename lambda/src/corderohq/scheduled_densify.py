from __future__ import annotations

import json
import logging
from typing import Any

from corderohq.aws.dynamodb import (
    BudgetCarryStore,
    BudgetTable,
    CategoryTable,
    active_category_ids,
    current_year_month,
)
from corderohq.budget.densify import densify
from corderohq.util import get_env_var

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

_CATEGORY_TABLE = CategoryTable(get_env_var("CATEGORY_TABLE_NAME"))
_BUDGET_TABLE = BudgetTable(get_env_var("BUDGET_TABLE_NAME"))


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Make the current year-month dense.

    An EventBridge schedule calls this at midnight ET on the 1st of each month. It logs one JSON line with `event =
    "scheduled_densify_done"`, for CloudWatch Insights.
    """

    current_ym = current_year_month()
    LOGGER.info(json.dumps({"event": "scheduled_densify_start", "currentYearMonth": current_ym}))
    try:
        rows_written = densify(
            BudgetCarryStore(_BUDGET_TABLE),
            active_category_ids(_CATEGORY_TABLE.get_all_categories()),
            current_ym,
            cold_start_value=0,
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
