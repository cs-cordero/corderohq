from typing import Any
from unittest.mock import MagicMock

import pytest

from corderohq.aws.dynamodb import TransactionsTable
from corderohq.budget.dispositions import Disposition
from corderohq.budget.transactions import UpdateTransactionArgs
from corderohq.common import Keep, Remove, SetTo


# `mock` and `table` are split into two fixtures sharing the same MagicMock instance
# so tests can use `mock.query.return_value = ...` directly. Going through
# `table._table.*` would force ty to treat `_table` as the real boto3 Table type
# (which it's annotated as on TransactionsTable) and reject the mock attribute access.
@pytest.fixture
def mock() -> MagicMock:
    return MagicMock()


@pytest.fixture
def table(mock: MagicMock) -> TransactionsTable:
    tt = TransactionsTable.__new__(TransactionsTable)
    tt._table = mock
    return tt


class TestList:
    def test_returns_transactions_for_month(self, table: TransactionsTable, mock: MagicMock) -> None:
        mock.query.return_value = {
            "Items": [
                {"yearMonth": "2026-03", "sortId": "2026-03-01#abc", "description": "Coffee"},
            ]
        }
        result = table.list_transactions_for_month("2026-03")
        assert len(result) == 1
        assert result[0]["description"] == "Coffee"


class TestCreate:
    def test_creates_transaction_with_sort_id(self, table: TransactionsTable, mock: MagicMock) -> None:
        result = table.create_transaction(
            year_month="2026-03",
            transaction_date="2026-03-15",
            description="Grocery store",
            amount=50000000,
            category_id="cat1",
        )
        assert result["yearMonth"] == "2026-03"
        assert result["sortId"].startswith("2026-03-15#")
        assert result["description"] == "Grocery store"
        assert result["amount"] == 50000000
        assert "createdAt" in result
        mock.put_item.assert_called_once()

    def test_raises_on_empty_description(self, table: TransactionsTable) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            table.create_transaction("2026-03", "2026-03-15", "", 100, "cat1")

    def test_stores_budget_disposition_by_default(self, table: TransactionsTable) -> None:
        result = table.create_transaction("2026-03", "2026-03-15", "Coffee", 100, "cat1")
        assert result["disposition"] == "BUDGET"
        assert result["categoryId"] == "cat1"
        assert "accountId" not in result
        assert "note" not in result

    def test_skip_row_has_no_category(self, table: TransactionsTable, mock: MagicMock) -> None:
        result = table.create_transaction(
            "2026-03",
            "2026-03-15",
            "Payroll",
            -100,
            None,
            disposition=Disposition.SKIP,
            account_id="acct1",
            note="paycheck",
        )
        assert result["disposition"] == "SKIP"
        assert "categoryId" not in result
        assert result["accountId"] == "acct1"
        assert result["note"] == "paycheck"
        assert mock.put_item.call_args.kwargs["Item"] == result


class TestBatchCreate:
    def test_copies_present_disposition_fields_only(self, table: TransactionsTable, mock: MagicMock) -> None:
        writer = mock.batch_writer.return_value.__enter__.return_value
        result = table.batch_create_transaction(
            [
                {
                    "yearMonth": "2026-03",
                    "transactionDate": "2026-03-15",
                    "description": " Hotel ",
                    "amount": 100,
                    "disposition": "OFF_BUDGET",
                    "note": "work trip",
                },
                {
                    "yearMonth": "2026-03",
                    "transactionDate": "2026-03-16",
                    "description": "Groceries",
                    "amount": 200,
                    "disposition": "BUDGET",
                    "categoryId": "cat1",
                },
            ]
        )
        assert result[0]["description"] == "Hotel"
        assert result[0]["disposition"] == "OFF_BUDGET"
        assert result[0]["note"] == "work trip"
        assert "categoryId" not in result[0]
        assert result[1]["categoryId"] == "cat1"
        assert "note" not in result[1]
        assert writer.put_item.call_count == 2


def _changes(**changes: Any) -> UpdateTransactionArgs:
    fields: dict[str, Any] = {
        "description": Keep(),
        "amount": Keep(),
        "disposition": Keep(),
        "category_id": Keep(),
        "account_id": Keep(),
        "note": Keep(),
    }
    return UpdateTransactionArgs(**(fields | changes))


