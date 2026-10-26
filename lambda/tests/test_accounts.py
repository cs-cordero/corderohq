from typing import Any, assert_never

import pytest

from corderohq.accounts import (
    AccountType,
    AssetClass,
    BudgetRole,
    ChosenClassAccount,
    CreateAccountArgs,
    FixedClassAccount,
    LoanAccount,
    LoanTerms,
    UpdateAccountArgs,
    budget_role_of,
    get_metadata_for_account_type,
    parse_create_account_args,
    parse_loan_terms,
    parse_update_account_args,
    resolve_target_year,
    validate_account_type,
    validate_asset_class,
    validate_asset_classes,
    validate_budget_role,
    validate_last_four,
    validate_loan_terms,
    validate_owners,
    validate_target_year,
)
from corderohq.field_changes import Keep, Remove, SetTo


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
        # get_metadata_for_account_type must resolve for every enum member (no gaps in the map).
        for t in AccountType:
            assert get_metadata_for_account_type(t.value) is not None

    def test_liability_types(self) -> None:
        assert get_metadata_for_account_type("mortgage").is_liability is True
        assert get_metadata_for_account_type("other_liability").is_liability is True
        assert get_metadata_for_account_type("checking").is_liability is False
        assert get_metadata_for_account_type("brokerage").is_liability is False

    def test_amortizing_only_on_debt_types(self) -> None:
        assert get_metadata_for_account_type("mortgage").amortizing is True
        assert get_metadata_for_account_type("other_liability").amortizing is True
        assert get_metadata_for_account_type("checking").amortizing is False


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


def _body(account_type: str, **fields: Any) -> dict[str, Any]:
    return {"name": "Account", "accountType": account_type, "owners": ["p1"], **fields}


class TestParseCreateAccountArgs:
    def test_fixed_class_type_ignores_client_asset_classes(self) -> None:
        args = parse_create_account_args(_body("checking", assetClasses=["us_equity_large_cap"]))
        assert args == CreateAccountArgs(
            name="Account",
            owners=["p1"],
            excluded_from_net_worth=False,
            notes=None,
            account=FixedClassAccount(AccountType.CHECKING),
        )

    def test_chosen_class_type_keeps_the_set(self) -> None:
        args = parse_create_account_args(_body("brokerage", assetClasses=["us_equity_large_cap", "intl_equity"]))
        assert args.account == ChosenClassAccount(
            AccountType.BROKERAGE, [AssetClass.US_EQUITY_LARGE_CAP, AssetClass.INTL_EQUITY], target_year=None
        )

    def test_target_date_needs_a_year(self) -> None:
        args = parse_create_account_args(_body("401k", assetClasses=["target_date", "cash"], targetYear=2055))
        assert isinstance(args.account, ChosenClassAccount)
        assert args.account.target_year == 2055
        with pytest.raises(ValueError, match="targetYear is required"):
            parse_create_account_args(_body("401k", assetClasses=["target_date"]))

    def test_year_rejected_without_target_date(self) -> None:
        with pytest.raises(ValueError, match="only allowed when target_date"):
            parse_create_account_args(_body("brokerage", assetClasses=["cash"], targetYear=2055))

    def test_loan_type_keeps_the_terms(self) -> None:
        args = parse_create_account_args(_body("mortgage", assetClasses=["us_equity_large_cap"], loanTerms=_LOAN_TERMS))
        assert args.account == LoanAccount(AccountType.MORTGAGE, LoanTerms(0.04, 1_000_000, "2030-01"))

    def test_loan_type_without_terms(self) -> None:
        args = parse_create_account_args(_body("other_liability"))
        assert args.account == LoanAccount(AccountType.OTHER_LIABILITY, None)

    @pytest.mark.parametrize(
        ("account_type", "key", "value"),
        [
            ("checking", "loanTerms", _LOAN_TERMS),
            ("checking", "targetYear", 2055),
            ("brokerage", "loanTerms", _LOAN_TERMS),
            ("mortgage", "targetYear", 2055),
        ],
    )
    def test_rejects_a_key_that_the_type_does_not_take(self, account_type: str, key: str, value: object) -> None:
        body = _body(account_type, assetClasses=["cash"], **{key: value})
        with pytest.raises(ValueError, match=f"{key} is not allowed for a {account_type} account"):
            parse_create_account_args(body)

    @pytest.mark.parametrize("account_type", list(AccountType))
    def test_the_variant_of_each_type_agrees_with_its_metadata(self, account_type: AccountType) -> None:
        # The match in parse_create_account_args and the metadata table both decide the kind of
        # an account. update_account and to_item() read the table, so the two must agree.
        account = parse_create_account_args(_body(account_type.value, assetClasses=["cash"])).account
        meta = get_metadata_for_account_type(account_type)
        match account:
            case FixedClassAccount():
                assert meta.fixed_asset_class is not None
                assert not meta.amortizing
            case ChosenClassAccount():
                assert meta.fixed_asset_class is None
                assert not meta.amortizing
                assert not meta.is_liability
            case LoanAccount():
                assert meta.fixed_asset_class is not None
                assert meta.amortizing
                assert meta.is_liability
            case _:
                assert_never(account)

    @pytest.mark.parametrize("account_type", [None, "chequing"])
    def test_rejects_invalid_account_type(self, account_type: object) -> None:
        with pytest.raises(ValueError, match="Invalid accountType"):
            parse_create_account_args(_body("checking") | {"accountType": account_type})

    def test_rejects_bad_asset_classes(self) -> None:
        with pytest.raises(ValueError, match="Invalid assetClass"):
            parse_create_account_args(_body("brokerage", assetClasses=["stonks"]))
        with pytest.raises(ValueError, match="assetClasses must be a non-empty list"):
            parse_create_account_args(_body("brokerage", assetClasses=[]))

    @pytest.mark.parametrize("name", [None, "", "   ", 5])
    def test_rejects_a_bad_name(self, name: object) -> None:
        with pytest.raises(ValueError, match="name is required"):
            parse_create_account_args(_body("checking") | {"name": name})

    def test_strips_the_name(self) -> None:
        assert parse_create_account_args(_body("checking") | {"name": "  Chase  "}).name == "Chase"

    def test_rejects_empty_owners(self) -> None:
        with pytest.raises(ValueError, match="owners must be a non-empty list"):
            parse_create_account_args(_body("checking") | {"owners": []})

    def test_rejects_a_non_bool_excluded_flag(self) -> None:
        with pytest.raises(ValueError, match="excludedFromNetWorth must be a boolean"):
            parse_create_account_args(_body("checking", excludedFromNetWorth="false"))

    def test_rejects_non_string_notes(self) -> None:
        with pytest.raises(ValueError, match="notes must be a string or null"):
            parse_create_account_args(_body("checking", notes=5))


