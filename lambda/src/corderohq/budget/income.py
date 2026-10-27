from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, assert_never

from corderohq.accounts import validate_owners
from corderohq.common import Keep, SetTo

# The sort keys of the IncomePlan table.
PAYMENT_LINE_PREFIX = "SRC#"
ONE_TIME_LINE_PREFIX = "ONE#"

LABEL_MAX_LENGTH = 100

_EDITABLE_INCOME_SOURCE_KEYS = ("label", "frequency", "owners")


class IncomeLineType(StrEnum):
    """The kind of an IncomePlan row. The value is stored in `IncomePlan.lineType`."""

    PAYMENT = "PAYMENT"  # lineId "SRC#{incomeSourceId}"; carries forward
    ONE_TIME = "ONE_TIME"  # lineId "ONE#{ULID}"; this month only


class PayFrequency(StrEnum):
    """How often an income source pays."""

    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    SEMIMONTHLY = "semimonthly"
    MONTHLY = "monthly"

    @property
    def paychecks_per_month(self) -> int:
        """The number of paychecks that the plan counts in each month.

        It is an estimate. A weekly source pays 5 times in some months, and a biweekly source pays 3 times in two
        months of each year.
        """

        match self:
            case PayFrequency.WEEKLY:
                return 4
            case PayFrequency.BIWEEKLY | PayFrequency.SEMIMONTHLY:
                return 2
            case PayFrequency.MONTHLY:
                return 1
            case _:
                assert_never(self)


class DeductionKind(StrEnum):
    """The kind of a deduction from a payment. For display only. No calculation uses it."""

    TAX = "TAX"
    PRETAX_SAVINGS = "PRETAX_SAVINGS"
    PRETAX_BENEFIT = "PRETAX_BENEFIT"
    POSTTAX_SAVINGS = "POSTTAX_SAVINGS"


@dataclass(frozen=True)
class Deduction:
    """One deduction from one payment, such as a tax."""

    label: str
    kind: DeductionKind
    amount: int  # millionths of a dollar

    def to_item(self) -> dict[str, Any]:
        """Converts an instance of Deduction to how it is stored in DynamoDB."""

        return {"label": self.label, "kind": self.kind, "amount": self.amount}


@dataclass(frozen=True)
class Payment:
    """One paycheck of an income source in one month: the gross amount and its deductions.

    The pay frequency of the income source sets the number of paychecks in the month.
    """

    gross: int  # millionths of a dollar
    deductions: tuple[Deduction, ...]

    @property
    def net(self) -> int:
        """The net amount of one paycheck: the gross amount minus every deduction."""

        return self.gross - sum(deduction.amount for deduction in self.deductions)

    def to_item(self) -> dict[str, Any]:
        """Converts an instance of Payment to how it is stored in DynamoDB."""

        return {"gross": self.gross, "deductions": [deduction.to_item() for deduction in self.deductions]}


@dataclass(frozen=True)
class CreateIncomeSourceArgs:
    """The checked arguments of a new income source, and its first payment."""

    owners: list[str]
    label: str
    frequency: PayFrequency
    initial_payment: Payment


@dataclass(frozen=True)
class UpdateIncomeSourceArgs:
    """The checked changes to an income source. Each field that the request did not name is Keep()."""

    label: Keep | SetTo[str]
    frequency: Keep | SetTo[PayFrequency]
    owners: Keep | SetTo[list[str]]


@dataclass(frozen=True)
class PaymentChange:
    """The new payment of one income source in one month. A payment of None removes the pin."""

    income_source_id: str
    payment: Payment | None


@dataclass(frozen=True)
class CreateOneTimeLineArgs:
    """The checked arguments of a new one-time income line."""

    label: str
    amount: int  # millionths of a dollar


@dataclass(frozen=True)
class UpdateOneTimeLineArgs:
    """The checked new label and amount of a one-time income line."""

    line_id: str
    label: str
    amount: int  # millionths of a dollar


@dataclass(frozen=True)
class DeleteOneTimeLineArgs:
    """The one-time income line to delete."""

    line_id: str


type OneTimeLineArgs = CreateOneTimeLineArgs | UpdateOneTimeLineArgs | DeleteOneTimeLineArgs


def payment_line_id(income_source_id: str) -> str:
    """Return the IncomePlan sort key of the payment row of one income source."""

    return f"{PAYMENT_LINE_PREFIX}{income_source_id}"


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


def validate_one_time_amount(value: Any) -> int:
    """Return the net amount of a one-time income line. It must be more than 0."""

    amount = _as_int(value, "amount")
    if amount <= 0:
        raise ValueError("amount must be more than 0")
    return amount


