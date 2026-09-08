"""Routing rules: decide which destinations an inbound event fans out to.

A destination matches when the source is accepted, the event type (a payload field, `type`
by default) matches one of its `event_types` globs, and every `when` predicate holds.
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Operator = Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "exists", "matches"]

_MISSING = object()


def lookup(data: Any, path: str) -> Any:
    """Resolve a dotted path (`customer.address.city`, `items.0.sku`) against nested data."""
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part, _MISSING)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
        if current is _MISSING:
            return _MISSING
    return current


def event_type_of(payload: Any, field: str = "type") -> str | None:
    value = lookup(payload, field)
    return value if isinstance(value, str) else None


class Predicate(BaseModel):
    field: str = Field(min_length=1)
    op: Operator = "eq"
    value: Any = None

    @model_validator(mode="after")
    def _value_shape(self) -> Predicate:
        if self.op in ("in", "not_in") and not isinstance(self.value, list):
            raise ValueError(f"{self.op} needs a list value")
        if self.op == "exists" and not isinstance(self.value, bool):
            raise ValueError("exists needs a boolean value")
        if self.op == "matches":
            if not isinstance(self.value, str):
                raise ValueError("matches needs a regular expression string")
            re.compile(self.value)
        return self

    def holds(self, payload: Any) -> bool:
        actual = lookup(payload, self.field)
        present = actual is not _MISSING
        if self.op == "exists":
            return present is self.value
        if not present:
            return self.op in ("ne", "not_in")
        try:
            match self.op:
                case "eq":
                    return actual == self.value
                case "ne":
                    return actual != self.value
                case "gt":
                    return actual > self.value
                case "gte":
                    return actual >= self.value
                case "lt":
                    return actual < self.value
                case "lte":
                    return actual <= self.value
                case "in":
                    return actual in self.value
                case "not_in":
                    return actual not in self.value
                case "matches":
                    return isinstance(actual, str) and re.search(self.value, actual) is not None
        except TypeError:
            return False
        return False

    def describe(self) -> str:
        return f"{self.field} {self.op} {self.value!r}"


class RouteDecision(BaseModel):
    destination: str
    routed: bool
    reason: str

    @classmethod
    def accept(cls, destination: str) -> RouteDecision:
        return cls(destination=destination, routed=True, reason="matched")

    @classmethod
    def reject(cls, destination: str, reason: str) -> RouteDecision:
        return cls(destination=destination, routed=False, reason=reason)


def decide(
    *,
    destination: str,
    sources: list[str],
    event_types: list[str],
    when: list[Predicate],
    source: str,
    payload: Any,
    event_type_field: str,
) -> RouteDecision:
    if "*" not in sources and source not in sources:
        return RouteDecision.reject(destination, f"source {source!r} not in {sources}")
    if event_types:
        event_type = event_type_of(payload, event_type_field)
        if event_type is None:
            return RouteDecision.reject(destination, f"payload has no {event_type_field!r} field")
        if not any(fnmatchcase(event_type, pattern) for pattern in event_types):
            return RouteDecision.reject(
                destination, f"event type {event_type!r} not in {event_types}"
            )
    for predicate in when:
        if not predicate.holds(payload):
            return RouteDecision.reject(destination, f"predicate failed: {predicate.describe()}")
    return RouteDecision.accept(destination)