def _stored(account_type: str, **fields: Any) -> dict[str, Any]:
    return {"accountId": "a1", "name": "A", "accountType": account_type, **fields}


class TestParseUpdateAccountArgs:
    def test_keeps_each_field_that_the_body_does_not_name(self) -> None:
        args = parse_update_account_args({"name": "  New  "}, _stored("brokerage", assetClasses=["cash"]))
        assert args == UpdateAccountArgs(
            name=SetTo("New"),
            owners=Keep(),
            excluded_from_net_worth=Keep(),
            sort_order=Keep(),
            notes=Keep(),
            asset_classes=Keep(),
            target_year=Keep(),
            loan_terms=Keep(),
            budget_role=Keep(),
            last_four=Keep(),
        )

    def test_needs_an_editable_key(self) -> None:
        # accountType and liability cannot change, so they do not count.
        with pytest.raises(ValueError, match="at least one editable field is required"):
            parse_update_account_args({"accountType": "mortgage", "liability": True}, _stored("checking"))

    def test_sets_the_simple_fields(self) -> None:
        body = {"owners": ["p1", "p2", "p1"], "excludedFromNetWorth": True, "sortOrder": 3, "notes": "n"}
        args = parse_update_account_args(body, _stored("checking"))
        assert args.owners == SetTo(["p1", "p2"])
        assert args.excluded_from_net_worth == SetTo(True)
        assert args.sort_order == SetTo(3)
        assert args.notes == SetTo("n")

    def test_null_notes_removes_them(self) -> None:
        assert parse_update_account_args({"notes": None}, _stored("checking")).notes == Remove()

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ({"name": "  "}, "must not be empty"),
            ({"owners": []}, "owners must be a non-empty list"),
            ({"excludedFromNetWorth": "yes"}, "excludedFromNetWorth must be a boolean"),
            ({"sortOrder": True}, "sortOrder must be an integer"),
            ({"notes": 5}, "notes must be a string or null"),
        ],
    )
    def test_rejects_bad_values(self, body: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            parse_update_account_args(body, _stored("checking"))

    def test_chosen_class_type_can_change_its_classes(self) -> None:
        args = parse_update_account_args({"assetClasses": ["bonds", "cash"]}, _stored("brokerage"))
        assert args.asset_classes == SetTo([AssetClass.BONDS, AssetClass.CASH])

    @pytest.mark.parametrize("account_type", ["checking", "mortgage"])
    def test_fixed_class_types_cannot_change_their_classes(self, account_type: str) -> None:
        with pytest.raises(ValueError, match=f"assetClasses is not allowed for a {account_type} account"):
            parse_update_account_args({"assetClasses": ["us_equity_large_cap"]}, _stored(account_type))

    def test_loan_type_sets_and_removes_its_terms(self) -> None:
        args = parse_update_account_args({"loanTerms": _LOAN_TERMS}, _stored("mortgage"))
        assert args.loan_terms == SetTo(LoanTerms(0.04, 1_000_000, "2030-01"))
        assert parse_update_account_args({"loanTerms": None}, _stored("mortgage")).loan_terms == Remove()

    @pytest.mark.parametrize("account_type", ["checking", "brokerage"])
    def test_other_types_reject_loan_terms(self, account_type: str) -> None:
        with pytest.raises(ValueError, match=f"loanTerms is not allowed for a {account_type} account"):
            parse_update_account_args({"loanTerms": _LOAN_TERMS}, _stored(account_type))

    def test_null_loan_terms_on_another_type_changes_nothing(self) -> None:
        # The frontend sends `loanTerms: null` for every type.
        args = parse_update_account_args({"name": "A", "loanTerms": None}, _stored("checking"))
        assert args.loan_terms == Keep()

    def test_adding_target_date_needs_a_year(self) -> None:
        with pytest.raises(ValueError, match="targetYear is required"):
            parse_update_account_args({"assetClasses": ["target_date"]}, _stored("brokerage", assetClasses=["cash"]))
        args = parse_update_account_args(
            {"assetClasses": ["target_date"], "targetYear": 2055}, _stored("brokerage", assetClasses=["cash"])
        )
        assert args.target_year == SetTo(2055)

    def test_a_stored_year_stays_with_target_date(self) -> None:
        stored = _stored("401k", assetClasses=["target_date"], targetYear=2050)
        assert parse_update_account_args({"name": "A"}, stored).target_year == SetTo(2050)

    def test_removing_target_date_removes_the_stale_year(self) -> None:
        stored = _stored("brokerage", assetClasses=["target_date"], targetYear=2055)
        assert parse_update_account_args({"assetClasses": ["cash"]}, stored).target_year == Remove()

    def test_a_year_without_target_date_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="only allowed when target_date"):
            parse_update_account_args({"targetYear": 2055}, _stored("brokerage", assetClasses=["cash"]))

    def test_null_year_without_target_date_changes_nothing(self) -> None:
        # The frontend sends `targetYear: null` when the classes do not hold target_date.
        args = parse_update_account_args({"targetYear": None}, _stored("checking", assetClasses=["cash"]))
        assert args.target_year == Keep()


