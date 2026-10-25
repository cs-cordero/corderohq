from unittest.mock import MagicMock

import pytest

from corderohq.aws.dynamodb import CategoryTable


# `mock` and `table` are split into two fixtures sharing the same MagicMock instance
# so tests can use `mock.scan.return_value = ...` directly. Going through
# `table._table.*` would force ty to treat `_table` as the real boto3 Table type
# (which it's annotated as on CategoryTable) and reject the mock attribute access.
@pytest.fixture
def mock() -> MagicMock:
    return MagicMock()


@pytest.fixture
def table(mock: MagicMock) -> CategoryTable:
    ct = CategoryTable.__new__(CategoryTable)
    ct._table = mock
    return ct


class TestListActive:
    def test_returns_active_categories_sorted_by_name(self, table: CategoryTable, mock: MagicMock) -> None:
        mock.scan.return_value = {
            "Items": [
                {"categoryId": "2", "name": "Groceries", "active": True},
                {"categoryId": "1", "name": "Auto", "active": True},
            ]
        }
        result = table.get_active_categories()
        assert len(result) == 2
        assert result[0]["name"] == "Auto"
        assert result[1]["name"] == "Groceries"

    def test_returns_empty_list_when_no_categories(self, table: CategoryTable, mock: MagicMock) -> None:
        mock.scan.return_value = {"Items": []}
        result = table.get_active_categories()
        assert result == []


class TestCreate:
    def test_creates_category_with_ulid_and_timestamps(self, table: CategoryTable, mock: MagicMock) -> None:
        result = table.create_category("Groceries", group_id="g1")
        assert result["name"] == "Groceries"
        assert result["active"] is True
        assert "categoryId" in result
        assert "createdAt" in result
        assert "updatedAt" in result
        assert result["nameHistory"] == []
        assert result["groupId"] == "g1"
        mock.put_item.assert_called_once()

    def test_strips_whitespace_from_name(self, table: CategoryTable) -> None:
        result = table.create_category("  Groceries  ", group_id="g1")
        assert result["name"] == "Groceries"

    def test_raises_on_empty_name(self, table: CategoryTable) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            table.create_category("", group_id="g1")

    def test_raises_on_whitespace_only_name(self, table: CategoryTable) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            table.create_category("   ", group_id="g1")


class TestUpdate:
    def test_updates_name_and_appends_to_name_history(self, table: CategoryTable, mock: MagicMock) -> None:
        mock.get_item.return_value = {"Item": {"categoryId": "abc", "name": "Old", "active": True, "nameHistory": []}}
        mock.update_item.return_value = {
            "Attributes": {
                "categoryId": "abc",
                "name": "New",
                "active": True,
                "nameHistory": [{"previousName": "Old", "replacedAt": "2026-06-15T12:00:00+00:00"}],
            }
        }
        result = table.update("abc", "New")
        assert result is not None
        assert result["name"] == "New"

        # The UpdateExpression must append to nameHistory using list_append.
        kwargs = mock.update_item.call_args.kwargs
        assert "list_append" in kwargs["UpdateExpression"]
        assert "nameHistory" in kwargs["UpdateExpression"]
        entries = kwargs["ExpressionAttributeValues"][":entry"]
        assert entries[0]["previousName"] == "Old"
        assert "replacedAt" in entries[0]

    def test_no_op_rename_skips_history_entry(self, table: CategoryTable, mock: MagicMock) -> None:
        # Submitting the same name should bump updatedAt but not pollute nameHistory.
        mock.get_item.return_value = {"Item": {"categoryId": "abc", "name": "Groceries", "active": True}}
        mock.update_item.return_value = {"Attributes": {"categoryId": "abc", "name": "Groceries", "active": True}}
        table.update("abc", "Groceries")
        kwargs = mock.update_item.call_args.kwargs
        assert "list_append" not in kwargs["UpdateExpression"]
        assert "nameHistory" not in kwargs["UpdateExpression"]

    def test_returns_none_when_not_found(self, table: CategoryTable, mock: MagicMock) -> None:
        mock.get_item.return_value = {}
        result = table.update("nonexistent", "New")
        assert result is None

    def test_raises_on_empty_name(self, table: CategoryTable) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            table.update("abc", "")


class TestDeactivate:
    def test_sets_active_to_false(self, table: CategoryTable, mock: MagicMock) -> None:
        mock.get_item.return_value = {"Item": {"categoryId": "abc", "name": "Groceries", "active": True}}
        mock.update_item.return_value = {"Attributes": {"categoryId": "abc", "name": "Groceries", "active": False}}
        result = table.deactivate("abc")
        assert result is not None
        assert result["active"] is False

    def test_returns_none_when_not_found(self, table: CategoryTable, mock: MagicMock) -> None:
        mock.get_item.return_value = {}
        result = table.deactivate("nonexistent")
        assert result is None
