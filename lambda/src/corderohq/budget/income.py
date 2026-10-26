from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Any

# The sort keys of the IncomePlan table.
PAYMENT_LINE_PREFIX = "SRC#"
ONE_TIME_LINE_PREFIX = "ONE#"


class IncomeLineType(StrEnum):
    """The kind of an IncomePlan row. The value is stored in `IncomePlan.lineType`."""

    PAYMENT = "PAYMENT"  # lineId "SRC#{incomeSourceId}"; carries forward
    ONE_TIME = "ONE_TIME"  # lineId "ONE#{ULID}"; this month only


class PayFrequency(StrEnum):
    """How often an income source pays. For display only. No calculation uses it."""

    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    SEMIMONTHLY = "semimonthly"
    MONTHLY = "monthly"


class DeductionKind(StrEnum):
    """The kind of a deduction from a payment. For display only. No calculation uses it."""

    TAX = "TAX"
    PRETAX_SAVINGS = "PRETAX_SAVINGS"
    PRETAX_BENEFIT = "PRETAX_BENEFIT"
    POSTTAX_SAVINGS = "POSTTAX_SAVINGS"


MIN_PAYMENTS_PER_MONTH = 1
MAX_PAYMENTS_PER_MONTH = 5
LABEL_MAX_LENGTH = 100


def payment_line_id(income_source_id: str) -> str:
    """Return the IncomePlan sort key of the payment row of one income source."""

    return f"{PAYMENT_LINE_PREFIX}{income_source_id}"


def _as_int(value: Any, name: str) -> int:
    """Return `value` as an int. Accept an int or an integral Decimal. Reject a bool."""

    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    raise ValueError(f"{name} must be an integer")


def validate_label(value: Any, name: str = "label") -> str:
    """Return the label without outer white space. It must not be empty."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    label = value.strip()
    if len(label) > LABEL_MAX_LENGTH:
        raise ValueError(f"{name} must be at most {LABEL_MAX_LENGTH} characters")
    return label


def validate_frequency(value: Any) -> PayFrequency:
    """Return the pay frequency."""

    try:
        return PayFrequency(value)
    except ValueError:
        raise ValueError(f"frequency must be one of {[f.value for f in PayFrequency]}") from None


def validate_payment(raw: Any) -> dict[str, Any]:
    """Check a payment, and return it in its stored shape. Raise `ValueError` if a rule fails.

    The stored shape is `{paymentsPerMonth, gross, deductions: [{label, kind, amount}]}`. Each
    amount is for one payment. The net amount must not be negative.
    """

    if not isinstance(raw, Mapping):
        raise ValueError("payment must be an object")

    payments_per_month = _as_int(raw.get("paymentsPerMonth"), "paymentsPerMonth")
    if not MIN_PAYMENTS_PER_MONTH <= payments_per_month <= MAX_PAYMENTS_PER_MONTH:
        raise ValueError(f"paymentsPerMonth must be from {MIN_PAYMENTS_PER_MONTH} to {MAX_PAYMENTS_PER_MONTH}")

    gross = _as_int(raw.get("gross"), "gross")
    if gross < 0:
        raise ValueError("gross must not be negative")

    raw_deductions = raw.get("deductions", [])
    if not isinstance(raw_deductions, list):
        raise ValueError("deductions must be a list")
    deductions: list[dict[str, Any]] = []
    for index, raw_deduction in enumerate(raw_deductions):
        where = f"deductions[{index}]"
        if not isinstance(raw_deduction, Mapping):
            raise ValueError(f"{where} must be an object")
        try:
            kind = DeductionKind(raw_deduction.get("kind"))
        except ValueError:
            raise ValueError(f"{where}.kind must be one of {[k.value for k in DeductionKind]}") from None
        amount = _as_int(raw_deduction.get("amount"), f"{where}.amount")
        if amount < 0:
            raise ValueError(f"{where}.amount must not be negative")
        deductions.append(
            {"label": validate_label(raw_deduction.get("label"), f"{where}.label"), "kind": kind, "amount": amount}
        )

    payment = {"paymentsPerMonth": payments_per_month, "gross": gross, "deductions": deductions}
    if net_amount(payment) < 0:
        raise ValueError("the deductions must not be more than the gross amount")
    return payment


def net_amount(payment: Mapping[str, Any]) -> int:
    """Return the net amount of one payment: the gross amount minus every deduction."""

    total_deductions = sum(_as_int(d["amount"], "amount") for d in payment["deductions"])
    return _as_int(payment["gross"], "gross") - total_deductions


def monthly_net(payment: Mapping[str, Any]) -> int:
    """Return the net amount that the plan counts in one month."""

    return net_amount(payment) * _as_int(payment["paymentsPerMonth"], "paymentsPerMonth")


def payments_equal(first: Mapping[str, Any] | None, second: Mapping[str, Any] | None) -> bool:
    """Return True if two payments are the same, after each one goes through the checks."""

    if first is None or second is None:
        return first is None and second is None
    return validate_payment(first) == validate_payment(second)


def validate_one_time_amount(value: Any) -> int:
    """Return the net amount of a one-time income line. It must be more than 0."""

    amount = _as_int(value, "amount")
    if amount <= 0:
        raise ValueError("amount must be more than 0")
    return amount
