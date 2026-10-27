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
os.environ.setdefault("INCOME_PLAN_TABLE_NAME", "test-IncomePlan")
os.environ.setdefault("PROFILE_TABLE_NAME", "test-Profile")
os.environ.setdefault("ACCOUNT_TABLE_NAME", "test-Account")
os.environ.setdefault("NETWORTH_SNAPSHOT_TABLE_NAME", "test-NetWorthSnapshot")
os.environ.setdefault("SSM_API_KEY_PARAM", "/budget/anthropic-api-key")
os.environ.setdefault("STAGE", "test")

from corderohq.aws.dynamodb import IncomePlanDensifyAdapter, IncomePlanTable, IncomeSourceTable, compute_income
from corderohq.budget.densify import densify
from corderohq.budget.income import (
    CreateIncomeSourceArgs,
    CreateOneTimeLineArgs,
    Deduction,
    DeductionKind,
    DeleteOneTimeLineArgs,
    PayFrequency,
    Payment,
    PaymentChange,
    UpdateIncomeSourceArgs,
    UpdateOneTimeLineArgs,
    parse_create_income_source_args,
    parse_one_time_line_args,
    parse_payment,
    parse_payment_changes,
    parse_update_income_source_args,
    validate_frequency,
    validate_label,
    validate_one_time_amount,
)
from corderohq.common import Keep, SetTo
from corderohq.personal_finance_handler import handler

# The same frozen "now" as test_personal_finance_handler: 2026-06 is the current month,
# 2026-07 to 2027-06 are editable future months, and 2026-05 and earlier are locked.
_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
_CURRENT = "2026-06"
_FUTURE = "2026-09"
_LOCKED = "2026-03"
_ADMIN_GROUP = "budget-admin-test"

_PAYMENT: dict[str, Any] = {
    "gross": 5_000_000_000,
    "deductions": [
        {"label": "Federal tax", "kind": "TAX", "amount": 600_000_000},
        {"label": "401k", "kind": "PRETAX_SAVINGS", "amount": 400_000_000},
    ],
}
_PAYMENT_ARGS = Payment(
    gross=5_000_000_000,
    deductions=(
        Deduction(label="Federal tax", kind=DeductionKind.TAX, amount=600_000_000),
        Deduction(label="401k", kind=DeductionKind.PRETAX_SAVINGS, amount=400_000_000),
    ),
)
_NET = 4_000_000_000


def _raised(payment: dict[str, Any], gross: int) -> dict[str, Any]:
    return {**payment, "gross": gross}


# --- The payment rules ---


