from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from corderohq.accounts import (
    AccountType,
    AssetClass,
    CashFlowRole,
    ChosenClassAccount,
    CreateAccountArgs,
    FixedClassAccount,
    LoanAccount,
    LoanTerms,
    NewAccount,
    UpdateAccountArgs,
)
from corderohq.aws.dynamodb import AccountTable, NetWorthSnapshotTable, ProfileTable, get_cash_flow_role
from corderohq.common import Keep, Remove, SetTo


# Same split-fixture trick as test_budget_categories: share one MagicMock as
# `._table` so tests can drive `mock.scan.return_value` etc. directly.
@pytest.fixture
def mock() -> MagicMock:
    return MagicMock()


@pytest.fixture
def profile(mock: MagicMock) -> ProfileTable:
    t = ProfileTable.__new__(ProfileTable)
    t._table = mock
    return t


@pytest.fixture
def accounts(mock: MagicMock) -> AccountTable:
    t = AccountTable.__new__(AccountTable)
    t._table = mock
    return t


@pytest.fixture
def snapshots(mock: MagicMock) -> NetWorthSnapshotTable:
    t = NetWorthSnapshotTable.__new__(NetWorthSnapshotTable)
    t._table = mock
    return t


class TestProfileTable:
    def test_list_people_excludes_settings_and_sorts_by_name(self, profile: ProfileTable, mock: MagicMock) -> None:
        mock.query.return_value = {
            "Items": [
                {"householdId": "HOUSEHOLD", "personId": "01B", "name": "Zoe", "birthYearMonth": "1990-01"},
                {"householdId": "HOUSEHOLD", "personId": "SETTINGS", "horizonAge": 95},
                {"householdId": "HOUSEHOLD", "personId": "01A", "name": "adam", "birthYearMonth": "1988-03"},
            ]
        }
        result = profile.list_profiles()
        assert [p["name"] for p in result] == ["adam", "Zoe"]

    def test_create_person_writes_item(self, profile: ProfileTable, mock: MagicMock) -> None:
        result = profile.create_profile("  Jordan  ", "1985-07")
        assert result["name"] == "Jordan"
        assert result["birthYearMonth"] == "1985-07"
        assert "personId" in result
        assert result["householdId"] == "HOUSEHOLD"
        assert "createdAt" in result and "updatedAt" in result
        mock.put_item.assert_called_once()

    def test_create_person_rejects_empty_name(self, profile: ProfileTable) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            profile.create_profile("   ", "1985-07")

    def test_create_person_rejects_bad_birth_month(self, profile: ProfileTable) -> None:
        with pytest.raises(ValueError, match="birthYearMonth"):
            profile.create_profile("Jordan", "July 1985")

    def test_update_person_returns_none_when_absent(self, profile: ProfileTable, mock: MagicMock) -> None:
        mock.get_item.return_value = {}
        assert profile.update_profile("nope", name="X") is None

    def test_update_person_sets_name_and_birth_month(self, profile: ProfileTable, mock: MagicMock) -> None:
        mock.get_item.return_value = {"Item": {"householdId": "HOUSEHOLD", "personId": "p1", "name": "Old"}}
        mock.update_item.return_value = {"Attributes": {"personId": "p1", "name": "New", "birthYearMonth": "1991-02"}}
        result = profile.update_profile("p1", name="New", birth_year_month="1991-02")
        assert result is not None and result["name"] == "New"
        kwargs = mock.update_item.call_args.kwargs
        assert "birthYearMonth = :bym" in kwargs["UpdateExpression"]
        assert kwargs["ExpressionAttributeNames"]["#n"] == "name"

    def test_update_person_rejects_bad_birth_month(self, profile: ProfileTable, mock: MagicMock) -> None:
        mock.get_item.return_value = {"Item": {"personId": "p1", "name": "Old"}}
        with pytest.raises(ValueError, match="birthYearMonth"):
            profile.update_profile("p1", birth_year_month="bad")


_CHECKING = FixedClassAccount(AccountType.CHECKING)


