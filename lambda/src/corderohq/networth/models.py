"""The enums and the check functions for the net worth record.

The two enums are the load-bearing "eye on the future" decision from the design:
`AssetClass` is the key the eventual Monte Carlo simulation uses to select a
return distribution, and `AccountType` later drives tax/withdrawal rules. Both
live in code (not the DB) so the simulation feature can import the exact same
value space. The server validates incoming values against these enums and
rejects unknown ones with a 400.

Validation functions here are pure (no AWS) and raise ValueError on bad input,
matching the repo convention where table wrappers translate ValueError into a
400 at the handler boundary. The one thing NOT validated here is `owner`
foreign-key existence — that requires a Profile lookup and lives in the handler.
"""

from __future__ import annotations

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
    """Account types. Values are stored verbatim in `Account.accountType`.

    Member names are Python-identifier-safe; the string VALUES are the contract
    (DB storage, API payloads, and the sim's future tax/withdrawal-rule keys).
    Adding a value is a one-line change here.
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
    CREDIT_CARD = "credit_card"


class AssetClass(StrEnum):
    """Asset classes. Values are stored verbatim in `Account.assetClass`.

    In the simulation feature this is the key that maps to a return distribution
    (cash ~0% real, us_equity_large_cap lognormal with drift+vol, etc.), which is why it is
    a controlled enum rather than free text.
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
    # The class of every liability. Only a liability type has it, as its fixed class. An asset
    # account can never choose it.
    DEBT = "debt"


