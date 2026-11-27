"""Storage interfaces the hosted-job path depends on.

The service and HTTP routes are typed against these protocols so the SQLite
and PostgreSQL implementations are interchangeable. Nothing here imports a
database driver.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from .job_models import HostedJob, JobBinding, ObservationConfidence
from .models import IssuedAPIKey, Principal


@runtime_checkable
class JobRepository(Protocol):
    """Durable hosted-job rows with compare-and-swap transitions.

    Every mutating method takes the caller's snapshot and applies only if the
    stored row still matches its version, lease, UID, binding, and phase.
    """

    def digest(self, domain: str, value: object) -> str:
        ...

    def get(self, organization_id: str, job_id: str) -> HostedJob:
        ...

    def reserve(
        self,
        organization_id: str,
        key: str,
        fingerprint: str,
        binding: JobBinding,
        resource_name: str,
        marker: str,
    ) -> HostedJob:
        """Return the one job for this key, or conflict on another request."""
        ...

    def start_dispatch(
        self, record: HostedJob, now: float, duration: float
    ) -> bool:
        ...

    def request_cancel(self, organization_id: str, job_id: str) -> HostedJob:
        ...

    def claim_observation(
        self, record: HostedJob, now: float, duration: float
    ) -> HostedJob | None:
        ...

    def dispatch_unconfirmed(self, organization_id: str, job_id: str) -> None:
        ...

    def complete_observation(
        self,
        record: HostedJob,
        *,
        now: float,
        uid: str | None = None,
        state: str | None = None,
        result: dict | None = None,
        confidence: ObservationConfidence = ObservationConfidence.VERIFIED,
        error_code: str | None = None,
    ) -> bool:
        ...

    def release_observation(self, record: HostedJob) -> bool:
        ...


@runtime_checkable
class AuthorityStore(Protocol):
    """Organizations, API keys, and the audit trail behind job routes."""

    def create_organization(self, name: str) -> dict[str, str]:
        ...

    def issue_api_key(
        self,
        organization_id: str,
        *,
        name: str,
        scopes: Iterable[str] = ("*",),
        expires_at: str | None = None,
    ) -> IssuedAPIKey:
        ...

    def authenticate(self, secret: str) -> Principal:
        ...

    def record_audit(
        self,
        organization_id: str,
        *,
        action: str,
        outcome: str,
        created_at: str,
        api_key_id: str | None = None,
        session_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        details: dict | None = None,
    ) -> str:
        ...
