"""Runtime settings loaded from the environment."""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_mapping(raw: str | dict[str, str]) -> dict[str, str]:
    """Accept JSON objects or `name=value,name2=value2` strings."""
    if isinstance(raw, dict):
        return raw
    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        parsed = json.loads(raw)
        return {str(k): str(v) for k, v in parsed.items()}
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(f"expected name=value, got {pair!r}")
        name, value = pair.split("=", 1)
        result[name.strip()] = value.strip()
    return result


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LAUNCHBRIDGE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://launchbridge:launchbridge@localhost:5432/launchbridge"
    webhook_secrets: dict[str, str] = Field(default_factory=dict)
    admin_api_keys: dict[str, str] = Field(default_factory=dict)
    signature_tolerance_seconds: int = 300
    destinations_file: str = "destinations.yaml"
    processed_events_ttl_hours: int = 72
    worker_poll_interval_seconds: float = 0.5
    worker_batch_size: int = 50
    worker_concurrency: int = 8
    worker_metrics_port: int = 0
    log_level: str = "INFO"

    @field_validator("webhook_secrets", "admin_api_keys", mode="before")
    @classmethod
    def _coerce_mapping(cls, value: object) -> dict[str, str]:
        if isinstance(value, str | dict):
            return _parse_mapping(value)
        raise TypeError("expected a mapping or a name=value string")

    @field_validator("database_url")
    @classmethod
    def _normalise_driver(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value[len("postgresql://") :]
        if value.startswith("postgres://"):
            return "postgresql+psycopg://" + value[len("postgres://") :]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
