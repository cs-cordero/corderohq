"""The IncomePlan table: the paycheck rows and one-time income lines of each month."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.income_source import IncomeSourceTable, active_income_source_ids
from corderohq.aws.dynamodb.util import BUDGET_TZ, DYNAMO_RESOURCE, query_pages, scan_pages
from corderohq.budget.densify import CarryRow, carry_map, walk_back_for
from corderohq.budget.editability import editability
from corderohq.budget.income import ONE_TIME_LINE_PREFIX, PAYCHECK_LINE_PREFIX, monthly_net, net_pay, paycheck_line_id


def _paycheck_of(row: dict[str, Any]) -> dict[str, Any]:
    """Return the paycheck value of a stored paycheck row."""
    return {
        "paychecksPerMonth": row["paychecksPerMonth"],
        "gross": row["gross"],
        "deductions": row.get("deductions", []),
    }


class IncomePlanTable:
    """The income plan of each month: paycheck rows and one-time income lines.

    PK `yearMonth`, SK `lineId`. A paycheck row has `lineId = "SRC#{incomeSourceId}"`. It obeys
    the dense-past, sparse-future rules of a Budget row (see `IncomePlanCarryStore`). A one-time
    line has `lineId = "ONE#{ULID}"`. It belongs to its month only.
    """

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def get_month(self, year_month: str) -> list[dict[str, Any]]:
        """Return every row of one month: paycheck rows and one-time lines."""
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {
            "KeyConditionExpression": "yearMonth = :ym",
            "ExpressionAttributeValues": {":ym": year_month},
        }
        for response in query_pages(self._table, **params):
            items.extend(response.get("Items", []))
        return items

    def scan_paychecks(self) -> list[dict[str, Any]]:
        """Return every paycheck row in the table. The one-time lines are left out."""
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {
            "FilterExpression": "begins_with(lineId, :prefix)",
            "ExpressionAttributeValues": {":prefix": PAYCHECK_LINE_PREFIX},
        }
        for response in scan_pages(self._table, **params):
            items.extend(response.get("Items", []))
        return [item for item in items if str(item["lineId"]).startswith(PAYCHECK_LINE_PREFIX)]

    def put_paycheck(self, year_month: str, income_source_id: str, paycheck: dict[str, Any]) -> dict[str, Any]:
        """Write the paycheck row of one income source in one month. The caller checks the paycheck."""
        item: dict[str, Any] = {
            "yearMonth": year_month,
            "lineId": paycheck_line_id(income_source_id),
            "lineType": "PAYCHECK",
            "incomeSourceId": income_source_id,
            **paycheck,
        }
        self._table.put_item(Item=item)
        return item

    def delete_paycheck(self, year_month: str, income_source_id: str) -> None:
        self._table.delete_item(Key={"yearMonth": year_month, "lineId": paycheck_line_id(income_source_id)})

    def find_future_months_with_source(self, income_source_id: str, after_month: str) -> list[str]:
        """Return the months after `after_month` that hold a paycheck row (a pin) for this source."""
        return sorted(
            {
                str(row["yearMonth"])
                for row in self.scan_paychecks()
                if row["incomeSourceId"] == income_source_id and str(row["yearMonth"]) > after_month
            }
        )

    def get_line(self, year_month: str, line_id: str) -> dict[str, Any] | None:
        response = self._table.get_item(Key={"yearMonth": year_month, "lineId": line_id})
        return response.get("Item")

    def put_one_time(self, year_month: str, label: str, amount: int, line_id: str | None = None) -> dict[str, Any]:
        """Create a one-time income line, or replace it when `line_id` is given. The caller checks the values."""
        now = datetime.now(tz=UTC).isoformat()
        existing = self.get_line(year_month, line_id) if line_id else None
        item: dict[str, Any] = {
            "yearMonth": year_month,
            "lineId": line_id or f"{ONE_TIME_LINE_PREFIX}{ULID()}",
            "lineType": "ONE_TIME",
            "label": label,
            "amount": amount,
            "createdAt": existing["createdAt"] if existing else now,
            "updatedAt": now,
        }
        self._table.put_item(Item=item)
        return item

    def delete_line(self, year_month: str, line_id: str) -> None:
        self._table.delete_item(Key={"yearMonth": year_month, "lineId": line_id})


class IncomePlanCarryStore:
    """The paycheck rows of the IncomePlan table as a `DenseSparseStore` for `budget/densify.py`.

    The key of a row is its `incomeSourceId`, and the value is its paycheck. The one-time lines
    are not part of it, so they never carry forward. A source with no earlier row gets no row:
    `put_carry_row` writes nothing for a `None` value.
    """

    def __init__(self, income_plan_table: IncomePlanTable) -> None:
        self._income_plan_table = income_plan_table

    def scan_carry_rows(self) -> list[CarryRow]:
        return [
            (str(row["yearMonth"]), str(row["incomeSourceId"]), _paycheck_of(row))
            for row in self._income_plan_table.scan_paychecks()
        ]

    def put_carry_row(self, year_month: str, key: str, value: Any) -> None:
        if value is None:
            return
        self._income_plan_table.put_paycheck(year_month, key, value)


def compute_income(
    income_plan: IncomePlanTable,
    income_sources: IncomeSourceTable,
    year_month: str,
    now: datetime,
) -> dict[str, Any]:
    """Return the income plan of one month, with the net pay of each paycheck and the planned net income.

    Membership, the same as the categories of the summary:
    - **A future month** — each active income source, with a pin or a value from the walk back.
      A source with no history is left out.
    - **The current month and the grace month** — each active income source with a paycheck row.
    - **A locked month** — each income source with a paycheck row in that month, active or not.

    The one-time lines of the month always count. Nothing here reads a transaction.
    """
    sources_by_id = {s["incomeSourceId"]: s for s in income_sources.list_all()}
    state = editability(year_month, now)
    now_et = now.astimezone(BUDGET_TZ)
    current_ym = f"{now_et.year:04d}-{now_et.month:02d}"
    is_future = year_month > current_ym

    month_rows = income_plan.get_month(year_month)
    paycheck_by_source: dict[str, dict[str, Any]] = {}
    pinned_ids: set[str] = set()
    if is_future:
        by_key = carry_map(IncomePlanCarryStore(income_plan).scan_carry_rows())
        for source_id in active_income_source_ids(list(sources_by_id.values())):
            if (year_month, source_id) in by_key:
                paycheck_by_source[source_id] = by_key[(year_month, source_id)]
                pinned_ids.add(source_id)
                continue
            carried = walk_back_for(by_key, year_month, source_id)
            if carried is not None:
                paycheck_by_source[source_id] = carried
    else:
        for row in month_rows:
            if not str(row["lineId"]).startswith(PAYCHECK_LINE_PREFIX):
                continue
            source = sources_by_id.get(row["incomeSourceId"])
            if state == "LOCKED" or (source is not None and source.get("active", False)):
                paycheck_by_source[str(row["incomeSourceId"])] = _paycheck_of(row)

    paychecks: list[dict[str, Any]] = []
    for source_id, source in sources_by_id.items():
        if source_id not in paycheck_by_source:
            continue
        paycheck = paycheck_by_source[source_id]
        entry: dict[str, Any] = {
            "incomeSourceId": source_id,
            "label": source["label"],
            "personId": source["personId"],
            "active": bool(source.get("active", False)),
            **paycheck,
            "net": net_pay(paycheck),
            "monthlyNet": monthly_net(paycheck),
        }
        if is_future:
            entry["pinned"] = source_id in pinned_ids
        paychecks.append(entry)

    one_time = sorted(
        (
            {"lineId": row["lineId"], "label": row["label"], "amount": int(row["amount"])}
            for row in month_rows
            if str(row["lineId"]).startswith(ONE_TIME_LINE_PREFIX)
        ),
        key=lambda line: str(line["lineId"]),
    )

    planned = sum(p["monthlyNet"] for p in paychecks) + sum(line["amount"] for line in one_time)
    return {
        "yearMonth": year_month,
        "state": state,
        "paychecks": paychecks,
        "oneTime": one_time,
        "plannedNetIncome": planned,
    }
