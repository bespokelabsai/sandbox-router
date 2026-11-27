"""HTTP pieces shared by the control-plane app and the jobs-only app.

Nothing here touches a store, a provider, or the hosted-job plugin, so both
app factories can use it without importing each other.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from bespokelabs.sandbox.control_plane.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    PolicyDeniedError,
    provider_error_payload,
)
from bespokelabs.sandbox.exceptions import ErrorCode, SandboxError

_JOB_ROUTE_ACTIONS = {
    ("POST", "/v1/jobs"): "job.submit",
    ("GET", "/v1/jobs/{job_id}"): "job.read",
    ("GET", "/v1/jobs/{job_id}/result"): "job.result",
    ("GET", "/v1/jobs/{job_id}/logs"): "job.logs",
    ("POST", "/v1/jobs/{job_id}/cancel"): "job.cancel",
}


def bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthenticationError("Bearer API key required")
    return authorization.removeprefix("Bearer ").strip()


def add_security_headers(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
        "object-src 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Strict-Transport-Security"] = (
        "max-age=63072000; includeSubDomains"
    )
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-store"
    return response


def path_job_mutation_action(request: Request) -> str | None:
    """Name a job mutation from the raw path, before routing has happened."""
    if request.method != "POST":
        return None
    if request.url.path == "/v1/jobs":
        return "job.submit"
    parts = request.url.path.split("/")
    if (
        len(parts) == 5
        and parts[1:3] == ["v1", "jobs"]
        and parts[3]
        and parts[4] == "cancel"
    ):
        return "job.cancel"
    return None


def job_route_action(request: Request) -> str:
    """Return the matched job route's fixed audit name, never a path value."""
    route = request.scope.get("route")
    template = getattr(route, "path", request.url.path)
    return _JOB_ROUTE_ACTIONS.get((request.method, template), "job.request")


def audit_outcome(status_code: int) -> str:
    if status_code < 400:
        return "success"
    return "denied" if status_code < 500 else "failure"


def install_exception_handlers(app: FastAPI) -> None:
    """Map control-plane errors to fixed JSON responses."""

    @app.exception_handler(AuthenticationError)
    async def authentication_error(
        _: Request, exc: AuthenticationError
    ) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    @app.exception_handler(AuthorizationError)
    async def authorization_error(
        _: Request, exc: AuthorizationError
    ) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(NotFoundError)
    async def not_found_error(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict_error(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(PolicyDeniedError)
    async def policy_denied_error(
        _: Request, exc: PolicyDeniedError
    ) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": exc.payload()})

    @app.exception_handler(ValueError)
    async def invalid_request(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(SandboxError)
    async def provider_error(_: Request, exc: SandboxError) -> JSONResponse:
        status_code = 504 if exc.code is ErrorCode.TIMEOUT else 502
        if exc.code in {
            ErrorCode.BACKEND_NOT_INSTALLED,
            ErrorCode.CONFIGURATION,
            ErrorCode.FEATURE_NOT_SUPPORTED,
        }:
            status_code = 422
        return JSONResponse(
            status_code=status_code,
            content={"detail": provider_error_payload(exc)},
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal control-plane error."},
        )
