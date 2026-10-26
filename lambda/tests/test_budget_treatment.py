import pytest

from corderohq.budget.treatment import BudgetTreatment, Classification, budget_treatment_of, parse_classification


class TestBudgetTreatmentOf:
    def test_absent_value_means_budget(self) -> None:
        assert budget_treatment_of({"categoryId": "cat1"}) == BudgetTreatment.BUDGET

    def test_returns_stored_value(self) -> None:
        assert budget_treatment_of({"budgetTreatment": "SKIP"}) == BudgetTreatment.SKIP


class TestParseClassification:
    def test_absent_budget_treatment_is_budget(self) -> None:
        assert parse_classification(None, "cat1") == Classification(BudgetTreatment.BUDGET, "cat1")
        assert parse_classification("", "cat1") == Classification(BudgetTreatment.BUDGET, "cat1")

    def test_rejects_unknown_budget_treatment(self) -> None:
        with pytest.raises(ValueError, match="budgetTreatment must be one of"):
            parse_classification("IGNORED", "cat1")

    @pytest.mark.parametrize("category_id", [None, "", "  "])
    def test_budget_needs_a_category(self, category_id: object) -> None:
        with pytest.raises(ValueError, match="categoryId is required"):
            parse_classification(BudgetTreatment.BUDGET, category_id)

    def test_budget_without_category_passes_when_not_required(self) -> None:
        assert parse_classification(None, None, require_category=False) == Classification(BudgetTreatment.BUDGET, None)

    def test_off_budget_category_is_optional(self) -> None:
        assert parse_classification("OFF_BUDGET", None) == Classification(BudgetTreatment.OFF_BUDGET, None)
        assert parse_classification("OFF_BUDGET", " cat1 ") == Classification(BudgetTreatment.OFF_BUDGET, "cat1")

    def test_skip_drops_the_category(self) -> None:
        assert parse_classification("SKIP", "cat1") == Classification(BudgetTreatment.SKIP, None)

    def test_rejects_a_category_that_is_not_a_string(self) -> None:
        with pytest.raises(ValueError, match="categoryId must be a string"):
            parse_classification("BUDGET", 5)
