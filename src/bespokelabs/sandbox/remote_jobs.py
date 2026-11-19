"""Opt-in, provider-free client for the existing hosted-job HTTP routes.

No retries, provider fallback, cookie authentication, or redirect following are
performed. Transport errors leave the remote operation's outcome unknown.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import datetime
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bespokelabs.sandbox.remote import RemoteSandboxError
from bespokelabs.sandbox.types import JobSpec, JobState

_MAX_REQUEST_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # JSON escaping of a 1 MiB log tail.
_MAX_ERROR_BYTES = 4096
_ID_PATTERN = r"[A-Za-z0-9_-]{1,255}"


class RemoteJobError(RemoteSandboxError):
    """Sanitized HTTP/protocol error; never proof of remote job failure.

    ``outcome`` remains ``unknown``, including for submit/cancel timeouts.
    ``retryable`` describes transport availability, not permission to submit a
    new operation. Recover a lost submit response with the SAME key and payload.
    """


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RemoteJobResult(_Response):
    """Allowlisted terminal snapshot, not stdout or a native JobStatus.

    Nullable fields are preserved exactly; absent exit codes are not zero.
    Timestamps remain timezone-aware ISO strings as returned by the service.
    """

    state: Literal["succeeded", "failed", "cancelled"] | None
    exit_code: int | None = Field(ge=-(2**31), lt=2**31)
    reason: (
        Literal[
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
        ]
        | None
    )
    started_at: str | None = Field(max_length=64)
    finished_at: str | None = Field(max_length=64)

    @field_validator("started_at", "finished_at")
    @classmethod
    def _timestamp(cls, value: str | None) -> str | None:
        if value is not None and datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("Timezone required")
        return value


class RemoteJobStatus(_Response):
    """Hosted observation including durable phase and confidence.

    Do not interpret cancellation intent, a missing provider observation, or a
    last-known state as verified completion. This deliberately does not reuse
    native JobStatus.done (where UNKNOWN has different terminal semantics).
    """

    id: str = Field(pattern="^" + _ID_PATTERN + "$", max_length=255)
    backend: str = Field(min_length=1, max_length=255)
    phase: Literal["reserved", "submitting", "uncertain", "active", "terminal"]
    state: JobState | None
    confidence: Literal[
        "unobserved", "verified", "missing", "unavailable", "ownership_conflict"
    ]
    cancel_requested: bool
    error_code: (
        Literal[
            "submit_unconfirmed",
            "provider_missing",
            "ownership_conflict",
            "provider_unavailable",
            "cancel_unavailable",
            "cancel_unconfirmed",
        ]
        | None
    )
    result: RemoteJobResult | None

    @field_validator("id")
    @classmethod
    def _job_id(cls, value: str) -> str:
        return _identifier(value)


class _ResultResponse(_Response):
    result: RemoteJobResult | None


class _LogsResponse(_Response):
    logs: str = Field(max_length=1024 * 1024)

    @field_validator("logs")
    @classmethod
    def _log_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Log limit exceeded")
        return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_ResponseT = TypeVar("_ResponseT", bound=_Response)


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(_ID_PATTERN, value):
        raise ValueError("job_id must be a bounded opaque path identifier")
    return value


def _header(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        rf"[\x21-\x7e]{{1,{maximum}}}", value
    ):
        raise ValueError(
            f"{name} must contain 1 to {maximum} visible ASCII characters"
        )
    return value


def _base_url(value: str) -> str:
    # Reject ambiguous encodings before urlsplit can strip control characters.
    if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]+", value):
        raise ValueError("Invalid base_url")
    try:
        url = urllib.parse.urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or "?" in value
            or "#" in value
            or "\\" in value
            or "%" in url.netloc
            or url.port == 0
        ):
            raise ValueError()
        path = url.path.rstrip("/")
        if path and (
            not path.startswith("/")
            or any(
                not re.fullmatch(_ID_PATTERN, p) for p in path[1:].split("/")
            )
        ):
            raise ValueError()
        return urllib.parse.urlunsplit((url.scheme, url.netloc, path, "", ""))
    except ValueError:
        raise ValueError("Invalid base_url") from None


class RemoteJobClient:
    """Synchronous client for an explicitly enabled hosted-job API.

    ``base_url`` is an HTTP(S) origin with an optional plain path prefix; it
    must not contain credentials, query parameters, fragments or dot segments.
    Use HTTPS except for trusted local testing. Only the supplied bearer key is
    sent: no dashboard cookies, CSRF tokens, admin headers or ambient API keys.
    Like RemoteSandboxClient, the timeout defaults to 60 seconds and urllib's
    environment proxy handling is used; set ``trust_env=False`` to disable it.
    Redirects are always rejected, including same-origin redirects, to avoid
    credential forwarding and implicit POST replays. Instances contain no job
    registry and do not own remote resources or cancel on close/destruction.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_secs: float = 60,
        trust_env: bool = True,
    ) -> None:
        self._base_url = _base_url(base_url)
        self._api_key = _header(api_key, "api_key", 4096)
        if (
            isinstance(timeout_secs, bool)
            or not isinstance(timeout_secs, (int, float))
            or not math.isfinite(timeout_secs)
            or timeout_secs <= 0
        ):
            raise ValueError("timeout_secs must be positive and finite")
        if type(trust_env) is not bool:
            raise ValueError("trust_env must be a boolean")
        self._timeout_secs = timeout_secs
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(None if trust_env else {}),
            _NoRedirect(),
        )

    def submit(
        self, spec: JobSpec, *, binding_id: str, idempotency_key: str
    ) -> RemoteJobStatus:
        """Submit once; caller MUST persist and reuse the key on uncertainty.

        The server scopes the key to the authenticated organization and rejects
        changed payloads/bindings with 409. No key is generated automatically.
        Replays still require server admission and the original binding version.
        """
        key = _header(idempotency_key, "idempotency_key", 512)
        if not isinstance(binding_id, str) or not 1 <= len(binding_id) <= 255:
            raise ValueError("binding_id must contain 1 to 255 characters")
        if not isinstance(spec, JobSpec):
            raise ValueError("spec must be a JobSpec")
        try:
            body = json.dumps(
                {"binding_id": binding_id, "spec": asdict(spec)},
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise ValueError("spec must contain finite JSON values") from None
        if len(body) > _MAX_REQUEST_BYTES:
            raise ValueError("Hosted-job request exceeds 64 KiB")
        return self._request(
            "POST",
            "/v1/jobs",
            RemoteJobStatus,
            body=body,
            key=key,
            expected=202,
        )

    def status(self, job_id: str) -> RemoteJobStatus:
        """Read/refresh a hosted observation; uncertainty is not completion."""
        return self._request(
            "GET", f"/v1/jobs/{_identifier(job_id)}", RemoteJobStatus
        )

    def result(self, job_id: str) -> RemoteJobResult | None:
        """Return the terminal snapshot, or None (not a success assertion)."""
        return self._request(
            "GET", f"/v1/jobs/{_identifier(job_id)}/result", _ResultResponse
        ).result

    def logs(self, job_id: str, *, tail: int = 1000) -> str:
        """Read at most 1000 log entries / 1 MiB, not a streaming log API."""
        job_id = _identifier(job_id)
        if type(tail) is not int or not 0 <= tail <= 1000:
            raise ValueError("tail must be an integer between 0 and 1000")
        return self._request(
            "GET", f"/v1/jobs/{job_id}/logs?tail={tail}", _LogsResponse
        ).logs

    def cancel(self, job_id: str) -> RemoteJobStatus:
        """Persist cancellation intent; response is not proof of cloud closure."""
        return self._request(
            "POST", f"/v1/jobs/{_identifier(job_id)}/cancel", RemoteJobStatus
        )

    def _request(
        self,
        method: str,
        path: str,
        model: type[_ResponseT],
        *,
        body: bytes | None = None,
        key: str | None = None,
        expected: int = 200,
    ) -> _ResponseT:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if key is not None:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            self._base_url + path, data=body, headers=headers, method=method
        )
        try:
            with self._opener.open(
                request, timeout=self._timeout_secs
            ) as response:
                if response.status != expected:
                    raise RemoteJobError(
                        "Unexpected hosted-job HTTP status",
                        status_code=response.status,
                        code="http_status",
                        op="http_request",
                    )
                limit = (
                    _MAX_RESPONSE_BYTES
                    if model is _LogsResponse
                    else _MAX_REQUEST_BYTES
                )
                raw = response.read(limit + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            # Never parse or reflect server/proxy error bodies, URLs or headers.
            try:
                exc.read(_MAX_ERROR_BYTES)
            except (OSError, http.client.HTTPException):
                pass
            finally:
                exc.close()
            raise RemoteJobError(
                "Hosted-job HTTP request rejected",
                status_code=status,
                code="http_status",
                op="http_request",
            ) from None
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            raise RemoteJobError(
                "Hosted-job transport unavailable; remote outcome unknown",
                code="connection",
                op="http_request",
                retryable=True,
            ) from None
        try:
            if len(raw) > limit:
                raise ValueError()
            return model.model_validate_json(raw)
        except (ValueError, RecursionError):
            raise RemoteJobError(
                "Invalid or oversized hosted-job response; remote outcome unknown",
                code="invalid_response",
                op="http_request",
            ) from None
