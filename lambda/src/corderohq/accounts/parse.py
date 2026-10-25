from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, assert_never

from corderohq.accounts.types import (
    AccountType,
    AssetClass,
    ChosenClassAccount,
    CreateAccountArgs,
    FixedClassAccount,
    LoanAccount,
    LoanTerms,
    NewAccount,
)
from corderohq.util import YEAR_MONTH_PATTERN


_LOAN_TERMS_FIELDS = ("interestRate", "monthlyPayment", "payoffYearMonth")
_ACCOUNT_TYPE_VALUES = frozenset(t.value for t in AccountType)
_ASSET_CLASS_VALUES = frozenset(c.value for c in AssetClass)


def parse_create_account_args(payload: Mapping[str, Any]) -> CreateAccountArgs:
    """Convert the body of a create request into checked arguments. Raise ValueError for bad input.

    The account type decides which other keys the body can hold. A type with a fixed asset class
    ignores `assetClasses`. The caller checks that each owner exists.
    """

    account_type = AccountType(validate_account_type(payload.get("accountType")))
    account: NewAccount
    match account_type:
        case AccountType.CHECKING | AccountType.SAVINGS | AccountType.REAL_ESTATE | AccountType.VEHICLE:
            _reject_keys(payload, account_type, "targetYear", "loanTerms")
            account = FixedClassAccount(account_type)
        case (
            AccountType.BROKERAGE
            | AccountType.RETIREMENT_401K
            | AccountType.RETIREMENT_403B
            | AccountType.ROTH_IRA
            | AccountType.TRADITIONAL_IRA
            | AccountType.HSA
            | AccountType.PLAN_529
            | AccountType.PLAN_530A
            | AccountType.OTHER_ASSET
        ):
            _reject_keys(payload, account_type, "loanTerms")
            asset_classes = [AssetClass(c) for c in validate_asset_classes(payload.get("assetClasses"))]
            target_year = resolve_target_year(asset_classes, payload.get("targetYear"))
            account = ChosenClassAccount(account_type, asset_classes, target_year)
        case AccountType.MORTGAGE | AccountType.OTHER_LIABILITY:
            _reject_keys(payload, account_type, "targetYear")
            account = LoanAccount(account_type, parse_loan_terms(payload.get("loanTerms"), amortizing=True))
        case _:
            assert_never(account_type)

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    excluded = payload.get("excludedFromNetWorth", False)
    if not isinstance(excluded, bool):
        raise ValueError("excludedFromNetWorth must be a boolean")
    notes = payload.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise ValueError("notes must be a string or null")
    return CreateAccountArgs(
        name=name.strip(),
        owners=validate_owners(payload.get("owners")),
        excluded_from_net_worth=excluded,
        notes=notes,
        account=account,
    )


def _reject_keys(payload: Mapping[str, Any], account_type: AccountType, *keys: str) -> None:
    """Raise ValueError if the body holds a value for a key that this account type does not take."""

    for key in keys:
        if payload.get(key) is not None:
            raise ValueError(f"{key} is not allowed for a {account_type} account")


def parse_loan_terms(value: Any, amortizing: bool) -> LoanTerms | None:
    """Convert the `loanTerms` of a request into LoanTerms, or None if it is absent.

    Raise ValueError for bad input, and for any loan terms on a type that does not amortize.
    """

    terms = validate_loan_terms(value, amortizing=amortizing)
    if terms is None:
        return None
    return LoanTerms(terms["interestRate"], terms["monthlyPayment"], terms["payoffYearMonth"])


def validate_account_type(value: Any) -> str:
    """Return `value` if it is a valid AccountType string, else raise ValueError."""

    if not isinstance(value, str) or value not in _ACCOUNT_TYPE_VALUES:
        raise ValueError(f"Invalid accountType '{value}'; expected one of {sorted(_ACCOUNT_TYPE_VALUES)}")
    return value


def validate_asset_class(value: Any) -> str:
    """Return `value` if it is a valid AssetClass string, else raise ValueError."""

    if not isinstance(value, str) or value not in _ASSET_CLASS_VALUES:
        raise ValueError(f"Invalid assetClass '{value}'; expected one of {sorted(_ASSET_CLASS_VALUES)}")
    return value


