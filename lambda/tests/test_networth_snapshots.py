"""Tests for the pure net-worth aggregation helpers."""

from corderohq.networth.snapshots import build_history, build_month_view, compute_prefill, row_total


def _row(year_month: str, account_id: str, by_class: dict[str, int], note: str | None = None) -> dict[str, object]:
    """A per-class snapshot row ({byAssetClass: {class: millionths}})."""
    row: dict[str, object] = {"yearMonth": year_month, "accountId": account_id, "byAssetClass": dict(by_class)}
    if note is not None:
        row["note"] = note
    return row


class TestRowTotal:
    def test_sums_by_asset_class(self) -> None:
        row = _row("2026-06", "a1", {"us_equity_large_cap": 300, "cash": 50})
        assert row_total(row) == 350

    def test_empty_map_totals_zero(self) -> None:
        assert row_total({"yearMonth": "2026-06", "accountId": "a1", "byAssetClass": {}}) == 0


class TestComputePrefill:
    def test_picks_most_recent_prior_value_per_account_and_class(self) -> None:
        rows = [
            _row("2026-01", "a1", {"cash": 100}),
            _row("2026-03", "a1", {"cash": 300}),
            _row("2026-02", "a2", {"cash": 50}),
        ]
        result = compute_prefill(rows, "2026-04")
        assert result["a1"]["cash"] == {"value": 300, "fromYearMonth": "2026-03"}
        assert result["a2"]["cash"] == {"value": 50, "fromYearMonth": "2026-02"}

    def test_prefill_is_independent_per_class(self) -> None:
        rows = [
            _row("2026-01", "a1", {"cash": 100}),
            _row("2026-02", "a1", {"us_equity_large_cap": 900}),
        ]
        result = compute_prefill(rows, "2026-03")
        assert result["a1"]["cash"] == {"value": 100, "fromYearMonth": "2026-01"}
        assert result["a1"]["us_equity_large_cap"] == {"value": 900, "fromYearMonth": "2026-02"}

    def test_excludes_target_month_and_future(self) -> None:
        rows = [
            _row("2026-03", "a1", {"cash": 300}),  # target month itself — not a prefill
            _row("2026-05", "a1", {"cash": 500}),  # future — ignored
            _row("2026-02", "a1", {"cash": 200}),  # prior — this is the prefill
        ]
        result = compute_prefill(rows, "2026-03")
        assert result["a1"]["cash"] == {"value": 200, "fromYearMonth": "2026-02"}

    def test_empty_when_no_prior_rows(self) -> None:
        assert compute_prefill([_row("2026-03", "a1", {"cash": 1})], "2026-01") == {}


class TestBuildMonthView:
    def _accounts(self) -> list[dict[str, object]]:
        return [
            {"accountId": "a1", "name": "Checking", "active": True, "sortOrder": 0, "assetClasses": ["cash"]},
            {
                "accountId": "a2",
                "name": "Old 401k",
                "active": False,
                "sortOrder": 1,
                "assetClasses": ["us_equity_large_cap"],
            },
            {
                "accountId": "a3",
                "name": "Brokerage",
                "active": True,
                "sortOrder": 2,
                "assetClasses": ["us_equity_large_cap", "cash"],
            },
        ]

    def test_includes_active_and_inactive_with_value(self) -> None:
        month_rows = [_row("2026-06", "a2", {"us_equity_large_cap": 900})]  # inactive but recorded
        result = build_month_view(self._accounts(), month_rows, {})
        assert [r["accountId"] for r in result] == ["a1", "a2", "a3"]

    def test_excludes_inactive_without_value(self) -> None:
        result = build_month_view(self._accounts(), [], {})
        assert [r["accountId"] for r in result] == ["a1", "a3"]

    def test_per_class_entries_with_value_and_prefill(self) -> None:
        month_rows = [_row("2026-06", "a3", {"us_equity_large_cap": 250})]
        prefill = {"a3": {"cash": {"value": 50, "fromYearMonth": "2026-05"}}}
        result = build_month_view(self._accounts(), month_rows, prefill)
        a3 = next(r for r in result if r["accountId"] == "a3")
        by_class = {c["assetClass"]: c for c in a3["classes"]}
        # Active classes in order: us_equity_large_cap (valued this month), then cash (carried).
        assert [c["assetClass"] for c in a3["classes"]] == ["us_equity_large_cap", "cash"]
        assert by_class["us_equity_large_cap"]["value"] == 250
        assert by_class["us_equity_large_cap"]["prefill"] is None
        assert by_class["cash"]["value"] is None
        assert by_class["cash"]["prefill"] == {"value": 50, "fromYearMonth": "2026-05"}

    def test_shows_valued_class_outside_active_set(self) -> None:
        # a1 is active with only [cash], but has a value this month in a class it no
        # longer tracks — that class still appears (removal keeps history editable).
        month_rows = [_row("2026-06", "a1", {"cash": 100, "us_equity_large_cap": 40})]
        result = build_month_view(self._accounts(), month_rows, {})
        a1 = next(r for r in result if r["accountId"] == "a1")
        assert [c["assetClass"] for c in a1["classes"]] == ["cash", "us_equity_large_cap"]

    def test_row_carries_note(self) -> None:
        month_rows = [_row("2026-06", "a1", {"cash": 250}, note="quarterly bonus")]
        result = build_month_view(self._accounts(), month_rows, {})
        by_id = {r["accountId"]: r for r in result}
        assert by_id["a1"]["note"] == "quarterly bonus"
        assert by_id["a3"]["note"] is None


