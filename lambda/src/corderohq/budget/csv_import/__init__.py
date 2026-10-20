"""The CSV import: an upload reads a bank or credit card CSV, and suggests a disposition for each row.

- `llm.py` — the two LLM calls: the column mapping and the categorization.
- `local_checks.py` — the column types, the dates, the amounts and their sign, and the bank category.
- `fingerprints.py` — the fingerprint of each row, to find a row that the app saved before.
- `directions.py` — the direction table: a suggested disposition for each row.
"""

from corderohq.budget.csv_import.directions import ReviewReason, SuggestedBy, Suggestion, suggest_dispositions
from corderohq.budget.csv_import.fingerprints import assign_fingerprints, fingerprint, normalize_description
from corderohq.budget.csv_import.llm import TransactionKind, categorize_transactions, map_columns
from corderohq.budget.csv_import.local_checks import (
    BankHint,
    bank_category_hint,
    detect_column_types,
    parse_amount,
    parse_date,
    spending_account_needs_flip,
)

__all__ = [
    "BankHint",
    "ReviewReason",
    "SuggestedBy",
    "Suggestion",
    "TransactionKind",
    "assign_fingerprints",
    "bank_category_hint",
    "categorize_transactions",
    "detect_column_types",
    "fingerprint",
    "map_columns",
    "normalize_description",
    "parse_amount",
    "parse_date",
    "spending_account_needs_flip",
    "suggest_dispositions",
]
