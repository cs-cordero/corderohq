import pytest

from corderohq.budget.transaction_args import (
    NOTE_MAX_LENGTH,
    CreateTransactionArgs,
    UpdateTransactionArgs,
    parse_create_transaction_args,
    parse_update_transaction_args,
)
from corderohq.budget.treatment import BudgetTreatment, Classification
from corderohq.common import Keep, Remove, SetTo

_BODY = {"transactionDate": "2026-03-15", "description": " Coffee ", "amount": 5, "categoryId": "cat1"}
_STORED = {"yearMonth": "2026-03", "sortId": "s", "categoryId": "cat1", "note": "old"}


class TestParseCreateTransactionArgs:
    def test_parses_the_fields(self) -> None:
        body = {**_BODY, "accountId": " acct1 ", "note": "  work trip  "}
        assert parse_create_transaction_args(body, year_month="2026-03") == CreateTransactionArgs(
            year_month="2026-03",
            transaction_date="2026-03-15",
            description="Coffee",
            amount=5,
            classification=Classification(BudgetTreatment.BUDGET_IMPACTING, "cat1"),
            account_id="acct1",
            note="work trip",
        )

    def test_to_item_leaves_out_absent_values(self) -> None:
        new = parse_create_transaction_args({**_BODY, "budgetTreatment": "SKIP", "note": "  "}, year_month="2026-03")
        assert new.to_item() == {
            "yearMonth": "2026-03",
            "transactionDate": "2026-03-15",
            "description": "Coffee",
            "amount": 5,
            "budgetTreatment": "SKIP",
        }

    def test_budget_without_category_passes_when_not_required(self) -> None:
        body = {key: value for key, value in _BODY.items() if key != "categoryId"}
        new = parse_create_transaction_args(body, year_month="2026-03", require_category=False)
        assert new.classification == Classification(BudgetTreatment.BUDGET_IMPACTING, None)
        with pytest.raises(ValueError, match="categoryId is required"):
            parse_create_transaction_args(body, year_month="2026-03")

    def test_note_length_limit(self) -> None:
        at_limit = "x" * NOTE_MAX_LENGTH
        assert parse_create_transaction_args({**_BODY, "note": at_limit}, year_month="2026-03").note == at_limit
        with pytest.raises(ValueError, match="note must be at most"):
            parse_create_transaction_args({**_BODY, "note": at_limit + "x"}, year_month="2026-03")

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"transactionDate": ""}, "transactionDate is required"),
            ({"description": "  "}, "description must not be empty"),
            ({"amount": True}, "amount must be an integer"),
            ({"amount": "5"}, "amount must be an integer"),
            ({"note": 5}, "note must be a string"),
            ({"accountId": 5}, "accountId must be a string"),
        ],
    )
    def test_rejects_bad_values(self, change: dict[str, object], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            parse_create_transaction_args({**_BODY, **change}, year_month="2026-03")


class TestParseUpdateTransactionArgs:
    def test_a_plain_change_does_not_touch_the_classification(self) -> None:
        args = parse_update_transaction_args({"description": " New ", "amount": 5}, _STORED)
        assert args == UpdateTransactionArgs(SetTo("New"), SetTo(5), Keep(), Keep(), Keep(), Keep())

    def test_an_empty_body_changes_nothing(self) -> None:
        assert parse_update_transaction_args({}, _STORED).changes_nothing()

    def test_change_to_skip_removes_the_category_only(self) -> None:
        args = parse_update_transaction_args({"budgetTreatment": "SKIP"}, _STORED)
        assert args == UpdateTransactionArgs(Keep(), Keep(), SetTo(BudgetTreatment.SKIP), Remove(), Keep(), Keep())

    def test_a_note_change_touches_only_the_note(self) -> None:
        args = parse_update_transaction_args({"note": "  new  "}, _STORED)
        assert args == UpdateTransactionArgs(Keep(), Keep(), Keep(), Keep(), Keep(), SetTo("new"))
        assert parse_update_transaction_args({"note": ""}, _STORED).note == Remove()

    def test_an_account_change_touches_only_the_account(self) -> None:
        assert parse_update_transaction_args({"accountId": "acct1"}, _STORED).account_id == SetTo("acct1")
        assert parse_update_transaction_args({"accountId": None}, _STORED).account_id == Remove()

    def test_a_category_change_is_checked_with_the_stored_budget_treatment(self) -> None:
        stored = {**_STORED, "budgetTreatment": "OFF_BUDGET"}
        args = parse_update_transaction_args({"categoryId": None}, stored)
        assert args.budget_treatment == SetTo(BudgetTreatment.OFF_BUDGET)
        assert args.category_id == Remove()

    def test_skip_row_back_to_budget_needs_a_category(self) -> None:
        stored = {"yearMonth": "2026-03", "sortId": "s", "budgetTreatment": "SKIP"}
        with pytest.raises(ValueError, match="categoryId is required"):
            parse_update_transaction_args({"budgetTreatment": "BUDGET_IMPACTING"}, stored)
        args = parse_update_transaction_args({"budgetTreatment": "BUDGET_IMPACTING", "categoryId": "cat1"}, stored)
        assert args.category_id == SetTo("cat1")

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ({"description": "  "}, "description must not be empty"),
            ({"amount": True}, "amount must be an integer"),
            ({"note": "x" * (NOTE_MAX_LENGTH + 1)}, "note must be at most"),
        ],
    )
    def test_rejects_bad_values(self, body: dict[str, object], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            parse_update_transaction_args(body, _STORED)