def _create(accounts: AccountTable, account: NewAccount, name: str = "Chase Checking", **kwargs: Any) -> dict[str, Any]:
    args = CreateAccountArgs(
        name=name,
        owners=kwargs.get("owners", ["p1"]),
        excluded_from_net_worth=kwargs.get("excluded_from_net_worth", False),
        notes=kwargs.get("notes"),
        account=account,
    )
    return accounts.create_account(args)


class TestGetCashFlowRole:
    @pytest.mark.parametrize(
        ("account_type", "expected"),
        [
            ("checking", "SPENDING"),
            ("credit_card", "SPENDING"),
            ("savings", "HOLDING"),
            ("brokerage", "HOLDING"),
            ("529", "HOLDING"),
            ("530a", "HOLDING"),
            ("401k", "HOLDING"),
            ("hsa", "HOLDING"),
            ("other_asset", "HOLDING"),
            ("mortgage", "EXTERNAL"),
            ("other_liability", "EXTERNAL"),
            ("real_estate", "NON_CASH"),
            ("vehicle", "NON_CASH"),
        ],
    )
    def test_default_for_each_type(self, account_type: str, expected: str) -> None:
        assert get_cash_flow_role({"accountType": account_type}) == expected

    @pytest.mark.parametrize("account_type", list(AccountType))
    def test_every_account_type_has_a_default(self, account_type: AccountType) -> None:
        assert get_cash_flow_role({"accountType": account_type.value}) in CashFlowRole

    def test_explicit_role_wins(self) -> None:
        assert get_cash_flow_role({"accountType": "other_liability", "cashFlowRole": "SPENDING"}) == "SPENDING"
        assert get_cash_flow_role({"accountType": "savings", "cashFlowRole": "SPENDING"}) == "SPENDING"


class TestAccountTableCreate:
    def test_creates_fixed_class_account(self, accounts: AccountTable, mock: MagicMock) -> None:
        mock.scan.return_value = {"Items": []}
        result = _create(accounts, _CHECKING)
        assert result["name"] == "Chase Checking"
        assert result["accountType"] == "checking"
        assert result["assetClasses"] == ["cash"]
        assert result["liability"] is False
        assert result["owners"] == ["p1"]
        assert result["excludedFromNetWorth"] is False
        assert result["active"] is True
        assert result["sortOrder"] == 0
        assert "loanTerms" not in result and "notes" not in result and "targetYear" not in result
        mock.put_item.assert_called_once()

    def test_creates_excluded_account(self, accounts: AccountTable, mock: MagicMock) -> None:
        mock.scan.return_value = {"Items": []}
        account = ChosenClassAccount(AccountType.PLAN_529, [AssetClass.US_EQUITY_LARGE_CAP], target_year=None)
        result = _create(accounts, account, name="Kid's 529", excluded_from_net_worth=True)
        assert result["excludedFromNetWorth"] is True

    def test_creates_loan_account(self, accounts: AccountTable, mock: MagicMock) -> None:
        mock.scan.return_value = {"Items": [{"accountId": "a0", "name": "Other", "sortOrder": 4}]}
        account = LoanAccount(AccountType.MORTGAGE, LoanTerms(0.045, 2_500_000_000, "2045-06"))
        result = _create(accounts, account, name="Mortgage", owners=["p1", "p2"], notes="30-year fixed")
        assert result["liability"] is True
        assert result["assetClasses"] == ["debt"]
        assert result["owners"] == ["p1", "p2"]
        # interestRate is stored as Decimal (DynamoDB rejects floats); the rest is unchanged.
        assert result["loanTerms"]["interestRate"] == Decimal("0.045")
        assert result["loanTerms"]["monthlyPayment"] == 2_500_000_000
        assert result["loanTerms"]["payoffYearMonth"] == "2045-06"
        assert result["notes"] == "30-year fixed"
        assert result["sortOrder"] == 5  # max existing (4) + 1

    def test_creates_loan_account_without_terms(self, accounts: AccountTable, mock: MagicMock) -> None:
        mock.scan.return_value = {"Items": []}
        result = _create(accounts, LoanAccount(AccountType.OTHER_LIABILITY, None), name="Loan")
        assert "loanTerms" not in result

    def test_creates_target_date_with_year(self, accounts: AccountTable, mock: MagicMock) -> None:
        mock.scan.return_value = {"Items": []}
        account = ChosenClassAccount(AccountType.RETIREMENT_401K, [AssetClass.TARGET_DATE, AssetClass.CASH], 2055)
        result = _create(accounts, account, name="TDF")
        assert result["assetClasses"] == ["target_date", "cash"]
        assert result["liability"] is False
        assert result["targetYear"] == 2055

    def test_rejects_duplicate_name_case_insensitive(self, accounts: AccountTable, mock: MagicMock) -> None:
        mock.scan.return_value = {"Items": [{"accountId": "a1", "name": "Chase Checking", "sortOrder": 0}]}
        with pytest.raises(ValueError, match="already exists"):
            _create(accounts, _CHECKING, name="chase checking")


