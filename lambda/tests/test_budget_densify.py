from typing import Any
from unittest.mock import MagicMock

from corderohq.aws.dynamodb import BudgetDensifyAdapter, BudgetTable, CategoryTable
from corderohq.budget.densify import (
    ResolvedValue,
    densify,
    effective_value,
    resolve_future,
    resolve_future_targets,
    value_at_or_before,
)


def _make_budget_table(rows: list[dict[str, object]]) -> tuple[BudgetTable, MagicMock]:
    bt = BudgetTable.__new__(BudgetTable)
    mock = MagicMock()
    bt._table = mock
    mock.scan.return_value = {"Items": rows}
    return bt, mock


def _make_category_table(categories: list[dict[str, object]]) -> tuple[CategoryTable, MagicMock]:
    ct = CategoryTable.__new__(CategoryTable)
    mock = MagicMock()
    ct._table = mock

    def scan(**params: Any) -> dict[str, Any]:
        # Apply the `active` filter of get_active_categories(), the same as DynamoDB does.
        if params.get("FilterExpression") == "active = :val":
            wanted = params["ExpressionAttributeValues"][":val"]
            return {"Items": [c for c in categories if c.get("active") == wanted]}
        return {"Items": categories}

    mock.scan.side_effect = scan
    return ct, mock


def _densify(budget: BudgetTable, cats: CategoryTable, current_ym: str) -> int:
    """Call `densify` the same way as the handler and the scheduled Lambda."""

    active_ids = [c["categoryId"] for c in cats.get_active_categories()]
    return densify(BudgetDensifyAdapter(budget), active_ids, current_ym, cold_start_value=0)


def _resolve(budget: BudgetTable, cats: CategoryTable, year_month: str) -> list[dict[str, object]]:
    """Call `resolve_future_targets` the same way as the handler."""

    return resolve_future_targets(
        BudgetDensifyAdapter(budget), [c["categoryId"] for c in cats.get_active_categories()], year_month
    )


def _cat(category_id: str, active: bool = True) -> dict[str, object]:
    return {"categoryId": category_id, "name": f"Cat {category_id}", "active": active}


def _row(ym: str, cat: str, amount: int) -> dict[str, object]:
    return {"yearMonth": ym, "categoryId": cat, "carriedAmount": amount}


class TestDensifyColdStart:
    def test_writes_zero_for_active_categories_at_current_ym(self) -> None:
        budget, _ = _make_budget_table([])
        cats, _ = _make_category_table([_cat("a"), _cat("b"), _cat("c", active=False)])

        written = _densify(budget, cats, "2026-06")

        # 2 active categories → 2 writes at $0.
        assert written == 2

    def test_noop_when_no_categories_exist_either(self) -> None:
        budget, _ = _make_budget_table([])
        cats, _ = _make_category_table([])
        assert _densify(budget, cats, "2026-06") == 0


class TestDensifyWalkForward:
    def test_idempotent_when_table_already_current(self) -> None:
        budget, _ = _make_budget_table([_row("2026-06", "a", 500)])
        cats, _ = _make_category_table([_cat("a")])
        assert _densify(budget, cats, "2026-06") == 0

    def test_fills_gap_between_m_last_and_current(self) -> None:
        # M_last = 2026-03, current = 2026-06. Need to fill 04, 05, 06.
        budget, mock_b = _make_budget_table([_row("2026-03", "a", 500), _row("2026-03", "b", 300)])
        cats, _ = _make_category_table([_cat("a"), _cat("b")])

        written = _densify(budget, cats, "2026-06")

        # 3 months x 2 categories = 6 rows.
        assert written == 6
        assert mock_b.put_item.call_count == 6

    def test_walk_back_preserves_amount(self) -> None:
        # A had a row in 2026-03 with $500. Walk-forward to 2026-05 should write $500.
        budget, mock_b = _make_budget_table([_row("2026-03", "a", 500)])
        cats, _ = _make_category_table([_cat("a")])

        _densify(budget, cats, "2026-05")

        written_amounts = [call.kwargs["Item"]["carriedAmount"] for call in mock_b.put_item.call_args_list]
        assert written_amounts == [500, 500]

    def test_walk_forward_starts_after_m_last_and_keeps_an_own_value(self) -> None:
        # 2026-05 is already dense for cat a (its own value). With current_ym = 2026-06,
        # M_last = 2026-05 and walk-forward only fills 2026-06. The value at 2026-05 must
        # not be overwritten and 2026-04 stays untouched (it was a historical hole that
        # densification can't retroactively backfill — locked months are frozen).
        rows = [_row("2026-03", "a", 500), _row("2026-05", "a", 999)]
        budget, mock_b = _make_budget_table(rows)
        cats, _ = _make_category_table([_cat("a")])

        _densify(budget, cats, "2026-06")

        written_keys = [
            (call.kwargs["Item"]["yearMonth"], call.kwargs["Item"]["carriedAmount"])
            for call in mock_b.put_item.call_args_list
        ]
        assert written_keys == [("2026-06", 999)]

    def test_inactive_category_skipped_in_walk_forward(self) -> None:
        # B is deactivated; densification should not write rows for it even though
        # an older row exists.
        rows = [_row("2026-03", "a", 500), _row("2026-03", "b", 300)]
        budget, mock_b = _make_budget_table(rows)
        cats, _ = _make_category_table([_cat("a"), _cat("b", active=False)])

        _densify(budget, cats, "2026-05")

        written_cats = {call.kwargs["Item"]["categoryId"] for call in mock_b.put_item.call_args_list}
        assert written_cats == {"a"}


