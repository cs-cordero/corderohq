from typing import Any

import pytest

from corderohq.networth.models import (
    AccountType,
    AssetClass,
    ChosenClassAccount,
    FixedClassAccount,
    LoanAccount,
    LoanTerms,
    account_type_meta,
    parse_loan_terms,
    parse_new_account,
    resolve_target_year,
    validate_account_type,
    validate_asset_class,
    validate_asset_classes,
    validate_birth_year_month,
    validate_loan_terms,
    validate_owners,
    validate_target_year,
)


class TestEnumValues:
    def test_account_type_covers_design_values(self) -> None:
        values = {t.value for t in AccountType}
        assert values == {
            "checking",
            "savings",
            "brokerage",
            "401k",
            "403b",
            "roth_ira",
            "traditional_ira",
            "hsa",
            "529",
            "530a",
            "real_estate",
            "mortgage",
            "vehicle",
            "other_asset",
            "other_liability",
        }

    def test_asset_class_covers_design_values(self) -> None:
        values = {c.value for c in AssetClass}
        assert values == {
            "cash",
            "us_equity_large_cap",
            "us_equity_small_cap",
            "intl_equity",
            "bonds",
            "fixed_income",
            "real_estate",
            "target_date",
            "other",
        }

    def test_numeric_valued_members_are_addressable(self) -> None:
        # The digit-leading values need identifier-safe member names.
        assert AccountType.RETIREMENT_401K == "401k"
        assert AccountType.PLAN_529 == "529"


class TestValidateAccountType:
    def test_accepts_valid_value(self) -> None:
        assert validate_account_type("401k") == "401k"

    @pytest.mark.parametrize("bad", ["Checking", "ira", "", None, 401, "roth"])
    def test_rejects_invalid_value(self, bad: object) -> None:
        with pytest.raises(ValueError, match="Invalid accountType"):
            validate_account_type(bad)


class TestValidateAssetClass:
    def test_accepts_valid_value(self) -> None:
        assert validate_asset_class("us_equity_large_cap") == "us_equity_large_cap"

    @pytest.mark.parametrize("bad", ["Equity", "stocks", "", None, 1])
    def test_rejects_invalid_value(self, bad: object) -> None:
        with pytest.raises(ValueError, match="Invalid assetClass"):
            validate_asset_class(bad)


class TestValidateOwners:
    def test_accepts_single_owner(self) -> None:
        assert validate_owners(["p1"]) == ["p1"]

    def test_accepts_multiple_owners(self) -> None:
        assert validate_owners(["p1", "p2"]) == ["p1", "p2"]

    def test_dedupes_preserving_order(self) -> None:
        assert validate_owners(["p2", "p1", "p2"]) == ["p2", "p1"]

    @pytest.mark.parametrize("bad", [[], None, "p1", ["p1", ""], ["p1", 2], [None]])
    def test_rejects_bad_shape(self, bad: object) -> None:
        with pytest.raises(ValueError, match="owner"):
            validate_owners(bad)


class TestAccountTypeMeta:
    def test_every_account_type_has_meta(self) -> None:
        # account_type_meta must resolve for every enum member (no gaps in the map).
        for t in AccountType:
            assert account_type_meta(t.value) is not None

    def test_liability_types(self) -> None:
        assert account_type_meta("mortgage").liability is True
        assert account_type_meta("other_liability").liability is True
        assert account_type_meta("checking").liability is False
        assert account_type_meta("brokerage").liability is False

    def test_amortizing_only_on_debt_types(self) -> None:
        assert account_type_meta("mortgage").amortizing is True
        assert account_type_meta("other_liability").amortizing is True
        assert account_type_meta("checking").amortizing is False


