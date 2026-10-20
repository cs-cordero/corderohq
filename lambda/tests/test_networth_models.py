"""Tests for net-worth enum and validation helpers."""

import pytest

from corderohq.networth.models import (
    AccountType,
    AssetClass,
    account_type_meta,
    budget_role_of,
    resolve_account_type_fields,
    resolve_target_year,
    validate_account_type,
    validate_asset_class,
    validate_asset_classes,
    validate_birth_year_month,
    validate_budget_role,
    validate_last_four,
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
            "credit_card",
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
            "debt",
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


class TestResolveAccountTypeFields:
    def test_derives_liability_and_forces_fixed_asset_class(self) -> None:
        # Even if the client claims equities, a mortgage's asset class is forced.
        account_type, liability, asset_classes, loan_terms = resolve_account_type_fields(
            "mortgage", ["us_equity_large_cap"], None
        )
        assert account_type == "mortgage"
        assert liability is True
        assert asset_classes == ["debt"]
        assert loan_terms is None

    def test_credit_card_is_a_debt_liability_with_no_loan_terms(self) -> None:
        _, liability, asset_classes, _ = resolve_account_type_fields("credit_card", None, None)
        assert liability is True
        assert asset_classes == ["debt"]
        terms = {"interestRate": 0.2, "monthlyPayment": 1, "payoffYearMonth": "2030-01"}
        with pytest.raises(ValueError, match="only allowed on amortizing"):
            resolve_account_type_fields("credit_card", None, terms)

    def test_every_liability_type_has_the_debt_class(self) -> None:
        for account_type in AccountType:
            meta = account_type_meta(account_type.value)
            assert meta.liability == (meta.fixed_asset_class == AssetClass.DEBT), account_type

    def test_asset_account_cannot_choose_debt(self) -> None:
        with pytest.raises(ValueError, match="cannot include 'debt'"):
            resolve_account_type_fields("brokerage", ["cash", "debt"], None)

    def test_cash_type_ignores_client_asset_classes(self) -> None:
        _, liability, asset_classes, _ = resolve_account_type_fields("checking", ["us_equity_large_cap"], None)
        assert liability is False
        assert asset_classes == ["cash"]

    def test_choose_type_validates_and_keeps_the_set(self) -> None:
        _, liability, asset_classes, _ = resolve_account_type_fields(
            "brokerage", ["us_equity_large_cap", "intl_equity", "cash"], None
        )
        assert liability is False
        assert asset_classes == ["us_equity_large_cap", "intl_equity", "cash"]

    def test_choose_type_rejects_bad_asset_class(self) -> None:
        with pytest.raises(ValueError, match="Invalid assetClass"):
            resolve_account_type_fields("brokerage", ["stonks"], None)

    def test_choose_type_rejects_empty_set(self) -> None:
        with pytest.raises(ValueError, match="assetClasses must be a non-empty list"):
            resolve_account_type_fields("brokerage", [], None)

    def test_loan_terms_rejected_on_non_amortizing_type(self) -> None:
        terms = {"interestRate": 0.04, "monthlyPayment": 1, "payoffYearMonth": "2030-01"}
        with pytest.raises(ValueError, match="only allowed on amortizing"):
            resolve_account_type_fields("brokerage", ["us_equity_large_cap"], terms)

    def test_loan_terms_allowed_on_amortizing_type(self) -> None:
        terms = {"interestRate": 0.04, "monthlyPayment": 1_000_000, "payoffYearMonth": "2030-01"}
        _, _, _, normalized = resolve_account_type_fields("other_liability", None, terms)
        assert normalized == terms


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
    def _valid(self) -> dict[str, object]:
        return {"interestRate": 0.04875, "monthlyPayment": 2_500_000_000, "payoffYearMonth": "2045-06"}

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


class TestBudgetRole:
    @pytest.mark.parametrize(
        ("account_type", "expected"),
        [
            ("checking", "SPENDING"),
            ("credit_card", "SPENDING"),
            ("savings", "HOLDING"),
            ("brokerage", "HOLDING"),
            ("529", "HOLDING"),
            ("530a", "HOLDING"),
            ("401k", None),
            ("mortgage", None),
            ("other_liability", None),
        ],
    )
    def test_default_for_each_type(self, account_type: str, expected: str | None) -> None:
        assert budget_role_of({"accountType": account_type}) == expected

    def test_explicit_role_wins(self) -> None:
        assert budget_role_of({"accountType": "other_liability", "budgetRole": "SPENDING"}) == "SPENDING"
        assert budget_role_of({"accountType": "savings", "budgetRole": "SPENDING"}) == "SPENDING"

    def test_validate_budget_role(self) -> None:
        assert validate_budget_role("HOLDING") == "HOLDING"
        assert validate_budget_role(None) is None
        with pytest.raises(ValueError, match="budgetRole must be one of"):
            validate_budget_role("spending")


class TestLastFour:
    def test_accepts_and_dedupes(self) -> None:
        assert validate_last_four(["1234", "5678", "1234"]) == ["1234", "5678"]
        assert validate_last_four([]) == []

    @pytest.mark.parametrize("bad", ["1234", ["123"], ["12345"], ["12a4"], [1234]])
    def test_rejects_bad_values(self, bad: object) -> None:
        with pytest.raises(ValueError, match="lastFour"):
            validate_last_four(bad)