class TestResolveFutureTargets:
    def test_returns_inherited_false_when_the_month_has_its_own_row(self) -> None:
        budget, _ = _make_budget_table([_row("2026-06", "a", 500), _row("2026-09", "a", 1234)])
        cats, _ = _make_category_table([_cat("a")])

        result = _resolve(budget, cats, "2026-09")

        assert len(result) == 1
        assert result[0]["carriedAmount"] == 1234
        assert result[0]["inherited"] is False

    def test_walk_back_returns_inherited_true(self) -> None:
        budget, _ = _make_budget_table([_row("2026-06", "a", 500)])
        cats, _ = _make_category_table([_cat("a")])

        result = _resolve(budget, cats, "2026-09")

        assert result[0]["carriedAmount"] == 500
        assert result[0]["inherited"] is True

    def test_walk_back_stops_at_the_first_month_with_its_own_value(self) -> None:
        # 2026-06=500, and 2026-07 holds its own 777. Lookup 2026-09 should find 777, not 500.
        rows = [_row("2026-06", "a", 500), _row("2026-07", "a", 777)]
        budget, _ = _make_budget_table(rows)
        cats, _ = _make_category_table([_cat("a")])

        result = _resolve(budget, cats, "2026-09")

        assert result[0]["carriedAmount"] == 777
        assert result[0]["inherited"] is True

    def test_omits_categories_with_no_history(self) -> None:
        budget, _ = _make_budget_table([])
        cats, _ = _make_category_table([_cat("a")])

        result = _resolve(budget, cats, "2026-09")

        assert result == []

    def test_omits_inactive_categories(self) -> None:
        budget, _ = _make_budget_table([_row("2026-06", "a", 500), _row("2026-06", "b", 300)])
        cats, _ = _make_category_table([_cat("a"), _cat("b", active=False)])

        result = _resolve(budget, cats, "2026-09")

        cat_ids = {r["categoryId"] for r in result}
        assert cat_ids == {"a"}


class _MemoryStore:
    """A `DenseSparseStore` in memory. The densify code must not depend on the Budget table."""

    def __init__(
        self, rows: list[tuple[str, str, object]], pinned: list[tuple[str, str, object]] | None = None
    ) -> None:
        self.rows = list(rows)
        self.pinned = list(pinned or [])

    def scan_stored_values(self) -> list[tuple[str, str, object]]:
        return list(self.rows)

    def scan_pinned_values(self) -> list[tuple[str, str, object]]:
        return list(self.pinned)

    def put_stored_value(self, year_month: str, key: str, value: object) -> None:
        self.rows.append((year_month, key, value))


