"""Response models for the HTTP API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class WebhookAccepted(BaseModel):
    event_id: uuid.UUID
    deduplicated: bool
    delivery_ids: list[uuid.UUID]


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attempt_number: int
    started_at: datetime
    duration_ms: int
    status_code: int | None
    outcome: str
    error: str | None
    succeeded: bool


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: uuid.UUID
    destination: str
    idempotency_key: str
    status: str
    series: int
    attempts: int
    max_attempts: int
    next_attempt_at: datetime | None
    last_status_code: int | None
    last_error: str | None
    replay_of: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime | None
    replayed_at: datetime | None
    latency_ms: int | None


class DeliveryDetail(DeliveryOut):
    source: str
    attempt_log: list[AttemptOut]


class DeliveryList(BaseModel):
    items: list[DeliveryOut]
    count: int


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    event_key: str
    content_hash: str
    status: str
    received_at: datetime
    payload: dict | list | None


class EventList(BaseModel):
    items: list[EventOut]
    count: int


class ReplayOut(BaseModel):
    original_delivery_id: uuid.UUID
    replay_delivery_id: uuid.UUID


class BulkReplayOut(BaseModel):
    replayed: int
    delivery_ids: list[uuid.UUID]


class ReplayAuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    original_delivery_id: uuid.UUID
    new_delivery_id: uuid.UUID
    actor: str
    mode: str
    reason: str | None
    requested_at: datetime


class ReplayAuditList(BaseModel):
    items: list[ReplayAuditOut]
    count: int


class DryRunDestination(BaseModel):
    destination: str
    routed: bool
    reason: str
    url: str | None = None
    payload: dict | list | str | None = None


class DryRunOut(BaseModel):
    source: str
    event_key: str
    event_type: str | None
    routed_to: list[str]
    destinations: list[DryRunDestination]


class BreakerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    breaker_state: str
    consecutive_failures: int
    opened_at: datetime | None
    updated_at: datetime


class DestinationOut(BaseModel):
    name: str
    url: str
    sources: list[str]
    event_types: list[str]
    predicates: int
    transform: bool
    max_attempts: int
    rate_limit: dict | None
    circuit_breaker: dict | None
    breaker: BreakerOut | None
    queued: int


class DestinationList(BaseModel):
    items: list[DestinationOut]
    count: int


class Health(BaseModel):
    status: str
    version: str


class Readiness(BaseModel):
    status: str
    database: str
