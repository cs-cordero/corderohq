from unittest.mock import MagicMock

import pytest

from corderohq.aws.dynamodb import BudgetTable, BudgetTargetAmounts


def _make_table() -> tuple[BudgetTable, MagicMock]:
    """Build a BudgetTable backed by a MagicMock DynamoDB resource.

    Returns (table, mock) so tests can assert on the mock directly instead of
    going through `table._table.*` — which ty rejects because the class
    annotates `_table` with the real boto3 Table type.
    """

    bt = BudgetTable.__new__(BudgetTable)
    mock = MagicMock()
    bt._table = mock
    return bt, mock


class TestGetTargets:
    def test_returns_items_for_month(self) -> None:
        table, mock = _make_table()
        mock.query.return_value = {
            "Items": [
                {"yearMonth": "2026-03", "categoryId": "cat1", "carriedAmount": 500000000},
            ]
        }
        result = table.get_budget_targets("2026-03")
        assert len(result) == 1
        assert result[0]["categoryId"] == "cat1"

    def test_returns_empty_for_no_targets(self) -> None:
        table, mock = _make_table()
        mock.query.return_value = {"Items": []}
        result = table.get_budget_targets("2026-03")
        assert result == []


class TestPutTargets:
    def test_writes_each_target_in_one_transaction(self) -> None:
        table, mock = _make_table()
        mock.name = "Budget-test"
        targets = {
            "cat1": BudgetTargetAmounts(carried_amount=200),
            "cat2": BudgetTargetAmounts(carried_amount=0, pinned_amount=50),
        }

        result = table.set_all_budget_targets("2026-03", targets)

        expected = [
            {"yearMonth": "2026-03", "categoryId": "cat1", "carriedAmount": 200},
            {"yearMonth": "2026-03", "categoryId": "cat2", "carriedAmount": 0, "pinnedAmount": 50},
        ]
        mock.meta.client.transact_write_items.assert_called_once_with(
            TransactItems=[{"Put": {"TableName": "Budget-test", "Item": item}} for item in expected]
        )
        assert result == expected

    def test_no_targets_writes_nothing(self) -> None:
        table, mock = _make_table()
        assert table.set_all_budget_targets("2026-03", {}) == []
        mock.meta.client.transact_write_items.assert_not_called()

    def test_more_targets_than_one_transaction_holds_is_an_error(self) -> None:
        table, mock = _make_table()
        targets = {f"cat{n}": BudgetTargetAmounts(carried_amount=n) for n in range(101)}
        with pytest.raises(ValueError, match="at most 100"):
            table.set_all_budget_targets("2026-03", targets)
        mock.meta.client.transact_write_items.assert_not_called()


class TestScan:
    def test_paginates_through_all_items(self) -> None:
        table, mock = _make_table()
        mock.scan.side_effect = [
            {
                "Items": [{"yearMonth": "2026-03", "categoryId": "a", "carriedAmount": 100}],
                "LastEvaluatedKey": {"k": "v"},
            },
            {"Items": [{"yearMonth": "2026-04", "categoryId": "b", "carriedAmount": 200}]},
        ]
        result = table.scan()
        assert len(result) == 2
        assert mock.scan.call_count == 2

    def test_returns_empty_for_empty_table(self) -> None:
        table, mock = _make_table()
        mock.scan.return_value = {"Items": []}
        assert table.scan() == []


class TestSetSingleBudgetTarget:
    def test_writes_one_row(self) -> None:
        table, mock = _make_table()
        table.set_single_budget_target("2026-03", "cat1", BudgetTargetAmounts(carried_amount=500))
        mock.put_item.assert_called_once_with(Item={"yearMonth": "2026-03", "categoryId": "cat1", "carriedAmount": 500})


class TestRowsForCategory:
    def test_returns_the_keys_from_every_page(self) -> None:
        table, mock = _make_table()
        mock.scan.side_effect = [
            {"Items": [{"yearMonth": "2026-03", "categoryId": "cat1"}], "LastEvaluatedKey": {"k": "v"}},
            {"Items": [{"yearMonth": "2026-09", "categoryId": "cat1"}]},
        ]
        assert table.get_rows_for_category("cat1") == [
            {"yearMonth": "2026-03", "categoryId": "cat1"},
            {"yearMonth": "2026-09", "categoryId": "cat1"},
        ]
        assert mock.scan.call_args.kwargs["ExpressionAttributeValues"] == {":cid": "cat1"}

    def test_delete_all_deletes_each_row(self) -> None:
        table, mock = _make_table()
        mock.scan.return_value = {"Items": [{"yearMonth": "2026-03", "categoryId": "cat1"}]}
        batch = mock.batch_writer.return_value.__enter__.return_value
        assert table.delete_all_for_category("cat1") == 1
        batch.delete_item.assert_called_once_with(Key={"yearMonth": "2026-03", "categoryId": "cat1"})

    def test_delete_all_with_no_rows_writes_nothing(self) -> None:
        table, mock = _make_table()
        mock.scan.return_value = {"Items": []}
        assert table.delete_all_for_category("cat1") == 0
        mock.batch_writer.assert_not_called()
