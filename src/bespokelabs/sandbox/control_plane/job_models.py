"""Durable hosted-job values and the trusted adapter boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Collection, Protocol

from bespokelabs.sandbox.types import JobSpec, JobStatus

DEFAULT_LOG_TAIL = 1000
MAX_LOG_TAIL = 1000
MAX_LOG_BYTES = 1024 * 1024
#: Most failed tasks a multi-task result describes; more set ``truncated``.
MAX_TASK_FAILURES = 20
_MAX_TASKS = 2**31


def bounded_tail(tail: int | None) -> int:
    if tail is None:
        return DEFAULT_LOG_TAIL
    if isinstance(tail, bool) or not isinstance(tail, int) or tail < 0:
        raise ValueError("Log tail must be a nonnegative integer")
    return min(tail, MAX_LOG_TAIL)


def bounded_log_text(value: str) -> str:
    # Slice before encoding so UTF-8 clipping itself has bounded allocations.
    return (
        value[:MAX_LOG_BYTES]
        .encode("utf-8")[:MAX_LOG_BYTES]
        .decode("utf-8", errors="ignore")
    )


def bounded_task_summary(
    value: object, reasons: Collection[str]
) -> dict | None:
    """Rebuild a multi-task summary from allowlisted fields only.

    Returns ``None`` for a single-task job (no summary) or a malformed one,
    so nothing but counts, task indexes, integer exit codes and reasons in
    ``reasons`` can pass, and at most ``MAX_TASK_FAILURES`` failures.
    """
    if not isinstance(value, dict):
        return None
    summary: dict = {}
    for key in ("count", "succeeded", "failed", "other"):
        number = value.get(key)
        if type(number) is not int or not 0 <= number < _MAX_TASKS:
            return None
        summary[key] = number
    listed = value.get("failures")
    if not isinstance(listed, (list, tuple)):
        listed = []
    failures = []
    for entry in listed[:MAX_TASK_FAILURES]:
        index = entry.get("index") if isinstance(entry, dict) else None
        if type(index) is not int or not 0 <= index < _MAX_TASKS:
            return None
        exit_code = entry.get("exit_code")
        reason = entry.get("reason")
        failures.append(
            {
                "index": index,
                "exit_code": exit_code
                if type(exit_code) is int and -(2**31) <= exit_code < 2**31
                else None,
                "reason": reason
                if isinstance(reason, str) and reason in reasons
                else None,
            }
        )
    summary["failures"] = failures
    summary["truncated"] = (
        value.get("truncated") is True or len(listed) > MAX_TASK_FAILURES
    )
    return summary


class JobPhase(str, Enum):
    RESERVED = "reserved"
    SUBMITTING = "submitting"
    UNCERTAIN = "uncertain"
    ACTIVE = "active"
    TERMINAL = "terminal"


class ObservationConfidence(str, Enum):
    UNOBSERVED = "unobserved"
    VERIFIED = "verified"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"
    OWNERSHIP_CONFLICT = "ownership_conflict"


@dataclass(frozen=True)
class JobBinding:
    """Non-secret identity of an immutable server-configured credential binding.

    A version must never be repointed to another credential account, project,
    or region. Credentials themselves belong only in the adapter factory.
    """

    id: str
    version: int
    backend: str
    account: str
    project: str
    region: str

    def __post_init__(self) -> None:
        if (
            not all(
                isinstance(v, str) and v and len(v) <= 255
                for v in (
                    self.id,
                    self.backend,
                    self.account,
                    self.project,
                    self.region,
                )
            )
            or isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise ValueError("Invalid hosted-job binding")


@dataclass(frozen=True)
class HostedJob:
    id: str
    organization_id: str
    binding: JobBinding
    resource_name: str
    ownership_marker: str
    phase: JobPhase
    version: int
    fingerprint: str
    uid: str | None = None
    state: str | None = None
    confidence: ObservationConfidence = ObservationConfidence.UNOBSERVED
    cancel_requested: bool = False
    terminal_result: dict | None = None
    error_code: str | None = None
    lease_token: str | None = None
    lease_until: float | None = None
    submit_deadline: float | None = None


@dataclass(frozen=True)
class JobObservation:
    """Status whose actual provider ownership labels and UID were verified."""

    status: JobStatus
    uid: str


class ProviderMissing(Exception):
    """A typed absence, not proof that dispatch failed or cancellation won."""


class OwnershipConflict(Exception):
    """The resource name is occupied by an unverified incarnation."""


class CancellationUnavailable(Exception):
    """Configure a bound SDK supporting CancelJob; deletion is never allowed."""


class HostedJobAdapter(Protocol):
    """Trusted adapter; reserve is local and must not perform provider I/O.

    Observations, cancellation, and logs must verify the reserved ownership
    marker and the pinned UID against actual provider status details. An
    adapter must not adopt an UNKNOWN status as a verified terminal outcome.
    """

    def resource_name(self, binding: JobBinding, token: str) -> str:
        ...

    def validate(self, spec: JobSpec, binding: JobBinding) -> None:
        """Pure local validation, with no client construction or network I/O."""
        ...

    def submit(self, record: HostedJob, spec: JobSpec) -> str:
        ...

    def observe(self, record: HostedJob) -> JobObservation:
        ...

    def cancel(self, record: HostedJob) -> JobObservation:
        ...

    def logs(self, record: HostedJob, tail: int | None) -> str:
        ...


@dataclass(frozen=True)
class ConfiguredJobBinding:
    """Server-only allowlist and lazy factory; never serialized to the DB."""

    binding: JobBinding
    organizations: frozenset[str]
    adapter_factory: Callable[[], HostedJobAdapter]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.binding, JobBinding)
            or not isinstance(self.organizations, (list, tuple, set, frozenset))
            or not all(
                isinstance(org, str) and org and len(org) <= 255
                for org in self.organizations
            )
            or not callable(self.adapter_factory)
        ):
            raise ValueError("Invalid hosted-job binding configuration")
        # Empty means explicitly no tenants, never a global/default grant.
        object.__setattr__(self, "organizations", frozenset(self.organizations))
