from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, assert_never
from zoneinfo import ZoneInfo

import boto3

from corderohq.field_changes import FieldChange, Keep, Remove, SetTo

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


def query_pages(table: Any, **params: Any) -> Iterator[dict[str, Any]]:
    """Yield each page of a Query. The next page starts at the `LastEvaluatedKey` of the page before."""

    while True:
        response = table.query(**params)
        yield response
        if "LastEvaluatedKey" not in response:
            return
        params["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def scan_all_items(table: Any, **params: Any) -> list[dict[str, Any]]:
    """Return the items of every page of a Scan."""

    return [item for page in scan_pages(table, **params) for item in page.get("Items", [])]


def query_all_items(table: Any, **params: Any) -> list[dict[str, Any]]:
    """Return the items of every page of a Query."""

    return [item for page in query_pages(table, **params) for item in page.get("Items", [])]


def update_expression(
    changes: Mapping[str, FieldChange[Any]], always_set: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Return the UpdateExpression of an update_item call, with its attribute names and values.

    Each SetTo change sets its attribute, each Remove change removes it, and each Keep change does
    nothing. The attributes in `always_set` are set too. Raise ValueError if nothing changes.
    """

    set_parts = [f"{attribute} = :{attribute}" for attribute in always_set or {}]
    values: dict[str, Any] = {f":{attribute}": value for attribute, value in (always_set or {}).items()}
    remove_parts: list[str] = []
    names: dict[str, str] = {}
    for attribute, change in changes.items():
        match change:
            case Keep():
                continue
            case SetTo(value=value):
                # The name goes through ExpressionAttributeNames, so a reserved word such as `name` is safe.
                names[f"#{attribute}"] = attribute
                values[f":{attribute}"] = value
                set_parts.append(f"#{attribute} = :{attribute}")
            case Remove():
                names[f"#{attribute}"] = attribute
                remove_parts.append(f"#{attribute}")
            case _:
                assert_never(change)

    clauses = []
    if set_parts:
        clauses.append("SET " + ", ".join(set_parts))
    if remove_parts:
        clauses.append("REMOVE " + ", ".join(remove_parts))
    if not clauses:
        raise ValueError("the update changes nothing")
    params: dict[str, Any] = {"UpdateExpression": " ".join(clauses)}
    if values:
        params["ExpressionAttributeValues"] = values
    if names:
        params["ExpressionAttributeNames"] = names
    return params
