"""Jobs-only HTTP application: the hosted-job routes and nothing else.

The app holds no state of its own, so several instances can serve one
database. It builds no sandbox control plane, mounts no dashboard, exposes no
organization or key administration, and starts no background thread. The host
supplies the stores and the hosted-job configuration as Python objects;
nothing here is read from the environment.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .errors import AuthorizationError, NotFoundError
from .http_support import (
    add_security_headers,
    audit_outcome,
    bearer,
    install_exception_handlers,
    job_route_action,
    path_job_mutation_action,
)
from .job_api import HostedJobConfiguration, install_job_routes
from .job_interfaces import AuthorityStore, JobRepository
from .models import Principal

_READINESS_PROBE_ID = "readiness-probe"


def create_job_app(
    *,
    authority: AuthorityStore,
    repository: JobRepository,
    configuration: HostedJobConfiguration,
    readiness_check: Callable[[], None] | None = None,
    readiness_timeout_secs: float = 5.0,
    on_shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    """Create the hosted-jobs API over caller-owned stores.

    ``repository`` must be built with ``hosted_job_hmac(configuration.hmac_secret)``
    on the database ``authority`` uses. ``readiness_check`` replaces the default
    database round trip behind ``/readyz``; it must raise when not ready. A
    check still running after ``readiness_timeout_secs`` counts as not ready.
    ``on_shutdown`` runs once at server shutdown, for example to close a
    connection pool the host opened.
    """
    if not isinstance(configuration, HostedJobConfiguration):
        raise ValueError("Explicit hosted-job server configuration is required")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if on_shutdown is not None:
                on_shutdown()

    app = FastAPI(
        title="Bespoke Sandbox Hosted Jobs",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    request_auth: ContextVar[dict] = ContextVar("request_auth")

    def database_round_trip() -> None:
        # The storage protocol has no ping; a lookup that cannot match still
        # proves a connection was obtained and a statement was answered.
        try:
            repository.get(_READINESS_PROBE_ID, _READINESS_PROBE_ID)
        except NotFoundError:
            pass

    check_ready = readiness_check or database_round_trip

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        state: dict = {}
        context_token = request_auth.set(state)
        try:
            response = await call_next(request)
            if request.url.path.startswith("/v1/jobs"):
                response.headers["Cache-Control"] = "no-store"
                principal = state.get("principal")
                if principal is not None:
                    action = path_job_mutation_action(
                        request
                    ) or job_route_action(request)
                    await run_in_threadpool(
                        authority.record_audit,
                        principal.organization_id,
                        api_key_id=principal.api_key_id,
                        action=action,
                        outcome=audit_outcome(response.status_code),
                        resource_type="http_route",
                        resource_id=action,
                        details={"status_code": response.status_code},
                        created_at=datetime.now(timezone.utc).isoformat(),
                    )
            return add_security_headers(response)
        finally:
            request_auth.reset(context_token)

    install_exception_handlers(app)

    def authenticate(authorization: str | None, scope: str) -> Principal:
        principal = authority.authenticate(bearer(authorization))
        request_auth.get({})["principal"] = principal
        if not principal.allows(scope):
            raise AuthorizationError(f"API key requires scope: {scope}")
        return principal

    install_job_routes(
        app, None, configuration, authenticate, repository=repository
    )

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    probe: list[asyncio.Future] = []

    def probe_finished(finished: asyncio.Future) -> None:
        probe.clear()
        if not finished.cancelled():
            finished.exception()

    @app.get("/readyz")
    async def ready():
        # A database that accepts a statement and never answers would
        # otherwise park one worker per probe. All callers wait on a single
        # check, and each stops waiting at the deadline.
        if not probe:
            probe.append(asyncio.ensure_future(run_in_threadpool(check_ready)))
            probe[0].add_done_callback(probe_finished)
        try:
            await asyncio.wait_for(
                asyncio.shield(probe[0]), readiness_timeout_secs
            )
        except Exception:
            return JSONResponse(
                status_code=503, content={"status": "unavailable"}
            )
        return {"status": "ready"}

    return app
