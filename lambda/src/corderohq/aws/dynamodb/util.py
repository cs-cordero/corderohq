from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import boto3

DYNAMO_RESOURCE = boto3.resource("dynamodb")
BUDGET_TZ = ZoneInfo("America/New_York")


def current_year_month() -> str:
    """Return the current YYYY-MM string."""

    now = datetime.now(tz=UTC)
    return f"{now.year}-{now.month:02d}"


def scan_pages(table: Any, **params: Any) -> Iterator[dict[str, Any]]:
    """Yield each page of a Scan. The next page starts at the `LastEvaluatedKey` of the page before."""

    while True:
        response = table.scan(**params)
        yield response
        if "LastEvaluatedKey" not in response:
            return
        params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
