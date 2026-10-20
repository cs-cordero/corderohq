"""Tests for the income plan: payment rules, the tables, compute_income, and the routes."""

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

from corderohq.aws.dynamodb import (
    IncomePlanCarryStore,
    IncomePlanTable,
    IncomeSourceTable,
    compute_income,
)
from corderohq.budget.densify import densify
from corderohq.budget.income import (
    monthly_net,
    net_amount,
    payments_equal,
    validate_frequency,
    validate_label,
    validate_one_time_amount,
    validate_payment,
)
from corderohq.personal_finance_handler import handler

# The same frozen "now" as test_personal_finance_handler: 2026-06 is the current month,
# 2026-07 to 2027-06 are editable future months, and 2026-05 and earlier are locked.
_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
_CURRENT = "2026-06"
_FUTURE = "2026-09"
_LOCKED = "2026-03"
_ADMIN_GROUP = "budget-admin-test"

_PAYMENT: dict[str, Any] = {
    "paymentsPerMonth": 2,
    "gross": 5_000_000_000,
    "deductions": [
        {"label": "Federal tax", "kind": "TAX", "amount": 600_000_000},
        {"label": "401k", "kind": "PRETAX_SAVINGS", "amount": 400_000_000},
    ],
}
_NET = 4_000_000_000


def _raised(payment: dict[str, Any], gross: int) -> dict[str, Any]:
    return {**payment, "gross": gross}


# --- The payment rules ---


class TestValidatePayment:
    def test_returns_the_stored_shape(self) -> None:
        assert validate_payment(_PAYMENT) == _PAYMENT

    def test_label_is_trimmed(self) -> None:
        raw = {**_PAYMENT, "deductions": [{"label": "  Tax  ", "kind": "TAX", "amount": 1}]}
        assert validate_payment(raw)["deductions"][0]["label"] == "Tax"

    def test_accepts_integral_decimals_from_dynamodb(self) -> None:
        raw = {
            "paymentsPerMonth": Decimal("2"),
            "gross": Decimal("5000000000"),
            "deductions": [{"label": "Tax", "kind": "TAX", "amount": Decimal("1000000000")}],
        }
        result = validate_payment(raw)
        assert result["gross"] == 5_000_000_000
        assert isinstance(result["gross"], int)

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"paymentsPerMonth": 0}, "paymentsPerMonth must be from 1 to 5"),
            ({"paymentsPerMonth": 6}, "paymentsPerMonth must be from 1 to 5"),
            ({"paymentsPerMonth": True}, "paymentsPerMonth must be an integer"),
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
            validate_payment({**_PAYMENT, **change})

    def test_rejects_a_value_that_is_not_an_object(self) -> None:
        with pytest.raises(ValueError, match="payment must be an object"):
            validate_payment(None)


class TestNetPay:
    def test_net_and_monthly_net(self) -> None:
        assert net_amount(_PAYMENT) == _NET
        assert monthly_net(_PAYMENT) == 2 * _NET

    def test_no_deductions(self) -> None:
        assert net_amount({"paymentsPerMonth": 1, "gross": 7, "deductions": []}) == 7


class TestSmallRules:
    def test_payments_equal_ignores_int_and_decimal(self) -> None:
        stored = {**_PAYMENT, "gross": Decimal("5000000000"), "paymentsPerMonth": Decimal("2")}
        assert payments_equal(stored, _PAYMENT)
        assert not payments_equal(stored, _raised(_PAYMENT, 6_000_000_000))
        assert payments_equal(None, None)
        assert not payments_equal(None, _PAYMENT)

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


def _source(source_id: str, active: bool = True, sort_order: int = 0) -> dict[str, Any]:
    return {
        "incomeSourceId": source_id,
        "personId": "p1",
        "label": f"Employer {source_id}",
        "frequency": "biweekly",
        "active": active,
        "sortOrder": sort_order,
    }


