from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, NamedTuple


class AccountType(StrEnum):
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
    """Additional metadata for each AccountType."""

    # Whether this AccountType represents a liability for the owner.
    is_liability: bool
    # Some AccountTypes are inexorably tied to a particular AssetClass. For example, checking and
    # savings accounts are unambiguously cash accounts. This field associates those certain
    # AccountTypes with those AssetClasses they are tied to. This field takes the value None when
    # the Account can differ in the AssetClasses stored within it.
    fixed_asset_class: AssetClass | None
    # True when the account can have loan terms.
    amortizing: bool


_ACCOUNT_TYPE_META: dict[AccountType, AccountTypeMeta] = {
    AccountType.CHECKING: AccountTypeMeta(is_liability=False, fixed_asset_class=AssetClass.CASH, amortizing=False),
    AccountType.SAVINGS: AccountTypeMeta(is_liability=False, fixed_asset_class=AssetClass.CASH, amortizing=False),
    AccountType.BROKERAGE: AccountTypeMeta(is_liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.RETIREMENT_401K: AccountTypeMeta(is_liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.RETIREMENT_403B: AccountTypeMeta(is_liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.ROTH_IRA: AccountTypeMeta(is_liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.TRADITIONAL_IRA: AccountTypeMeta(is_liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.HSA: AccountTypeMeta(is_liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.PLAN_529: AccountTypeMeta(is_liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.PLAN_530A: AccountTypeMeta(is_liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.REAL_ESTATE: AccountTypeMeta(
        is_liability=False, fixed_asset_class=AssetClass.REAL_ESTATE, amortizing=False
    ),
    AccountType.VEHICLE: AccountTypeMeta(is_liability=False, fixed_asset_class=AssetClass.OTHER, amortizing=False),
    AccountType.MORTGAGE: AccountTypeMeta(is_liability=True, fixed_asset_class=AssetClass.OTHER, amortizing=True),
    AccountType.OTHER_ASSET: AccountTypeMeta(is_liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.OTHER_LIABILITY: AccountTypeMeta(
        is_liability=True, fixed_asset_class=AssetClass.OTHER, amortizing=True
    ),
}


def get_metadata_for_account_type(account_type: str) -> AccountTypeMeta:
    """Return the rules for an account type. Raise ValueError if the type is unknown."""

    return _ACCOUNT_TYPE_META[AccountType(account_type)]


@dataclass(frozen=True)
class LoanTerms:
    """The terms of an amortizing loan."""

    interest_rate: float  # a decimal in [0, 1), such as 0.04875 for 4.875%
    monthly_payment: int  # millionths of a dollar
    payoff_year_month: str  # YYYY-MM

    def to_item(self) -> dict[str, Any]:
        """Converts an instance of LoanTerms to how it is stored in DynamoDB."""

        return {
            # DynamoDB rejects floats.
            "interestRate": Decimal(str(self.interest_rate)),
            "monthlyPayment": self.monthly_payment,
            "payoffYearMonth": self.payoff_year_month,
        }


@dataclass(frozen=True)
class FixedClassAccount:
    """A new account whose type fixes its asset class, such as a checking account."""

    account_type: AccountType


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
    loan_terms: LoanTerms | None


NewAccount = FixedClassAccount | ChosenClassAccount | LoanAccount


@dataclass(frozen=True)
class CreateAccountArgs:
    """The checked arguments of a new account."""

    name: str
    owners: list[str]
    excluded_from_net_worth: bool
    notes: str | None
    account: NewAccount
