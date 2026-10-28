import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CATEGORY_GROUP_TABLE_NAME", "test-CategoryGroup")
os.environ.setdefault("CATEGORY_TABLE_NAME", "test-Category")
os.environ.setdefault("BUDGET_TABLE_NAME", "test-Budget")
os.environ.setdefault("BUDGET_AUDIT_LOG_TABLE_NAME", "test-BudgetAuditLog")
os.environ.setdefault("TRANSACTIONS_TABLE_NAME", "test-Transactions")
os.environ.setdefault("INCOME_SOURCE_TABLE_NAME", "test-IncomeSource")
os.environ.setdefault("PLANNED_INCOME_TABLE_NAME", "test-PlannedIncome")
os.environ.setdefault("PROFILE_TABLE_NAME", "test-Profile")
os.environ.setdefault("ACCOUNT_TABLE_NAME", "test-Account")
os.environ.setdefault("NETWORTH_SNAPSHOT_TABLE_NAME", "test-NetWorthSnapshot")
os.environ.setdefault("SSM_API_KEY_PARAM", "/budget/anthropic-api-key")
os.environ.setdefault("STAGE", "test")

from corderohq.aws.dynamodb import (
    IncomeSourceTable,
    PlannedIncomeTable,
    RecurringIncomeDensifyAdapter,
    compute_income_plan,
)
from corderohq.budget.densify import densify
from corderohq.budget.income import (
    ClearPin,
    CreateIncomeSourceArgs,
    CreateOneTimeIncomeArgs,
    Deduction,
    DeductionKind,
    DeleteOneTimeIncomeArgs,
    OneTimeIncome,
    PayFrequency,
    RecurringIncome,
    SetPin,
    SetRecurringIncome,
    StopRecurringIncome,
    UnpinRecurringIncome,
    UpdateIncomeSourceArgs,
    UpdateOneTimeIncomeArgs,
    parse_create_income_source_args,
    parse_one_time_income,
    parse_one_time_income_args,
    parse_recurring_income,
    parse_recurring_income_changes,
    parse_update_income_source_args,
    validate_label,
)
from corderohq.common import Keep, SetTo
from corderohq.personal_finance_handler import handler

# The same frozen "now" as test_personal_finance_handler: 2026-06 is the current month,
# 2026-07 to 2027-06 are editable future months, and 2026-05 and earlier are locked.
_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
_CURRENT = "2026-06"
_NEXT = "2026-07"
_FUTURE = "2026-09"
_LOCKED = "2026-03"
_ADMIN_GROUP = "budget-admin-test"

_SALARY_BODY: dict[str, Any] = {
    "incomeSourceId": "s1",
    "label": "Salary",
    "frequency": "biweekly",
    "gross": 5_000_000_000,
    "deductions": [
        {"label": "Federal tax", "kind": "TAX", "amount": 600_000_000},
        {"label": "401k", "kind": "PRETAX_SAVINGS", "amount": 400_000_000},
    ],
}
_SALARY = RecurringIncome(
    income_source_id="s1",
    label="Salary",
    frequency=PayFrequency.BIWEEKLY,
    gross=5_000_000_000,
    deductions=(
        Deduction(label="Federal tax", kind=DeductionKind.TAX, amount=600_000_000),
        Deduction(label="401k", kind=DeductionKind.PRETAX_SAVINGS, amount=400_000_000),
    ),
)
_NET = 4_000_000_000
_RAISED_BODY: dict[str, Any] = {**_SALARY_BODY, "gross": 6_000_000_000}


# --- The income rules ---