class TestValidateAssetClasses:
    def test_accepts_single(self) -> None:
        assert validate_asset_classes(["us_equity_large_cap"]) == ["us_equity_large_cap"]

    def test_accepts_multiple(self) -> None:
        assert validate_asset_classes(["us_equity_large_cap", "cash"]) == ["us_equity_large_cap", "cash"]

    def test_dedupes_preserving_order(self) -> None:
        assert validate_asset_classes(["cash", "bonds", "cash"]) == ["cash", "bonds"]

    @pytest.mark.parametrize("bad", [[], None, "cash", ["cash", "stonks"], [1]])
    def test_rejects_bad_shape(self, bad: object) -> None:
        with pytest.raises(ValueError, match=r"assetClasses|Invalid assetClass"):
            validate_asset_classes(bad)


_LOAN_TERMS: dict[str, Any] = {"interestRate": 0.04, "monthlyPayment": 1_000_000, "payoffYearMonth": "2030-01"}


class TestParseNewAccount:
    def test_fixed_class_type_ignores_client_asset_classes(self) -> None:
        account = parse_new_account("checking", ["us_equity_large_cap"], None, None)
        assert account == FixedClassAccount(AccountType.CHECKING, AssetClass.CASH, liability=False)

    def test_chosen_class_type_keeps_the_set(self) -> None:
        account = parse_new_account("brokerage", ["us_equity_large_cap", "intl_equity", "cash"], None, None)
        assert account == ChosenClassAccount(
            AccountType.BROKERAGE,
            [AssetClass.US_EQUITY_LARGE_CAP, AssetClass.INTL_EQUITY, AssetClass.CASH],
            target_year=None,
        )

    def test_chosen_class_type_with_target_date_needs_a_year(self) -> None:
        account = parse_new_account("401k", ["target_date", "cash"], 2055, None)
        assert isinstance(account, ChosenClassAccount)
        assert account.target_year == 2055
        with pytest.raises(ValueError, match="targetYear is required"):
            parse_new_account("401k", ["target_date"], None, None)

    def test_year_rejected_without_target_date(self) -> None:
        with pytest.raises(ValueError, match="only allowed when target_date"):
            parse_new_account("brokerage", ["cash"], 2055, None)
        with pytest.raises(ValueError, match="only allowed when target_date"):
            parse_new_account("checking", None, 2055, None)

    def test_loan_type_forces_its_class_and_keeps_the_terms(self) -> None:
        # Even if the client claims equities, a mortgage's asset class is forced.
        account = parse_new_account("mortgage", ["us_equity_large_cap"], None, _LOAN_TERMS)
        assert account == LoanAccount(
            AccountType.MORTGAGE, AssetClass.OTHER, LoanTerms(0.04, 1_000_000, "2030-01")
        )

    def test_loan_type_without_terms(self) -> None:
        account = parse_new_account("other_liability", None, None, None)
        assert isinstance(account, LoanAccount)
        assert account.loan_terms is None

    @pytest.mark.parametrize("account_type", ["brokerage", "checking"])
    def test_loan_terms_rejected_on_a_type_that_does_not_amortize(self, account_type: str) -> None:
        with pytest.raises(ValueError, match="only allowed on amortizing"):
            parse_new_account(account_type, ["us_equity_large_cap"], None, _LOAN_TERMS)

    def test_rejects_invalid_account_type(self) -> None:
        with pytest.raises(ValueError, match="Invalid accountType"):
            parse_new_account("chequing", ["cash"], None, None)

    def test_rejects_bad_asset_class(self) -> None:
        with pytest.raises(ValueError, match="Invalid assetClass"):
            parse_new_account("brokerage", ["stonks"], None, None)

    def test_rejects_empty_set(self) -> None:
        with pytest.raises(ValueError, match="assetClasses must be a non-empty list"):
            parse_new_account("brokerage", [], None, None)


class TestParseLoanTerms:
    def test_absent_terms_are_none(self) -> None:
        assert parse_loan_terms(None, amortizing=True) is None
        assert parse_loan_terms(None, amortizing=False) is None

    def test_converts_the_terms(self) -> None:
        terms = {"interestRate": 0.045, "monthlyPayment": 5, "payoffYearMonth": "2045-06"}
        assert parse_loan_terms(terms, amortizing=True) == LoanTerms(0.045, 5, "2045-06")