class TestLiabilities:
    def test_credit_card_is_a_debt_liability_with_no_loan_terms(self) -> None:
        args = parse_create_account_args(_body("credit_card"))
        assert args.account == FixedClassAccount(AccountType.CREDIT_CARD)
        meta = get_metadata_for_account_type(AccountType.CREDIT_CARD)
        assert meta.is_liability
        assert meta.fixed_asset_class == AssetClass.DEBT
        with pytest.raises(ValueError, match="loanTerms is not allowed for a credit_card account"):
            parse_create_account_args(_body("credit_card", loanTerms=_LOAN_TERMS))

    @pytest.mark.parametrize("account_type", list(AccountType))
    def test_every_liability_type_has_the_debt_class(self, account_type: AccountType) -> None:
        meta = get_metadata_for_account_type(account_type)
        assert meta.is_liability == (meta.fixed_asset_class == AssetClass.DEBT)

    def test_asset_account_cannot_choose_debt(self) -> None:
        with pytest.raises(ValueError, match="cannot include 'debt'"):
            parse_create_account_args(_body("brokerage", assetClasses=["cash", "debt"]))


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

    def test_update_sets_and_removes_the_budget_fields(self) -> None:
        args = parse_update_account_args({"budgetRole": "SPENDING", "lastFour": ["1234"]}, _stored("other_liability"))
        assert args.budget_role == SetTo(BudgetRole.SPENDING)
        assert args.last_four == SetTo(["1234"])
        args = parse_update_account_args({"budgetRole": None, "lastFour": []}, _stored("other_liability"))
        assert args.budget_role == Remove()
        assert args.last_four == Remove()


class TestLastFour:
    def test_accepts_and_dedupes(self) -> None:
        assert validate_last_four(["1234", "5678", "1234"]) == ["1234", "5678"]
        assert validate_last_four([]) == []

    @pytest.mark.parametrize("bad", ["1234", ["123"], ["12345"], ["12a4"], [1234]])
    def test_rejects_bad_values(self, bad: object) -> None:
        with pytest.raises(ValueError, match="lastFour"):
            validate_last_four(bad)


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