def _update_args(**changes: Any) -> UpdateAccountArgs:
    fields: dict[str, Any] = {
        "name": Keep(),
        "owners": Keep(),
        "excluded_from_net_worth": Keep(),
        "sort_order": Keep(),
        "notes": Keep(),
        "asset_classes": Keep(),
        "target_year": Keep(),
        "loan_terms": Keep(),
        "cash_flow_role": Keep(),
        "account_number_last_four": Keep(),
    }
    return UpdateAccountArgs(**(fields | changes))


class TestAccountTableUpdate:
    def test_sets_each_changed_field(self, accounts: AccountTable, mock: MagicMock) -> None:
        mock.scan.return_value = {"Items": [{"accountId": "a1", "name": "Old", "sortOrder": 0}]}
        mock.update_item.return_value = {"Attributes": {"accountId": "a1", "name": "New"}}
        args = _update_args(
            name=SetTo("New"),
            owners=SetTo(["p1", "p2"]),
            asset_classes=SetTo([AssetClass.BONDS]),
            loan_terms=SetTo(LoanTerms(0.045, 5, "2045-06")),
        )
        assert accounts.update_account("a1", args) == {"accountId": "a1", "name": "New"}
        kwargs = mock.update_item.call_args.kwargs
        assert kwargs["Key"] == {"accountId": "a1"}
        assert kwargs["UpdateExpression"] == (
            "SET updatedAt = :updatedAt, #name = :name, #owners = :owners, "
            "#assetClasses = :assetClasses, #loanTerms = :loanTerms"
        )
        values = kwargs["ExpressionAttributeValues"]
        assert values[":name"] == "New"
        assert values[":owners"] == ["p1", "p2"]
        assert values[":assetClasses"] == ["bonds"]
        # interestRate is stored as Decimal, because DynamoDB rejects floats.
        assert values[":loanTerms"]["interestRate"] == Decimal("0.045")
        assert kwargs["ExpressionAttributeNames"]["#name"] == "name"

    def test_removes_each_removed_field(self, accounts: AccountTable, mock: MagicMock) -> None:
        mock.update_item.return_value = {"Attributes": {"accountId": "a1"}}
        accounts.update_account("a1", _update_args(notes=Remove(), target_year=Remove(), loan_terms=Remove()))
        expression = mock.update_item.call_args.kwargs["UpdateExpression"]
        assert expression.startswith("SET updatedAt = :updatedAt REMOVE ")
        for attribute in ("#notes", "#targetYear", "#loanTerms"):
            assert attribute in expression

    def test_sets_and_removes_the_budget_fields(self, accounts: AccountTable, mock: MagicMock) -> None:
        mock.update_item.return_value = {"Attributes": {"accountId": "a1"}}
        args = _update_args(cash_flow_role=SetTo(CashFlowRole.SPENDING), account_number_last_four=Remove())
        accounts.update_account("a1", args)
        kwargs = mock.update_item.call_args.kwargs
        assert kwargs["UpdateExpression"] == (
            "SET updatedAt = :updatedAt, #cashFlowRole = :cashFlowRole REMOVE #lastFour"
        )
        assert kwargs["ExpressionAttributeValues"][":cashFlowRole"] == "SPENDING"

    def test_with_no_changes_sets_only_the_time(self, accounts: AccountTable, mock: MagicMock) -> None:
        mock.update_item.return_value = {"Attributes": {"accountId": "a1"}}
        accounts.update_account("a1", _update_args())
        kwargs = mock.update_item.call_args.kwargs
        assert kwargs["UpdateExpression"] == "SET updatedAt = :updatedAt"
        assert "ExpressionAttributeNames" not in kwargs

    def test_rejects_duplicate_name_excluding_self(self, accounts: AccountTable, mock: MagicMock) -> None:
        mock.scan.return_value = {
            "Items": [
                {"accountId": "a1", "name": "A", "sortOrder": 0},
                {"accountId": "a2", "name": "Taken", "sortOrder": 1},
            ]
        }
        with pytest.raises(ValueError, match="already exists"):
            accounts.update_account("a1", _update_args(name=SetTo("taken")))
        mock.update_item.assert_not_called()

    def test_keeping_its_own_name_is_not_a_duplicate(self, accounts: AccountTable, mock: MagicMock) -> None:
        mock.scan.return_value = {"Items": [{"accountId": "a1", "name": "A", "sortOrder": 0}]}
        mock.update_item.return_value = {"Attributes": {"accountId": "a1"}}
        accounts.update_account("a1", _update_args(name=SetTo("a")))
        mock.update_item.assert_called_once()