class TestIncomePlanCarryStore:
    def test_reads_payment_rows_only_and_strips_the_prefix(self) -> None:
        table, _ = _plan_table([_payment_row("2026-05", "s1"), _one_time_row("2026-05", "x", 9)])
        rows = IncomePlanCarryStore(table).scan_carry_rows()
        assert rows == [("2026-05", "s1", _PAYMENT)]

    def test_writes_nothing_for_none(self) -> None:
        table, mock = _plan_table([])
        IncomePlanCarryStore(table).put_carry_row("2026-06", "s1", None)
        mock.put_item.assert_not_called()

    def test_densify_copies_the_payment_and_never_a_one_time_line(self) -> None:
        table, mock = _plan_table([_payment_row("2026-04", "s1"), _one_time_row("2026-04", "x", 9)])
        written = densify(IncomePlanCarryStore(table), ["s1"], "2026-06", cold_start_value=None)
        assert written == 2
        items = [call.kwargs["Item"] for call in mock.put_item.call_args_list]
        assert [(i["yearMonth"], i["lineId"], i["gross"]) for i in items] == [
            ("2026-05", "SRC#s1", _PAYMENT["gross"]),
            ("2026-06", "SRC#s1", _PAYMENT["gross"]),
        ]

    def test_densify_with_no_history_writes_nothing(self) -> None:
        table, mock = _plan_table([])
        densify(IncomePlanCarryStore(table), ["s1"], "2026-06", cold_start_value=None)
        mock.put_item.assert_not_called()


class TestIncomeSourceTable:
    def test_list_all_sorts_by_order_then_label(self) -> None:
        table = _source_table([_source("b", sort_order=1), _source("c", sort_order=0), _source("a", sort_order=1)])
        assert [s["incomeSourceId"] for s in table.list_all()] == ["c", "a", "b"]

    def test_create_goes_to_the_end(self) -> None:
        table = _source_table([_source("a", sort_order=4)])
        item = table.create("p1", "Employer A", "biweekly")
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
        source_table.get.return_value = _source("s1")
        profile_table.get_person.return_value = {"personId": "p1"}
        plan_table.put_payment.side_effect = lambda ym, sid, payment: _payment_row(ym, sid, payment)
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
        tables["source"].create.return_value = _source("s1")
        body = {
            "personId": "p1",
            "label": "Employer A",
            "frequency": "biweekly",
            "initialPayment": _PAYMENT,
            "explanation": "new job",
        }
        result = handler(_event("POST", "/api/income-sources", body), None)

        assert result["statusCode"] == 201
        tables["source"].create.assert_called_once_with("p1", "Employer A", "biweekly")
        tables["plan"].put_payment.assert_called_once_with(_CURRENT, "s1", _PAYMENT)
        audit = tables["audit"].write_entry.call_args.kwargs
        assert audit["action"] == "INCOME_CREATE"
        assert audit["category_id"] is None
        assert audit["income_source_id"] == "s1"
        assert audit["income_line_id"] == "SRC#s1"

    def test_create_needs_a_known_person(self, tables: dict[str, MagicMock]) -> None:
        tables["profile"].get_person.return_value = None
        body = {"personId": "nobody", "label": "A", "frequency": "biweekly", "initialPayment": _PAYMENT}
        result = handler(_event("POST", "/api/income-sources", {**body, "explanation": "x"}), None)
        assert result["statusCode"] == 400
        tables["source"].create.assert_not_called()

    def test_create_with_a_broken_payment_returns_400(self, tables: dict[str, MagicMock]) -> None:
        body = {
            "personId": "p1",
            "label": "A",
            "frequency": "biweekly",
            "initialPayment": {**_PAYMENT, "gross": 1},
            "explanation": "x",
        }
        result = handler(_event("POST", "/api/income-sources", body), None)
        assert result["statusCode"] == 400

    def test_list_hides_inactive_sources_by_default(self, tables: dict[str, MagicMock]) -> None:
        tables["source"].list_all.return_value = [_source("s1"), _source("s2", active=False)]
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

    def test_rename_has_no_audit_entry(self, tables: dict[str, MagicMock]) -> None:
        tables["source"].update.return_value = _source("s1")
        result = handler(_event("PUT", "/api/income-sources/s1", {"label": "Employer B"}), None)
        assert result["statusCode"] == 200
        tables["source"].update.assert_called_once_with("s1", {"label": "Employer B"})
        tables["audit"].write_entry.assert_not_called()