class TestParsePayment:
    def test_returns_a_payment(self) -> None:
        assert parse_payment(_PAYMENT) == _PAYMENT_ARGS

    def test_to_item_returns_the_stored_shape(self) -> None:
        assert _PAYMENT_ARGS.to_item() == _PAYMENT

    def test_label_is_trimmed(self) -> None:
        raw = {**_PAYMENT, "deductions": [{"label": "  Tax  ", "kind": "TAX", "amount": 1}]}
        assert parse_payment(raw).deductions[0].label == "Tax"

    def test_accepts_integral_decimals_from_dynamodb(self) -> None:
        raw = {
            "gross": Decimal("5000000000"),
            "deductions": [{"label": "Tax", "kind": "TAX", "amount": Decimal("1000000000")}],
        }
        result = parse_payment(raw)
        assert result.gross == 5_000_000_000
        assert isinstance(result.gross, int)

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"gross": True}, "gross must be an integer"),
            ({"gross": -1}, "gross must not be negative"),
            ({"gross": 1.5}, "gross must be an integer"),
            ({"deductions": "x"}, "deductions must be a list"),
            ({"deductions": [{"label": "x", "kind": "OTHER", "amount": 1}]}, "kind must be one of"),
            ({"deductions": [{"label": "", "kind": "TAX", "amount": 1}]}, "label is required"),
            ({"deductions": [{"label": "x", "kind": "TAX", "amount": -1}]}, "amount must not be negative"),
            ({"gross": 100}, "the deductions must not be more than the gross amount"),
        ],
    )
    def test_rejects_a_broken_payment(self, change: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            parse_payment({**_PAYMENT, **change})

    def test_rejects_a_value_that_is_not_an_object(self) -> None:
        with pytest.raises(ValueError, match="payment must be an object"):
            parse_payment(None)


class TestNetPay:
    def test_net(self) -> None:
        assert _PAYMENT_ARGS.net == _NET

    def test_no_deductions(self) -> None:
        assert Payment(gross=7, deductions=()).net == 7

    @pytest.mark.parametrize(
        ("frequency", "count"),
        [
            (PayFrequency.WEEKLY, 4),
            (PayFrequency.BIWEEKLY, 2),
            (PayFrequency.SEMIMONTHLY, 2),
            (PayFrequency.MONTHLY, 1),
        ],
    )
    def test_the_frequency_sets_the_paychecks_per_month(self, frequency: PayFrequency, count: int) -> None:
        assert frequency.paychecks_per_month == count


class TestSmallRules:
    def test_a_stored_payment_equals_the_same_payment_from_a_request(self) -> None:
        stored = {**_PAYMENT, "gross": Decimal("5000000000")}
        assert parse_payment(stored) == _PAYMENT_ARGS
        assert parse_payment(stored) != parse_payment(_raised(_PAYMENT, 6_000_000_000))

    def test_one_time_amount_must_be_positive(self) -> None:
        assert validate_one_time_amount(5) == 5
        with pytest.raises(ValueError, match="more than 0"):
            validate_one_time_amount(0)

    def test_frequency_and_label(self) -> None:
        assert validate_frequency("biweekly") == "biweekly"
        with pytest.raises(ValueError, match="frequency must be one of"):
            validate_frequency("daily")
        assert validate_label("  Employer A ") == "Employer A"
        with pytest.raises(ValueError, match="at most"):
            validate_label("x" * 101)


class TestParseIncomeSourceArgs:
    def test_create(self) -> None:
        body = {"owners": ["p1", "p1"], "label": " A ", "frequency": "monthly", "initialPayment": _PAYMENT}
        assert parse_create_income_source_args(body) == CreateIncomeSourceArgs(
            owners=["p1"], label="A", frequency=PayFrequency.MONTHLY, initial_payment=_PAYMENT_ARGS
        )

    def test_update_keeps_each_absent_key(self) -> None:
        assert parse_update_income_source_args({"frequency": "weekly"}) == UpdateIncomeSourceArgs(
            label=Keep(), frequency=SetTo(PayFrequency.WEEKLY), owners=Keep()
        )

    def test_update_needs_a_key(self) -> None:
        with pytest.raises(ValueError, match="at least one editable field is required"):
            parse_update_income_source_args({"note": "x"})


class TestParsePaymentChanges:
    def test_null_payment_removes_the_pin_only_when_permitted(self) -> None:
        body = {"payments": [{"incomeSourceId": "s1", "payment": None}]}
        assert parse_payment_changes(body, allow_remove=True) == [PaymentChange(income_source_id="s1", payment=None)]
        with pytest.raises(ValueError, match="must not be null"):
            parse_payment_changes(body, allow_remove=False)

    def test_payment_is_required(self) -> None:
        with pytest.raises(ValueError, match="must have payment"):
            parse_payment_changes({"payments": [{"incomeSourceId": "s1"}]}, allow_remove=True)

    def test_broken_payment_names_its_index(self) -> None:
        body = {"payments": [{"incomeSourceId": "s1", "payment": {**_PAYMENT, "gross": -1}}]}
        with pytest.raises(ValueError, match=r"payments\[0\]: gross must not be negative"):
            parse_payment_changes(body, allow_remove=True)


class TestParseOneTimeLineArgs:
    def test_each_body_shape(self) -> None:
        assert parse_one_time_line_args({"label": "Bonus", "amount": 5}) == CreateOneTimeLineArgs("Bonus", 5)
        assert parse_one_time_line_args({"lineId": "ONE#x", "label": "Bonus", "amount": 5}) == UpdateOneTimeLineArgs(
            "ONE#x", "Bonus", 5
        )
        assert parse_one_time_line_args({"lineId": "ONE#x", "delete": True}) == DeleteOneTimeLineArgs("ONE#x")

    def test_a_payment_line_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="lineId of a one-time line is required"):
            parse_one_time_line_args({"lineId": "SRC#s1", "delete": True})


