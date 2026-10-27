from __future__ import annotations

from dataclasses import dataclass
from typing import Any, assert_never


@dataclass(frozen=True)
class Keep:
    """A change that leaves a field as it is."""


@dataclass(frozen=True)
class SetTo[T]:
    """A change that sets a field to `value`."""

    value: T


@dataclass(frozen=True)
class Remove:
    """A change that removes an optional field."""


type FieldChange[T] = Keep | SetTo[T] | Remove


def resolve_change[T](change: FieldChange[T], current: T | None) -> T | None:
    """Return the value of a field after `change`: `current` for Keep, the new value for SetTo, and None for Remove."""

    match change:
        case Keep():
            return current
        case SetTo(value=value):
            return value
        case Remove():
            return None
        case _:
            assert_never(change)


def parse_optional_str(value: Any, name: str) -> str | None:
    """Return the value without outer white space. None and an empty string give None.

    Raise ValueError if the value is not a string.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value.strip() or None
