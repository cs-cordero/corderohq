"""The income plan of one month: each payment, the one-time lines, and the planned net income."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from corderohq.aws.dynamodb.income_plan import IncomePlanCarryStore, IncomePlanTable, _paycheck_of
from corderohq.aws.dynamodb.income_source import IncomeSourceTable, active_income_source_ids
from corderohq.aws.dynamodb.util import BUDGET_TZ
from corderohq.budget.densify import carry_map, walk_back_for
from corderohq.budget.editability import editability
from corderohq.budget.income import ONE_TIME_LINE_PREFIX, PAYCHECK_LINE_PREFIX, monthly_net, net_pay


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
