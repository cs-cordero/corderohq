"""The two LLM calls of the CSV import: the column mapping and the categorization."""

import json
import logging
from enum import StrEnum
from typing import Any

import anthropic
from anthropic.types import TextBlock, TextBlockParam

from corderohq.aws.ssm import get_ssm_param
from corderohq.util import get_env_var

LOGGER = logging.getLogger(__name__)

# Each prompt gets as little of the user's data as possible. The column mapping gets the header
# row and a type for each column, and no cell values. The categorization gets each unique
# description one time, and no amounts, dates, or account names.

_COLUMN_MAPPING_PROMPT = """\
You are a financial data analyst. You get the header row of a CSV file from a bank or a credit \
card, with a type for each column: date, number, or text. You do not get any cell values.

Find the column that holds each of these:
- "date": the transaction date. If two columns hold dates, such as "Transaction Date" and \
"Post Date", use the transaction date.
- "description": the merchant name or the transaction description.
- "amount": the dollar amount.
- "bankCategory": the category that the bank gives each row, such as "Groceries" or "Payment". \
Leave this key out if no column holds one.

The app stores an amount as positive for money out (a purchase) and negative for money in (a \
credit or a refund). If the header names show that the file uses the opposite sign, set \
"amount_invert": true. If you are not sure, leave it out. The app checks the sign again.

Respond with a JSON object that maps each key to the exact header name. Example:
{"date": "Transaction Date", "description": "Description", "amount": "Amount", "bankCategory": "Category"}

Respond ONLY with the JSON object, no other text."""

# The kinds that the categorization returns for each description. The import uses the kind to
# suggest a disposition.
class TransactionKind(StrEnum):
    """The kind that the categorization gives each description. The prompt names these values."""

    EXPENSE = "expense"  # a purchase or a bill
    REFUND = "refund"  # money back from a merchant
    INCOME = "income"  # a paycheck, a bonus, interest, or a dividend
    CARD_PAYMENT = "card_payment"  # a payment to or from a credit card
    OWN_TRANSFER = "own_transfer"  # money moved between the household's own accounts

_CATEGORIZATION_PROMPT_TEMPLATE = """\
You are a household budget categorizer. You get a list of transaction descriptions and a list \
of budget categories.

For each description, give:
- "kind": one of "expense" (a purchase or a bill), "refund" (money back from a merchant), \
"income" (a paycheck, a bonus, interest, or a dividend), "card_payment" (a payment to or from a \
credit card), or "own_transfer" (money moved between the household's own accounts, such as to a \
savings fund, a brokerage, or a 529).
- "categoryId" and "categoryName": the best category. For "income" and "card_payment", use null. \
For "own_transfer", use a savings category if one fits, else null.

Categories:
{categories}

Descriptions (one per line, format "INDEX|DESCRIPTION"):
{transactions}

Respond with a JSON array of objects, one per description, in the same order:
[{{"index": 0, "kind": "expense", "categoryId": "...", "categoryName": "..."}}, ...]

If no category fits well for an expense or a refund, use the closest match. Never invent new \
categories.

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


def map_columns(headers: list[str], column_types: list[str]) -> dict[str, Any]:
    """Use Claude to find the date, description, amount, and bank category columns.

    The prompt holds the header names and the type of each column (see `detect_column_types`),
    and no cell values. Returns a dict with keys date, description, amount, and optionally
    bankCategory and amount_invert.
    """
    api_key = _get_api_key()

    preview = ", ".join(f"{header} ({column_type})" for header, column_type in zip(headers, column_types, strict=True))
    LOGGER.info("Mapping columns for headers: %s", headers)
    result_text = _call_claude(api_key, _COLUMN_MAPPING_PROMPT, f"Headers and types:\n{preview}")
    mapping: dict[str, Any] = json.loads(result_text)
    LOGGER.info("Column mapping result: %s", mapping)
    return mapping


def categorize_transactions(
    descriptions: list[str],
    categories: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Use Claude to give a kind and a category to each description.

    The prompt holds each unique description one time only. The result maps back to every
    input, in order: one dict per input with index, kind, categoryId, and categoryName. An
    unknown kind becomes None, and a category that is not in `categories` becomes "".
    """
    unique: list[str] = []
    position: dict[str, int] = {}
    for description in descriptions:
        if description not in position:
            position[description] = len(unique)
            unique.append(description)
    if not unique:
        return []

    api_key = _get_api_key()
    category_lines = "\n".join(f"- {c['name']} (categoryId: {c['categoryId']})" for c in categories)
    transaction_lines = "\n".join(f"{i}|{desc}" for i, desc in enumerate(unique))
    user_text = _CATEGORIZATION_PROMPT_TEMPLATE.format(categories=category_lines, transactions=transaction_lines)

    LOGGER.info(
        "Categorizing %d unique descriptions (%d rows) across %d categories",
        len(unique),
        len(descriptions),
        len(categories),
    )
    result_text = _call_claude(api_key, "", user_text)
    assignments: list[dict[str, Any]] = json.loads(result_text)

    names_by_id = {str(c["categoryId"]): str(c["name"]) for c in categories}
    by_unique_index: dict[int, dict[str, Any]] = {}
    for assignment in assignments:
        index = assignment.get("index")
        if not isinstance(index, int):
            continue
        kind = assignment.get("kind")
        category_id = assignment.get("categoryId")
        valid_category = isinstance(category_id, str) and category_id in names_by_id
        by_unique_index[index] = {
            "kind": TransactionKind(kind) if isinstance(kind, str) and kind in TransactionKind else None,
            "categoryId": category_id if valid_category else "",
            "categoryName": names_by_id[category_id] if valid_category else "",
        }
    LOGGER.info("Categorization complete: %d assignments", len(by_unique_index))

    empty = {"kind": None, "categoryId": "", "categoryName": ""}
    return [
        {"index": i, **by_unique_index.get(position[description], empty)} for i, description in enumerate(descriptions)
    ]
