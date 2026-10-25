from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NamedTuple

from corderohq.util import YEAR_MONTH_PATTERN

# The keys for the Profile table. `HOUSEHOLD_PK` is the one partition key, because there
# is one household in practice. `PROFILE_SETTINGS_SK` is a reserved sort key, kept for a
# settings item for the whole household later, such as the horizon age of the simulation.
# The sort key for a person is a ULID, so it never equals that reserved value. These two
# names are the only definitions. The table wrapper imports them, and does not repeat them.
HOUSEHOLD_PK = "HOUSEHOLD"
PROFILE_SETTINGS_SK = "SETTINGS"

_LOAN_TERMS_FIELDS = ("interestRate", "monthlyPayment", "payoffYearMonth")


class AccountType(StrEnum):
    """Account types. The value is stored in `Account.accountType`.

    The values are a contract: the database, the API, and the later simulation use them.
    """

    CHECKING = "checking"
    SAVINGS = "savings"
    BROKERAGE = "brokerage"
    RETIREMENT_401K = "401k"
    RETIREMENT_403B = "403b"
    ROTH_IRA = "roth_ira"
    TRADITIONAL_IRA = "traditional_ira"
    HSA = "hsa"
    PLAN_529 = "529"
    PLAN_530A = "530a"
    REAL_ESTATE = "real_estate"
    MORTGAGE = "mortgage"
    VEHICLE = "vehicle"
    OTHER_ASSET = "other_asset"
    OTHER_LIABILITY = "other_liability"


class AssetClass(StrEnum):
    """Asset classes. The value is stored in `Account.assetClasses`.

    The later simulation uses each value as the key of a return distribution.
    """

    CASH = "cash"
    US_EQUITY_LARGE_CAP = "us_equity_large_cap"
    US_EQUITY_SMALL_CAP = "us_equity_small_cap"
    INTL_EQUITY = "intl_equity"
    BONDS = "bonds"
    FIXED_INCOME = "fixed_income"
    REAL_ESTATE = "real_estate"
    TARGET_DATE = "target_date"
    OTHER = "other"


class AccountTypeMeta(NamedTuple):
    """The rules of one account type.

    - liability: True for a liability. The type decides it, and the client never sends it.
    - fixed_asset_class: the one asset class of the type, or None when the user chooses the classes.
    - amortizing: True when the account can have loan terms.
    """

    liability: bool
    fixed_asset_class: AssetClass | None
    amortizing: bool


_ACCOUNT_TYPE_META: dict[AccountType, AccountTypeMeta] = {
    AccountType.CHECKING: AccountTypeMeta(liability=False, fixed_asset_class=AssetClass.CASH, amortizing=False),
    AccountType.SAVINGS: AccountTypeMeta(liability=False, fixed_asset_class=AssetClass.CASH, amortizing=False),
    AccountType.BROKERAGE: AccountTypeMeta(liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.RETIREMENT_401K: AccountTypeMeta(liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.RETIREMENT_403B: AccountTypeMeta(liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.ROTH_IRA: AccountTypeMeta(liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.TRADITIONAL_IRA: AccountTypeMeta(liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.HSA: AccountTypeMeta(liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.PLAN_529: AccountTypeMeta(liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.PLAN_530A: AccountTypeMeta(liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.REAL_ESTATE: AccountTypeMeta(
        liability=False, fixed_asset_class=AssetClass.REAL_ESTATE, amortizing=False
    ),
    AccountType.VEHICLE: AccountTypeMeta(liability=False, fixed_asset_class=AssetClass.OTHER, amortizing=False),
    AccountType.MORTGAGE: AccountTypeMeta(liability=True, fixed_asset_class=AssetClass.OTHER, amortizing=True),
    AccountType.OTHER_ASSET: AccountTypeMeta(liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.OTHER_LIABILITY: AccountTypeMeta(liability=True, fixed_asset_class=AssetClass.OTHER, amortizing=True),
}


def account_type_meta(account_type: str) -> AccountTypeMeta:
    """Return the rules for an account type. Raise an error if the type is unknown."""

    return _ACCOUNT_TYPE_META[AccountType(validate_account_type(account_type))]


@dataclass(frozen=True)
class LoanTerms:
    """The terms of an amortizing loan."""

    interest_rate: float  # a decimal in [0, 1), such as 0.04875 for 4.875%
    monthly_payment: int  # millionths of a dollar
    payoff_year_month: str  # YYYY-MM


@dataclass(frozen=True)
class FixedClassAccount:
    """A new account whose type fixes its asset class, such as a checking account."""

    account_type: AccountType
    asset_class: AssetClass
    liability: bool


@dataclass(frozen=True)
class ChosenClassAccount:
    """A new asset account whose asset classes the user chooses, such as a brokerage account."""

    account_type: AccountType
    asset_classes: list[AssetClass]
    target_year: int | None  # set only when asset_classes holds TARGET_DATE


@dataclass(frozen=True)
class LoanAccount:
    """A new amortizing liability, such as a mortgage."""

    account_type: AccountType
    asset_class: AssetClass
    loan_terms: LoanTerms | None


NewAccount = FixedClassAccount | ChosenClassAccount | LoanAccount


def parse_new_account(account_type: Any, asset_classes: Any, target_year: Any, loan_terms: Any) -> NewAccount:
    """Convert the type fields of a create request into the variant for the account type.

    A fixed-class type ignores `asset_classes`. Raise ValueError for an unknown type or class, for
    loan terms on a type that does not amortize, and for a target year without TARGET_DATE.
    """

    kind = AccountType(validate_account_type(account_type))
    meta = _ACCOUNT_TYPE_META[kind]
    if meta.fixed_asset_class is None:
        parse_loan_terms(loan_terms, amortizing=False)
        classes = [AssetClass(c) for c in validate_asset_classes(asset_classes)]
        return ChosenClassAccount(kind, classes, resolve_target_year(classes, target_year))
    resolve_target_year([meta.fixed_asset_class], target_year)
    if meta.amortizing:
        return LoanAccount(kind, meta.fixed_asset_class, parse_loan_terms(loan_terms, amortizing=True))
    parse_loan_terms(loan_terms, amortizing=False)
    return FixedClassAccount(kind, meta.fixed_asset_class, meta.liability)


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


def validate_birth_year_month(value: Any) -> str:
    """Return `value` if it is a YYYY-MM string, else raise ValueError."""

    if not isinstance(value, str) or not YEAR_MONTH_PATTERN.match(value):
        raise ValueError("birthYearMonth must be a YYYY-MM string")
    return value


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


_ACCOUNT_TYPE_VALUES = frozenset(t.value for t in AccountType)
_ASSET_CLASS_VALUES = frozenset(c.value for c in AssetClass)
