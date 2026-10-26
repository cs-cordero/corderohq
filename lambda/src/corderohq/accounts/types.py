from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, NamedTuple

from corderohq.field_changes import FieldChange, Keep, SetTo


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
    CREDIT_CARD = "credit_card"


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
    # The class of every liability. Only a liability type has it, as its fixed class. An asset
    # account can never choose it.
    DEBT = "debt"


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
    AccountType.MORTGAGE: AccountTypeMeta(is_liability=True, fixed_asset_class=AssetClass.DEBT, amortizing=True),
    AccountType.OTHER_ASSET: AccountTypeMeta(is_liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.OTHER_LIABILITY: AccountTypeMeta(is_liability=True, fixed_asset_class=AssetClass.DEBT, amortizing=True),
    AccountType.CREDIT_CARD: AccountTypeMeta(is_liability=True, fixed_asset_class=AssetClass.DEBT, amortizing=False),
}


def get_metadata_for_account_type(account_type: str) -> AccountTypeMeta:
    """Return the rules for an account type. Raise ValueError if the type is unknown."""

    return _ACCOUNT_TYPE_META[AccountType(account_type)]


class BudgetRole(StrEnum):
    """The budget role of an account, for the CSV import.

    Money counts in the budget only when it crosses the boundary between the outside world, the
    spending accounts, and the holding accounts. An account with no role cannot take a CSV upload.
    """

    SPENDING = "SPENDING"  # checking, a credit card: expenses come from here
    HOLDING = "HOLDING"  # a savings fund, a brokerage, a 529: money sits here


_DEFAULT_BUDGET_ROLE: dict[AccountType, BudgetRole] = {
    AccountType.CHECKING: BudgetRole.SPENDING,
    AccountType.CREDIT_CARD: BudgetRole.SPENDING,
    AccountType.SAVINGS: BudgetRole.HOLDING,
    AccountType.BROKERAGE: BudgetRole.HOLDING,
    AccountType.PLAN_529: BudgetRole.HOLDING,
    AccountType.PLAN_530A: BudgetRole.HOLDING,
}


def budget_role_of(account: Mapping[str, Any]) -> BudgetRole | None:
    """Return the budget role of a stored account: its own `budgetRole`, else the default for its type."""

    explicit = account.get("budgetRole")
    if isinstance(explicit, str) and explicit in BudgetRole:
        return BudgetRole(explicit)
    account_type = account.get("accountType")
    if isinstance(account_type, str) and account_type in AccountType:
        return _DEFAULT_BUDGET_ROLE.get(AccountType(account_type))
    return None


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

    def to_item(self) -> dict[str, Any]:
        """Converts an instance of FixedClassAccount to how it is stored in DynamoDB."""

        meta = get_metadata_for_account_type(self.account_type)
        return {
            "accountType": self.account_type,
            "assetClasses": [meta.fixed_asset_class],
            "liability": meta.is_liability,
        }


@dataclass(frozen=True)
class ChosenClassAccount:
    """A new asset account whose asset classes the user chooses, such as a brokerage account."""

    account_type: AccountType
    asset_classes: list[AssetClass]
    target_year: int | None  # set only when asset_classes holds TARGET_DATE

    def to_item(self) -> dict[str, Any]:
        """Converts an instance of ChosenClassAccount to how it is stored in DynamoDB."""

        item: dict[str, Any] = {
            "accountType": self.account_type,
            "assetClasses": list(self.asset_classes),
            "liability": False,
        }
        if self.target_year is not None:
            item["targetYear"] = self.target_year
        return item


@dataclass(frozen=True)
class LoanAccount:
    """A new amortizing liability, such as a mortgage."""

    account_type: AccountType
    loan_terms: LoanTerms | None

    def to_item(self) -> dict[str, Any]:
        """Converts an instance of LoanAccount to how it is stored in DynamoDB."""

        item: dict[str, Any] = {
            "accountType": self.account_type,
            "assetClasses": [get_metadata_for_account_type(self.account_type).fixed_asset_class],
            "liability": True,
        }
        if self.loan_terms is not None:
            item["loanTerms"] = self.loan_terms.to_item()
        return item


NewAccount = FixedClassAccount | ChosenClassAccount | LoanAccount


@dataclass(frozen=True)
class CreateAccountArgs:
    """The checked arguments of a new account."""

    name: str
    owners: list[str]
    excluded_from_net_worth: bool
    notes: str | None
    account: NewAccount


@dataclass(frozen=True)
class UpdateAccountArgs:
    """The checked changes to an account. Each field that the request did not name is Keep()."""

    name: Keep | SetTo[str]
    owners: Keep | SetTo[list[str]]
    excluded_from_net_worth: Keep | SetTo[bool]
    sort_order: Keep | SetTo[int]
    notes: FieldChange[str]
    asset_classes: Keep | SetTo[list[AssetClass]]
    target_year: FieldChange[int]
    loan_terms: FieldChange[LoanTerms]
    budget_role: FieldChange[BudgetRole]
    last_four: FieldChange[list[str]]
