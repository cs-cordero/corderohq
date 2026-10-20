"""Tests for the direction table and the steps that find the other side of a CSV row."""

from typing import Any

import pytest

from corderohq.budget.csv_import.directions import Suggestion, suggest_dispositions
from corderohq.budget.csv_import.local_checks import BankHint

_CHECKING = {"accountId": "chk", "accountType": "checking"}
_CARD = {"accountId": "card", "accountType": "credit_card", "lastFour": ["4321"]}
_VACATION = {"accountId": "vac", "accountType": "savings", "lastFour": ["1234"]}
_BROKERAGE = {"accountId": "brk", "accountType": "brokerage"}
_MORTGAGE = {"accountId": "mtg", "accountType": "mortgage"}
_ACCOUNTS = [_CHECKING, _CARD, _VACATION, _BROKERAGE, _MORTGAGE]


def _row(amount: int, description: str = "x", **extra: Any) -> dict[str, Any]:
    return {"transactionDate": "2026-06-10", "description": description, "amount": amount, "categoryId": "", **extra}


def _one(
    row: dict[str, Any], upload: dict[str, Any] = _CHECKING, saved: list[dict[str, Any]] | None = None
) -> Suggestion:
    return suggest_dispositions([row], upload, _ACCOUNTS, saved or [])[0]


class TestSpendingAccount:
    def test_purchase_is_budget_with_the_llm_category(self) -> None:
        s = _one(_row(500, _kind="expense", categoryId="food"))
        assert (s.disposition, s.category_id, s.suggested_by, s.review) == ("BUDGET", "food", "LLM", [])

    def test_refund_is_budget(self) -> None:
        s = _one(_row(-500, _kind="refund", categoryId="food"))
        assert (s.disposition, s.category_id) == ("BUDGET", "food")

    def test_paycheck_is_skip(self) -> None:
        s = _one(_row(-500, _kind="income", categoryId="food"))
        assert (s.disposition, s.category_id) == ("SKIP", "")

    def test_bank_income_label_wins_over_the_llm(self) -> None:
        s = _one(_row(-500, _bankHint=BankHint.INCOME, _kind="refund"))
        assert (s.disposition, s.suggested_by) == ("SKIP", "BANK")

    def test_card_payment_from_the_llm_is_skip_with_no_mark(self) -> None:
        s = _one(_row(500, _kind="card_payment"))
        assert (s.disposition, s.review) == ("SKIP", [])

    def test_own_transfer_with_a_category_is_budget_and_marked(self) -> None:
        s = _one(_row(500, _kind="own_transfer", categoryId="vacation-fund"))
        assert (s.disposition, s.category_id, s.review) == ("BUDGET", "vacation-fund", ["llm_guess"])

    def test_own_transfer_without_a_category_is_skip_and_marked(self) -> None:
        s = _one(_row(500, _bankHint=BankHint.TRANSFER))
        assert (s.disposition, s.review) == ("SKIP", ["no_pair_found"])

    def test_unknown_direction_is_skip_and_marked(self) -> None:
        s = _one(_row(500))
        assert (s.disposition, s.suggested_by, s.review) == ("SKIP", "DEFAULT", ["unknown_direction"])

    def test_money_in_from_an_own_account_is_skip(self) -> None:
        s = _one(_row(-500, "TRANSFER FROM SAV 1234", _kind="refund"))
        assert (s.disposition, s.suggested_by) == ("SKIP", "LAST_FOUR")


class TestLastFour:
    def test_transfer_to_a_holding_account_is_budget(self) -> None:
        s = _one(_row(500, "ONLINE TRANSFER TO SAV ...1234", _kind="card_payment", categoryId="vacation-fund"))
        assert (s.disposition, s.category_id, s.suggested_by) == ("BUDGET", "vacation-fund", "LAST_FOUR")

    def test_payment_to_a_spending_account_is_skip(self) -> None:
        s = _one(_row(500, "CARD PAYMENT XXXX4321", _kind="expense", categoryId="food"))
        assert (s.disposition, s.suggested_by) == ("SKIP", "LAST_FOUR")

    def test_digits_of_the_upload_account_do_not_count(self) -> None:
        s = _one(_row(500, "PAYMENT 4321", _kind="expense", categoryId="food"), upload=_CARD)
        assert s.suggested_by == "LLM"

    def test_five_digits_are_not_four(self) -> None:
        s = _one(_row(500, "STORE 12345", _kind="expense", categoryId="food"))
        assert s.suggested_by == "LLM"


class TestPairMatch:
    def _saved(self, account_id: str, amount: int, day: str = "2026-06-12", sort_id: str = "s1") -> dict[str, Any]:
        return {
            "yearMonth": "2026-06",
            "sortId": sort_id,
            "accountId": account_id,
            "amount": amount,
            "transactionDate": day,
        }

    def test_one_pair_gives_the_account(self) -> None:
        s = _one(_row(500, _kind="expense", categoryId="food"), saved=[self._saved("card", -500)])
        assert (s.disposition, s.suggested_by) == ("SKIP", "PAIR")
        assert s.pair_with == {"yearMonth": "2026-06", "sortId": "s1"}

    def test_pair_with_a_holding_account_is_budget(self) -> None:
        s = _one(_row(500, _kind="expense", categoryId="fund"), saved=[self._saved("brk", -500)])
        assert (s.disposition, s.category_id, s.suggested_by) == ("BUDGET", "fund", "PAIR")

    def test_pair_with_a_mortgage_counts_as_outside(self) -> None:
        s = _one(_row(500, _kind="expense", categoryId="housing"), saved=[self._saved("mtg", -500)])
        assert (s.disposition, s.category_id) == ("BUDGET", "housing")

    @pytest.mark.parametrize(
        "saved",
        [
            {"accountId": "card", "amount": 500},  # the same sign
            {"accountId": "card", "amount": -499},  # another amount
            {"accountId": "chk", "amount": -500},  # the upload account itself
            {"accountId": "card", "amount": -500, "transactionDate": "2026-06-20"},  # too far apart
        ],
    )
    def test_no_pair(self, saved: dict[str, Any]) -> None:
        txn = {**self._saved("card", -500), **saved}
        s = _one(_row(500, _kind="expense", categoryId="food"), saved=[txn])
        assert s.suggested_by == "LLM"

    def test_many_pairs_mark_the_row_and_fall_back(self) -> None:
        saved = [self._saved("card", -500, sort_id="a"), self._saved("vac", -500, sort_id="b")]
        s = _one(_row(500, _kind="expense", categoryId="food"), saved=saved)
        assert (s.suggested_by, s.review) == ("LLM", ["many_pairs"])

    def test_dates_in_other_formats_still_match(self) -> None:
        row = {**_row(500, _kind="expense"), "transactionDate": "06/10/2026"}
        s = _one(row, saved=[self._saved("card", -500)])
        assert s.suggested_by == "PAIR"


class TestHoldingAccount:
    def test_money_in_is_skip(self) -> None:
        s = _one(_row(-500, "TRANSFER FROM CHECKING", _kind="own_transfer"), upload=_VACATION)
        assert (s.disposition, s.review) == ("SKIP", [])

    def test_payment_to_the_outside_is_off_budget(self) -> None:
        s = _one(_row(500, "HOTEL", _kind="expense", categoryId="travel"), upload=_VACATION)
        assert (s.disposition, s.category_id) == ("OFF_BUDGET", "travel")

    def test_money_out_to_an_own_account_is_skip(self) -> None:
        s = _one(_row(500, "TRANSFER TO CHECKING", _kind="own_transfer"), upload=_VACATION)
        assert s.disposition == "SKIP"