class TestIncomeReplace:
    def test_writes_a_changed_payment_with_an_update_entry(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].get_month.return_value = [_payment_row(_CURRENT, "s1")]
        raised = _raised(_PAYMENT, 6_000_000_000)
        body = {"payments": [{"incomeSourceId": "s1", "payment": raised}], "explanation": "raise"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/replace", body), None)
        assert result["statusCode"] == 200
        tables["plan"].put_payment.assert_called_once_with(_CURRENT, "s1", raised)
        assert _audit_actions(tables["audit"]) == ["INCOME_UPDATE"]

    def test_same_payment_is_a_no_op(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].get_month.return_value = [_payment_row(_CURRENT, "s1")]
        body = {"payments": [{"incomeSourceId": "s1", "payment": _PAYMENT}], "explanation": "same"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/replace", body), None)
        assert result["statusCode"] == 200
        tables["plan"].put_payment.assert_not_called()
        tables["audit"].write_entry.assert_not_called()

    def test_body_must_hold_every_source_with_a_row(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].get_month.return_value = [_payment_row(_CURRENT, "s1"), _payment_row(_CURRENT, "s2")]
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
        tables["plan"].get_month.return_value = []
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
        tables["plan"].put_payment.assert_called_once_with(_FUTURE, "s1", _PAYMENT)
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
        tables["source"].get.return_value = None
        body = {"payments": [{"incomeSourceId": "nope", "payment": _PAYMENT}], "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_FUTURE}/pin", body), None)
        assert result["statusCode"] == 400


class TestOneTimeLines:
    def test_create_in_a_future_month(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].put_one_time.return_value = _one_time_row(_FUTURE, "x", 1_000)
        body = {"label": "Bonus", "amount": 1_000, "explanation": "bonus"}
        result = handler(_event("POST", f"/api/income/{_FUTURE}/one-time", body), None)
        assert result["statusCode"] == 201
        tables["plan"].put_one_time.assert_called_once_with(_FUTURE, "Bonus", 1_000, line_id=None)
        audit = tables["audit"].write_entry.call_args.kwargs
        assert audit["action"] == "ONE_TIME_CREATE"
        assert audit["income_line_id"] == "ONE#x"
        assert audit["changes"] == {"line": {"before": None, "after": {"label": "Bonus", "amount": 1_000}}}

    def test_update_and_delete_need_an_existing_line(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].get_line.return_value = None
        body = {"lineId": "ONE#x", "label": "Bonus", "amount": 5, "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/one-time/update", body), None)
        assert result["statusCode"] == 404

    def test_delete_keeps_the_label_in_the_audit_entry(self, tables: dict[str, MagicMock]) -> None:
        tables["plan"].get_line.return_value = _one_time_row(_CURRENT, "x", 1_000)
        body = {"lineId": "ONE#x", "explanation": "mistake"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/one-time/delete", body), None)
        assert result["statusCode"] == 200
        tables["plan"].delete_line.assert_called_once_with(_CURRENT, "ONE#x")
        audit = tables["audit"].write_entry.call_args.kwargs
        assert audit["action"] == "ONE_TIME_DELETE"
        assert audit["changes"] == {"line": {"before": {"label": "Bonus", "amount": 1_000}, "after": None}}

    def test_payment_line_id_is_not_a_one_time_line(self, tables: dict[str, MagicMock]) -> None:
        body = {"lineId": "SRC#s1", "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_CURRENT}/one-time/delete", body), None)
        assert result["statusCode"] == 400

    def test_locked_month_returns_409(self, tables: dict[str, MagicMock]) -> None:
        body = {"label": "Bonus", "amount": 1_000, "explanation": "x"}
        result = handler(_event("POST", f"/api/income/{_LOCKED}/one-time", body), None)
        assert result["statusCode"] == 409
        tables["plan"].put_one_time.assert_not_called()

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