class TestGenericStore:
    def test_a_value_set_in_advance_does_not_stop_its_month_from_densifying(self) -> None:
        # In June, the user set August for "food" only. When August arrives, "rent" still gets its row.
        store = _MemoryStore([("2026-06", "food", 500), ("2026-06", "rent", 2000), ("2026-08", "food", 700)])
        densify(store, ["food", "rent"], "2026-07", cold_start_value=0)
        densify(store, ["food", "rent"], "2026-08", cold_start_value=0)
        assert ("2026-08", "rent", 2000) in store.rows
        assert [row for row in store.rows if row[:2] == ("2026-08", "food")] == [("2026-08", "food", 700)]

    def test_cold_start_keeps_a_value_set_in_advance(self) -> None:
        store = _MemoryStore([("2026-06", "x", "set")])
        assert densify(store, ["x", "y"], "2026-06", cold_start_value="start") == 1
        assert store.rows == [("2026-06", "x", "set"), ("2026-06", "y", "start")]

    def test_cold_start_writes_the_given_value(self) -> None:
        store = _MemoryStore([])
        assert densify(store, ["x"], "2026-06", cold_start_value="start") == 1
        assert store.rows == [("2026-06", "x", "start")]

    def test_walk_forward_copies_any_value_type(self) -> None:
        payment = {"gross": 100, "deductions": []}
        store = _MemoryStore([("2026-04", "x", payment)])
        densify(store, ["x"], "2026-06", cold_start_value=None)
        assert store.rows[1:] == [("2026-05", "x", payment), ("2026-06", "x", payment)]

    def test_resolve_future_gives_own_and_inherited_values(self) -> None:
        store = _MemoryStore([("2026-06", "x", 1), ("2026-06", "y", 2), ("2026-09", "y", 3)])
        result = resolve_future(store, ["x", "y"], "2026-09")
        assert result == [
            ResolvedValue(key="x", value=1, inherited=True),
            ResolvedValue(key="y", value=3, inherited=False),
        ]


class TestValueAtOrBefore:
    def test_uses_the_row_in_the_month_first(self) -> None:
        by_key = {("2026-06", "x"): 1, ("2026-09", "x"): 2}
        assert value_at_or_before(by_key, "2026-09", "x") == 2

    def test_walks_back_when_the_month_has_no_row(self) -> None:
        by_key = {("2026-06", "x"): 1}
        assert value_at_or_before(by_key, "2026-09", "x") == 1

    def test_returns_none_with_no_history(self) -> None:
        assert value_at_or_before({}, "2026-09", "x") is None


class TestPinnedValues:
    """A pinned value applies to its own month, and never carries forward."""

    def test_resolve_future_reports_the_pinned_value_and_keeps_the_carried_one(self) -> None:
        store = _MemoryStore([("2026-06", "x", 100)], pinned=[("2026-09", "x", 50)])
        [resolved] = resolve_future(store, ["x"], "2026-09")
        assert resolved == ResolvedValue(key="x", value=100, inherited=True, pinned_value=50, has_pinned_value=True)
        assert resolved.effective == 50

    def test_the_next_month_goes_back_to_the_carried_value(self) -> None:
        store = _MemoryStore([("2026-06", "x", 100)], pinned=[("2026-09", "x", 50)])
        [resolved] = resolve_future(store, ["x"], "2026-10")
        assert (resolved.effective, resolved.has_pinned_value) == (100, False)

    def test_a_key_with_only_a_pinned_value_is_kept(self) -> None:
        store = _MemoryStore([], pinned=[("2026-09", "x", 50)])
        [resolved] = resolve_future(store, ["x"], "2026-09")
        assert (resolved.value, resolved.effective) == (None, 50)

    def test_densify_ignores_pinned_values(self) -> None:
        store = _MemoryStore([("2026-04", "x", 100)], pinned=[("2026-05", "x", 50)])
        densify(store, ["x"], "2026-06", cold_start_value=None)
        assert store.rows[1:] == [("2026-05", "x", 100), ("2026-06", "x", 100)]

    def test_effective_value(self) -> None:
        carried = {("2026-06", "x"): 100}
        pinned = {("2026-09", "x"): 50}
        assert effective_value(carried, pinned, "2026-09", "x") == 50
        assert effective_value(carried, pinned, "2026-10", "x") == 100

    def test_budget_store_keeps_the_pinned_amount_when_densify_fills_the_month(self) -> None:
        # 2026-06 has only a pinned amount. Densify writes its carried target, and the
        # row must keep the pinned amount.
        budget, mock_b = _make_budget_table(
            [_row("2026-05", "a", 500), {"yearMonth": "2026-06", "categoryId": "a", "pinnedAmount": 80}]
        )
        cats, _ = _make_category_table([_cat("a")])

        _densify(budget, cats, "2026-06")

        items = [call.kwargs["Item"] for call in mock_b.put_item.call_args_list]
        assert items == [{"yearMonth": "2026-06", "categoryId": "a", "carriedAmount": 500, "pinnedAmount": 80}]