def validate_asset_classes(value: Any) -> list[str]:
    """Check an `assetClasses` list, and return it without duplicates, in the order given.

    The list must not be empty, and each entry must be an AssetClass value. Raise ValueError for bad input.
    """

    if not isinstance(value, list) or not value:
        raise ValueError("assetClasses must be a non-empty list")
    seen: set[str] = set()
    result: list[str] = []
    for entry in value:
        validate_asset_class(entry)
        if entry not in seen:
            seen.add(entry)
            result.append(entry)
    return result


def validate_loan_terms(loan_terms: Any, amortizing: bool) -> dict[str, Any] | None:
    """Check a `loanTerms` value. Return it in the standard form, or None if it is absent.

    - Loan terms on a type that does not amortize raise ValueError.
    - All three fields are required: `interestRate`, a decimal in [0, 1), such as 0.04875 for 4.875%; `monthlyPayment`,
      a non-negative integer in millionths of a dollar; and `payoffYearMonth`, a YYYY-MM string.
    """

    if loan_terms is None:
        return None
    if not isinstance(loan_terms, dict):
        raise ValueError("loanTerms must be an object")
    if not amortizing:
        raise ValueError("loanTerms is only allowed on amortizing accounts (e.g. mortgage, personal loan)")

    missing = [f for f in _LOAN_TERMS_FIELDS if f not in loan_terms]
    if missing:
        raise ValueError(f"loanTerms requires all of {list(_LOAN_TERMS_FIELDS)}; missing {missing}")
    extra = [k for k in loan_terms if k not in _LOAN_TERMS_FIELDS]
    if extra:
        raise ValueError(f"loanTerms has unexpected fields {extra}; allowed {list(_LOAN_TERMS_FIELDS)}")

    interest_rate = loan_terms["interestRate"]
    # In Python a bool is a kind of int. Reject it here, so that `True` cannot act as a rate.
    if isinstance(interest_rate, bool) or not isinstance(interest_rate, (int, float)):
        raise ValueError("loanTerms.interestRate must be a number")
    if not (0 <= interest_rate < 1):
        raise ValueError("loanTerms.interestRate must be a decimal in [0, 1), e.g. 0.04875 for 4.875%")

    monthly_payment = loan_terms["monthlyPayment"]
    if isinstance(monthly_payment, bool) or not isinstance(monthly_payment, int):
        raise ValueError("loanTerms.monthlyPayment must be an integer (millionths of a dollar)")
    if monthly_payment < 0:
        raise ValueError("loanTerms.monthlyPayment must be non-negative")

    payoff = loan_terms["payoffYearMonth"]
    if not isinstance(payoff, str) or not YEAR_MONTH_PATTERN.match(payoff):
        raise ValueError("loanTerms.payoffYearMonth must be a YYYY-MM string")

    return {
        "interestRate": interest_rate,
        "monthlyPayment": monthly_payment,
        "payoffYearMonth": payoff,
    }


def validate_owners(value: Any) -> list[str]:
    """Check an `owners` list, and return it without duplicates, in the order given.

    The list must not be empty, and each entry must be a non-empty personId. The caller checks that each person exists.
    Raise ValueError for bad input.
    """

    if not isinstance(value, list) or not value:
        raise ValueError("owners must be a non-empty list of personIds")
    seen: set[str] = set()
    result: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry:
            raise ValueError("each owner must be a non-empty personId string")
        if entry not in seen:
            seen.add(entry)
            result.append(entry)
    return result


# A target-date fund comes in a 5-year step, and the UI shows a list of them. The server
# checks only that the year has 4 digits and is possible. A fund can be past its target
# date, so the year does not have to be in the future.
def validate_target_year(value: Any) -> int:
    """Return `value` if it is a plausible 4-digit target year, else raise ValueError."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("targetYear must be an integer year")
    if not (1990 <= value <= 2100):
        raise ValueError("targetYear must be a 4-digit year in [1990, 2100]")
    return value


def resolve_target_year(asset_classes: Sequence[str], target_year: Any) -> int | None:
    """Check that the asset classes and the target year agree. Return the year to store, or None.

    The year is required when the classes hold `target_date`, and not permitted when they do not. Raise ValueError if
    they do not agree.
    """

    if "target_date" in asset_classes:
        if target_year is None:
            raise ValueError("targetYear is required when target_date is one of the asset classes")
        return validate_target_year(target_year)
    if target_year is not None:
        raise ValueError("targetYear is only allowed when target_date is an asset class")
    return None
