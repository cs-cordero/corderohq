from unittest.mock import MagicMock

import pytest

from corderohq.aws.dynamodb import CategoryGroupTable, CategoryTable


# `mock` and `group_table` share state via the same MagicMock so tests can assert
# directly on the mock instead of poking through `group_table._table.*` — same
# rationale as the other table-test fixtures (see test_budget_categories.py).
@pytest.fixture
def group_mock() -> MagicMock:
    return MagicMock()


@pytest.fixture
def group_table(group_mock: MagicMock) -> CategoryGroupTable:
    gt = CategoryGroupTable.__new__(CategoryGroupTable)
    gt._table = group_mock
    return gt


@pytest.fixture
def cat_mock() -> MagicMock:
    return MagicMock()


@pytest.fixture
def cat_table(cat_mock: MagicMock) -> CategoryTable:
    ct = CategoryTable.__new__(CategoryTable)
    ct._table = cat_mock
    return ct


class TestCategoryGroupCreate:
    def test_creates_with_required_fields_and_next_order(
        self, group_table: CategoryGroupTable, group_mock: MagicMock
    ) -> None:
        # Two existing groups with orders 0 and 1 — next create should land at order 2.
        group_mock.scan.return_value = {
            "Items": [
                {"groupId": "g1", "name": "Income", "active": True, "order": 0},
                {"groupId": "g2", "name": "Essentials", "active": True, "order": 1},
            ]
        }
        result = group_table.create_category_group("Discretionary")
        assert result["name"] == "Discretionary"
        assert result["order"] == 2
        assert "active" not in result  # groups no longer have a lifecycle flag
        group_mock.put_item.assert_called_once()

    def test_empty_name_raises(self, group_table: CategoryGroupTable) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            group_table.create_category_group("   ")

    def test_duplicate_name_rejected_case_insensitive(
        self, group_table: CategoryGroupTable, group_mock: MagicMock
    ) -> None:
        group_mock.scan.return_value = {"Items": [{"groupId": "g1", "name": "Essentials", "active": True, "order": 0}]}
        with pytest.raises(ValueError, match="already exists"):
            group_table.create_category_group("essentials")


class TestCategoryGroupReorder:
    def test_writes_index_per_id_in_order(self, group_table: CategoryGroupTable, group_mock: MagicMock) -> None:
        group_table.reorder(["g3", "g1", "g2"])
        assert group_mock.update_item.call_count == 3
        # First call writes order=0 on g3, second order=1 on g1, third order=2 on g2.
        for index, expected_id in enumerate(["g3", "g1", "g2"]):
            call = group_mock.update_item.call_args_list[index]
            assert call.kwargs["Key"] == {"groupId": expected_id}
            assert call.kwargs["ExpressionAttributeValues"][":o"] == index


class TestCategoryCreateRequiresGroup:
    def test_create_with_group_id_assigns_to_group(self, cat_table: CategoryTable, cat_mock: MagicMock) -> None:
        cat_mock.scan.return_value = {"Items": []}
        result = cat_table.create_category("Groceries", group_id="g1")
        assert result["groupId"] == "g1"
        assert result["orderInGroup"] == 0

    def test_create_without_group_raises(self, cat_table: CategoryTable, cat_mock: MagicMock) -> None:
        cat_mock.scan.return_value = {"Items": []}
        with pytest.raises(ValueError, match="group_id is required"):
            cat_table.create_category("Groceries", group_id="")

    def test_orderInGroup_continues_from_existing_max(self, cat_table: CategoryTable, cat_mock: MagicMock) -> None:
        # Group g1 already has two cats with orderInGroup 0 and 1 — new cat gets 2.
        cat_mock.scan.return_value = {
            "Items": [
                {"categoryId": "c1", "name": "A", "active": True, "groupId": "g1", "orderInGroup": 0},
                {"categoryId": "c2", "name": "B", "active": True, "groupId": "g1", "orderInGroup": 1},
                # Cat in a different group shouldn't push the new cat's order.
                {"categoryId": "c3", "name": "X", "active": True, "groupId": "g2", "orderInGroup": 9},
            ]
        }
        result = cat_table.create_category("New", group_id="g1")
        assert result["orderInGroup"] == 2


class TestCategoryMoveAndReorder:
    def test_move_to_group_appends_at_end(self, cat_table: CategoryTable, cat_mock: MagicMock) -> None:
        cat_mock.get_item.return_value = {"Item": {"categoryId": "c1", "name": "X", "groupId": "g1", "orderInGroup": 0}}
        cat_mock.update_item.return_value = {
            "Attributes": {"categoryId": "c1", "name": "X", "groupId": "g2", "orderInGroup": 3}
        }
        # Existing in g2: orders 0..2 → new cat lands at 3.
        cat_mock.scan.return_value = {
            "Items": [
                {"categoryId": "a", "name": "a", "active": True, "groupId": "g2", "orderInGroup": 0},
                {"categoryId": "b", "name": "b", "active": True, "groupId": "g2", "orderInGroup": 1},
                {"categoryId": "c", "name": "c", "active": True, "groupId": "g2", "orderInGroup": 2},
            ]
        }
        result = cat_table.move_to_group("c1", "g2")
        assert result is not None
        kwargs = cat_mock.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":g"] == "g2"
        assert kwargs["ExpressionAttributeValues"][":o"] == 3

    def test_move_to_same_group_is_noop(self, cat_table: CategoryTable, cat_mock: MagicMock) -> None:
        cat_mock.get_item.return_value = {"Item": {"categoryId": "c1", "groupId": "g1", "orderInGroup": 0}}
        result = cat_table.move_to_group("c1", "g1")
        # Identity returned, no update written.
        assert result is not None
        assert result["groupId"] == "g1"
        cat_mock.update_item.assert_not_called()

    def test_reorder_in_group_assigns_indices_and_sets_groupId(
        self, cat_table: CategoryTable, cat_mock: MagicMock
    ) -> None:
        cat_table.reorder_in_group("g2", ["c3", "c1", "c2"])
        assert cat_mock.update_item.call_count == 3
        for index, expected_id in enumerate(["c3", "c1", "c2"]):
            call = cat_mock.update_item.call_args_list[index]
            assert call.kwargs["Key"] == {"categoryId": expected_id}
            assert call.kwargs["ExpressionAttributeValues"][":g"] == "g2"
            assert call.kwargs["ExpressionAttributeValues"][":o"] == index
