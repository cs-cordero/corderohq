"""Local checks of the CSV import: the column types, the dates, the amounts and their sign, and the bank category.

The code does these checks itself, so no cell value goes to the LLM.
"""

from datetime import date, datetime
from enum import Enum, auto

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%m-%d-%Y", "%d-%b-%Y", "%b %d, %Y")
_TYPE_SAMPLE_SIZE = 50
_TYPE_THRESHOLD = 0.8


def parse_date(value: str) -> date | None:
    """Return the date in a cell, in one of the common bank formats, or None."""
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), date_format).date()
        except ValueError:
            continue
    return None


def _looks_like_date(value: str) -> bool:
    return parse_date(value) is not None


def parse_amount(value: str) -> float | None:
    """Return the number in an amount cell, or None. Accept "$1,234.50", "-5", and "(5.00)"."""
    cleaned = value.strip().replace(",", "").replace("$", "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative else number


def detect_column_types(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Return "date", "number", or "text" for each column, from the first rows of the file.

    A column is a date column (or a number column) when most of its cells that are not empty
    parse as a date (or as an amount). The code finds this locally, so no cell value goes to
    the LLM.
    """
    sample = rows[:_TYPE_SAMPLE_SIZE]
    result: list[str] = []
    for index in range(len(headers)):
        values = [row[index].strip() for row in sample if index < len(row) and row[index].strip()]
        if not values:
            result.append("text")
            continue
        if sum(_looks_like_date(v) for v in values) >= _TYPE_THRESHOLD * len(values):
            result.append("date")
        elif sum(parse_amount(v) is not None for v in values) >= _TYPE_THRESHOLD * len(values):
            result.append("number")
        else:
            result.append("text")
    return result


def spending_account_needs_flip(amounts: list[int]) -> bool:
    """Return True if the amounts of a spending account have the wrong sign.

    Most rows of a credit card or a checking account are money out, which the app stores as a
    positive amount. If most rows are negative, the sign is probably the opposite of the app's.
    """
    positive = sum(1 for a in amounts if a > 0)
    negative = sum(1 for a in amounts if a < 0)
    return negative > positive


# Generic bank labels. These are words that banks use for a category, and not the names of an
# employer or a merchant. The repository is public, so it must hold no real names.
_BANK_INCOME_WORDS = ("payroll", "paycheck", "salary", "direct deposit", "income", "interest", "dividend")
_BANK_CARD_PAYMENT_WORDS = ("credit card payment", "card payment")
_BANK_TRANSFER_WORDS = ("transfer",)


class BankHint(Enum):
    """What the bank's own category says about a row. It stays inside the upload, and is never stored."""

    INCOME = auto()
    CARD_PAYMENT = auto()
    TRANSFER = auto()


def bank_category_hint(value: str | None) -> BankHint | None:
    """Return the hint in the bank's category of a row, or None."""
    if not value:
        return None
    text = value.lower()
    if any(word in text for word in _BANK_CARD_PAYMENT_WORDS):
        return BankHint.CARD_PAYMENT
    if any(word in text for word in _BANK_TRANSFER_WORDS):
        return BankHint.TRANSFER
    if any(word in text for word in _BANK_INCOME_WORDS):
        return BankHint.INCOME
    return None