class TestBuildHistory:
    def _accounts(self) -> list[dict[str, object]]:
        return [
            {"accountId": "a1", "name": "Checking", "active": True, "liability": False, "sortOrder": 0},
            {"accountId": "a2", "name": "Mortgage", "active": True, "liability": True, "sortOrder": 1},
            {"accountId": "a3", "name": "Closed", "active": False, "liability": False, "sortOrder": 2},
        ]

    def test_totals_net_assets_minus_liabilities(self) -> None:
        rows = [
            _row("2026-05", "a1", {"cash": 1000}),
            _row("2026-05", "a2", {"other": 400}),
            _row("2026-06", "a1", {"cash": 1200}),
            _row("2026-06", "a2", {"other": 390}),
        ]
        history = build_history(self._accounts(), rows)
        assert history["months"] == ["2026-05", "2026-06"]
        assert history["totals"]["2026-05"] == {"assets": 1000, "liabilities": 400, "netWorth": 600}
        assert history["totals"]["2026-06"] == {"assets": 1200, "liabilities": 390, "netWorth": 810}
        assert history["values"]["2026-06"]["a1"] == 1200

    def test_rolls_up_multi_class_account(self) -> None:
        accounts = [{"accountId": "a1", "name": "Roth", "active": True, "liability": False, "sortOrder": 0}]
        rows = [_row("2026-06", "a1", {"us_equity_large_cap": 300, "cash": 50})]
        history = build_history(accounts, rows)
        assert history["values"]["2026-06"]["a1"] == 350
        assert history["totals"]["2026-06"]["netWorth"] == 350

    def test_included_accounts_are_active_or_have_history(self) -> None:
        rows = [_row("2026-05", "a1", {"cash": 1000})]
        history = build_history(self._accounts(), rows)
        assert [a["accountId"] for a in history["accounts"]] == ["a1", "a2"]

        rows_with_a3 = [*rows, _row("2020-01", "a3", {"cash": 77})]
        history2 = build_history(self._accounts(), rows_with_a3)
        assert "a3" in [a["accountId"] for a in history2["accounts"]]

    def test_empty_history(self) -> None:
        history = build_history(self._accounts(), [])
        assert history["months"] == []
        assert history["values"] == {}
        assert history["notes"] == {}
        assert history["totals"] == {}
        assert [a["accountId"] for a in history["accounts"]] == ["a1", "a2"]

    def test_excluded_accounts_kept_in_values_but_dropped_from_totals(self) -> None:
        accounts = [
            {"accountId": "a1", "name": "Checking", "active": True, "liability": False, "sortOrder": 0},
            {
                "accountId": "a4",
                "name": "Kid 529",
                "active": True,
                "liability": False,
                "sortOrder": 3,
                "excludedFromNetWorth": True,
            },
        ]
        rows = [_row("2026-05", "a1", {"cash": 1000}), _row("2026-05", "a4", {"us_equity_large_cap": 5000})]
        history = build_history(accounts, rows)
        assert history["values"]["2026-05"]["a4"] == 5000  # still shown
        assert history["totals"]["2026-05"] == {"assets": 1000, "liabilities": 0, "netWorth": 1000}  # not counted

    def test_notes_only_for_cells_with_a_note(self) -> None:
        rows = [
            _row("2026-05", "a1", {"cash": 1000}, note="opened account"),
            _row("2026-06", "a1", {"cash": 1200}),
        ]
        history = build_history(self._accounts(), rows)
        assert history["notes"]["2026-05"]["a1"] == "opened account"
        assert "2026-06" not in history["notes"]
