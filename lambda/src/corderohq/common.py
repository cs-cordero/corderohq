from __future__ import annotations

from dataclasses import dataclass


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
