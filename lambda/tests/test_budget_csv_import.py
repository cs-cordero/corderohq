"""Tests for LLM-powered CSV import."""

import json
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("SSM_API_KEY_PARAM", "/budget/anthropic-api-key")

from corderohq.budget.csv_import import assign_fingerprints, categorize_transactions, map_columns


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
    @patch("corderohq.budget.csv_import.get_ssm_param", return_value="sk-ant-test")
    @patch("corderohq.budget.csv_import.anthropic.Anthropic")
    def test_returns_column_mapping(self, mock_anthropic_cls: MagicMock, mock_ssm: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_claude_response(
            json.dumps({"date": "Trans Date", "description": "Description", "amount": "Amount"})
        )

        result = map_columns(
            headers=["Trans Date", "Description", "Amount"],
            sample_rows=[["2026-03-01", "Coffee Shop", "5.50"]],
        )

        assert result["date"] == "Trans Date"
        assert result["description"] == "Description"
        assert result["amount"] == "Amount"

    @patch("corderohq.budget.csv_import.get_ssm_param", return_value="sk-ant-test")
    @patch("corderohq.budget.csv_import.anthropic.Anthropic")
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

        result = map_columns(["Date", "Desc", "Amount"], [["2026-03-01", "Store", "-10.00"]])
        assert result["amount_invert"] is True


class TestCategorizeTransactions:
    @patch("corderohq.budget.csv_import.get_ssm_param", return_value="sk-ant-test")
    @patch("corderohq.budget.csv_import.anthropic.Anthropic")
    def test_returns_category_assignments(self, mock_anthropic_cls: MagicMock, mock_ssm: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_claude_response(
            json.dumps(
                [
                    {"index": 0, "categoryId": "cat1", "categoryName": "Groceries"},
                    {"index": 1, "categoryId": "cat2", "categoryName": "Dining"},
                ]
            )
        )

        categories = [
            {"categoryId": "cat1", "name": "Groceries"},
            {"categoryId": "cat2", "name": "Dining"},
        ]
        result = categorize_transactions(["Whole Foods", "Chipotle"], categories)

        assert len(result) == 2
        assert result[0]["categoryId"] == "cat1"
        assert result[1]["categoryId"] == "cat2"


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
