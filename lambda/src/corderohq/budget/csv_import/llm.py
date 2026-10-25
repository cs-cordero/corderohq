"""An LLM reads a CSV file, and it gives a category to each transaction."""

import json
import logging
from typing import Any

import anthropic
from anthropic.types import TextBlock, TextBlockParam

from corderohq.aws.ssm import get_ssm_param
from corderohq.util import get_env_var

LOGGER = logging.getLogger(__name__)

_COLUMN_MAPPING_PROMPT = """\
You are a financial data analyst. Given the following CSV headers and a few sample rows \
from a bank or credit card statement, identify which columns correspond to:
- "date": the transaction date (when the purchase happened)
- "description": the merchant name or transaction description
- "amount": the dollar amount

Respond with a JSON object mapping these keys to the exact CSV header names. Example:
{"date": "Transaction Date", "description": "Description", "amount": "Amount"}

If there are multiple date columns (e.g., "Transaction Date" and "Post Date"), \
prefer the transaction date over the post date.

Our schema stores amounts with positive=expense and negative=income/return.
If the bank CSV uses the opposite convention (negative for purchases, positive for credits/refunds), \
set "amount_invert": true so we flip the sign on every row. Otherwise omit it or set it to false.

Respond ONLY with the JSON object, no other text."""

_CATEGORIZATION_PROMPT_TEMPLATE = """\
You are a household budget categorizer. Given a list of transaction descriptions and a list \
of budget categories, assign each transaction to the most appropriate category.

Categories:
{categories}

Transactions (one per line, format "INDEX|DESCRIPTION"):
{transactions}

Respond with a JSON array of objects, one per transaction, in the same order:
[{{"index": 0, "categoryId": "...", "categoryName": "..."}}, ...]

If no category fits well, use the categoryId and categoryName of the closest match. \
Never invent new categories.

Respond ONLY with the JSON array, no other text."""


def _get_api_key() -> str:
    param_name = get_env_var("SSM_API_KEY_PARAM")
    return get_ssm_param(param_name)


def _call_claude(api_key: str, system: str, user_text: str) -> str:
    """Call Claude Haiku and return the text response."""
    client = anthropic.Anthropic(api_key=api_key)

    prompt = TextBlockParam(type="text", text=user_text)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": [prompt]}],
    )

    for block in response.content:
        if isinstance(block, TextBlock):
            text = block.text.strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [line for line in lines[1:] if line.strip() != "```"]
                text = "\n".join(lines)
            return text

    raise ValueError("Claude returned no text response")


def map_columns(headers: list[str], sample_rows: list[list[str]]) -> dict[str, Any]:
    """Use Claude to identify which CSV columns are date, description, and amount.

    Returns a dict with keys: date, description, amount, and optionally amount_invert.
    """
    api_key = _get_api_key()

    csv_preview = ",".join(headers) + "\n"
    for row in sample_rows[:5]:
        csv_preview += ",".join(row) + "\n"

    LOGGER.info("Mapping columns for headers: %s", headers)
    result_text = _call_claude(api_key, _COLUMN_MAPPING_PROMPT, csv_preview)
    mapping: dict[str, Any] = json.loads(result_text)
    LOGGER.info("Column mapping result: %s", mapping)
    return mapping


def categorize_transactions(
    descriptions: list[str],
    categories: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Use Claude to assign categories to transaction descriptions.

    Args:
        descriptions: list of transaction description strings
        categories: list of category dicts with categoryId and name

    Returns a list of dicts with index, categoryId, categoryName.
    """
    api_key = _get_api_key()

    category_lines = "\n".join(f"- {c['name']} (categoryId: {c['categoryId']})" for c in categories)
    transaction_lines = "\n".join(f"{i}|{desc}" for i, desc in enumerate(descriptions))

    user_text = _CATEGORIZATION_PROMPT_TEMPLATE.format(
        categories=category_lines,
        transactions=transaction_lines,
    )

    LOGGER.info("Categorizing %d transactions across %d categories", len(descriptions), len(categories))
    result_text = _call_claude(api_key, "", user_text)
    assignments: list[dict[str, Any]] = json.loads(result_text)
    LOGGER.info("Categorization complete: %d assignments", len(assignments))
    return assignments
