import hashlib
from typing import Any


def normalize_description(description: str) -> str:
    """Return the description in upper case, with each run of white space as one space."""

    return " ".join(description.upper().split())


def fingerprint(account_id: str, transaction_date: str, amount: int, description: str, occurrence: int) -> str:
    """Return the fingerprint of one CSV row.

    `occurrence` is the count of earlier rows in the same file with the same account, date,
    amount, and description. Two real purchases of the same coffee on the same day then get
    different fingerprints.
    """

    key = "|".join([account_id, transaction_date, str(amount), normalize_description(description), str(occurrence)])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def assign_fingerprints(rows: list[dict[str, Any]], account_id: str) -> list[str]:
    """Return the fingerprint of each row, in order. Each row has transactionDate, amount, description.

    A fingerprint marks a CSV row that the app saved before, such as from an earlier CSV with dates that overlap. It
    also makes a retry of a commit safe: the commit drops each row whose fingerprint is in the table already.
    """

    seen: dict[tuple[str, int, str], int] = {}
    result: list[str] = []
    for row in rows:
        base = (str(row["transactionDate"]), int(row["amount"]), normalize_description(str(row["description"])))
        occurrence = seen.get(base, 0)
        seen[base] = occurrence + 1
        result.append(fingerprint(account_id, base[0], base[1], str(row["description"]), occurrence))
    return result