# --- The tables ---


def _plan_table(rows: list[dict[str, Any]]) -> tuple[IncomePlanTable, MagicMock]:
    table = IncomePlanTable.__new__(IncomePlanTable)
    mock = MagicMock()
    table._table = mock
    mock.scan.return_value = {"Items": rows}
    mock.query.return_value = {"Items": rows}
    return table, mock


def _source_table(sources: list[dict[str, Any]]) -> IncomeSourceTable:
    table = IncomeSourceTable.__new__(IncomeSourceTable)
    mock = MagicMock()
    table._table = mock
    mock.scan.return_value = {"Items": sources}
    return table


def _payment_row(year_month: str, source_id: str, payment: dict[str, Any] = _PAYMENT) -> dict[str, Any]:
    return {
        "yearMonth": year_month,
        "lineId": f"SRC#{source_id}",
        "lineType": "PAYMENT",
        "incomeSourceId": source_id,
        **payment,
    }


def _one_time_row(year_month: str, line: str, amount: int) -> dict[str, Any]:
    return {
        "yearMonth": year_month,
        "lineId": f"ONE#{line}",
        "lineType": "ONE_TIME",
        "label": "Bonus",
        "amount": amount,
    }


def _source(source_id: str, active: bool = True, sort_order: int = 0, frequency: str = "biweekly") -> dict[str, Any]:
    return {
        "incomeSourceId": source_id,
        "owners": ["p1"],
        "label": f"Employer {source_id}",
        "frequency": frequency,
        "active": active,
        "sortOrder": sort_order,
    }


class TestIncomePlanCarryStore:
    def test_reads_payment_rows_only_and_strips_the_prefix(self) -> None:
        table, _ = _plan_table([_payment_row("2026-05", "s1"), _one_time_row("2026-05", "x", 9)])
        rows = IncomePlanDensifyAdapter(table).scan_stored_values()
        assert rows == [("2026-05", "s1", _PAYMENT_ARGS)]

    def test_writes_nothing_for_none(self) -> None:
        table, mock = _plan_table([])
        IncomePlanDensifyAdapter(table).put_stored_value("2026-06", "s1", None)
        mock.put_item.assert_not_called()

    def test_densify_copies_the_payment_and_never_a_one_time_line(self) -> None:
        table, mock = _plan_table([_payment_row("2026-04", "s1"), _one_time_row("2026-04", "x", 9)])
        written = densify(IncomePlanDensifyAdapter(table), ["s1"], "2026-06", cold_start_value=None)
        assert written == 2
        items = [call.kwargs["Item"] for call in mock.put_item.call_args_list]
        assert [(i["yearMonth"], i["lineId"], i["gross"]) for i in items] == [
            ("2026-05", "SRC#s1", _PAYMENT["gross"]),
            ("2026-06", "SRC#s1", _PAYMENT["gross"]),
        ]

    def test_densify_with_no_history_writes_nothing(self) -> None:
        table, mock = _plan_table([])
        densify(IncomePlanDensifyAdapter(table), ["s1"], "2026-06", cold_start_value=None)
        mock.put_item.assert_not_called()


class TestIncomeSourceTable:
    def test_list_all_sorts_by_order_then_label(self) -> None:
        table = _source_table([_source("b", sort_order=1), _source("c", sort_order=0), _source("a", sort_order=1)])
        assert [s["incomeSourceId"] for s in table.get_all_income_sources()] == ["c", "a", "b"]

    def test_create_goes_to_the_end(self) -> None:
        table = _source_table([_source("a", sort_order=4)])
        item = table.create_income_source(
            CreateIncomeSourceArgs(["p1"], "Employer A", PayFrequency.BIWEEKLY, _PAYMENT_ARGS)
        )
        assert item["sortOrder"] == 5
        assert item["active"] is True


# --- compute_income ---


