"""Tests for compute_summary (Spec 5 membership rules + historical names)."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from corderohq.aws.dynamodb import BudgetTable, CategoryTable, TransactionsTable, compute_summary


def _make_tables() -> tuple[
    BudgetTable,
    TransactionsTable,
    CategoryTable,
    MagicMock,
    MagicMock,
    MagicMock,
]:
    """Return (budget, transactions, category, budget_mock, txn_mock, cat_mock)."""
    bt = BudgetTable.__new__(BudgetTable)
    budget_mock = MagicMock()
    bt._table = budget_mock
    tt = TransactionsTable.__new__(TransactionsTable)
    txn_mock = MagicMock()
    tt._table = txn_mock
    ct = CategoryTable.__new__(CategoryTable)
    cat_mock = MagicMock()
    ct._table = cat_mock
    return bt, tt, ct, budget_mock, txn_mock, cat_mock


# Stable "now" lined up with test_personal_finance_handler so 2026-06 is current.
_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def _cat(cid: str, name: str = "", active: bool = True, history: list[dict[str, str]] | None = None) -> dict:
    return {
        "categoryId": cid,
        "name": name or f"Cat {cid}",
        "active": active,
        "nameHistory": history or [],
    }


class TestComputeSummary:
    def test_computes_budget_vs_actuals(self) -> None:
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {
            "Items": [
                {"categoryId": "cat1", "amount": 1_000_000},
                {"categoryId": "cat2", "amount": 500_000},
            ]
        }
        txn_mock.query.return_value = {
            "Items": [
                {"categoryId": "cat1", "amount": 300_000},
                {"categoryId": "cat1", "amount": 200_000},
                {"categoryId": "cat2", "amount": 600_000},
            ]
        }
        cat_mock.scan.return_value = {"Items": [_cat("cat1"), _cat("cat2")]}

        result = compute_summary(budget, transactions, category_table, "2026-06", _NOW)

        assert result["yearMonth"] == "2026-06"
        assert result["state"] == "EDITABLE"
        cat1 = next(c for c in result["categories"] if c["categoryId"] == "cat1")
        assert cat1["budgeted"] == 1_000_000
        assert cat1["actual"] == 500_000
        assert cat1["delta"] == 500_000
        cat2 = next(c for c in result["categories"] if c["categoryId"] == "cat2")
        assert cat2["delta"] == -100_000

    def test_refund_produces_negative_actual_and_higher_delta(self) -> None:
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {"Items": [{"categoryId": "cat1", "amount": 100_000_000}]}
        txn_mock.query.return_value = {
            "Items": [
                {"categoryId": "cat1", "amount": 5_000_000},
                {"categoryId": "cat1", "amount": -7_000_000},
            ]
        }
        cat_mock.scan.return_value = {"Items": [_cat("cat1")]}

        result = compute_summary(budget, transactions, category_table, "2026-06", _NOW)

        cat1 = result["categories"][0]
        assert cat1["actual"] == -2_000_000
        assert cat1["delta"] == 102_000_000


class TestEditableMonthMembership:
    """Spec 5: editable month = active cats + (inactive with non-zero txn)."""

    def test_includes_active_with_no_txn(self) -> None:
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {"Items": []}
        txn_mock.query.return_value = {"Items": []}
        cat_mock.scan.return_value = {"Items": [_cat("active_nothing")]}

        result = compute_summary(budget, transactions, category_table, "2026-06", _NOW)
        assert [c["categoryId"] for c in result["categories"]] == ["active_nothing"]

    def test_excludes_inactive_with_no_txn(self) -> None:
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        # Inactive category has a budget row but no txn. Editable rule should exclude it.
        budget_mock.query.return_value = {"Items": [{"categoryId": "dead_cat", "amount": 500_000}]}
        txn_mock.query.return_value = {"Items": []}
        cat_mock.scan.return_value = {"Items": [_cat("dead_cat", active=False)]}

        result = compute_summary(budget, transactions, category_table, "2026-06", _NOW)
        assert result["categories"] == []

    def test_includes_inactive_with_nonzero_txn(self) -> None:
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {"Items": []}
        txn_mock.query.return_value = {"Items": [{"categoryId": "dead_cat", "amount": 5_000_000}]}
        cat_mock.scan.return_value = {"Items": [_cat("dead_cat", active=False)]}

        result = compute_summary(budget, transactions, category_table, "2026-06", _NOW)
        assert [c["categoryId"] for c in result["categories"]] == ["dead_cat"]

    def test_excludes_inactive_with_only_zero_txn(self) -> None:
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {"Items": []}
        # amount == 0 doesn't count per Spec 5 ("at least one transaction with amount != 0").
        txn_mock.query.return_value = {"Items": [{"categoryId": "dead_cat", "amount": 0}]}
        cat_mock.scan.return_value = {"Items": [_cat("dead_cat", active=False)]}

        result = compute_summary(budget, transactions, category_table, "2026-06", _NOW)
        assert result["categories"] == []


class TestLockedMonthMembership:
    """Spec 5: locked month = every cat with a budget row OR a transaction."""

    def test_includes_inactive_with_budget_row_no_txn(self) -> None:
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        # Past month, well outside the grace window → LOCKED.
        budget_mock.query.return_value = {"Items": [{"categoryId": "dead_cat", "amount": 500_000}]}
        txn_mock.query.return_value = {"Items": []}
        cat_mock.scan.return_value = {"Items": [_cat("dead_cat", active=False)]}

        result = compute_summary(budget, transactions, category_table, "2026-01", _NOW)
        assert result["state"] == "LOCKED"
        assert [c["categoryId"] for c in result["categories"]] == ["dead_cat"]

    def test_includes_zero_budget_row(self) -> None:
        # Even a $0 budget row makes the category a member on locked months.
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {"Items": [{"categoryId": "cat1", "amount": 0}]}
        txn_mock.query.return_value = {"Items": []}
        cat_mock.scan.return_value = {"Items": [_cat("cat1")]}

        result = compute_summary(budget, transactions, category_table, "2026-01", _NOW)
        assert [c["categoryId"] for c in result["categories"]] == ["cat1"]

    def test_includes_txn_with_zero_amount(self) -> None:
        # Locked rule says "or at least one transaction" — no amount != 0 qualifier.
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {"Items": []}
        txn_mock.query.return_value = {"Items": [{"categoryId": "cat1", "amount": 0}]}
        cat_mock.scan.return_value = {"Items": [_cat("cat1")]}

        result = compute_summary(budget, transactions, category_table, "2026-01", _NOW)
        assert [c["categoryId"] for c in result["categories"]] == ["cat1"]


class TestHistoricalNameResolution:
    """Locked-month historicalName resolution off Category.nameHistory."""

    def test_no_history_returns_null(self) -> None:
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {"Items": [{"categoryId": "cat1", "amount": 100}]}
        txn_mock.query.return_value = {"Items": []}
        cat_mock.scan.return_value = {"Items": [_cat("cat1", name="Groceries")]}

        result = compute_summary(budget, transactions, category_table, "2026-01", _NOW)
        cat = result["categories"][0]
        assert cat["name"] == "Groceries"
        assert cat["historicalName"] is None

    def test_rename_after_month_returns_previous_name(self) -> None:
        # 2026-01 was locked. Rename happened at 2026-03. Locked-month summary for
        # 2026-01 should return historicalName = "Old name".
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {"Items": [{"categoryId": "cat1", "amount": 100}]}
        txn_mock.query.return_value = {"Items": []}
        cat_mock.scan.return_value = {
            "Items": [
                _cat(
                    "cat1",
                    name="Food",
                    history=[{"previousName": "Groceries", "replacedAt": "2026-03-10T12:00:00+00:00"}],
                )
            ]
        }

        result = compute_summary(budget, transactions, category_table, "2026-01", _NOW)
        cat = result["categories"][0]
        assert cat["name"] == "Food"
        assert cat["historicalName"] == "Groceries"

    def test_rename_before_month_returns_null(self) -> None:
        # Rename happened BEFORE 2026-04 (the locked month). Current name was in effect.
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {"Items": [{"categoryId": "cat1", "amount": 100}]}
        txn_mock.query.return_value = {"Items": []}
        cat_mock.scan.return_value = {
            "Items": [
                _cat(
                    "cat1",
                    name="Food",
                    history=[{"previousName": "Groceries", "replacedAt": "2025-12-01T00:00:00+00:00"}],
                )
            ]
        }

        # 2026-04 is well past the grace window relative to _NOW (2026-06-15) so it's LOCKED.
        result = compute_summary(budget, transactions, category_table, "2026-04", _NOW)
        cat = result["categories"][0]
        assert cat["historicalName"] is None

    def test_earliest_post_dating_entry_wins_with_multiple_renames(self) -> None:
        # Multi-rename: Groceries → Food → Eats. Locked-month 2026-01 should pick the
        # earliest replacedAt strictly greater than month-end-utc, which is the Food→Eats
        # transition would be wrong; Groceries→Food is the correct one because its
        # replacedAt is the first one POST-dating 2026-01's end.
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {"Items": [{"categoryId": "cat1", "amount": 100}]}
        txn_mock.query.return_value = {"Items": []}
        cat_mock.scan.return_value = {
            "Items": [
                _cat(
                    "cat1",
                    name="Eats",
                    history=[
                        {"previousName": "Groceries", "replacedAt": "2026-02-10T00:00:00+00:00"},
                        {"previousName": "Food", "replacedAt": "2026-05-10T00:00:00+00:00"},
                    ],
                )
            ]
        }

        result = compute_summary(budget, transactions, category_table, "2026-01", _NOW)
        assert result["categories"][0]["historicalName"] == "Groceries"

    def test_editable_month_never_returns_historical_name(self) -> None:
        # Editable months don't carry historicalName even if history exists, since no
        # rename can have post-dated a still-editable month.
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {"Items": [{"categoryId": "cat1", "amount": 100}]}
        txn_mock.query.return_value = {"Items": []}
        cat_mock.scan.return_value = {
            "Items": [
                _cat(
                    "cat1",
                    name="Food",
                    history=[{"previousName": "Groceries", "replacedAt": "2030-01-01T00:00:00+00:00"}],
                )
            ]
        }

        result = compute_summary(budget, transactions, category_table, "2026-06", _NOW)
        assert result["categories"][0]["historicalName"] is None

    def test_dst_aware_month_end_cutoff(self) -> None:
        # 2024-03 ends on March 31 at 23:59:59.999999 EDT (UTC-4 because DST already
        # started March 10). A rename whose replacedAt is 2024-04-01T03:59:59Z is
        # NOT strictly greater than month_end_utc (2024-04-01T03:59:59.999999Z), so
        # it should NOT be considered post-dating — historicalName must be None.
        # If the cutoff were computed naively as UTC-5 (EST), this test would fail.
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {"Items": [{"categoryId": "cat1", "amount": 100}]}
        txn_mock.query.return_value = {"Items": []}
        cat_mock.scan.return_value = {
            "Items": [
                _cat(
                    "cat1",
                    name="Food",
                    history=[{"previousName": "Groceries", "replacedAt": "2024-04-01T03:59:59+00:00"}],
                )
            ]
        }

        # Use a now far enough in the future that 2024-03 is unambiguously locked.
        far_future_now = datetime(2030, 1, 1, tzinfo=UTC)
        result = compute_summary(budget, transactions, category_table, "2024-03", far_future_now)
        # The rename is BEFORE the EDT-aware month-end (2024-04-01T03:59:59.999999Z),
        # so the current name was in effect during 2024-03.
        assert result["categories"][0]["historicalName"] is None


class TestCurrentMonthInEasternTime:
    def test_last_evening_of_a_month_is_still_that_month(self) -> None:
        # 2026-06-30 at 22:00 ET is 2026-07-01 at 02:00 UTC. The current month is still June,
        # so July is a future month and its summary walks back to the June row.
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.scan.return_value = {"Items": [{"yearMonth": "2026-06", "categoryId": "cat1", "amount": 500}]}
        budget_mock.query.return_value = {"Items": []}
        txn_mock.query.return_value = {"Items": []}
        cat_mock.scan.return_value = {"Items": [_cat("cat1")]}
        late_june = datetime(2026, 7, 1, 2, 0, 0, tzinfo=UTC)

        result = compute_summary(budget, transactions, category_table, "2026-07", late_june)

        assert result["totals"]["budgeted"] == 500


class TestDispositions:
    """Only BUDGET rows are actuals. OFF_BUDGET rows have their own total."""

    def test_off_budget_and_skip_rows_do_not_change_a_category(self) -> None:
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {"Items": [{"categoryId": "cat1", "amount": 1_000_000}]}
        txn_mock.query.return_value = {
            "Items": [
                {"categoryId": "cat1", "amount": 300_000},
                {"categoryId": "cat1", "amount": 400_000, "disposition": "BUDGET"},
                {"categoryId": "cat1", "amount": 5_000_000, "disposition": "OFF_BUDGET"},
                {"amount": 2_000_000, "disposition": "OFF_BUDGET"},
                {"amount": -9_000_000, "disposition": "SKIP"},
            ]
        }
        cat_mock.scan.return_value = {"Items": [_cat("cat1")]}

        result = compute_summary(budget, transactions, category_table, "2026-06", _NOW)

        assert result["categories"][0]["actual"] == 700_000
        assert result["totals"] == {"budgeted": 1_000_000, "actual": 700_000, "delta": 300_000}
        assert result["offBudget"] == {"actual": 7_000_000}
        assert result["totalsWithOffBudget"] == {"budgeted": 1_000_000, "actual": 7_700_000}

    def test_no_off_budget_rows_gives_zero(self) -> None:
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {"Items": [{"categoryId": "cat1", "amount": 1_000_000}]}
        txn_mock.query.return_value = {"Items": [{"categoryId": "cat1", "amount": 300_000}]}
        cat_mock.scan.return_value = {"Items": [_cat("cat1")]}

        result = compute_summary(budget, transactions, category_table, "2026-06", _NOW)

        assert result["offBudget"] == {"actual": 0}
        assert result["totalsWithOffBudget"] == {"budgeted": 1_000_000, "actual": 300_000}

    def test_editable_month_ignores_off_budget_row_of_inactive_category(self) -> None:
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {"Items": []}
        txn_mock.query.return_value = {
            "Items": [{"categoryId": "dead_cat", "amount": 5_000_000, "disposition": "OFF_BUDGET"}]
        }
        cat_mock.scan.return_value = {"Items": [_cat("dead_cat", active=False)]}

        result = compute_summary(budget, transactions, category_table, "2026-06", _NOW)

        assert result["categories"] == []
        assert result["offBudget"] == {"actual": 5_000_000}

    def test_locked_month_ignores_off_budget_and_skip_rows_for_membership(self) -> None:
        budget, transactions, category_table, budget_mock, txn_mock, cat_mock = _make_tables()
        budget_mock.query.return_value = {"Items": []}
        txn_mock.query.return_value = {
            "Items": [
                {"categoryId": "cat1", "amount": 1_000_000, "disposition": "OFF_BUDGET"},
                {"amount": 1_000_000, "disposition": "SKIP"},
                {"categoryId": "cat2", "amount": 0},
            ]
        }
        cat_mock.scan.return_value = {"Items": [_cat("cat1"), _cat("cat2")]}

        result = compute_summary(budget, transactions, category_table, "2026-01", _NOW)

        assert result["state"] == "LOCKED"
        assert [c["categoryId"] for c in result["categories"]] == ["cat2"]