class TestParseRecurringIncome:
    def test_returns_a_recurring_income(self) -> None:
        assert parse_recurring_income(_SALARY_BODY) == _SALARY

    def test_to_item_returns_the_stored_shape(self) -> None:
        assert _SALARY.to_item() == _SALARY_BODY

    def test_labels_are_trimmed(self) -> None:
        raw = {**_SALARY_BODY, "label": " Salary ", "deductions": [{"label": "  Tax  ", "kind": "TAX", "amount": 1}]}
        income = parse_recurring_income(raw)
        assert (income.label, income.deductions[0].label) == ("Salary", "Tax")

    def test_a_stored_income_equals_the_same_income_from_a_request(self) -> None:
        stored = {**_SALARY_BODY, "gross": Decimal("5000000000")}
        assert parse_recurring_income(stored) == _SALARY
        assert parse_recurring_income(stored) != parse_recurring_income(_RAISED_BODY)

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"incomeSourceId": None}, "incomeSourceId is required"),
            ({"label": ""}, "label is required"),
            ({"frequency": "daily"}, "frequency must be one of"),
            ({"gross": -1}, "gross must not be negative"),
            ({"gross": 1.5}, "gross must be an integer"),
            ({"gross": True}, "gross must be an integer"),
            ({"deductions": "x"}, "deductions must be a list"),
            ({"deductions": [{"label": "x", "kind": "OTHER", "amount": 1}]}, "kind must be one of"),
            ({"deductions": [{"label": "", "kind": "TAX", "amount": 1}]}, "label is required"),
            ({"deductions": [{"label": "x", "kind": "TAX", "amount": -1}]}, "amount must not be negative"),
            ({"gross": 100}, "the deductions must not be more than the gross amount"),
        ],
    )
    def test_rejects_a_broken_income(self, change: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            parse_recurring_income({**_SALARY_BODY, **change})

    def test_rejects_a_value_that_is_not_an_object(self) -> None:
        with pytest.raises(ValueError, match="recurringIncome must be an object"):
            parse_recurring_income(None)

    def test_label_has_a_maximum_length(self) -> None:
        with pytest.raises(ValueError, match="at most"):
            validate_label("x" * 101)


class TestNetAmounts:
    def test_net_and_monthly_net(self) -> None:
        assert _SALARY.net == _NET
        assert _SALARY.monthly_net == 2 * _NET

    @pytest.mark.parametrize(
        ("frequency", "count"),
        [
            (PayFrequency.WEEKLY, 4),
            (PayFrequency.BIWEEKLY, 2),
            (PayFrequency.SEMIMONTHLY, 2),
            (PayFrequency.MONTHLY, 1),
        ],
    )
    def test_the_frequency_sets_the_times_per_month(self, frequency: PayFrequency, count: int) -> None:
        assert frequency.times_per_month == count

    def test_a_one_time_income_with_no_deductions_nets_its_gross_amount(self) -> None:
        assert OneTimeIncome(label="Gift", income_source_id=None, gross=7, deductions=()).net == 7


class TestParseOneTimeIncome:
    def test_the_income_source_is_optional(self) -> None:
        income = parse_one_time_income({"label": "Tax refund", "gross": 900, "deductions": []})
        assert income == OneTimeIncome(label="Tax refund", income_source_id=None, gross=900, deductions=())

    def test_a_one_time_income_can_name_its_source_and_deductions(self) -> None:
        raw = {
            "label": "Q3 RSU vest",
            "incomeSourceId": "s1",
            "gross": 10_000,
            "deductions": [{"label": "Withholding", "kind": "TAX", "amount": 2_200}],
        }
        income = parse_one_time_income(raw)
        assert (income.income_source_id, income.net) == ("s1", 7_800)
        assert income.to_item() == raw


class TestParseIncomeSourceArgs:
    def test_create(self) -> None:
        assert parse_create_income_source_args({"owners": ["p1", "p1"], "label": " A "}) == CreateIncomeSourceArgs(
            owners=["p1"], label="A"
        )

    def test_update_keeps_each_absent_key(self) -> None:
        assert parse_update_income_source_args({"label": "B"}) == UpdateIncomeSourceArgs(
            label=SetTo("B"), owners=Keep()
        )

    def test_update_needs_a_key(self) -> None:
        with pytest.raises(ValueError, match="at least one editable field is required"):
            parse_update_income_source_args({"frequency": "weekly"})


class TestParseRecurringIncomeChanges:
    def test_each_action(self) -> None:
        body = {
            "recurring": [
                {"action": "SET", "recurringIncome": _SALARY_BODY},
                {"action": "SET", "plannedIncomeId": "REC#a", "recurringIncome": _SALARY_BODY},
                {"action": "STOP", "plannedIncomeId": "REC#b"},
                {"action": "UNPIN", "plannedIncomeId": "REC#c"},
            ]
        }
        assert parse_recurring_income_changes(body) == [
            SetRecurringIncome(planned_income_id=None, income=_SALARY),
            SetRecurringIncome(planned_income_id="REC#a", income=_SALARY),
            StopRecurringIncome(planned_income_id="REC#b"),
            UnpinRecurringIncome(planned_income_id="REC#c"),
        ]

    @pytest.mark.parametrize(
        ("entry", "message"),
        [
            ({"action": "STOP"}, "STOP needs plannedIncomeId"),
            ({"action": "UNPIN"}, "UNPIN needs plannedIncomeId"),
            ({"action": "DROP", "plannedIncomeId": "REC#a"}, "action must be one of"),
            ({"action": "STOP", "plannedIncomeId": "ONE#a"}, "plannedIncomeId must be the id of a recurring income"),
            ({"action": "SET"}, "recurringIncome must be an object"),
        ],
    )
    def test_rejects_a_broken_entry_and_names_its_index(self, entry: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=rf"recurring\[0\]: {message}"):
            parse_recurring_income_changes({"recurring": [entry]})

    def test_recurring_is_required(self) -> None:
        with pytest.raises(ValueError, match="recurring array is required"):
            parse_recurring_income_changes({})


class TestParseOneTimeIncomeArgs:
    def test_each_body_shape(self) -> None:
        income = OneTimeIncome(label="Bonus", income_source_id=None, gross=5, deductions=())
        body = {"label": "Bonus", "gross": 5}
        assert parse_one_time_income_args(body) == CreateOneTimeIncomeArgs(income=income)
        assert parse_one_time_income_args({**body, "plannedIncomeId": "ONE#x"}) == UpdateOneTimeIncomeArgs(
            planned_income_id="ONE#x", income=income
        )
        assert parse_one_time_income_args({"plannedIncomeId": "ONE#x", "delete": True}) == DeleteOneTimeIncomeArgs(
            planned_income_id="ONE#x"
        )

    def test_a_recurring_income_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="plannedIncomeId of a one-time income is required"):
            parse_one_time_income_args({"plannedIncomeId": "REC#a", "delete": True})


# --- The tables ---


def _plan_table(items: list[dict[str, Any]]) -> tuple[PlannedIncomeTable, MagicMock]:
    table = PlannedIncomeTable.__new__(PlannedIncomeTable)
    mock = MagicMock()
    table._table = mock
    mock.scan.return_value = {"Items": items}
    mock.query.side_effect = lambda **kwargs: {
        "Items": [item for item in items if item["yearMonth"] == kwargs["ExpressionAttributeValues"][":ym"]]
    }
    return table, mock


