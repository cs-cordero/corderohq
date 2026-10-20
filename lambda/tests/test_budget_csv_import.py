"""Tests for LLM-powered CSV import."""

import json
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("SSM_API_KEY_PARAM", "/budget/anthropic-api-key")

from corderohq.budget.csv_import import (
    BankHint,
    assign_fingerprints,
    bank_category_hint,
    categorize_transactions,
    detect_column_types,
    map_columns,
    parse_amount,
    spending_account_needs_flip,
)


def _mock_claude_response(text: str) -> MagicMock:
    """Create a mock Claude API response with the given text."""
    from anthropic.types import TextBlock

    mock_block = MagicMock(spec=TextBlock)
    mock_block.text = text
    mock_response = MagicMock()
    mock_response.content = [mock_block]
    mock_response.stop_reason = "end_turn"
    return mock_response


class TestMapColumns:
    @patch("corderohq.budget.csv_import.llm.get_ssm_param", return_value="sk-ant-test")
    @patch("corderohq.budget.csv_import.llm.anthropic.Anthropic")
    def test_returns_column_mapping(self, mock_anthropic_cls: MagicMock, mock_ssm: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_claude_response(
            json.dumps({"date": "Trans Date", "description": "Description", "amount": "Amount"})
        )

        result = map_columns(headers=["Trans Date", "Description", "Amount"], column_types=["date", "text", "number"])

        assert result["date"] == "Trans Date"
        assert result["description"] == "Description"
        assert result["amount"] == "Amount"

    @patch("corderohq.budget.csv_import.llm.get_ssm_param", return_value="sk-ant-test")
    @patch("corderohq.budget.csv_import.llm.anthropic.Anthropic")
    def test_handles_amount_invert_flag(self, mock_anthropic_cls: MagicMock, mock_ssm: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_claude_response(
            json.dumps(
                {
                    "date": "Date",
                    "description": "Desc",
                    "amount": "Amount",
                    "amount_invert": True,
                }
            )
        )

        result = map_columns(["Date", "Desc", "Amount"], ["date", "text", "number"])
        assert result["amount_invert"] is True

    @patch("corderohq.budget.csv_import.llm.get_ssm_param", return_value="sk-ant-test")
    @patch("corderohq.budget.csv_import.llm.anthropic.Anthropic")
    def test_prompt_has_headers_and_types_only(self, mock_anthropic_cls: MagicMock, mock_ssm: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_claude_response(json.dumps({"date": "Date"}))

        map_columns(["Date", "Desc", "Amount"], ["date", "text", "number"])

        sent = json.dumps(mock_client.messages.create.call_args.kwargs["messages"])
        assert "Date (date), Desc (text), Amount (number)" in sent


class TestCategorizeTransactions:
    @patch("corderohq.budget.csv_import.llm.get_ssm_param", return_value="sk-ant-test")
    @patch("corderohq.budget.csv_import.llm.anthropic.Anthropic")
    def test_returns_category_assignments(self, mock_anthropic_cls: MagicMock, mock_ssm: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_claude_response(
            json.dumps(
                [
                    {"index": 0, "kind": "expense", "categoryId": "cat1", "categoryName": "Groceries"},
                    {"index": 1, "kind": "expense", "categoryId": "cat2", "categoryName": "Dining"},
                ]
            )
        )

        categories = [
            {"categoryId": "cat1", "name": "Groceries"},
            {"categoryId": "cat2", "name": "Dining"},
        ]
        result = categorize_transactions(["Grocery store", "Diner"], categories)

        assert len(result) == 2
        assert result[0]["categoryId"] == "cat1"
        assert result[1]["categoryId"] == "cat2"
        assert result[0]["kind"] == "expense"

    @patch("corderohq.budget.csv_import.llm.get_ssm_param", return_value="sk-ant-test")
    @patch("corderohq.budget.csv_import.llm.anthropic.Anthropic")
    def test_sends_each_description_once_and_maps_back(
        self, mock_anthropic_cls: MagicMock, mock_ssm: MagicMock
    ) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_claude_response(
            json.dumps(
                [
                    {"index": 0, "kind": "expense", "categoryId": "cat1", "categoryName": "Groceries"},
                    {"index": 1, "kind": "income", "categoryId": None, "categoryName": None},
                ]
            )
        )
        categories = [{"categoryId": "cat1", "name": "Groceries"}]

        result = categorize_transactions(["Grocery store", "Payroll", "Grocery store"], categories)

        sent = mock_client.messages.create.call_args.kwargs["messages"][0]["content"][0]["text"]
        assert sent.count("Grocery store") == 1
        assert [r["kind"] for r in result] == ["expense", "income", "expense"]
        assert [r["categoryId"] for r in result] == ["cat1", "", "cat1"]
        assert [r["index"] for r in result] == [0, 1, 2]

    @patch("corderohq.budget.csv_import.llm.get_ssm_param", return_value="sk-ant-test")
    @patch("corderohq.budget.csv_import.llm.anthropic.Anthropic")
    def test_unknown_kind_and_category_are_dropped(self, mock_anthropic_cls: MagicMock, mock_ssm: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_claude_response(
            json.dumps([{"index": 0, "kind": "salary", "categoryId": "made-up", "categoryName": "X"}])
        )

        result = categorize_transactions(["Store"], [{"categoryId": "cat1", "name": "Groceries"}])

        assert result == [{"index": 0, "kind": None, "categoryId": "", "categoryName": ""}]


class TestLocalChecks:
    def test_detect_column_types(self) -> None:
        headers = ["Date", "Description", "Amount", "Category"]
        rows = [
            ["03/15/2026", "Coffee", "$5.50", "Dining"],
            ["2026-03-16", "Payroll", "(1,000.00)", "Income"],
        ]
        assert detect_column_types(headers, rows) == ["date", "text", "number", "text"]

    def test_empty_column_is_text(self) -> None:
        assert detect_column_types(["A"], [[""], [""]]) == ["text"]

    def test_parse_amount(self) -> None:
        assert parse_amount("$1,234.50") == 1234.5
        assert parse_amount("(5.00)") == -5.0
        assert parse_amount("-5") == -5.0
        assert parse_amount("") is None
        assert parse_amount("abc") is None

    def test_spending_account_needs_flip_when_most_rows_are_negative(self) -> None:
        assert spending_account_needs_flip([-5, -6, 7]) is True
        assert spending_account_needs_flip([5, 6, -7]) is False
        assert spending_account_needs_flip([5, -5]) is False

    def test_bank_category_hint(self) -> None:
        assert bank_category_hint("Payroll Deposit") == BankHint.INCOME
        assert bank_category_hint("Interest Paid") == BankHint.INCOME
        assert bank_category_hint("Credit Card Payment") == BankHint.CARD_PAYMENT
        assert bank_category_hint("Transfer") == BankHint.TRANSFER
        assert bank_category_hint("Groceries") is None
        assert bank_category_hint(None) is None


class TestFingerprints:
    def test_same_row_in_two_files_gets_the_same_fingerprint(self) -> None:
        row = {"transactionDate": "2026-06-01", "amount": 5_000_000, "description": "Coffee  shop"}
        assert assign_fingerprints([row], "acct1") == assign_fingerprints([dict(row)], "acct1")

    def test_two_equal_rows_in_one_file_get_different_fingerprints(self) -> None:
        row = {"transactionDate": "2026-06-01", "amount": 5_000_000, "description": "Coffee"}
        first, second = assign_fingerprints([row, dict(row)], "acct1")
        assert first != second

    def test_description_case_and_spaces_do_not_matter(self) -> None:
        a = {"transactionDate": "2026-06-01", "amount": 1, "description": "coffee   shop"}
        b = {"transactionDate": "2026-06-01", "amount": 1, "description": "COFFEE SHOP"}
        assert assign_fingerprints([a], "acct1") == assign_fingerprints([b], "acct1")

    def test_account_changes_the_fingerprint(self) -> None:
        row = {"transactionDate": "2026-06-01", "amount": 1, "description": "x"}
        assert assign_fingerprints([row], "acct1") != assign_fingerprints([row], "acct2")
