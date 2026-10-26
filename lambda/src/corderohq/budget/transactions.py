from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from corderohq.budget.treatment import BudgetTreatment, Classification, parse_classification
from corderohq.common import FieldChange, Keep, Remove, SetTo, parse_optional_str

NOTE_MAX_LENGTH = 200


@dataclass(frozen=True)
class NewTransaction:
    """The checked fields of a new transaction."""

    year_month: str
    transaction_date: str
    description: str
    amount: int
    classification: Classification
    account_id: str | None
    note: str | None

    def to_item(self) -> dict[str, Any]:
        """Converts an instance of NewTransaction to how it is stored in DynamoDB, without its sort key and time."""

        item: dict[str, Any] = {
            "yearMonth": self.year_month,
            "transactionDate": self.transaction_date,
            "description": self.description,
            "amount": self.amount,
            "budgetTreatment": self.classification.budget_treatment,
        }
        if self.classification.category_id is not None:
            item["categoryId"] = self.classification.category_id
        if self.account_id is not None:
            item["accountId"] = self.account_id
        if self.note is not None:
            item["note"] = self.note
        return item


@dataclass(frozen=True)
class UpdateTransactionArgs:
    """The checked changes to a transaction. Each field that the request did not name is Keep()."""

    description: Keep | SetTo[str]
    amount: Keep | SetTo[int]
    budget_treatment: Keep | SetTo[BudgetTreatment]
    category_id: FieldChange[str]
    account_id: FieldChange[str]
    note: FieldChange[str]

    def changes_nothing(self) -> bool:
        """Return True if every field is Keep()."""

        return all(isinstance(getattr(self, field.name), Keep) for field in fields(self))


def parse_new_transaction(
    payload: Mapping[str, Any], *, year_month: str, require_category: bool = True
) -> NewTransaction:
    """Convert the body of a new transaction into checked fields. Raise ValueError for bad input.

    With `require_category=False`, a BUDGET transaction with no category passes. It does not check
    that the account exists.
    """

    transaction_date = payload.get("transactionDate")
    if not isinstance(transaction_date, str) or not transaction_date.strip():
        raise ValueError("transactionDate is required")
    return NewTransaction(
        year_month=year_month,
        transaction_date=transaction_date.strip(),
        description=_parse_description(payload.get("description")),
        amount=_parse_amount(payload.get("amount")),
        classification=parse_classification(
            payload.get("budgetTreatment"), payload.get("categoryId"), require_category
        ),
        account_id=parse_optional_str(payload.get("accountId"), "accountId"),
        note=_parse_note(payload.get("note")),
    )


def parse_update_transaction_args(payload: Mapping[str, Any], existing: Mapping[str, Any]) -> UpdateTransactionArgs:
    """Convert the body of an update request into checked changes. Raise ValueError for bad input.

    `existing` is the stored transaction. A change to the budget treatment or the category is checked
    together with the stored value of the other one. The account and the note change on their own.
    It does not check that the account exists.
    """

    description: Keep | SetTo[str] = Keep()
    if "description" in payload:
        description = SetTo(_parse_description(payload["description"]))

    amount: Keep | SetTo[int] = Keep()
    if "amount" in payload:
        amount = SetTo(_parse_amount(payload["amount"]))

    budget_treatment: Keep | SetTo[BudgetTreatment] = Keep()
    category_id: FieldChange[str] = Keep()
    if "budgetTreatment" in payload or "categoryId" in payload:
        classification = parse_classification(
            payload["budgetTreatment"] if "budgetTreatment" in payload else existing.get("budgetTreatment"),
            payload["categoryId"] if "categoryId" in payload else existing.get("categoryId"),
        )
        budget_treatment = SetTo(classification.budget_treatment)
        category_id = _set_or_remove(classification.category_id)

    account_id: FieldChange[str] = Keep()
    if "accountId" in payload:
        account_id = _set_or_remove(parse_optional_str(payload["accountId"], "accountId"))

    note: FieldChange[str] = Keep()
    if "note" in payload:
        note = _set_or_remove(_parse_note(payload["note"]))

    return UpdateTransactionArgs(description, amount, budget_treatment, category_id, account_id, note)


def _parse_description(value: Any) -> str:
    """Return the description without outer white space. Raise ValueError if it is empty."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("description must not be empty")
    return value.strip()


def _parse_amount(value: Any) -> int:
    """Return the amount in millionths of a dollar. Raise ValueError if it is not an integer."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("amount must be an integer (millionths of a dollar)")
    return value


def _parse_note(value: Any) -> str | None:
    """Return the note without outer white space, or None. Raise ValueError if it is too long."""

    note = parse_optional_str(value, "note")
    if note is not None and len(note) > NOTE_MAX_LENGTH:
        raise ValueError(f"note must be at most {NOTE_MAX_LENGTH} characters")
    return note


def _set_or_remove(value: str | None) -> FieldChange[str]:
    """Return SetTo(value), or Remove() for None."""

    return Remove() if value is None else SetTo(value)
