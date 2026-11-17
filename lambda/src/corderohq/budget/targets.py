from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeIs

from corderohq.common import FieldChange, Keep, Remove, SetTo, parse_optional_str


@dataclass(frozen=True)
class ReplaceTarget:
    """One checked target of a budget /replace request.

    `carried_amount` is the new carried amount. `pinned_amount` changes the pinned amount, and Keep() leaves the stored
    one. `explanation` is None when the row has none.
    """

    index: int
    category_id: str
    carried_amount: int
    pinned_amount: FieldChange[int]
    explanation: str | None


@dataclass(frozen=True)
class UpdateTarget:
    """One checked target of a budget /update request. A Keep() amount does not change."""

    index: int
    category_id: str
    carried_amount: FieldChange[int]
    pinned_amount: FieldChange[int]
    explanation: str | None


def parse_replace_targets(payload: Mapping[str, Any]) -> list[ReplaceTarget]:
    """Convert `targets`, a list of `{categoryId, carriedAmount, pinnedAmount?, explanation?}`. Raise ValueError else.

    `carriedAmount` is an amount. `pinnedAmount` is an amount, null to remove it, or absent to keep it.
    """

    targets: list[ReplaceTarget] = []
    for index, raw in enumerate(_raw_targets(payload)):
        try:
            carried_amount = raw.get("carriedAmount")
            if not _is_amount(carried_amount):
                raise ValueError("carriedAmount must be an integer")
            targets.append(
                ReplaceTarget(
                    index=index,
                    category_id=_parse_category_id(raw),
                    carried_amount=carried_amount,
                    pinned_amount=_parse_amount_change(raw, "pinnedAmount"),
                    explanation=parse_optional_str(raw.get("explanation"), "explanation"),
                )
            )
        except ValueError as e:
            raise ValueError(f"targets[{index}]: {e}") from None
    return targets


def parse_update_targets(payload: Mapping[str, Any]) -> list[UpdateTarget]:
    """Convert `targets`, a list of `{categoryId, carriedAmount?, pinnedAmount?, explanation?}`. Raise ValueError else.

    Each target needs `carriedAmount`, `pinnedAmount`, or both. Each one is an amount, or null to remove it.
    """

    targets: list[UpdateTarget] = []
    for index, raw in enumerate(_raw_targets(payload)):
        try:
            if "carriedAmount" not in raw and "pinnedAmount" not in raw:
                raise ValueError("carriedAmount and/or pinnedAmount is required (each may be null)")
            targets.append(
                UpdateTarget(
                    index=index,
                    category_id=_parse_category_id(raw),
                    carried_amount=_parse_amount_change(raw, "carriedAmount"),
                    pinned_amount=_parse_amount_change(raw, "pinnedAmount"),
                    explanation=parse_optional_str(raw.get("explanation"), "explanation"),
                )
            )
        except ValueError as e:
            raise ValueError(f"targets[{index}]: {e}") from None
    return targets


def _is_amount(value: Any) -> TypeIs[int]:
    """Return True if `value` is an amount: an integer in millionths of a dollar. A bool is not an amount."""

    return isinstance(value, int) and not isinstance(value, bool)


def _parse_amount_change(raw: Mapping[str, Any], key: str) -> FieldChange[int]:
    """Return Keep() for an absent key, Remove() for null, and SetTo(amount) for an amount. Raise ValueError else."""

    if key not in raw:
        return Keep()
    value = raw[key]
    if value is None:
        return Remove()
    if not _is_amount(value):
        raise ValueError(f"{key} must be an integer or null")
    return SetTo(value)


def _raw_targets(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the `targets` list of the body. Raise ValueError if it is not a list of objects."""

    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list):
        raise ValueError("targets array is required")
    for index, raw in enumerate(raw_targets):
        if not isinstance(raw, Mapping):
            raise ValueError(f"targets[{index}]: each target must be an object")
    return raw_targets


def _parse_category_id(raw: Mapping[str, Any]) -> str:
    """Return the categoryId of a target. Raise ValueError if it is not a string that is not empty."""

    category_id = raw.get("categoryId")
    if not isinstance(category_id, str) or not category_id:
        raise ValueError("categoryId is required")
    return category_id