class TestTargetYear:
    def test_validate_accepts_plausible_year(self) -> None:
        assert validate_target_year(2055) == 2055

    @pytest.mark.parametrize("bad", [1980, 2200, "2055", 20.5, True, None])
    def test_validate_rejects_bad(self, bad: object) -> None:
        with pytest.raises(ValueError, match="targetYear"):
            validate_target_year(bad)

    def test_resolve_requires_year_with_target_date(self) -> None:
        assert resolve_target_year(["target_date", "cash"], 2055) == 2055
        with pytest.raises(ValueError, match="required"):
            resolve_target_year(["target_date"], None)

    def test_resolve_forbids_year_without_target_date(self) -> None:
        assert resolve_target_year(["cash"], None) is None
        with pytest.raises(ValueError, match="only allowed"):
            resolve_target_year(["cash"], 2055)


class TestValidateBirthYearMonth:
    def test_accepts_year_month(self) -> None:
        assert validate_birth_year_month("1985-07") == "1985-07"

    @pytest.mark.parametrize("bad", ["1985-7", "1985", "1985-07-01", "", None, 198507])
    def test_rejects_bad_format(self, bad: object) -> None:
        with pytest.raises(ValueError, match="birthYearMonth"):
            validate_birth_year_month(bad)


class TestValidateLoanTerms:
    def test_none_is_allowed_and_returns_none(self) -> None:
        assert validate_loan_terms(None, amortizing=True) is None
        assert validate_loan_terms(None, amortizing=False) is None

    def test_valid_terms_round_trip(self) -> None:
        result = validate_loan_terms(self._valid(), amortizing=True)
        assert result == self._valid()

    def test_rejected_on_non_amortizing(self) -> None:
        with pytest.raises(ValueError, match="only allowed on amortizing"):
            validate_loan_terms(self._valid(), amortizing=False)

    def test_requires_all_three_fields(self) -> None:
        terms = self._valid()
        del terms["payoffYearMonth"]
        with pytest.raises(ValueError, match="requires all of"):
            validate_loan_terms(terms, amortizing=True)

    def test_rejects_unexpected_fields(self) -> None:
        terms = self._valid()
        terms["extra"] = 1
        with pytest.raises(ValueError, match="unexpected fields"):
            validate_loan_terms(terms, amortizing=True)

    @pytest.mark.parametrize("rate", [-0.01, 1.0, 1.5, "0.05", True])
    def test_rejects_bad_interest_rate(self, rate: object) -> None:
        terms = self._valid()
        terms["interestRate"] = rate
        with pytest.raises(ValueError, match="interestRate"):
            validate_loan_terms(terms, amortizing=True)

    @pytest.mark.parametrize("payment", [-1, 2500.5, "2500", True])
    def test_rejects_bad_monthly_payment(self, payment: object) -> None:
        terms = self._valid()
        terms["monthlyPayment"] = payment
        with pytest.raises(ValueError, match="monthlyPayment"):
            validate_loan_terms(terms, amortizing=True)

    @pytest.mark.parametrize("payoff", ["2045", "2045-6", "not-a-date", 204506])
    def test_rejects_bad_payoff(self, payoff: object) -> None:
        terms = self._valid()
        terms["payoffYearMonth"] = payoff
        with pytest.raises(ValueError, match="payoffYearMonth"):
            validate_loan_terms(terms, amortizing=True)

    def test_zero_interest_rate_allowed(self) -> None:
        terms = self._valid()
        terms["interestRate"] = 0
        assert validate_loan_terms(terms, amortizing=True) is not None

    def _valid(self) -> dict[str, object]:
        return {"interestRate": 0.04875, "monthlyPayment": 2_500_000_000, "payoffYearMonth": "2045-06"}