class TestComputeIncome:
    def test_editable_month_counts_active_sources_and_one_time_lines(self) -> None:
        rows = [
            _payment_row(_CURRENT, "s1"),
            _payment_row(_CURRENT, "s2"),
            _one_time_row(_CURRENT, "b", 1_000_000),
        ]
        plan, _ = _plan_table(rows)
        sources = _source_table([_source("s1"), _source("s2", active=False)])

        result = compute_income(plan, sources, _CURRENT, _NOW)

        assert result["state"] == "EDITABLE"
        assert [p["incomeSourceId"] for p in result["payments"]] == ["s1"]
        assert result["payments"][0]["net"] == _NET
        assert result["payments"][0]["frequency"] == "biweekly"
        assert result["payments"][0]["monthlyNet"] == 2 * _NET
        assert "pinned" not in result["payments"][0]
        assert result["oneTime"] == [{"lineId": "ONE#b", "label": "Bonus", "amount": 1_000_000}]
        assert result["plannedNetIncome"] == 2 * _NET + 1_000_000

    def test_locked_month_counts_inactive_sources_with_a_row(self) -> None:
        plan, _ = _plan_table([_payment_row(_LOCKED, "s2")])
        sources = _source_table([_source("s1"), _source("s2", active=False)])

        result = compute_income(plan, sources, _LOCKED, _NOW)

        assert result["state"] == "LOCKED"
        assert [p["incomeSourceId"] for p in result["payments"]] == ["s2"]
        assert result["plannedNetIncome"] == 2 * _NET

    def test_a_weekly_source_counts_four_paychecks(self) -> None:
        plan, _ = _plan_table([_payment_row(_CURRENT, "s1")])
        sources = _source_table([_source("s1", frequency="weekly")])

        result = compute_income(plan, sources, _CURRENT, _NOW)

        assert result["payments"][0]["monthlyNet"] == 4 * _NET
        assert result["plannedNetIncome"] == 4 * _NET

    def test_future_month_uses_pins_and_the_walk_back(self) -> None:
        raised = _raised(_PAYMENT, 6_000_000_000)
        rows = [_payment_row(_CURRENT, "s1"), _payment_row(_CURRENT, "s2"), _payment_row(_FUTURE, "s2", raised)]
        plan, _ = _plan_table(rows)
        sources = _source_table([_source("s1"), _source("s2"), _source("s3")])

        result = compute_income(plan, sources, _FUTURE, _NOW)

        by_id = {p["incomeSourceId"]: p for p in result["payments"]}
        assert set(by_id) == {"s1", "s2"}
        assert by_id["s1"]["pinned"] is False
        assert by_id["s2"]["pinned"] is True
        assert by_id["s2"]["gross"] == 6_000_000_000
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
    """Replace the tables of the handler, and `compute_income`, with mocks."""

    with (
        patch("corderohq.personal_finance_handler._INCOME_SOURCE_TABLE") as source_table,
        patch("corderohq.personal_finance_handler._INCOME_PLAN_TABLE") as plan_table,
        patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE") as audit_table,
        patch("corderohq.personal_finance_handler._PROFILE_TABLE") as profile_table,
        patch("corderohq.personal_finance_handler.compute_income", return_value={"plannedNetIncome": 0}) as compute,
    ):
        source_table.get_income_source_by_id.return_value = _source("s1")
        profile_table.get_profile.return_value = {"personId": "p1"}
        plan_table.put_payment.side_effect = lambda ym, source_id, payment: _payment_row(
            ym, source_id, payment.to_item()
        )
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


