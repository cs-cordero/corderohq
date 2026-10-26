import pytest

from corderohq.budget.dispositions import Disposition
from corderohq.budget.transactions import UpdateTransactionArgs, parse_update_transaction_args
from corderohq.common import Keep, Remove, SetTo

_STORED = {"yearMonth": "2026-03", "sortId": "s", "categoryId": "cat1", "note": "old"}


class TestParseUpdateTransactionArgs:
    def test_a_plain_change_does_not_touch_the_disposition(self) -> None:
        args = parse_update_transaction_args({"description": " New ", "amount": 5}, _STORED)
        assert args == UpdateTransactionArgs(SetTo("New"), SetTo(5), Keep(), Keep(), Keep(), Keep())

    def test_an_empty_body_changes_nothing(self) -> None:
        assert parse_update_transaction_args({}, _STORED).changes_nothing()

    def test_change_to_skip_removes_the_category_and_keeps_the_note(self) -> None:
        args = parse_update_transaction_args({"disposition": "SKIP"}, _STORED)
        assert args.disposition == SetTo(Disposition.SKIP)
        assert args.category_id == Remove()
        assert args.account_id == Remove()
        assert args.note == SetTo("old")

    def test_an_empty_note_removes_the_note(self) -> None:
        stored = {**_STORED, "disposition": "BUDGET"}
        args = parse_update_transaction_args({"note": ""}, stored)
        assert args.note == Remove()
        assert args.category_id == SetTo("cat1")

    def test_a_rule_failure_raises(self) -> None:
        with pytest.raises(ValueError, match="categoryId is required"):
            parse_update_transaction_args({"disposition": "BUDGET"}, {"yearMonth": "2026-03", "disposition": "SKIP"})

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ({"description": "  "}, "description must not be empty"),
            ({"amount": True}, "amount must be an integer"),
            ({"amount": "5"}, "amount must be an integer"),
        ],
    )
    def test_rejects_bad_values(self, body: dict[str, object], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            parse_update_transaction_args(body, _STORED)
