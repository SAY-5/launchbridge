"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from launchbridge import __version__
from launchbridge.api import router
from launchbridge.config import Settings, get_settings
from launchbridge.destinations import DestinationRegistry
from launchbridge.logging import configure_logging, get_logger

DESCRIPTION = """
Signed inbound webhooks are verified (HMAC-SHA256 over timestamp and body), deduplicated in
PostgreSQL by (source, event key), fanned out to configured destinations with bounded retries,
and can be replayed after failure with the same idempotency key.

Admin endpoints require the `X-API-Key` header.
"""


def create_app(
    settings: Settings | None = None, registry: DestinationRegistry | None = None
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    log = get_logger("launchbridge.app")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.registry = registry or DestinationRegistry.load(settings.destinations_file)
        log.info(
            "startup",
            destinations=[d.name for d in app.state.registry.destinations],
            sources=sorted(settings.webhook_secrets),
        )
        yield

    app = FastAPI(
        title="LaunchBridge",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
