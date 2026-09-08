"""Per-destination payload transforms: pick, drop, rename, then set templated fields.

Templates reference the event with `{source}`, `{event_key}`, `{event_id}`, `{received_at}` and
`{payload.<dotted.path>}`. A value that is exactly one placeholder keeps the referenced type
(numbers stay numbers); placeholders embedded in longer text are rendered as strings.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from launchbridge.routing import _MISSING, lookup

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\}")


class TransformError(ValueError):
    pass


def render(value: Any, context: dict[str, Any]) -> Any:
    """Render placeholders inside strings; leave other JSON values untouched."""
    if isinstance(value, dict):
        return {k: render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, context) for v in value]
    if not isinstance(value, str):
        return value

    whole = _PLACEHOLDER.fullmatch(value)
    if whole:
        resolved = lookup(context, whole.group(1))
        return None if resolved is _MISSING else resolved

    def substitute(match: re.Match[str]) -> str:
        resolved = lookup(context, match.group(1))
        if resolved is _MISSING or resolved is None:
            return ""
        return resolved if isinstance(resolved, str) else str(resolved)

    return _PLACEHOLDER.sub(substitute, value)


class Transform(BaseModel):
    pick: list[str] = Field(default_factory=list)
    drop: list[str] = Field(default_factory=list)
    rename: dict[str, str] = Field(default_factory=dict)
    set: dict[str, Any] = Field(default_factory=dict)

    def is_identity(self) -> bool:
        return not (self.pick or self.drop or self.rename or self.set)

    def apply(self, payload: Any, context: dict[str, Any]) -> Any:
        """Return the outbound payload. Non-object payloads pass through unchanged."""
        if not isinstance(payload, dict):
            return payload
        result = dict(payload)
        if self.pick:
            result = {k: v for k, v in result.items() if k in self.pick}
        for key in self.drop:
            result.pop(key, None)
        for old, new in self.rename.items():
            if old in result:
                result[new] = result.pop(old)
        full_context = {**context, "payload": payload}
        for key, template in self.set.items():
            result[key] = render(template, full_context)
        return result
