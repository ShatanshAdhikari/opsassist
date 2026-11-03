"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from opsassist.api.errors import install_error_handlers
from opsassist.api.middleware import OriginCheckMiddleware, RequestContextMiddleware
from opsassist.api.routes import assistant, auth, health, knowledge, tools, workspaces
from opsassist.config import Settings, get_settings
from opsassist.container import Container, build_container
from opsassist.observability import configure_logging


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    resolved = settings or (container.settings if container else get_settings())
    configure_logging(resolved.log_level)
    built = container or build_container(resolved)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await built.close()

    is_production = resolved.environment == "production"
    app = FastAPI(
        title="OpsAssist API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if is_production else "/docs",
        redoc_url=None,
        openapi_url=None if is_production else "/openapi.json",
    )
    app.state.container = built
    install_error_handlers(app)
    # Last added runs first: request context wraps the origin check, so rejections are logged.
    app.add_middleware(OriginCheckMiddleware, allowed_origins=resolved.allowed_origins)
    app.add_middleware(RequestContextMiddleware)

    for module in (health, auth, workspaces, knowledge, assistant, tools):
        app.include_router(module.router)
    return app