def _source_table(sources: list[dict[str, Any]]) -> IncomeSourceTable:
    table = IncomeSourceTable.__new__(IncomeSourceTable)
    mock = MagicMock()
    table._table = mock
    mock.scan.return_value = {"Items": sources}
    return table


def _recurring_item(year_month: str, planned_income_id: str, body: dict[str, Any] = _SALARY_BODY) -> dict[str, Any]:
    return {"yearMonth": year_month, "plannedIncomeId": planned_income_id, "kind": "RECURRING", **body}


def _stop_item(year_month: str, planned_income_id: str) -> dict[str, Any]:
    return {"yearMonth": year_month, "plannedIncomeId": planned_income_id, "kind": "RECURRING", "stopped": True}


def _one_time_item(year_month: str, planned_income_id: str, gross: int) -> dict[str, Any]:
    return {
        "yearMonth": year_month,
        "plannedIncomeId": planned_income_id,
        "kind": "ONE_TIME",
        "label": "Bonus",
        "gross": gross,
        "deductions": [],
    }


def _source(source_id: str, active: bool = True, sort_order: int = 0) -> dict[str, Any]:
    return {
        "incomeSourceId": source_id,
        "owners": ["p1"],
        "label": f"Source {source_id}",
        "active": active,
        "sortOrder": sort_order,
    }


class TestRecurringIncomeDensifyAdapter:
    def test_reads_recurring_incomes_and_stops_and_no_one_time_income(self) -> None:
        items = [
            _recurring_item("2026-05", "REC#a"),
            _stop_item("2026-05", "REC#b"),
            _one_time_item("2026-05", "ONE#x", 9),
        ]
        table, _ = _plan_table(items)
        adapter = RecurringIncomeDensifyAdapter(table)
        assert adapter.scan_stored_values() == [("2026-05", "REC#a", _SALARY), ("2026-05", "REC#b", None)]
        assert adapter.recurring_income_ids() == ["REC#a", "REC#b"]

    def test_densify_copies_each_recurring_income_and_never_a_one_time_income(self) -> None:
        items = [
            _recurring_item("2026-04", "REC#a"),
            _recurring_item("2026-04", "REC#b", {**_SALARY_BODY, "label": "Property 2 rent"}),
            _one_time_item("2026-04", "ONE#x", 9),
        ]
        table, mock = _plan_table(items)
        adapter = RecurringIncomeDensifyAdapter(table)
        assert densify(adapter, adapter.recurring_income_ids(), "2026-06", cold_start_value=None) == 4
        written = [
            (c.kwargs["Item"]["yearMonth"], c.kwargs["Item"]["plannedIncomeId"]) for c in mock.put_item.call_args_list
        ]
        assert sorted(written) == [
            ("2026-05", "REC#a"),
            ("2026-05", "REC#b"),
            ("2026-06", "REC#a"),
            ("2026-06", "REC#b"),
        ]

    def test_densify_after_a_stop_writes_nothing(self) -> None:
        table, mock = _plan_table([_recurring_item("2026-03", "REC#a"), _stop_item("2026-04", "REC#a")])
        adapter = RecurringIncomeDensifyAdapter(table)
        densify(adapter, adapter.recurring_income_ids(), "2026-06", cold_start_value=None)
        mock.put_item.assert_not_called()


class TestPlannedIncomeTable:
    def test_a_stop_is_stored_with_no_value(self) -> None:
        table, mock = _plan_table([])
        table.put_recurring_income(_CURRENT, "REC#a", None)
        mock.put_item.assert_called_once_with(Item=_stop_item(_CURRENT, "REC#a"))

    def test_update_of_a_one_time_income_with_no_source_removes_the_source(self) -> None:
        table, mock = _plan_table([])
        mock.update_item.return_value = {"Attributes": {}}
        table.update_one_time_income(
            _CURRENT, "ONE#x", OneTimeIncome(label="Bonus", income_source_id=None, gross=5, deductions=())
        )
        assert "REMOVE #incomeSourceId" in mock.update_item.call_args.kwargs["UpdateExpression"]


class TestIncomeSourceTable:
    def test_list_all_sorts_by_order_then_label(self) -> None:
        table = _source_table([_source("b", sort_order=1), _source("c", sort_order=0), _source("a", sort_order=1)])
        assert [s["incomeSourceId"] for s in table.get_all_income_sources()] == ["c", "a", "b"]

    def test_create_goes_to_the_end(self) -> None:
        table = _source_table([_source("a", sort_order=4)])
        item = table.create_income_source(CreateIncomeSourceArgs(owners=["p1"], label="Job A"))
        assert (item["sortOrder"], item["active"]) == (5, True)


# --- compute_income_plan ---


