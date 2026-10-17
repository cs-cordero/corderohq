"""Tests for the disposition rules of a transaction."""

import pytest

from corderohq.budget.dispositions import (
    DISPOSITION_BUDGET,
    DISPOSITION_OFF_BUDGET,
    DISPOSITION_SKIP,
    NOTE_MAX_LENGTH,
    disposition_of,
    resolve_disposition_fields,
)


class TestDispositionOf:
    def test_absent_value_means_budget(self) -> None:
        assert disposition_of({"categoryId": "cat1"}) == DISPOSITION_BUDGET

    def test_returns_stored_value(self) -> None:
        assert disposition_of({"disposition": DISPOSITION_SKIP}) == DISPOSITION_SKIP


class TestResolveNewRow:
    def test_default_is_budget(self) -> None:
        fields = resolve_disposition_fields({"categoryId": "cat1"})
        assert fields.disposition == DISPOSITION_BUDGET
        assert fields.category_id == "cat1"

    def test_rejects_unknown_disposition(self) -> None:
        with pytest.raises(ValueError, match="disposition must be one of"):
            resolve_disposition_fields({"disposition": "IGNORED", "categoryId": "cat1"})

    def test_budget_needs_a_category(self) -> None:
        with pytest.raises(ValueError, match="categoryId is required"):
            resolve_disposition_fields({"disposition": DISPOSITION_BUDGET})

    def test_budget_with_blank_category_needs_a_category(self) -> None:
        with pytest.raises(ValueError, match="categoryId is required"):
            resolve_disposition_fields({"categoryId": "  "})

    def test_budget_without_category_passes_when_not_required(self) -> None:
        fields = resolve_disposition_fields({}, require_category=False)
        assert fields.disposition == DISPOSITION_BUDGET
        assert fields.category_id is None

    def test_off_budget_category_is_optional(self) -> None:
        fields = resolve_disposition_fields({"disposition": DISPOSITION_OFF_BUDGET})
        assert fields.category_id is None
        with_category = resolve_disposition_fields({"disposition": DISPOSITION_OFF_BUDGET, "categoryId": "cat1"})
        assert with_category.category_id == "cat1"

    def test_skip_drops_the_category(self) -> None:
        fields = resolve_disposition_fields({"disposition": DISPOSITION_SKIP, "categoryId": "cat1"})
        assert fields.category_id is None

    def test_note_is_trimmed_and_blank_note_is_absent(self) -> None:
        assert resolve_disposition_fields({"categoryId": "cat1", "note": "  work trip  "}).note == "work trip"
        assert resolve_disposition_fields({"categoryId": "cat1", "note": "   "}).note is None

    def test_note_length_limit(self) -> None:
        at_limit = "x" * NOTE_MAX_LENGTH
        assert resolve_disposition_fields({"categoryId": "cat1", "note": at_limit}).note == at_limit
        with pytest.raises(ValueError, match="note must be at most"):
            resolve_disposition_fields({"categoryId": "cat1", "note": at_limit + "x"})

    def test_rejects_value_that_is_not_a_string(self) -> None:
        with pytest.raises(ValueError, match="note must be a string"):
            resolve_disposition_fields({"categoryId": "cat1", "note": 5})
        with pytest.raises(ValueError, match="accountId must be a string"):
            resolve_disposition_fields({"categoryId": "cat1", "accountId": 5})


class TestResolveOverStoredRow:
    def test_keeps_stored_values_that_do_not_change(self) -> None:
        stored = {"disposition": DISPOSITION_OFF_BUDGET, "categoryId": "cat1", "note": "work trip"}
        fields = resolve_disposition_fields({"accountId": "acct1"}, base=stored)
        assert fields.disposition == DISPOSITION_OFF_BUDGET
        assert fields.category_id == "cat1"
        assert fields.account_id == "acct1"
        assert fields.note == "work trip"

    def test_old_row_without_disposition_is_budget(self) -> None:
        fields = resolve_disposition_fields({"note": "x"}, base={"categoryId": "cat1"})
        assert fields.disposition == DISPOSITION_BUDGET
        assert fields.category_id == "cat1"

    def test_change_to_none_wins_over_stored_value(self) -> None:
        stored = {"disposition": DISPOSITION_OFF_BUDGET, "categoryId": "cat1", "note": "work trip"}
        fields = resolve_disposition_fields({"note": None, "categoryId": None}, base=stored)
        assert fields.note is None
        assert fields.category_id is None

    def test_skip_row_back_to_budget_needs_a_category(self) -> None:
        stored = {"disposition": DISPOSITION_SKIP}
        with pytest.raises(ValueError, match="categoryId is required"):
            resolve_disposition_fields({"disposition": DISPOSITION_BUDGET}, base=stored)
        fields = resolve_disposition_fields({"disposition": DISPOSITION_BUDGET, "categoryId": "cat1"}, base=stored)
        assert fields.category_id == "cat1"


class TestStoredValues:
    def test_lists_present_values_and_absent_attributes(self) -> None:
        fields = resolve_disposition_fields({"disposition": DISPOSITION_SKIP, "note": "paycheck"})
        assert fields.stored_values() == {"disposition": DISPOSITION_SKIP, "note": "paycheck"}
        assert fields.absent_attributes() == ["categoryId", "accountId"]