class TestAccountTableMisc:
    def test_get_active_accounts_sorts_by_sort_order_then_name(self, accounts: AccountTable, mock: MagicMock) -> None:
        mock.scan.return_value = {
            "Items": [
                {"accountId": "a2", "name": "Bravo", "sortOrder": 1, "active": True},
                {"accountId": "a1", "name": "Alpha", "sortOrder": 0, "active": True},
            ]
        }
        result = accounts.get_active_accounts()
        assert [a["name"] for a in result] == ["Alpha", "Bravo"]
        kwargs = mock.scan.call_args.kwargs
        assert kwargs["FilterExpression"] == "active = :val"

    def test_deactivate_returns_none_when_absent(self, accounts: AccountTable, mock: MagicMock) -> None:
        mock.get_item.return_value = {}
        assert accounts.deactivate("nope") is None

    def test_deactivate_sets_active_false(self, accounts: AccountTable, mock: MagicMock) -> None:
        mock.get_item.return_value = {"Item": {"accountId": "a1", "name": "A"}}
        mock.update_item.return_value = {"Attributes": {"accountId": "a1", "active": False}}
        result = accounts.deactivate("a1")
        assert result is not None and result["active"] is False

    def test_reorder_writes_index_per_account(self, accounts: AccountTable, mock: MagicMock) -> None:
        accounts.reorder(["a2", "a0", "a1"])
        assert mock.update_item.call_count == 3
        first = mock.update_item.call_args_list[0].kwargs
        assert first["Key"] == {"accountId": "a2"}
        assert first["ExpressionAttributeValues"][":o"] == 0

    def test_accounts_with_owner_filters_by_person(self, accounts: AccountTable, mock: MagicMock) -> None:
        mock.scan.return_value = {
            "Items": [
                {"accountId": "a1", "name": "His 401k", "owners": ["p1"], "sortOrder": 0},
                {"accountId": "a2", "name": "Joint Checking", "owners": ["p1", "p2"], "sortOrder": 1},
                {"accountId": "a3", "name": "Her IRA", "owners": ["p2"], "sortOrder": 2},
            ]
        }
        # p1 owns a1 outright and co-owns the joint a2; both come back.
        result = accounts.accounts_with_owner("p1")
        assert result == [{"accountId": "a1", "name": "His 401k"}, {"accountId": "a2", "name": "Joint Checking"}]