class AccountTypeMeta(NamedTuple):
    """The rules that each account type follows. This is the only definition of them.

    The account type is the "driver": it determines whether the account is a
    liability, whether its asset class is fixed (and to what) or user-chosen, and
    whether loan terms may be attached. Keeping this in code (not per-account
    input) is what makes "Mortgage implies liability" and "no US-equity mortgage"
    invariants rather than data the client could contradict.

    - liability: derived, never a client input.
    - fixed_asset_class: when set, the asset class is forced to this value and the
      client's choice is ignored. When None, the client picks from AssetClass.
      Liabilities pin this to `debt`. The financed asset, such as a house, is a
      separate account.
    - amortizing: whether loanTerms may be attached (mortgage, other liabilities).
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
    AccountType.MORTGAGE: AccountTypeMeta(liability=True, fixed_asset_class=AssetClass.DEBT, amortizing=True),
    AccountType.OTHER_ASSET: AccountTypeMeta(liability=False, fixed_asset_class=None, amortizing=False),
    AccountType.OTHER_LIABILITY: AccountTypeMeta(liability=True, fixed_asset_class=AssetClass.DEBT, amortizing=True),
    AccountType.CREDIT_CARD: AccountTypeMeta(liability=True, fixed_asset_class=AssetClass.DEBT, amortizing=False),
}

# The budget role of an account, for the CSV import. Money counts in the budget only when it
# crosses the boundary between the outside world, the spending accounts, and the holding
# accounts. An account with no role cannot take a CSV upload.
BUDGET_ROLE_SPENDING = "SPENDING"
BUDGET_ROLE_HOLDING = "HOLDING"
BUDGET_ROLES = frozenset({BUDGET_ROLE_SPENDING, BUDGET_ROLE_HOLDING})

_DEFAULT_BUDGET_ROLE: dict[AccountType, str] = {
    AccountType.CHECKING: BUDGET_ROLE_SPENDING,
    AccountType.CREDIT_CARD: BUDGET_ROLE_SPENDING,
    AccountType.SAVINGS: BUDGET_ROLE_HOLDING,
    AccountType.BROKERAGE: BUDGET_ROLE_HOLDING,
    AccountType.PLAN_529: BUDGET_ROLE_HOLDING,
    AccountType.PLAN_530A: BUDGET_ROLE_HOLDING,
}


def budget_role_of(account: dict[str, Any]) -> str | None:
    """Return the budget role of an account: its own `budgetRole`, else the default for its type."""
    explicit = account.get("budgetRole")
    if isinstance(explicit, str) and explicit in BUDGET_ROLES:
        return explicit
    account_type = account.get("accountType")
    if isinstance(account_type, str) and account_type in _ACCOUNT_TYPE_VALUES:
        return _DEFAULT_BUDGET_ROLE.get(AccountType(account_type))
    return None


def validate_budget_role(value: Any) -> str | None:
    """Return the budget role, or None to remove it (the account then uses its default)."""
    if value is None:
        return None
    if not isinstance(value, str) or value not in BUDGET_ROLES:
        raise ValueError(f"budgetRole must be one of {sorted(BUDGET_ROLES)} or null")
    return value


def validate_last_four(value: Any) -> list[str]:
    """Return the list of "last four digits" of an account, without duplicates, in the order given.

    Each entry is a string of exactly 4 digits. An empty list removes the attribute.
    """
    if not isinstance(value, list):
        raise ValueError("lastFour must be a list of 4-digit strings")
    result: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or len(entry) != 4 or not entry.isdigit():
            raise ValueError("each lastFour entry must be a string of exactly 4 digits")
        if entry not in result:
            result.append(entry)
    return result


def account_type_meta(account_type: str) -> AccountTypeMeta:
    """Return the rules for an account type. Raise an error if the type is unknown."""
    return _ACCOUNT_TYPE_META[AccountType(validate_account_type(account_type))]


def resolve_account_type_fields(
    account_type: Any, asset_classes: Any, loan_terms: Any
) -> tuple[str, bool, list[str], dict[str, Any] | None]:
    """Work out accountType, liability, assetClasses, and loanTerms. This is the only source.

    `liability` comes from the type, never the client. `assetClasses` is a non-empty
    set of classes: forced to a single value when the type fixes it (client value
    ignored), otherwise the client's list is validated against the enum. `loanTerms`
    is gated on the type's `amortizing` flag. Raises ValueError on any invalid or
    disallowed input; the table wrapper/handler surface that as a 400.
    """
    account_type = validate_account_type(account_type)
    meta = _ACCOUNT_TYPE_META[AccountType(account_type)]
    if meta.fixed_asset_class is not None:
        resolved_asset_classes = [str(meta.fixed_asset_class)]
    else:
        resolved_asset_classes = validate_asset_classes(asset_classes)
    normalized_loan_terms = validate_loan_terms(loan_terms, amortizing=meta.amortizing)
    return account_type, meta.liability, resolved_asset_classes, normalized_loan_terms


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
    """Check the `assetClasses` list on an account. Return a copy in the standard form.

    An account holds one or more asset classes (a set — see the per-value design).
    Rules: a non-empty list, each entry a valid AssetClass value, duplicates
    collapsed with original order preserved. Mirrors `validate_owners`.
    """
    if not isinstance(value, list) or not value:
        raise ValueError("assetClasses must be a non-empty list")
    seen: set[str] = set()
    result: list[str] = []
    for entry in value:
        validate_asset_class(entry)
        if entry == AssetClass.DEBT:
            raise ValueError("assetClasses cannot include 'debt'; only a liability type has it")
        if entry not in seen:
            seen.add(entry)
            result.append(entry)
    return result


def validate_loan_terms(loan_terms: Any, amortizing: bool) -> dict[str, Any] | None:
    """Check the loanTerms value, which is optional. Return a dict in the standard form, or None.

    Rules:
    - None/absent is always allowed and returns None.
    - Present but the account type is not `amortizing` → ValueError (loan terms
      only make sense on amortizing debts like a mortgage or personal loan).
    - All-or-nothing: if provided, all three fields (interestRate, monthlyPayment,
      payoffYearMonth) are required.
    - interestRate: a number in [0, 1) expressed as a decimal (0.04875 = 4.875%).
    - monthlyPayment: a non-negative integer in millionths of a dollar.
    - payoffYearMonth: a YYYY-MM string.

    The tracking feature never reads these back; they exist so the simulation can
    amortize liabilities. Validation is still strict so bad data can't silently
    land in the table.
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
    """Check the shape of the `owners` list on an account. Return a copy in the standard form.

    An account is owned by one or more household people (a jointly-held account is
    simply two+ owners — there is no separate "joint" sentinel). Rules:
    - must be a list with at least one entry,
    - every entry a non-empty string (a personId),
    - duplicates collapsed, original order preserved.

    Foreign-key existence of each personId against the household is NOT checked
    here — that needs a Profile lookup and lives in the handler, the same split as
    the enums' value-space-vs-existence separation.
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


def resolve_target_year(asset_classes: list[str], target_year: Any) -> int | None:
    """Check that target_date and targetYear agree. Return the value to store, or None.

    `targetYear` is required and valid exactly when `target_date` is one of the
    account's asset classes; otherwise it must be absent/None. Mirrors the
    loanTerms "present only when relevant" rule.
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
