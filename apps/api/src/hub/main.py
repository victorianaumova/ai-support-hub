"""FastAPI app factory. Run with `uvicorn hub.main:create_app --factory`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from hub.api.admin_jobs import router as admin_jobs_router
from hub.api.admin_runs import router as admin_runs_router
from hub.api.admin_settings import router as admin_settings_router
from hub.api.auth import router as auth_router
from hub.api.console import router as console_router
from hub.api.console_account import router as console_account_router
from hub.api.console_actions import router as console_actions_router
from hub.api.console_drafts import router as console_drafts_router
from hub.api.console_metrics import router as console_metrics_router
from hub.api.demo import router as demo_router
from hub.api.errors import install_error_handlers
from hub.api.health import router as health_router
from hub.api.kb import admin_router as kb_admin_router
from hub.api.kb import console_router as kb_console_router
from hub.api.kb import help_router
from hub.api.limits import BodySizeLimitMiddleware
from hub.api.middleware import RequestContextMiddleware
from hub.api.privacy import admin_router as privacy_admin_router
from hub.api.privacy import public_router as privacy_router
from hub.api.security_headers import SecurityHeadersMiddleware
from hub.api.tickets import router as tickets_router
from hub.config import load_settings
from hub.deps import Container, build_container
from hub.graph.build import compile_graph
from hub.observability.logging import configure_logging

# Longest run: triage + write timeouts with retries are longer, but a stop must not hang.
RUN_SHUTDOWN_WAIT_S = 30.0


def create_app(container: Container | None = None) -> FastAPI:
    """Build the app. Without a container, settings are read now, so a bad `.env` stops start."""
    if container is None:
        container = build_container(load_settings())
    configure_logging(container.settings.log_level)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if container.pipeline is not None:
            # Compile the graph and open the checkpointer's pool now (ai-pipeline §1: once at
            # start-up), in the app's own task: pool workers started inside a request's task
            # would end with it.
            await container.pipeline.checkpoints.graph(compile_graph)
        yield
        # Runs are tasks of their own (`hub.graph.pipeline.RunTasks`): let the ones in flight
        # finish before the database goes away. A run cut off here stays `running` for
        # `recover-runs`.
        await container.runs.wait(timeout_s=RUN_SHUTDOWN_WAIT_S)
        if container.pipeline is not None:
            await container.pipeline.checkpoints.close()
        if container.kb is not None:
            await container.kb.close()
        if container.database is not None:
            await container.database.dispose()

    # debug stays off: Starlette's debug pages would show tracebacks with request data.
    # The public demo serves no Swagger UI or ReDoc pages (they need a CDN and inline scripts,
    # `hub.api.security_headers`); the OpenAPI JSON stays for `make web-types`.
    public = container.settings.app_env == "demo"
    app = FastAPI(
        title="Enterprise AI Support Hub",
        version="0.1.0",
        debug=False,
        lifespan=lifespan,
        docs_url=None if public else "/docs",
        redoc_url=None if public else "/redoc",
    )
    app.state.container = container
    install_error_handlers(app)
    # The last one added runs first: the request id exists before the body cap can answer 413,
    # and the security headers reach every answer, a 413 or a 500 of the middlewares included.
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(health_router)
    app.include_router(demo_router)
    app.include_router(tickets_router)
    app.include_router(auth_router)
    app.include_router(console_router)
    app.include_router(console_drafts_router)
    app.include_router(console_actions_router)
    app.include_router(console_account_router)
    app.include_router(console_metrics_router)
    app.include_router(help_router)
    app.include_router(kb_console_router)
    app.include_router(kb_admin_router)
    app.include_router(admin_runs_router)
    app.include_router(admin_jobs_router)
    app.include_router(admin_settings_router)
    app.include_router(privacy_router)
    app.include_router(privacy_admin_router)
    return app
