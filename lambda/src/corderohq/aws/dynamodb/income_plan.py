from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from corderohq.aws.dynamodb.util import DYNAMO_RESOURCE, query_all_items, scan_all_items
from corderohq.budget.densify import DenseSparseStore, StoredValue
from corderohq.budget.income import ONE_TIME_LINE_PREFIX, PAYMENT_LINE_PREFIX, IncomeLineType, payment_line_id


def get_payment(row: dict[str, Any]) -> dict[str, Any]:
    """Return the payment of a stored payment row."""

    return {
        "paymentsPerMonth": row["paymentsPerMonth"],
        "gross": row["gross"],
        "deductions": row.get("deductions", []),
    }


class IncomePlanTable:
    """The income plan of each month: payment rows and one-time income lines.

    PK `yearMonth`, SK `lineId`. A payment row has `lineId = "SRC#{incomeSourceId}"`. It obeys
    the dense-past, sparse-future rules of a Budget row (see `IncomePlanDensifyAdapter`). Its
    payment is in `paymentsPerMonth`, `gross`, and `deductions`, and later months copy it. A one-time
    line has `lineId = "ONE#{ULID}"`. It belongs to its month only.
    """

    def __init__(self, table_name: str) -> None:
        self._table = DYNAMO_RESOURCE.Table(table_name)

    def get_income_plan_for_month(self, year_month: str) -> list[dict[str, Any]]:
        """Return every row of one month: payment rows and one-time lines."""

        return query_all_items(
            self._table, KeyConditionExpression="yearMonth = :ym", ExpressionAttributeValues={":ym": year_month}
        )

    def scan_payments(self) -> list[dict[str, Any]]:
        """Return every payment row in the table. The one-time lines are left out."""

        items = scan_all_items(
            self._table,
            FilterExpression="begins_with(lineId, :prefix)",
            ExpressionAttributeValues={":prefix": PAYMENT_LINE_PREFIX},
        )
        return [item for item in items if str(item["lineId"]).startswith(PAYMENT_LINE_PREFIX)]

    def put_payment(self, year_month: str, income_source_id: str, payment: dict[str, Any]) -> dict[str, Any]:
        """Write the payment row of one income source in one month."""

        item: dict[str, Any] = {
            "yearMonth": year_month,
            "lineId": payment_line_id(income_source_id),
            "lineType": IncomeLineType.PAYMENT,
            "incomeSourceId": income_source_id,
            **payment,
        }
        self._table.put_item(Item=item)
        return item

    def delete_payment(self, year_month: str, income_source_id: str) -> None:
        self._table.delete_item(Key={"yearMonth": year_month, "lineId": payment_line_id(income_source_id)})

    def find_future_months_with_source(self, income_source_id: str, after_month: str) -> list[str]:
        """Return the months after `after_month` that hold a payment row (a pin) for this source."""

        return sorted(
            {
                str(row["yearMonth"])
                for row in self.scan_payments()
                if row["incomeSourceId"] == income_source_id and str(row["yearMonth"]) > after_month
            }
        )

    def get_line(self, year_month: str, line_id: str) -> dict[str, Any] | None:
        response = self._table.get_item(Key={"yearMonth": year_month, "lineId": line_id})
        return response.get("Item")

    def put_one_time(self, year_month: str, label: str, amount: int, line_id: str | None = None) -> dict[str, Any]:
        """Create a one-time income line, or replace it when `line_id` is given."""

        now = datetime.now(tz=UTC).isoformat()
        existing = self.get_line(year_month, line_id) if line_id else None
        item: dict[str, Any] = {
            "yearMonth": year_month,
            "lineId": line_id or f"{ONE_TIME_LINE_PREFIX}{ULID()}",
            "lineType": IncomeLineType.ONE_TIME,
            "label": label,
            "amount": amount,
            "createdAt": existing["createdAt"] if existing else now,
            "updatedAt": now,
        }
        self._table.put_item(Item=item)
        return item

    def delete_line(self, year_month: str, line_id: str) -> None:
        self._table.delete_item(Key={"yearMonth": year_month, "lineId": line_id})


class IncomePlanDensifyAdapter(DenseSparseStore):
    """Implements the DenseSparseStore protocol for the payment rows of the IncomePlanTable class.

    The key is the `incomeSourceId`, and the value is the payment. The one-time lines are not part of
    it, so they never carry forward. A None value writes nothing, so a source with no earlier row gets
    no row.
    """

    def __init__(self, income_plan_table: IncomePlanTable) -> None:
        self._income_plan_table = income_plan_table

    def scan_stored_values(self) -> list[StoredValue]:
        """Return each payment row as `(yearMonth, incomeSourceId, payment)`."""

        return [
            (str(row["yearMonth"]), str(row["incomeSourceId"]), get_payment(row))
            for row in self._income_plan_table.scan_payments()
        ]

    def put_stored_value(self, year_month: str, key: str, value: Any) -> None:
        """Write the payment row of one income source in one month."""

        if value is None:
            return
        self._income_plan_table.put_payment(year_month, key, value)
