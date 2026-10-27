from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, assert_never

from corderohq.accounts import validate_owners
from corderohq.common import Keep, SetTo, parse_optional_str

# The prefixes of the sort key of the PlannedIncome table.
RECURRING_INCOME_PREFIX = "REC#"
ONE_TIME_INCOME_PREFIX = "ONE#"

LABEL_MAX_LENGTH = 100

_EDITABLE_INCOME_SOURCE_KEYS = ("label", "owners")


class PlannedIncomeKind(StrEnum):
    """The kind of a planned income."""

    RECURRING = "RECURRING"
    ONE_TIME = "ONE_TIME"


class PayFrequency(StrEnum):
    """How often a recurring income pays."""

    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    SEMIMONTHLY = "semimonthly"
    MONTHLY = "monthly"

    @property
    def times_per_month(self) -> int:
        """The number of times that the plan counts the income in each month.

        Budgeting is a monthly endeavor. Pay frequencies of bi-weekly and weekly do not fit this cleanly: some months
        in a bi-weekly schedule will receive 3 payments instead of 2. Some months in a weekly schedule will receive 5
        payments instead of 4. For smoothing purposes, we floor the times_per_month for all frequencies, which provides
        a consistent assumed income from month to month.
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
    """Amounts that reduce gross pay immediately from a paycheck."""

    # A tax that is withheld from the payment, such as federal income tax or Social Security tax.
    TAX = "TAX"
    # Savings that come out before tax, such as a traditional 401k contribution or an HSA contribution.
    PRETAX_SAVINGS = "PRETAX_SAVINGS"
    # A benefit that is paid for before tax, such as a medical insurance premium or a DCFSA contribution.
    PRETAX_BENEFIT = "PRETAX_BENEFIT"
    # Savings that come out after tax, such as a Roth 401k contribution.
    POSTTAX_SAVINGS = "POSTTAX_SAVINGS"


class RecurringIncomeAction(StrEnum):
    """Identifies the type of update a user can make to a RecurringIncome."""

    SET = "SET"
    STOP = "STOP"  # stop the recurring income from this month on
    UNPIN = "UNPIN"  # remove the entry of a future month, which is a pin or a stop


@dataclass(frozen=True)
class Deduction:
    """One deduction from one payment of an income, such as a tax."""

    label: str
    kind: DeductionKind
    amount: int  # millionths of a dollar

    def to_item(self) -> dict[str, Any]:
        """Converts an instance of Deduction to how it is stored in DynamoDB."""

        return {"label": self.label, "kind": self.kind, "amount": self.amount}


@dataclass(frozen=True)
class RecurringIncome:
    """An income that repeats each month, such as a salary or the rent of a property.

    `gross` and each deduction are for one payment. The frequency sets the number of payments in the month.
    """

    income_source_id: str
    label: str
    frequency: PayFrequency
    gross: int  # millionths of a dollar
    deductions: tuple[Deduction, ...]

    @property
    def net(self) -> int:
        """The net amount of one payment: the gross amount minus every deduction."""

        return self.gross - sum(deduction.amount for deduction in self.deductions)

    @property
    def monthly_net(self) -> int:
        """The net amount that the plan counts in one month."""

        return self.net * self.frequency.times_per_month

    def to_item(self) -> dict[str, Any]:
        """Converts an instance of RecurringIncome to how it is stored in DynamoDB."""

        return {
            "incomeSourceId": self.income_source_id,
            "label": self.label,
            "frequency": self.frequency,
            "gross": self.gross,
            "deductions": [deduction.to_item() for deduction in self.deductions],
        }


@dataclass(frozen=True)
class OneTimeIncome:
    """An income in one month only, such as a bonus or an RSU vest. It never carries forward."""

    label: str
    income_source_id: str | None
    gross: int  # millionths of a dollar
    deductions: tuple[Deduction, ...]

    @property
    def net(self) -> int:
        """The net amount: the gross amount minus every deduction."""

        return self.gross - sum(deduction.amount for deduction in self.deductions)

    def to_item(self) -> dict[str, Any]:
        """Converts an instance of OneTimeIncome to how it is stored in DynamoDB."""

        item: dict[str, Any] = {
            "label": self.label,
            "gross": self.gross,
            "deductions": [deduction.to_item() for deduction in self.deductions],
        }
        if self.income_source_id is not None:
            item["incomeSourceId"] = self.income_source_id
        return item


@dataclass(frozen=True)
class CreateIncomeSourceArgs:
    """The checked arguments of a new income source."""

    owners: list[str]
    label: str


@dataclass(frozen=True)
class UpdateIncomeSourceArgs:
    """The checked changes to an income source. Each field that the request did not name is Keep()."""

    label: Keep | SetTo[str]
    owners: Keep | SetTo[list[str]]


@dataclass(frozen=True)
class SetRecurringIncome:
    """Set the value of a recurring income in one month. A `planned_income_id` of None creates a new one."""

    planned_income_id: str | None
    income: RecurringIncome


@dataclass(frozen=True)
class StopRecurringIncome:
    """Stop a recurring income from one month on."""

    planned_income_id: str


@dataclass(frozen=True)
class UnpinRecurringIncome:
    """Remove the entry of a recurring income in a future month, which is a pin or a stop."""

    planned_income_id: str


type RecurringIncomeChange = SetRecurringIncome | StopRecurringIncome | UnpinRecurringIncome


@dataclass(frozen=True)
class CreateOneTimeIncomeArgs:
    """The checked arguments of a new one-time income."""

    income: OneTimeIncome


@dataclass(frozen=True)
class UpdateOneTimeIncomeArgs:
    """The checked new value of a one-time income."""

    planned_income_id: str
    income: OneTimeIncome


@dataclass(frozen=True)
class DeleteOneTimeIncomeArgs:
    """The one-time income to delete."""

    planned_income_id: str


type OneTimeIncomeArgs = CreateOneTimeIncomeArgs | UpdateOneTimeIncomeArgs | DeleteOneTimeIncomeArgs


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


def parse_recurring_income(raw: Any) -> RecurringIncome:
    """Convert a recurring income from a request body or a stored item. Raise ValueError if a rule fails.

    The shape is `{incomeSourceId, label, frequency, gross, deductions: [{label, kind, amount}]}`. A value read back
    from DynamoDB is a `Decimal`, so an integral `Decimal` is accepted. The net amount must not be negative. It does
    not check that the income source exists.
    """

    if not isinstance(raw, Mapping):
        raise ValueError("recurringIncome must be an object")
    income_source_id = raw.get("incomeSourceId")
    if not isinstance(income_source_id, str) or not income_source_id:
        raise ValueError("incomeSourceId is required")
    gross, deductions = _parse_money(raw)
    return RecurringIncome(
        income_source_id=income_source_id,
        label=validate_label(raw.get("label")),
        frequency=validate_frequency(raw.get("frequency")),
        gross=gross,
        deductions=deductions,
    )


def parse_one_time_income(raw: Any) -> OneTimeIncome:
    """Convert a one-time income from a request body or a stored item. Raise ValueError if a rule fails.

    The shape is `{label, incomeSourceId?, gross, deductions: [{label, kind, amount}]}`. The net amount must not be
    negative. It does not check that the income source exists.
    """

    if not isinstance(raw, Mapping):
        raise ValueError("a one-time income must be an object")
    gross, deductions = _parse_money(raw)
    return OneTimeIncome(
        label=validate_label(raw.get("label")),
        income_source_id=parse_optional_str(raw.get("incomeSourceId"), "incomeSourceId"),
        gross=gross,
        deductions=deductions,
    )


def parse_create_income_source_args(payload: Mapping[str, Any]) -> CreateIncomeSourceArgs:
    """Convert the body of a create request into checked arguments. Raise ValueError for bad input.

    It does not check that each owner exists.
    """

    return CreateIncomeSourceArgs(
        owners=validate_owners(payload.get("owners")), label=validate_label(payload.get("label"))
    )


def parse_update_income_source_args(payload: Mapping[str, Any]) -> UpdateIncomeSourceArgs:
    """Convert the body of an update request into checked changes. Raise ValueError for bad input.

    An absent key changes nothing. It does not check that each owner exists.
    """

    if not any(key in payload for key in _EDITABLE_INCOME_SOURCE_KEYS):
        raise ValueError(f"at least one editable field is required: {list(_EDITABLE_INCOME_SOURCE_KEYS)}")
    return UpdateIncomeSourceArgs(
        label=SetTo(validate_label(payload["label"])) if "label" in payload else Keep(),
        owners=SetTo(validate_owners(payload["owners"])) if "owners" in payload else Keep(),
    )


def parse_recurring_income_changes(payload: Mapping[str, Any]) -> list[RecurringIncomeChange]:
    """Convert `recurring`, a list of `{action, plannedIncomeId?, recurringIncome?}`, into checked changes.

    Raise ValueError for bad input. SET needs `recurringIncome`, and creates a new recurring income when it has no
    `plannedIncomeId`. STOP and UNPIN need `plannedIncomeId`. It does not check that an id or an income source exists.
    """

    raw_rows = payload.get("recurring")
    if not isinstance(raw_rows, list):
        raise ValueError("recurring array is required")
    changes: list[RecurringIncomeChange] = []
    for index, raw in enumerate(raw_rows):
        try:
            changes.append(_parse_recurring_income_change(raw))
        except ValueError as e:
            raise ValueError(f"recurring[{index}]: {e}") from None
    return changes


def parse_one_time_income_args(payload: Mapping[str, Any]) -> OneTimeIncomeArgs:
    """Convert the body of a one-time income request into checked arguments. Raise ValueError for bad input.

    A body with no `plannedIncomeId` creates a one-time income. A body with `plannedIncomeId` and `delete: true`
    deletes it. Any other body with `plannedIncomeId` changes it.
    """

    planned_income_id = payload.get("plannedIncomeId")
    if planned_income_id is None:
        return CreateOneTimeIncomeArgs(income=parse_one_time_income(payload))
    if not isinstance(planned_income_id, str) or not planned_income_id.startswith(ONE_TIME_INCOME_PREFIX):
        raise ValueError("plannedIncomeId of a one-time income is required")
    if payload.get("delete") is True:
        return DeleteOneTimeIncomeArgs(planned_income_id=planned_income_id)
    return UpdateOneTimeIncomeArgs(planned_income_id=planned_income_id, income=parse_one_time_income(payload))


def _parse_recurring_income_change(raw: Any) -> RecurringIncomeChange:
    """Convert one entry of `recurring` into a checked change. Raise ValueError for bad input."""

    if not isinstance(raw, Mapping):
        raise ValueError("each entry must be an object")
    try:
        action = RecurringIncomeAction(raw.get("action"))
    except ValueError:
        raise ValueError(f"action must be one of {[a.value for a in RecurringIncomeAction]}") from None
    planned_income_id = raw.get("plannedIncomeId")
    if planned_income_id is not None and (
        not isinstance(planned_income_id, str) or not planned_income_id.startswith(RECURRING_INCOME_PREFIX)
    ):
        raise ValueError("plannedIncomeId must be the id of a recurring income")
    match action:
        case RecurringIncomeAction.SET:
            return SetRecurringIncome(
                planned_income_id=planned_income_id, income=parse_recurring_income(raw.get("recurringIncome"))
            )
        case RecurringIncomeAction.STOP:
            if planned_income_id is None:
                raise ValueError("STOP needs plannedIncomeId")
            return StopRecurringIncome(planned_income_id=planned_income_id)
        case RecurringIncomeAction.UNPIN:
            if planned_income_id is None:
                raise ValueError("UNPIN needs plannedIncomeId")
            return UnpinRecurringIncome(planned_income_id=planned_income_id)
        case _:
            assert_never(action)


def _parse_money(raw: Mapping[str, Any]) -> tuple[int, tuple[Deduction, ...]]:
    """Return the checked gross amount and deductions of an income. Raise ValueError if a rule fails."""

    gross = _as_int(raw.get("gross"), "gross")
    if gross < 0:
        raise ValueError("gross must not be negative")
    raw_deductions = raw.get("deductions", [])
    if not isinstance(raw_deductions, list):
        raise ValueError("deductions must be a list")
    deductions = tuple(
        _parse_deduction(raw_deduction, f"deductions[{index}]") for index, raw_deduction in enumerate(raw_deductions)
    )
    if gross - sum(deduction.amount for deduction in deductions) < 0:
        raise ValueError("the deductions must not be more than the gross amount")
    return gross, deductions


def _parse_deduction(raw: Any, where: str) -> Deduction:
    """Convert one deduction into a Deduction. Raise ValueError if a rule fails."""

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


def _as_int(value: Any, name: str) -> int:
    """Return `value` as an int. Accept an int or an integral Decimal. Reject a bool."""

    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    raise ValueError(f"{name} must be an integer")