class TestComputeIncomePlan:
    def test_dense_month_counts_each_recurring_and_one_time_income(self) -> None:
        items = [_recurring_item(_CURRENT, "REC#a"), _one_time_item(_CURRENT, "ONE#b", 1_000_000)]
        plan, _ = _plan_table(items)

        result = compute_income_plan(plan, _source_table([_source("s1")]), _CURRENT, _NOW)

        assert result["state"] == "EDITABLE"
        [entry] = result["recurring"]
        assert (entry["plannedIncomeId"], entry["label"], entry["sourceLabel"]) == ("REC#a", "Salary", "Source s1")
        assert (entry["net"], entry["monthlyNet"], entry["stopped"]) == (_NET, 2 * _NET, False)
        assert "pinned" not in entry
        assert [line["net"] for line in result["oneTime"]] == [1_000_000]
        assert result["plannedNetIncome"] == 2 * _NET + 1_000_000

    def test_one_source_can_have_many_recurring_incomes(self) -> None:
        rent = {**_SALARY_BODY, "label": "Property 2 rent", "frequency": "monthly", "deductions": []}
        plan, _ = _plan_table([_recurring_item(_CURRENT, "REC#a"), _recurring_item(_CURRENT, "REC#b", rent)])

        result = compute_income_plan(plan, _source_table([_source("s1")]), _CURRENT, _NOW)

        assert [entry["label"] for entry in result["recurring"]] == ["Salary", "Property 2 rent"]
        assert result["plannedNetIncome"] == 2 * _NET + 5_000_000_000

    def test_a_weekly_income_counts_four_times(self) -> None:
        plan, _ = _plan_table([_recurring_item(_CURRENT, "REC#a", {**_SALARY_BODY, "frequency": "weekly"})])
        result = compute_income_plan(plan, _source_table([_source("s1")]), _CURRENT, _NOW)
        assert result["plannedNetIncome"] == 4 * _NET

    def test_a_stop_shows_only_in_its_month_and_does_not_count(self) -> None:
        items = [_recurring_item("2026-04", "REC#a"), _stop_item("2026-05", "REC#a")]
        plan, _ = _plan_table(items)
        sources = _source_table([_source("s1")])

        stop_month = compute_income_plan(plan, sources, "2026-05", _NOW)
        [entry] = stop_month["recurring"]
        assert (entry["stopped"], entry["before"]) == (True, _SALARY_BODY)
        assert stop_month["plannedNetIncome"] == 0
        assert compute_income_plan(plan, sources, _FUTURE, _NOW)["recurring"] == []

    def test_future_month_uses_pins_and_the_walk_back(self) -> None:
        items = [
            _recurring_item(_CURRENT, "REC#a"),
            _recurring_item(_CURRENT, "REC#b"),
            _recurring_item(_FUTURE, "REC#b", _RAISED_BODY),
        ]
        plan, _ = _plan_table(items)

        result = compute_income_plan(plan, _source_table([_source("s1")]), _FUTURE, _NOW)

        by_id = {entry["plannedIncomeId"]: entry for entry in result["recurring"]}
        assert (by_id["REC#a"]["pinned"], by_id["REC#b"]["pinned"]) == (False, True)
        assert by_id["REC#b"]["gross"] == 6_000_000_000
        assert result["plannedNetIncome"] == 2 * _NET + 2 * (_NET + 1_000_000_000)


# --- The routes ---


@pytest.fixture(autouse=True)
def _freeze_now() -> Iterator[MagicMock]:
    with patch("corderohq.personal_finance_handler._now_utc", return_value=_NOW) as p:
        yield p


@pytest.fixture(autouse=True)
def _skip_hydration() -> Iterator[MagicMock]:
    with patch("corderohq.personal_finance_handler._ensure_hydrated") as p:
        yield p


@pytest.fixture
def tables() -> Iterator[dict[str, MagicMock]]:
    """Replace the tables of the handler, `compute_income_plan`, and the new id, with mocks."""

    with (
        patch("corderohq.personal_finance_handler._INCOME_SOURCE_TABLE") as source_table,
        patch("corderohq.personal_finance_handler._PLANNED_INCOME_TABLE") as plan_table,
        patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE") as audit_table,
        patch("corderohq.personal_finance_handler._PROFILE_TABLE") as profile_table,
        patch(
            "corderohq.personal_finance_handler.compute_income_plan", return_value={"plannedNetIncome": 0}
        ) as compute,
        patch("corderohq.personal_finance_handler.new_recurring_income_id", return_value="REC#new"),
    ):
        source_table.get_income_source_by_id.return_value = _source("s1")
        profile_table.get_profile.return_value = {"personId": "p1"}
        plan_table.scan_recurring_incomes.return_value = []
        yield {
            "source": source_table,
            "plan": plan_table,
            "audit": audit_table,
            "profile": profile_table,
            "compute": compute,
        }


def _event(method: str, path: str, body: dict[str, Any] | None = None, admin: bool = False) -> dict[str, Any]:
    claims: dict[str, object] = {"email": "test@example.com"}
    if admin:
        claims["cognito:groups"] = [_ADMIN_GROUP]
    event: dict[str, Any] = {
        "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": claims}}},
        "rawPath": path,
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def _audit_actions(audit: MagicMock) -> list[str]:
    return [call.kwargs["action"] for call in audit.write_entry.call_args_list]


def _set(planned_income_id: str | None, body: dict[str, Any] = _SALARY_BODY) -> dict[str, Any]:
    entry: dict[str, Any] = {"action": "SET", "recurringIncome": body}
    if planned_income_id is not None:
        entry["plannedIncomeId"] = planned_income_id
    return entry