class TestIncomeSourceRoutes:
    def test_create_writes_the_first_payment_in_the_current_month(self, tables: dict[str, MagicMock]) -> None:
        tables["source"].create_income_source.return_value = _source("s1")
        body = {
            "owners": ["p1"],
            "label": "Employer A",
            "frequency": "biweekly",
            "initialPayment": _PAYMENT,
            "explanation": "new job",
        }
        result = handler(_event("POST", "/api/income-sources", body), None)

        assert result["statusCode"] == 201
        tables["source"].create_income_source.assert_called_once_with(
            CreateIncomeSourceArgs(["p1"], "Employer A", PayFrequency.BIWEEKLY, _PAYMENT_ARGS)
        )
        tables["plan"].put_payment.assert_called_once_with(_CURRENT, "s1", _PAYMENT_ARGS)
        audit = tables["audit"].write_entry.call_args.kwargs
        assert audit["action"] == "INCOME_CREATE"
        assert audit["category_id"] is None
        assert audit["income_source_id"] == "s1"
        assert audit["income_line_id"] == "SRC#s1"

    def test_create_needs_a_known_person(self, tables: dict[str, MagicMock]) -> None:
        tables["profile"].get_profile.return_value = None
        body = {"owners": ["nobody"], "label": "A", "frequency": "biweekly", "initialPayment": _PAYMENT}
        result = handler(_event("POST", "/api/income-sources", {**body, "explanation": "x"}), None)
        assert result["statusCode"] == 400
        tables["source"].create_income_source.assert_not_called()

    def test_create_with_a_broken_payment_returns_400(self, tables: dict[str, MagicMock]) -> None:
        body = {
            "owners": ["p1"],
            "label": "A",
            "frequency": "biweekly",
            "initialPayment": {**_PAYMENT, "gross": 1},
            "explanation": "x",
        }
        result = handler(_event("POST", "/api/income-sources", body), None)
        assert result["statusCode"] == 400

    def test_list_hides_inactive_sources_by_default(self, tables: dict[str, MagicMock]) -> None:
        tables["source"].get_all_income_sources.return_value = [_source("s1"), _source("s2", active=False)]
        result = handler(_event("GET", "/api/income-sources"), None)
        assert [s["incomeSourceId"] for s in json.loads(result["body"])] == ["s1"]

    def test_deactivate_with_future_pins_needs_a_confirmation(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].find_future_months_with_source.return_value = [_FUTURE]
        result = handler(_event("POST", "/api/income-sources/s1/deactivate", {}), None)
        assert result["statusCode"] == 409
        assert json.loads(result["body"])["affectedMonths"] == [_FUTURE]
        tables["source"].set_active.assert_not_called()

    def test_deactivate_with_confirm_drops_the_pins(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].find_future_months_with_source.return_value = [_FUTURE]
        tables["plan"].get_line.return_value = _payment_row(_FUTURE, "s1")
        tables["source"].set_active.return_value = _source("s1", active=False)
        body = {"confirm": True, "explanation": "job ended"}
        result = handler(_event("POST", "/api/income-sources/s1/deactivate", body), None)
        assert result["statusCode"] == 200
        tables["plan"].delete_payment.assert_called_once_with(_FUTURE, "s1")
        assert _audit_actions(tables["audit"]) == ["INCOME_UNPIN"]
        tables["source"].set_active.assert_called_once_with("s1", False)

    def test_joint_source_has_two_owners(self, tables: dict[str, MagicMock]) -> None:
        tables["source"].create_income_source.return_value = _source("s1")
        body = {
            "owners": ["p1", "p2", "p1"],
            "label": "Joint interest",
            "frequency": "monthly",
            "initialPayment": {"gross": 50_000_000, "deductions": []},
            "explanation": "interest",
        }
        result = handler(_event("POST", "/api/income-sources", body), None)
        assert result["statusCode"] == 201
        tables["source"].create_income_source.assert_called_once_with(
            CreateIncomeSourceArgs(
                ["p1", "p2"],
                "Joint interest",
                PayFrequency.MONTHLY,
                Payment(gross=50_000_000, deductions=()),
            )
        )

    def test_change_of_owners_checks_each_person(self, tables: dict[str, MagicMock]) -> None:
        tables["profile"].get_profile.return_value = None
        result = handler(_event("PUT", "/api/income-sources/s1", {"owners": ["nobody"]}), None)
        assert result["statusCode"] == 400
        tables["source"].update_income_source.assert_not_called()

    def test_rename_has_no_audit_entry(self, tables: dict[str, MagicMock]) -> None:
        tables["source"].update_income_source.return_value = _source("s1")
        result = handler(_event("PUT", "/api/income-sources/s1", {"label": "Employer B"}), None)
        assert result["statusCode"] == 200
        tables["source"].update_income_source.assert_called_once_with(
            "s1", UpdateIncomeSourceArgs(label=SetTo("Employer B"), frequency=Keep(), owners=Keep())
        )
        tables["audit"].write_entry.assert_not_called()