class TestUpdate:
    def test_get_transaction(self, table: TransactionsTable, mock: MagicMock) -> None:
        mock.get_item.return_value = {"Item": {"yearMonth": "2026-03", "sortId": "s"}}
        assert table.get_transaction("2026-03", "s") == {"yearMonth": "2026-03", "sortId": "s"}
        mock.get_item.return_value = {}
        assert table.get_transaction("2026-03", "nope") is None

    def test_sets_each_changed_field(self, table: TransactionsTable, mock: MagicMock) -> None:
        mock.update_item.return_value = {"Attributes": {"description": "New"}}
        result = table.update_transaction("2026-03", "s", _changes(description=SetTo("New"), amount=SetTo(5)))
        assert result == {"description": "New"}
        kwargs = mock.update_item.call_args.kwargs
        assert kwargs["Key"] == {"yearMonth": "2026-03", "sortId": "s"}
        assert kwargs["UpdateExpression"] == "SET #description = :description, #amount = :amount"
        assert kwargs["ExpressionAttributeValues"] == {":description": "New", ":amount": 5}

    def test_sets_and_removes_the_disposition_fields(self, table: TransactionsTable, mock: MagicMock) -> None:
        mock.update_item.return_value = {"Attributes": {}}
        args = _changes(
            disposition=SetTo(Disposition.SKIP), category_id=Remove(), account_id=Remove(), note=SetTo("old")
        )
        table.update_transaction("2026-03", "s", args)
        kwargs = mock.update_item.call_args.kwargs
        assert kwargs["UpdateExpression"] == (
            "SET #disposition = :disposition, #note = :note REMOVE #categoryId, #accountId"
        )
        assert kwargs["ExpressionAttributeValues"] == {":disposition": "SKIP", ":note": "old"}

    def test_a_change_of_nothing_is_an_error(self, table: TransactionsTable, mock: MagicMock) -> None:
        with pytest.raises(ValueError, match="changes nothing"):
            table.update_transaction("2026-03", "s", _changes())
        mock.update_item.assert_not_called()


class TestDelete:
    def test_deletes_existing_transaction(self, table: TransactionsTable, mock: MagicMock) -> None:
        mock.get_item.return_value = {"Item": {"yearMonth": "2026-03", "sortId": "2026-03-15#abc"}}
        result = table.delete_transaction("2026-03", "2026-03-15#abc")
        assert result is True
        mock.delete_item.assert_called_once()

    def test_returns_false_when_not_found(self, table: TransactionsTable, mock: MagicMock) -> None:
        mock.get_item.return_value = {}
        result = table.delete_transaction("2026-03", "nonexistent")
        assert result is False
        mock.delete_item.assert_not_called()


class TestRowsForCategory:
    def test_returns_the_keys_from_every_page(self, table: TransactionsTable, mock: MagicMock) -> None:
        mock.scan.side_effect = [
            {"Items": [{"yearMonth": "2026-03", "sortId": "a"}], "LastEvaluatedKey": {"k": "v"}},
            {"Items": [{"yearMonth": "2026-04", "sortId": "b"}]},
        ]
        assert table.get_rows_for_category("cat1") == [
            {"yearMonth": "2026-03", "sortId": "a"},
            {"yearMonth": "2026-04", "sortId": "b"},
        ]
        assert mock.scan.call_args.kwargs["ExpressionAttributeValues"] == {":cid": "cat1"}

    def test_delete_all_deletes_each_row(self, table: TransactionsTable, mock: MagicMock) -> None:
        mock.scan.return_value = {"Items": [{"yearMonth": "2026-03", "sortId": "a"}]}
        batch = mock.batch_writer.return_value.__enter__.return_value
        assert table.delete_all_transactions_for_category("cat1") == 1
        batch.delete_item.assert_called_once_with(Key={"yearMonth": "2026-03", "sortId": "a"})

    def test_delete_all_with_no_rows_writes_nothing(self, table: TransactionsTable, mock: MagicMock) -> None:
        mock.scan.return_value = {"Items": []}
        assert table.delete_all_transactions_for_category("cat1") == 0
        mock.batch_writer.assert_not_called()