class TestIncomeSourceRoutes:
    def test_create_holds_no_money_and_writes_no_audit_entry(self, tables: dict[str, MagicMock]) -> None:
        tables["source"].create_income_source.return_value = _source("s1")
        result = handler(_event("POST", "/api/income-sources", {"owners": ["p1"], "label": "Job A"}), None)

        assert result["statusCode"] == 201
        tables["source"].create_income_source.assert_called_once_with(CreateIncomeSourceArgs(["p1"], "Job A"))
        tables["plan"].put_recurring_income.assert_not_called()
        tables["audit"].write_entry.assert_not_called()

    def test_create_needs_a_known_person(self, tables: dict[str, MagicMock]) -> None:
        tables["profile"].get_profile.return_value = None
        result = handler(_event("POST", "/api/income-sources", {"owners": ["nobody"], "label": "A"}), None)
        assert result["statusCode"] == 400
        tables["source"].create_income_source.assert_not_called()

    def test_list_hides_inactive_sources_by_default(self, tables: dict[str, MagicMock]) -> None:
        tables["source"].get_all_income_sources.return_value = [_source("s1"), _source("s2", active=False)]
        result = handler(_event("GET", "/api/income-sources"), None)
        assert [s["incomeSourceId"] for s in json.loads(result["body"])] == ["s1"]

    def test_change_of_owners_checks_each_person(self, tables: dict[str, MagicMock]) -> None:
        tables["profile"].get_profile.return_value = None
        result = handler(_event("PUT", "/api/income-sources/s1", {"owners": ["nobody"]}), None)
        assert result["statusCode"] == 400
        tables["source"].update_income_source.assert_not_called()

    def test_rename_has_no_audit_entry(self, tables: dict[str, MagicMock]) -> None:
        tables["source"].update_income_source.return_value = _source("s1")
        result = handler(_event("PUT", "/api/income-sources/s1", {"label": "Job B"}), None)
        assert result["statusCode"] == 200
        tables["source"].update_income_source.assert_called_once_with(
            "s1", UpdateIncomeSourceArgs(label=SetTo("Job B"), owners=Keep())
        )
        tables["audit"].write_entry.assert_not_called()


class TestDeactivateIncomeSource:
    def test_a_source_with_no_recurring_income_needs_no_explanation(self, tables: dict[str, MagicMock]) -> None:
        tables["source"].set_active.return_value = _source("s1", active=False)
        result = handler(_event("POST", "/api/income-sources/s1/deactivate", {}), None)
        assert result["statusCode"] == 200
        tables["audit"].write_entry.assert_not_called()

    def test_stops_each_recurring_income_in_the_next_month(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].scan_recurring_incomes.return_value = [_recurring_item(_CURRENT, "REC#a")]
        tables["source"].set_active.return_value = _source("s1", active=False)

        assert handler(_event("POST", "/api/income-sources/s1/deactivate", {}), None)["statusCode"] == 400
        result = handler(_event("POST", "/api/income-sources/s1/deactivate", {"explanation": "job ended"}), None)

        assert result["statusCode"] == 200
        tables["plan"].put_recurring_income.assert_called_once_with(_NEXT, "REC#a", None)
        assert _audit_actions(tables["audit"]) == ["RECURRING_INCOME_STOP"]
        tables["source"].set_active.assert_called_once_with("s1", False)

    def test_future_pins_need_a_confirmation(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].scan_recurring_incomes.return_value = [
            _recurring_item(_CURRENT, "REC#a"),
            _recurring_item(_FUTURE, "REC#a", _RAISED_BODY),
        ]
        tables["source"].set_active.return_value = _source("s1", active=False)
        body = {"explanation": "job ended"}
        result = handler(_event("POST", "/api/income-sources/s1/deactivate", body), None)
        assert result["statusCode"] == 409
        assert json.loads(result["body"])["futurePins"] == [{"yearMonth": _FUTURE, "plannedIncomeId": "REC#a"}]

        confirmed = handler(_event("POST", "/api/income-sources/s1/deactivate", {**body, "confirm": True}), None)
        assert confirmed["statusCode"] == 200
        tables["plan"].delete_recurring_income.assert_called_once_with(_FUTURE, "REC#a")
        assert _audit_actions(tables["audit"]) == ["RECURRING_INCOME_UNPIN", "RECURRING_INCOME_STOP"]