class TestNetWorthSnapshotTable:
    def test_get_month_queries_partition(self, snapshots: NetWorthSnapshotTable, mock: MagicMock) -> None:
        mock.query.return_value = {
            "Items": [{"yearMonth": "2026-06", "accountId": "a1", "byAssetClass": {"cash": 100}}]
        }
        result = snapshots.get_snapshot_for_month("2026-06")
        assert result[0]["accountId"] == "a1"
        kwargs = mock.query.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":ym"] == "2026-06"

    def test_scan_all_paginates(self, snapshots: NetWorthSnapshotTable, mock: MagicMock) -> None:
        mock.scan.side_effect = [
            {
                "Items": [{"yearMonth": "2026-05", "accountId": "a1", "byAssetClass": {"cash": 1}}],
                "LastEvaluatedKey": {"k": 1},
            },
            {"Items": [{"yearMonth": "2026-06", "accountId": "a1", "byAssetClass": {"cash": 2}}]},
        ]
        result = snapshots.scan()
        assert len(result) == 2
        assert mock.scan.call_count == 2

    def test_upsert_month_merges_classes_and_deletes_when_empty(
        self, snapshots: NetWorthSnapshotTable, mock: MagicMock
    ) -> None:
        def get_item(**kwargs: Any) -> dict[str, object]:
            key = kwargs["Key"]
            assert isinstance(key, dict)
            if key["accountId"] == "a1":
                return {
                    "Item": {"yearMonth": "2026-06", "accountId": "a1", "byAssetClass": {"cash": 500, "bonds": 100}}
                }
            return {}  # a2 has no existing row

        mock.get_item.side_effect = get_item
        mock.query.return_value = {"Items": []}
        snapshots.upsert_snapshot_record(
            "2026-06",
            [
                {"accountId": "a1", "classes": {"cash": 600, "bonds": None}},  # update cash, clear bonds
                {"accountId": "a2", "classes": {"cash": None}},  # nothing to clear → row stays empty → delete
            ],
        )
        put_item = mock.put_item.call_args.kwargs["Item"]
        assert put_item["accountId"] == "a1"
        assert put_item["byAssetClass"] == {"cash": 600}  # cash merged, bonds removed, others untouched
        mock.delete_item.assert_called_once_with(Key={"yearMonth": "2026-06", "accountId": "a2"})

    def test_delete_single(self, snapshots: NetWorthSnapshotTable, mock: MagicMock) -> None:
        snapshots.delete_snapshot_record("2026-06", "a1")
        mock.delete_item.assert_called_once_with(Key={"yearMonth": "2026-06", "accountId": "a1"})

    def test_upsert_month_preserves_note_when_absent(self, snapshots: NetWorthSnapshotTable, mock: MagicMock) -> None:
        mock.get_item.return_value = {
            "Item": {"yearMonth": "2026-06", "accountId": "a1", "byAssetClass": {"cash": 500}, "note": "kept"}
        }
        mock.query.return_value = {"Items": []}
        snapshots.upsert_snapshot_record("2026-06", [{"accountId": "a1", "classes": {"cash": 600}}])  # no note key
        put_item = mock.put_item.call_args.kwargs["Item"]
        assert put_item["note"] == "kept"

    def test_upsert_month_replaces_note_trimmed(self, snapshots: NetWorthSnapshotTable, mock: MagicMock) -> None:
        mock.get_item.return_value = {}
        mock.query.return_value = {"Items": []}
        rows = [{"accountId": "a1", "classes": {"cash": 500}, "note": "  bonus  "}]
        snapshots.upsert_snapshot_record("2026-06", rows)
        put_item = mock.put_item.call_args.kwargs["Item"]
        assert put_item["note"] == "bonus"

    def test_upsert_month_clears_blank_note(self, snapshots: NetWorthSnapshotTable, mock: MagicMock) -> None:
        mock.get_item.return_value = {
            "Item": {"yearMonth": "2026-06", "accountId": "a1", "byAssetClass": {"cash": 500}, "note": "old"}
        }
        mock.query.return_value = {"Items": []}
        snapshots.upsert_snapshot_record("2026-06", [{"accountId": "a1", "classes": {"cash": 500}, "note": "   "}])
        put_item = mock.put_item.call_args.kwargs["Item"]
        assert "note" not in put_item
