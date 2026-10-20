"""The income plan: income sources, payments, and one-time income lines.

An income source is any stream of income that the plan counts, such as a job, interest, or
dividends. Every kind works the same way. Each dense month holds one payment row for each
income source, with the same month rules as a budget target. A payment is one value: the gross
amount of one payment, a list of deductions, and the number of payments that the plan counts in
each month. For a source with no deductions, such as interest, the net amount is the gross
amount. The app calculates the net amount. It never stores it.

A one-time income line is a net amount in one month only, such as a bonus. It never carries
forward.

Each amount is an integer in millionths of a dollar. A value read back from DynamoDB is a
`Decimal`, so the checks here accept an integral `Decimal` and return an `int`.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

# The sort keys of the IncomePlan table.
PAYMENT_LINE_PREFIX = "SRC#"
ONE_TIME_LINE_PREFIX = "ONE#"

FREQUENCIES = frozenset({"weekly", "biweekly", "semimonthly", "monthly"})
DEDUCTION_KINDS = frozenset({"TAX", "PRETAX_SAVINGS", "PRETAX_BENEFIT", "POSTTAX_SAVINGS"})

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


def validate_frequency(value: Any) -> str:
    """Return the pay frequency. It is for display only. No calculation uses it."""
    if not isinstance(value, str) or value not in FREQUENCIES:
        raise ValueError(f"frequency must be one of {sorted(FREQUENCIES)}")
    return value


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
        kind = raw_deduction.get("kind")
        if not isinstance(kind, str) or kind not in DEDUCTION_KINDS:
            raise ValueError(f"{where}.kind must be one of {sorted(DEDUCTION_KINDS)}")
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