class TestIncomeReplace:
    def test_writes_a_changed_payment_with_an_update_entry(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].get_income_plan_for_month.return_value = [_payment_row(_CURRENT, "s1")]
        raised = _raised(_PAYMENT, 6_000_000_000)
        body = {"payments": [{"incomeSourceId": "s1", "payment": raised}], "explanation": "raise"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/replace", body), None)
        assert result["statusCode"] == 200
        tables["plan"].put_payment.assert_called_once_with(_CURRENT, "s1", parse_payment(raised))
        assert _audit_actions(tables["audit"]) == ["INCOME_UPDATE"]

    def test_same_payment_is_a_no_op(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].get_income_plan_for_month.return_value = [_payment_row(_CURRENT, "s1")]
        body = {"payments": [{"incomeSourceId": "s1", "payment": _PAYMENT}], "explanation": "same"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/replace", body), None)
        assert result["statusCode"] == 200
        tables["plan"].put_payment.assert_not_called()
        tables["audit"].write_entry.assert_not_called()

    def test_body_must_hold_every_source_with_a_row(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].get_income_plan_for_month.return_value = [
            _payment_row(_CURRENT, "s1"),
            _payment_row(_CURRENT, "s2"),
        ]
        body = {"payments": [{"incomeSourceId": "s1", "payment": _PAYMENT}], "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/replace", body), None)
        assert result["statusCode"] == 409
        assert json.loads(result["body"])["missingIncomeSourceIds"] == ["s2"]

    def test_null_payment_returns_400(self, tables: dict[str, MagicMock]) -> None:
        body = {"payments": [{"incomeSourceId": "s1", "payment": None}], "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/replace", body), None)
        assert result["statusCode"] == 400

    def test_future_month_returns_409(self, tables: dict[str, MagicMock]) -> None:
        body = {"payments": [], "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_FUTURE}/replace", body), None)
        assert result["statusCode"] == 409

    def test_locked_month_needs_the_override(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].get_income_plan_for_month.return_value = []
        body = {"payments": [{"incomeSourceId": "s1", "payment": _PAYMENT}], "explanation": "x"}
        locked = handler(_event("POST", f"/api/income/{_LOCKED}/replace", body), None)
        assert locked["statusCode"] == 409

        overridden = handler(_event("POST", f"/api/income/{_LOCKED}/replace", {**body, "override": True}, True), None)
        assert overridden["statusCode"] == 200
        assert tables["audit"].write_entry.call_args.kwargs["override"] is True
        assert _audit_actions(tables["audit"]) == ["INCOME_CREATE"]


class TestIncomePin:
    def test_pin_writes_and_warns_when_it_matches_the_carried_value(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].scan_payments.return_value = [_payment_row(_CURRENT, "s1")]
        body = {"payments": [{"incomeSourceId": "s1", "payment": _PAYMENT}], "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_FUTURE}/pin", body), None)
        assert result["statusCode"] == 200
        tables["plan"].put_payment.assert_called_once_with(_FUTURE, "s1", _PAYMENT_ARGS)
        assert _audit_actions(tables["audit"]) == ["INCOME_PIN"]
        assert json.loads(result["body"])["warnings"]["pinMatchesCarriedValue"] == ["s1"]

    def test_pin_lists_downstream_pins(self, tables: dict[str, MagicMock]) -> None:
        later = "2026-12"
        tables["plan"].scan_payments.return_value = [_payment_row(_CURRENT, "s1"), _payment_row(later, "s1")]
        raised = _raised(_PAYMENT, 6_000_000_000)
        body = {"payments": [{"incomeSourceId": "s1", "payment": raised}], "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_FUTURE}/pin", body), None)
        warnings = json.loads(result["body"])["warnings"]
        assert warnings["downstreamPins"] == [{"incomeSourceId": "s1", "yearMonth": later}]
        assert warnings["pinMatchesCarriedValue"] == []

    def test_unpin_deletes_the_row(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].scan_payments.return_value = [_payment_row(_FUTURE, "s1")]
        body = {"payments": [{"incomeSourceId": "s1", "payment": None}], "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_FUTURE}/pin", body), None)
        assert result["statusCode"] == 200
        tables["plan"].delete_payment.assert_called_once_with(_FUTURE, "s1")
        assert _audit_actions(tables["audit"]) == ["INCOME_UNPIN"]

    def test_current_month_returns_409(self, tables: dict[str, MagicMock]) -> None:
        body = {"payments": [], "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/pin", body), None)
        assert result["statusCode"] == 409

    def test_unknown_source_returns_400(self, tables: dict[str, MagicMock]) -> None:
        tables["source"].get_income_source_by_id.return_value = None
        body = {"payments": [{"incomeSourceId": "nope", "payment": _PAYMENT}], "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_FUTURE}/pin", body), None)
        assert result["statusCode"] == 400


