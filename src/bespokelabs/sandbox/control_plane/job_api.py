"""Explicitly enabled hosted-job HTTP plugin (optional server dependencies).

Import this module only in server applications. No adapter or provider SDK is
imported or selected here. Admission is mandatory, server-owned, and fail-closed.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from bespokelabs.sandbox.exceptions import SandboxConfigurationError
from bespokelabs.sandbox.types import JobSpec, JobState

from .errors import AuthorizationError, ConflictError, NotFoundError
from .job_interfaces import JobRepository
from .job_models import ConfiguredJobBinding, HostedJob, JobBinding, JobPhase
from .job_service import HostedJobService
from .job_store import HostedJobRepository, hosted_job_hmac
from .models import Principal

if TYPE_CHECKING:
    from .store import SQLiteStore

_MAX_REQUEST_BYTES = 64 * 1024
_SAFE_REASONS = frozenset(
    {
        "cancelled_before_dispatch",
        "cancelled",
        "timeout",
        "batch_failed",
        "exit_code",
        "vm_preempted",
        "vm_lost",
        "vm_rebooted",
        "vm_unresponsive",
        "vm_recreated",
    }
)
_SAFE_ERRORS = frozenset(
    {
        "submit_unconfirmed",
        "provider_missing",
        "ownership_conflict",
        "provider_unavailable",
        "cancel_unavailable",
        "cancel_unconfirmed",
    }
)
_RESERVED_OPTIONS = frozenset(
    {
        "project",
        "region",
        "job_id",
        "job_id_prefix",
        "credentials",
        "binding",
        "binding_id",
        "binding_version",
        "account",
    }
)


@dataclass(frozen=True)
class HostedJobConfiguration:
    """Trusted server configuration, never accepted from an HTTP request.

    ``submit_bindings`` maps opaque binding IDs to current submission versions;
    older versions stay in ``bindings`` for observation/cancellation. Empty
    containers grant nothing. ``admit`` must return exactly True before any key
    reservation or provider access, or raise/return False to deny. It must enforce
    the host's resource, image, account, deadline, option, and spend/rate policies.
    It is not a replacement billing ledger or a new quota authority.
    """

    bindings: tuple[ConfiguredJobBinding, ...]
    submit_bindings: dict[str, int]
    hmac_secret: bytes = field(repr=False)
    admit: Callable[[Principal, JobBinding, JobSpec], bool] = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.bindings, (list, tuple))
            or not all(
                isinstance(b, ConfiguredJobBinding) for b in self.bindings
            )
            or not isinstance(self.submit_bindings, dict)
            or not isinstance(self.hmac_secret, bytes)
            or len(self.hmac_secret) < 32
            or not callable(self.admit)
        ):
            raise ValueError("Invalid hosted-job server configuration")
        bindings = tuple(self.bindings)
        versions = {(b.binding.id, b.binding.version) for b in bindings}
        if len(versions) != len(bindings) or any(
            not isinstance(k, str)
            or not k
            or type(v) is not int
            or (k, v) not in versions
            for k, v in self.submit_bindings.items()
        ):
            raise ValueError("Invalid hosted-job submission binding versions")
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "submit_bindings", dict(self.submit_bindings))


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    command: str | list[str]
    image: str | None = Field(default=None, max_length=2048)
    env_vars: dict[str, str] = Field(default_factory=dict, max_length=256)
    cpu: float = Field(default=1.0, gt=0, le=256)
    memory_mb: int = Field(default=1024, gt=0, le=1048576)
    disk_mb: int | None = Field(default=None, gt=0, le=16777216)
    gpu: str | None = Field(default=None, max_length=128)
    timeout_secs: int = Field(default=3600, gt=0, le=604800)
    allow_internet: bool = True
    labels: dict[str, str] = Field(default_factory=dict, max_length=64)
    workdir: str | None = Field(default=None, max_length=4096)
    backend_options: dict[str, JsonValue] = Field(
        default_factory=dict, max_length=64
    )


class _Submission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    binding_id: str = Field(min_length=1, max_length=255)
    spec: _Spec


def _result_response(result: dict | None) -> dict | None:
    if not isinstance(result, dict):
        return None
    states = {"succeeded", "failed", "cancelled"}
    response = {
        "state": result.get("state")
        if isinstance(result.get("state"), str) and result["state"] in states
        else None,
        "exit_code": result.get("exit_code")
        if type(result.get("exit_code")) is int
        and -(2**31) <= result["exit_code"] < 2**31
        else None,
        "reason": result.get("reason")
        if isinstance(result.get("reason"), str)
        and result["reason"] in _SAFE_REASONS
        else None,
    }
    for key in ("started_at", "finished_at"):
        value = result.get(key)
        response[key] = None
        if isinstance(value, str) and len(value) <= 64:
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is not None:
                    response[key] = parsed.isoformat()
            except ValueError:
                pass
    return response


def _job_response(record: HostedJob) -> dict:
    # Deliberately not asdict/model_dump: provider identity and metadata never
    # cross this boundary, even if a future adapter adds fields to its snapshot.
    return {
        "id": record.id,
        "backend": record.binding.backend,
        "phase": record.phase.value,
        "state": record.state
        if record.state in {s.value for s in JobState}
        else None,
        "confidence": record.confidence.value,
        "cancel_requested": record.cancel_requested,
        "error_code": record.error_code
        if record.error_code in _SAFE_ERRORS
        else None,
        "result": _result_response(record.terminal_result)
        if record.phase is JobPhase.TERMINAL
        else None,
    }


def install_job_routes(
    app: FastAPI,
    store: SQLiteStore | None,
    configuration: HostedJobConfiguration,
    authenticate: Callable[[str | None, str], Principal],
    *,
    repository: JobRepository | None = None,
) -> None:
    """Install routes sharing the host's existing auth, CSRF, audit, and DB.

    By default the repository is built on ``store``'s SQLite database. Pass a
    ready ``repository`` (and ``store=None``) to use another implementation; it
    must have been built with ``hosted_job_hmac(configuration.hmac_secret)`` on
    the database that owns the organizations ``authenticate`` resolves.
    """
    if not isinstance(configuration, HostedJobConfiguration):
        raise ValueError("Explicit hosted-job server configuration is required")
    if (store is None) == (repository is None):
        raise ValueError(
            "Exactly one of an authority store or a job repository is required"
        )
    # Snapshot mutable server containers; there is no second path/DB authority.
    bindings = tuple(configuration.bindings)
    versions = dict(configuration.submit_bindings)
    configured = {(b.binding.id, b.binding.version): b for b in bindings}
    secret = configuration.hmac_secret
    admit = configuration.admit
    if repository is None:
        repository = HostedJobRepository(store.connect, hosted_job_hmac(secret))
    service = HostedJobService(repository, bindings)

    def invoke(operation: Callable):
        try:
            return operation()
        except AuthorizationError:
            raise HTTPException(403, "Hosted-job access denied") from None
        except NotFoundError:
            raise HTTPException(404, "Hosted job not found") from None
        except ConflictError:
            raise HTTPException(
                409, "Hosted-job operation conflicts with current state"
            ) from None
        except (ValueError, SandboxConfigurationError):
            raise HTTPException(422, "Invalid hosted-job request") from None
        except Exception:
            raise HTTPException(
                502, "Hosted-job operation unavailable"
            ) from None

    @app.post("/v1/jobs", status_code=202)
    async def submit(request: Request):
        principal = authenticate(
            request.headers.get("authorization"), "jobs:submit"
        )
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[\x21-\x7e]{1,512}", key):
            raise HTTPException(
                422, "A bounded Idempotency-Key header is required"
            )
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > _MAX_REQUEST_BYTES:
                raise HTTPException(413, "Hosted-job request is too large")
            data.extend(chunk)
        try:

            def reject_constant(_: str):
                raise ValueError("Non-finite JSON number")

            # Reject non-JSON numeric constants too (including inside options).
            body = _Submission.model_validate(
                json.loads(data, parse_constant=reject_constant)
            )
            spec = JobSpec(**body.spec.model_dump())
            if _RESERVED_OPTIONS.intersection(spec.backend_options):
                raise ValueError()
        except Exception:
            raise HTTPException(422, "Invalid hosted-job request") from None
        version = versions.get(body.binding_id)
        binding = configured.get((body.binding_id, version))
        if (
            binding is None
            or principal.organization_id not in binding.organizations
        ):
            raise HTTPException(403, "Hosted-job binding not allowed")

        def dispatch():
            try:
                allowed = admit(principal, binding.binding, copy.deepcopy(spec))
            except Exception:
                allowed = False
            if allowed is not True:
                raise AuthorizationError()
            return _job_response(
                service.submit(
                    principal,
                    spec,
                    idempotency_key=key,
                    binding_id=body.binding_id,
                    binding_version=version,
                )
            )

        return await run_in_threadpool(invoke, dispatch)

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        principal = authenticate(
            request.headers.get("authorization"), "jobs:read"
        )
        return invoke(lambda: _job_response(service.get(principal, job_id)))

    @app.get("/v1/jobs/{job_id}/result")
    def result(job_id: str, request: Request):
        principal = authenticate(
            request.headers.get("authorization"), "jobs:result"
        )
        return invoke(
            lambda: {
                "result": _result_response(service.result(principal, job_id))
            }
        )

    @app.get("/v1/jobs/{job_id}/logs")
    def logs(job_id: str, request: Request):
        principal = authenticate(
            request.headers.get("authorization"), "jobs:logs"
        )
        raw_tail = request.query_params.get("tail", "1000")
        if not re.fullmatch(r"[0-9]{1,4}", raw_tail) or int(raw_tail) > 1000:
            raise HTTPException(422, "Log tail must be between 0 and 1000")
        return invoke(
            lambda: {"logs": service.logs(principal, job_id, int(raw_tail))}
        )

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str, request: Request):
        principal = authenticate(
            request.headers.get("authorization"), "jobs:cancel"
        )
        return invoke(lambda: _job_response(service.cancel(principal, job_id)))