class TestIncomeReplace:
    def test_set_with_no_id_creates_a_recurring_income(self, tables: dict[str, MagicMock]) -> None:
        body = {"recurring": [_set(None)], "explanation": "new job"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/replace", body), None)
        assert result["statusCode"] == 200
        tables["plan"].put_recurring_income.assert_called_once_with(_CURRENT, "REC#new", _SALARY, pinned=None)
        audit = tables["audit"].write_entry.call_args.kwargs
        assert (audit["action"], audit["planned_income_id"], audit["income_source_id"]) == (
            "RECURRING_INCOME_CREATE",
            "REC#new",
            "s1",
        )

    def test_writes_a_changed_value_with_an_update_entry(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].scan_recurring_incomes.return_value = [_recurring_item(_CURRENT, "REC#a")]
        body = {"recurring": [_set("REC#a", _RAISED_BODY)], "explanation": "raise"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/replace", body), None)
        assert result["statusCode"] == 200
        tables["plan"].put_recurring_income.assert_called_once_with(
            _CURRENT, "REC#a", parse_recurring_income(_RAISED_BODY), pinned=None
        )
        assert _audit_actions(tables["audit"]) == ["RECURRING_INCOME_UPDATE"]

    def test_same_value_is_a_no_op(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].scan_recurring_incomes.return_value = [_recurring_item(_CURRENT, "REC#a")]
        body = {"recurring": [_set("REC#a")], "explanation": "same"}
        assert handler(_event("POST", f"/api/income/{_CURRENT}/replace", body), None)["statusCode"] == 200
        tables["plan"].put_recurring_income.assert_not_called()
        tables["audit"].write_entry.assert_not_called()

    def test_stop_writes_a_stop(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].scan_recurring_incomes.return_value = [_recurring_item(_CURRENT, "REC#a")]
        body = {"recurring": [{"action": "STOP", "plannedIncomeId": "REC#a"}], "explanation": "sold the property"}
        assert handler(_event("POST", f"/api/income/{_CURRENT}/replace", body), None)["statusCode"] == 200
        tables["plan"].put_recurring_income.assert_called_once_with(_CURRENT, "REC#a", None, pinned=None)
        assert _audit_actions(tables["audit"]) == ["RECURRING_INCOME_STOP"]

    def test_body_must_hold_every_recurring_income_with_a_value(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].scan_recurring_incomes.return_value = [
            _recurring_item(_CURRENT, "REC#a"),
            _recurring_item(_CURRENT, "REC#b"),
            _stop_item(_CURRENT, "REC#c"),
        ]
        body = {"recurring": [_set("REC#a")], "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/replace", body), None)
        assert result["statusCode"] == 409
        assert json.loads(result["body"])["missingPlannedIncomeIds"] == ["REC#b"]

    def test_unknown_id_or_source_returns_400(self, tables: dict[str, MagicMock]) -> None:
        body = {"recurring": [_set("REC#nope")], "explanation": "x"}
        assert handler(_event("POST", f"/api/income/{_CURRENT}/replace", body), None)["statusCode"] == 400
        tables["source"].get_income_source_by_id.return_value = None
        body = {"recurring": [_set(None)], "explanation": "x"}
        assert handler(_event("POST", f"/api/income/{_CURRENT}/replace", body), None)["statusCode"] == 400

    def test_unpin_returns_400(self, tables: dict[str, MagicMock]) -> None:
        body = {"recurring": [{"action": "UNPIN", "plannedIncomeId": "REC#a"}], "explanation": "x"}
        assert handler(_event("POST", f"/api/income/{_CURRENT}/replace", body), None)["statusCode"] == 400

    def test_future_month_returns_409(self, tables: dict[str, MagicMock]) -> None:
        body = {"recurring": [], "explanation": "x"}
        assert handler(_event("POST", f"/api/income/{_FUTURE}/replace", body), None)["statusCode"] == 409

    def test_locked_month_needs_the_override(self, tables: dict[str, MagicMock]) -> None:
        body = {"recurring": [_set(None)], "explanation": "x"}
        assert handler(_event("POST", f"/api/income/{_LOCKED}/replace", body), None)["statusCode"] == 409

        overridden = handler(_event("POST", f"/api/income/{_LOCKED}/replace", {**body, "override": True}, True), None)
        assert overridden["statusCode"] == 200
        assert tables["audit"].write_entry.call_args.kwargs["override"] is True


class TestIncomePin:
    def test_pin_writes_and_warns_when_it_matches_the_carried_value(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].scan_recurring_incomes.return_value = [_recurring_item(_CURRENT, "REC#a")]
        body = {"recurring": [_set("REC#a")], "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_FUTURE}/pin", body), None)
        assert result["statusCode"] == 200
        tables["plan"].put_recurring_income.assert_called_once_with(_FUTURE, "REC#a", _SALARY, pinned=None)
        assert _audit_actions(tables["audit"]) == ["RECURRING_INCOME_PIN"]
        assert json.loads(result["body"])["warnings"]["pinMatchesCarriedValue"] == ["REC#a"]

    def test_set_with_no_id_creates_a_recurring_income_from_that_month(self, tables: dict[str, MagicMock]) -> None:
        body = {"recurring": [_set(None)], "explanation": "new tenant"}
        assert handler(_event("POST", f"/api/income/{_FUTURE}/pin", body), None)["statusCode"] == 200
        tables["plan"].put_recurring_income.assert_called_once_with(_FUTURE, "REC#new", _SALARY, pinned=None)
        assert _audit_actions(tables["audit"]) == ["RECURRING_INCOME_CREATE"]

    def test_pin_lists_downstream_pins(self, tables: dict[str, MagicMock]) -> None:
        later = "2026-12"
        tables["plan"].scan_recurring_incomes.return_value = [
            _recurring_item(_CURRENT, "REC#a"),
            _recurring_item(later, "REC#a"),
        ]
        body = {"recurring": [_set("REC#a", _RAISED_BODY)], "explanation": "x"}
        warnings = json.loads(handler(_event("POST", f"/api/income/{_FUTURE}/pin", body), None)["body"])["warnings"]
        assert warnings["downstreamPins"] == [{"plannedIncomeId": "REC#a", "yearMonth": later}]
        assert warnings["pinMatchesCarriedValue"] == []

    def test_stop_and_unpin(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].scan_recurring_incomes.return_value = [
            _recurring_item(_CURRENT, "REC#a"),
            _stop_item(_FUTURE, "REC#b"),
            _recurring_item(_CURRENT, "REC#b"),
        ]
        body = {
            "recurring": [
                {"action": "STOP", "plannedIncomeId": "REC#a"},
                {"action": "UNPIN", "plannedIncomeId": "REC#b"},
            ],
            "explanation": "x",
        }
        assert handler(_event("POST", f"/api/income/{_FUTURE}/pin", body), None)["statusCode"] == 200
        tables["plan"].put_recurring_income.assert_called_once_with(_FUTURE, "REC#a", None, pinned=None)
        tables["plan"].delete_recurring_income.assert_called_once_with(_FUTURE, "REC#b")
        assert _audit_actions(tables["audit"]) == ["RECURRING_INCOME_STOP", "RECURRING_INCOME_UNPIN"]

    def test_current_month_returns_409(self, tables: dict[str, MagicMock]) -> None:
        body = {"recurring": [], "explanation": "x"}
        assert handler(_event("POST", f"/api/income/{_CURRENT}/pin", body), None)["statusCode"] == 409