def parse_payment(raw: Any) -> Payment:
    """Convert a payment from a request body or a stored row into a Payment. Raise ValueError if a rule fails.

    The shape is `{gross, deductions: [{label, kind, amount}]}`. A value read back from DynamoDB is a `Decimal`, so an
    integral `Decimal` is accepted. The net amount must not be negative.
    """

    if not isinstance(raw, Mapping):
        raise ValueError("payment must be an object")

    gross = _as_int(raw.get("gross"), "gross")
    if gross < 0:
        raise ValueError("gross must not be negative")

    raw_deductions = raw.get("deductions", [])
    if not isinstance(raw_deductions, list):
        raise ValueError("deductions must be a list")
    deductions = tuple(
        _parse_deduction(raw_deduction, f"deductions[{index}]") for index, raw_deduction in enumerate(raw_deductions)
    )

    payment = Payment(gross=gross, deductions=deductions)
    if payment.net < 0:
        raise ValueError("the deductions must not be more than the gross amount")
    return payment


def parse_create_income_source_args(payload: Mapping[str, Any]) -> CreateIncomeSourceArgs:
    """Convert the body of a create request into checked arguments. Raise ValueError for bad input.

    It does not check that each owner exists.
    """

    return CreateIncomeSourceArgs(
        owners=validate_owners(payload.get("owners")),
        label=validate_label(payload.get("label")),
        frequency=validate_frequency(payload.get("frequency")),
        initial_payment=parse_payment(payload.get("initialPayment")),
    )


def parse_update_income_source_args(payload: Mapping[str, Any]) -> UpdateIncomeSourceArgs:
    """Convert the body of an update request into checked changes. Raise ValueError for bad input.

    An absent key changes nothing. It does not check that each owner exists.
    """

    if not any(key in payload for key in _EDITABLE_INCOME_SOURCE_KEYS):
        raise ValueError(f"at least one editable field is required: {list(_EDITABLE_INCOME_SOURCE_KEYS)}")
    return UpdateIncomeSourceArgs(
        label=SetTo(validate_label(payload["label"])) if "label" in payload else Keep(),
        frequency=SetTo(validate_frequency(payload["frequency"])) if "frequency" in payload else Keep(),
        owners=SetTo(validate_owners(payload["owners"])) if "owners" in payload else Keep(),
    )


def parse_payment_changes(payload: Mapping[str, Any], *, allow_remove: bool) -> list[PaymentChange]:
    """Convert `payments`, a list of `{incomeSourceId, payment}`, into checked changes. Raise ValueError for bad input.

    A null `payment` removes the pin, and only `allow_remove` permits it. It does not check that each income source
    exists.
    """

    raw_rows = payload.get("payments")
    if not isinstance(raw_rows, list):
        raise ValueError("payments array is required")
    changes: list[PaymentChange] = []
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, Mapping) or not isinstance(raw.get("incomeSourceId"), str):
            raise ValueError(f"payments[{index}] must have incomeSourceId")
        if "payment" not in raw:
            raise ValueError(f"payments[{index}] must have payment")
        if raw["payment"] is None and not allow_remove:
            raise ValueError(f"payments[{index}]: payment must not be null here")
        try:
            payment = parse_payment(raw["payment"]) if raw["payment"] is not None else None
        except ValueError as e:
            raise ValueError(f"payments[{index}]: {e}") from None
        changes.append(PaymentChange(income_source_id=raw["incomeSourceId"], payment=payment))
    return changes


def parse_one_time_line_args(payload: Mapping[str, Any]) -> OneTimeLineArgs:
    """Convert the body of a one-time line request into checked arguments. Raise ValueError for bad input.

    A body with no `lineId` creates a line. A body with `lineId` and `delete: true` deletes the line. Any other body
    with `lineId` changes the line.
    """

    raw_line_id = payload.get("lineId")
    if raw_line_id is None:
        return CreateOneTimeLineArgs(
            label=validate_label(payload.get("label")), amount=validate_one_time_amount(payload.get("amount"))
        )
    if not isinstance(raw_line_id, str) or not raw_line_id.startswith(ONE_TIME_LINE_PREFIX):
        raise ValueError("lineId of a one-time line is required")
    if payload.get("delete") is True:
        return DeleteOneTimeLineArgs(line_id=raw_line_id)
    return UpdateOneTimeLineArgs(
        line_id=raw_line_id,
        label=validate_label(payload.get("label")),
        amount=validate_one_time_amount(payload.get("amount")),
    )


def _as_int(value: Any, name: str) -> int:
    """Return `value` as an int. Accept an int or an integral Decimal. Reject a bool."""

    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    raise ValueError(f"{name} must be an integer")


def _parse_deduction(raw: Any, where: str) -> Deduction:
    """Convert one deduction of a payment into a Deduction. Raise ValueError if a rule fails."""

    if not isinstance(raw, Mapping):
        raise ValueError(f"{where} must be an object")
    try:
        kind = DeductionKind(raw.get("kind"))
    except ValueError:
        raise ValueError(f"{where}.kind must be one of {[k.value for k in DeductionKind]}") from None
    amount = _as_int(raw.get("amount"), f"{where}.amount")
    if amount < 0:
        raise ValueError(f"{where}.amount must not be negative")
    return Deduction(label=validate_label(raw.get("label"), f"{where}.label"), kind=kind, amount=amount)