class TestOneTimeLines:
    def test_create_in_a_future_month(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].create_one_time_line.return_value = _one_time_row(_FUTURE, "x", 1_000)
        body = {"label": "Bonus", "amount": 1_000, "explanation": "bonus"}
        result = handler(_event("POST", f"/api/income/{_FUTURE}/one-time", body), None)
        assert result["statusCode"] == 201
        tables["plan"].create_one_time_line.assert_called_once_with(_FUTURE, CreateOneTimeLineArgs("Bonus", 1_000))
        audit = tables["audit"].write_entry.call_args.kwargs
        assert audit["action"] == "ONE_TIME_CREATE"
        assert audit["income_line_id"] == "ONE#x"
        assert audit["changes"] == {"line": {"before": None, "after": {"label": "Bonus", "amount": 1_000}}}

    def test_update_and_delete_need_an_existing_line(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].get_line.return_value = None
        body = {"lineId": "ONE#x", "label": "Bonus", "amount": 5, "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/one-time", body), None)
        assert result["statusCode"] == 404

    def test_update_writes_the_new_label_and_amount(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].get_line.return_value = _one_time_row(_CURRENT, "x", 1_000)
        tables["plan"].update_one_time_line.return_value = _one_time_row(_CURRENT, "x", 2_000)
        body = {"lineId": "ONE#x", "label": "Bonus", "amount": 2_000, "explanation": "bigger"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/one-time", body), None)
        assert result["statusCode"] == 200
        tables["plan"].update_one_time_line.assert_called_once_with(
            _CURRENT, UpdateOneTimeLineArgs("ONE#x", "Bonus", 2_000)
        )
        audit = tables["audit"].write_entry.call_args.kwargs
        assert audit["action"] == "ONE_TIME_UPDATE"
        assert audit["changes"] == {
            "line": {"before": {"label": "Bonus", "amount": 1_000}, "after": {"label": "Bonus", "amount": 2_000}}
        }

    def test_delete_keeps_the_label_in_the_audit_entry(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].get_line.return_value = _one_time_row(_CURRENT, "x", 1_000)
        body = {"lineId": "ONE#x", "delete": True, "explanation": "mistake"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/one-time", body), None)
        assert result["statusCode"] == 200
        tables["plan"].delete_one_time_line.assert_called_once_with(_CURRENT, "ONE#x")
        audit = tables["audit"].write_entry.call_args.kwargs
        assert audit["action"] == "ONE_TIME_DELETE"
        assert audit["changes"] == {"line": {"before": {"label": "Bonus", "amount": 1_000}, "after": None}}

    def test_payment_line_id_is_not_a_one_time_line(self, tables: dict[str, MagicMock]) -> None:
        body = {"lineId": "SRC#s1", "delete": True, "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/one-time", body), None)
        assert result["statusCode"] == 400

    def test_locked_month_returns_409(self, tables: dict[str, MagicMock]) -> None:
        body = {"label": "Bonus", "amount": 1_000, "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_LOCKED}/one-time", body), None)
        assert result["statusCode"] == 409
        tables["plan"].create_one_time_line.assert_not_called()

    def test_zero_amount_returns_400(self, tables: dict[str, MagicMock]) -> None:
        body = {"label": "Bonus", "amount": 0, "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/one-time", body), None)
        assert result["statusCode"] == 400


class TestGetIncome:
    def test_returns_compute_income(self, tables: dict[str, MagicMock]) -> None:
        result = handler(_event("GET", f"/api/income/{_CURRENT}"), None)
        assert result["statusCode"] == 200
        assert json.loads(result["body"]) == {"plannedNetIncome": 0}
        tables["compute"].assert_called_once()

    def test_bad_month_returns_400(self, tables: dict[str, MagicMock]) -> None:
        result = handler(_event("GET", "/api/income/2026-6"), None)
        assert result["statusCode"] == 400