_RSU_BODY: dict[str, Any] = {
    "label": "Q3 RSU vest",
    "incomeSourceId": "s1",
    "gross": 10_000,
    "deductions": [{"label": "Withholding", "kind": "TAX", "amount": 2_200}],
}


class TestOneTimeIncomes:
    def test_create_in_a_future_month(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].create_one_time_income.return_value = _one_time_item(_FUTURE, "ONE#x", 10_000)
        result = handler(_event("POST", f"/api/income/{_FUTURE}/one-time", {**_RSU_BODY, "explanation": "vest"}), None)
        assert result["statusCode"] == 201
        tables["plan"].create_one_time_income.assert_called_once_with(_FUTURE, parse_one_time_income(_RSU_BODY))
        audit = tables["audit"].write_entry.call_args.kwargs
        assert (audit["action"], audit["planned_income_id"], audit["income_source_id"]) == (
            "ONE_TIME_INCOME_CREATE",
            "ONE#x",
            "s1",
        )
        assert audit["changes"] == {"oneTimeIncome": {"before": None, "after": _RSU_BODY}}

    def test_update_writes_the_new_value(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].get_planned_income.return_value = _one_time_item(_CURRENT, "ONE#x", 1_000)
        tables["plan"].update_one_time_income.return_value = _one_time_item(_CURRENT, "ONE#x", 10_000)
        body = {**_RSU_BODY, "plannedIncomeId": "ONE#x", "explanation": "bigger"}
        assert handler(_event("POST", f"/api/income/{_CURRENT}/one-time", body), None)["statusCode"] == 200
        tables["plan"].update_one_time_income.assert_called_once_with(
            _CURRENT, "ONE#x", parse_one_time_income(_RSU_BODY)
        )
        assert _audit_actions(tables["audit"]) == ["ONE_TIME_INCOME_UPDATE"]

    def test_update_and_delete_need_an_existing_income(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].get_planned_income.return_value = None
        body = {"plannedIncomeId": "ONE#x", "delete": True, "explanation": "x"}
        assert handler(_event("POST", f"/api/income/{_CURRENT}/one-time", body), None)["statusCode"] == 404

    def test_delete_keeps_the_value_in_the_audit_entry(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].get_planned_income.return_value = _one_time_item(_CURRENT, "ONE#x", 1_000)
        body = {"plannedIncomeId": "ONE#x", "delete": True, "explanation": "mistake"}
        assert handler(_event("POST", f"/api/income/{_CURRENT}/one-time", body), None)["statusCode"] == 200
        tables["plan"].delete_one_time_income.assert_called_once_with(_CURRENT, "ONE#x")
        audit = tables["audit"].write_entry.call_args.kwargs
        assert audit["action"] == "ONE_TIME_INCOME_DELETE"
        assert audit["changes"]["oneTimeIncome"]["before"] == {"label": "Bonus", "gross": 1_000, "deductions": []}

    def test_an_unknown_source_returns_400(self, tables: dict[str, MagicMock]) -> None:
        tables["source"].get_income_source_by_id.return_value = None
        body = {**_RSU_BODY, "explanation": "x"}
        assert handler(_event("POST", f"/api/income/{_CURRENT}/one-time", body), None)["statusCode"] == 400
        tables["plan"].create_one_time_income.assert_not_called()

    def test_locked_month_returns_409(self, tables: dict[str, MagicMock]) -> None:
        body = {**_RSU_BODY, "explanation": "x"}
        assert handler(_event("POST", f"/api/income/{_LOCKED}/one-time", body), None)["statusCode"] == 409
        tables["plan"].create_one_time_income.assert_not_called()


class TestGetIncome:
    def test_returns_compute_income_plan(self, tables: dict[str, MagicMock]) -> None:
        result = handler(_event("GET", f"/api/income/{_CURRENT}"), None)
        assert result["statusCode"] == 200
        assert json.loads(result["body"]) == {"plannedNetIncome": 0}
        tables["compute"].assert_called_once()

    def test_bad_month_returns_400(self, tables: dict[str, MagicMock]) -> None:
        assert handler(_event("GET", "/api/income/2026-6"), None)["statusCode"] == 400


# A short payment for one month, such as during unpaid leave.
_SHORT_BODY: dict[str, Any] = {**_SALARY_BODY, "gross": 2_000_000_000, "deductions": []}


def _with_only(item: dict[str, Any], body: dict[str, Any] = _SHORT_BODY) -> dict[str, Any]:
    return {**item, "pinnedRecurringIncome": body}


def _only_item(year_month: str, planned_income_id: str) -> dict[str, Any]:
    return {
        "yearMonth": year_month,
        "plannedIncomeId": planned_income_id,
        "kind": "RECURRING",
        "pinnedRecurringIncome": _SHORT_BODY,
    }


