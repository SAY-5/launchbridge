"""Prometheus metrics. Counters are per-process; delivery state gauges are read from the database."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

EVENTS_RECEIVED = Counter(
    "launchbridge_events_received_total", "Accepted inbound events", ["source"]
)
EVENTS_DEDUPLICATED = Counter(
    "launchbridge_events_deduplicated_total", "Inbound events flagged as duplicates", ["source"]
)
SIGNATURE_REJECTIONS = Counter(
    "launchbridge_signature_rejections_total", "Rejected inbound requests", ["source", "reason"]
)
DELIVERIES_DELIVERED = Counter(
    "launchbridge_deliveries_delivered_total", "Deliveries that reached a destination", ["destination"]
)
DELIVERIES_RETRIED = Counter(
    "launchbridge_deliveries_retried_total", "Retry attempts scheduled", ["destination"]
)
DELIVERIES_FAILED = Counter(
    "launchbridge_deliveries_failed_total", "Deliveries that reached the failed state", ["destination"]
)
DELIVERIES_REPLAYED = Counter(
    "launchbridge_deliveries_replayed_total", "Failed deliveries replayed", ["mode"]
)
DELIVERY_LATENCY = Histogram(
    "launchbridge_delivery_latency_seconds",
    "Time from delivery creation to successful delivery",
    ["destination"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
ATTEMPT_DURATION = Histogram(
    "launchbridge_attempt_duration_seconds",
    "Duration of individual outbound HTTP attempts",
    ["destination"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
DELIVERIES_BY_STATUS = Gauge(
    "launchbridge_deliveries", "Deliveries in the database by status", ["status"]
)
