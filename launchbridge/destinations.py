"""Destination registry loaded from a YAML file with `${VAR:-default}` expansion."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from launchbridge.retry import RetryPolicy
from launchbridge.routing import Predicate, RouteDecision, decide
from launchbridge.transform import Transform

_ENV_PATTERN = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")


def expand_env(text: str, env: dict[str, str] | None = None) -> str:
    source = os.environ if env is None else env

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")
        value = source.get(name)
        if value is not None:
            return value
        if default is not None:
            return default
        raise KeyError(f"environment variable {name} is not set and has no default")

    return _ENV_PATTERN.sub(replace, text)


class RetryConfig(BaseModel):
    max_attempts: int = 5
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0
    multiplier: float = 2.0
    jitter: float = 0.2
    timeout_seconds: float = 5.0

    def policy(self) -> RetryPolicy:
        return RetryPolicy(**self.model_dump())


class Destination(BaseModel):
    name: str
    url: str
    secret: str
    sources: list[str] = Field(default_factory=lambda: ["*"])
    event_types: list[str] = Field(default_factory=list)
    when: list[Predicate] = Field(default_factory=list)
    transform: Transform = Field(default_factory=Transform)
    retry: RetryConfig = Field(default_factory=RetryConfig)

    @field_validator("name")
    @classmethod
    def _name_shape(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
            raise ValueError("destination names are lowercase alphanumerics, dashes, underscores")
        return value

    @field_validator("url")
    @classmethod
    def _url_shape(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("destination url must be http(s)")
        return value

    def accepts(self, source: str) -> bool:
        return "*" in self.sources or source in self.sources

    def decide(self, source: str, payload: Any, event_type_field: str = "type") -> RouteDecision:
        return decide(
            destination=self.name,
            sources=self.sources,
            event_types=self.event_types,
            when=self.when,
            source=source,
            payload=payload,
            event_type_field=event_type_field,
        )

    def render_payload(self, payload: Any, context: dict[str, Any]) -> Any:
        return self.transform.apply(payload, context)

    @property
    def policy(self) -> RetryPolicy:
        return self.retry.policy()


class DestinationRegistry(BaseModel):
    event_type_field: str = "type"
    destinations: list[Destination] = Field(default_factory=list)

    @field_validator("destinations")
    @classmethod
    def _unique_names(cls, value: list[Destination]) -> list[Destination]:
        names = [d.name for d in value]
        if len(names) != len(set(names)):
            raise ValueError("destination names must be unique")
        return value

    def get(self, name: str) -> Destination | None:
        return next((d for d in self.destinations if d.name == name), None)

    def for_source(self, source: str) -> list[Destination]:
        return [d for d in self.destinations if d.accepts(source)]

    def decisions(self, source: str, payload: Any) -> list[RouteDecision]:
        return [d.decide(source, payload, self.event_type_field) for d in self.destinations]

    def route(self, source: str, payload: Any) -> list[Destination]:
        """Destinations whose source, event type and predicates all match the event."""
        return [
            d for d in self.destinations if d.decide(source, payload, self.event_type_field).routed
        ]

    @classmethod
    def from_yaml(cls, text: str, env: dict[str, str] | None = None) -> DestinationRegistry:
        data = yaml.safe_load(expand_env(text, env)) or {}
        return cls.model_validate(data)

    @classmethod
    def load(cls, path: str | Path, env: dict[str, str] | None = None) -> DestinationRegistry:
        return cls.from_yaml(Path(path).read_text(), env)