class TestPinnedRecurringIncome:
    """A pinned recurring income changes one month only. It never carries forward."""

    def test_parse_the_two_actions(self) -> None:
        body = {
            "recurring": [
                {"action": "SET_PIN", "plannedIncomeId": "REC#a", "recurringIncome": _SHORT_BODY},
                {"action": "CLEAR_PIN", "plannedIncomeId": "REC#a"},
            ]
        }
        assert parse_recurring_income_changes(body) == [
            SetPin(planned_income_id="REC#a", income=parse_recurring_income(_SHORT_BODY)),
            ClearPin(planned_income_id="REC#a"),
        ]
        with pytest.raises(ValueError, match="SET_PIN needs plannedIncomeId"):
            parse_recurring_income_changes({"recurring": [{"action": "SET_PIN", "recurringIncome": _SHORT_BODY}]})

    def test_densify_keeps_the_pinned_value(self) -> None:
        table, mock = _plan_table([_recurring_item("2026-04", "REC#a"), _only_item("2026-05", "REC#a")])
        adapter = RecurringIncomeDensifyAdapter(table)
        densify(adapter, adapter.recurring_income_ids(), "2026-05", cold_start_value=None)
        mock.put_item.assert_called_once_with(Item=_with_only(_recurring_item("2026-05", "REC#a")))

    def test_dense_month_shows_the_pinned_value(self) -> None:
        plan, _ = _plan_table([_with_only(_recurring_item(_CURRENT, "REC#a"))])

        result = compute_income_plan(plan, _source_table([_source("s1")]), _CURRENT, _NOW)

        [entry] = result["recurring"]
        assert (entry["gross"], entry["hasPinnedRecurringIncome"]) == (2_000_000_000, True)
        assert entry["carriedRecurringIncome"] == _SALARY_BODY
        assert result["plannedNetIncome"] == 2 * 2_000_000_000

    def test_future_month_uses_it_and_the_next_month_does_not(self) -> None:
        plan, _ = _plan_table([_recurring_item(_CURRENT, "REC#a"), _only_item(_FUTURE, "REC#a")])
        sources = _source_table([_source("s1")])

        [entry] = compute_income_plan(plan, sources, _FUTURE, _NOW)["recurring"]
        assert (entry["gross"], entry["hasPinnedRecurringIncome"], entry["pinned"]) == (2_000_000_000, True, False)

        [later] = compute_income_plan(plan, sources, "2026-10", _NOW)["recurring"]
        assert (later["gross"], later["hasPinnedRecurringIncome"]) == (_SALARY.gross, False)

    def test_replace_sets_a_pinned_value_and_keeps_the_carried_value(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].scan_recurring_incomes.return_value = [_recurring_item(_CURRENT, "REC#a")]
        body = {
            "recurring": [
                _set("REC#a"),
                {"action": "SET_PIN", "plannedIncomeId": "REC#a", "recurringIncome": _SHORT_BODY},
            ],
            "explanation": "unpaid leave",
        }
        assert handler(_event("POST", f"/api/income/{_CURRENT}/replace", body), None)["statusCode"] == 200
        tables["plan"].put_recurring_income.assert_called_once_with(
            _CURRENT, "REC#a", _SALARY, pinned=parse_recurring_income(_SHORT_BODY)
        )
        audit = tables["audit"].write_entry.call_args.kwargs
        assert (audit["action"], list(audit["changes"])) == ("RECURRING_INCOME_UPDATE", ["pinnedRecurringIncome"])

    def test_pin_with_no_carried_value(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].scan_recurring_incomes.return_value = [_recurring_item(_CURRENT, "REC#a")]
        body = {
            "recurring": [{"action": "SET_PIN", "plannedIncomeId": "REC#a", "recurringIncome": _SHORT_BODY}],
            "explanation": "short month",
        }
        result = handler(_event("POST", f"/api/income/{_FUTURE}/pin", body), None)
        assert result["statusCode"] == 200
        tables["plan"].put_pinned_recurring_income.assert_called_once_with(
            _FUTURE, "REC#a", parse_recurring_income(_SHORT_BODY)
        )
        audit = tables["audit"].write_entry.call_args.kwargs
        assert (audit["action"], list(audit["changes"])) == ("RECURRING_INCOME_PIN", ["pinnedRecurringIncome"])
        assert json.loads(result["body"])["warnings"]["downstreamPins"] == []

    def test_clear_of_the_pin_deletes_an_item_with_nothing_else(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].scan_recurring_incomes.return_value = [
            _recurring_item(_CURRENT, "REC#a"),
            _only_item(_FUTURE, "REC#a"),
        ]
        body = {"recurring": [{"action": "CLEAR_PIN", "plannedIncomeId": "REC#a"}], "explanation": "undo"}
        assert handler(_event("POST", f"/api/income/{_FUTURE}/pin", body), None)["statusCode"] == 200
        tables["plan"].delete_recurring_income.assert_called_once_with(_FUTURE, "REC#a")
        assert _audit_actions(tables["audit"]) == ["RECURRING_INCOME_UNPIN"]

    def test_deactivation_deletes_a_later_pinned_value(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].scan_recurring_incomes.return_value = [
            _recurring_item(_CURRENT, "REC#a"),
            _only_item(_FUTURE, "REC#a"),
        ]
        tables["source"].set_active.return_value = _source("s1", active=False)
        body = {"explanation": "job ended", "confirm": True}
        assert handler(_event("POST", "/api/income-sources/s1/deactivate", body), None)["statusCode"] == 200
        tables["plan"].delete_recurring_income.assert_called_once_with(_FUTURE, "REC#a")
