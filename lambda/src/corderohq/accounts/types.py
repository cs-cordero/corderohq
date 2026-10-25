from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple


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
    """Return the rules for an account type. Raise ValueError if the type is unknown."""

    return _ACCOUNT_TYPE_META[AccountType(account_type)]


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
