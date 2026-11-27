"""Tenant-authorized durable native jobs, independent of HTTP wiring."""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import asdict, replace
from typing import Callable, Iterable

from bespokelabs.sandbox.types import JobSpec, JobState

from .errors import AuthorizationError, ConflictError, ControlPlaneError
from .job_interfaces import JobRepository
from .job_models import (
    CancellationUnavailable,
    ConfiguredJobBinding,
    HostedJob,
    HostedJobAdapter,
    JobObservation,
    JobPhase,
    ObservationConfidence,
    OwnershipConflict,
    ProviderMissing,
    bounded_log_text,
    bounded_tail,
)
from .models import Principal

_TERMINAL = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}
_SAFE_REASONS = {
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


class HostedJobService:

    def __init__(
        self,
        repository: JobRepository,
        bindings: Iterable[ConfiguredJobBinding],
        *,
        clock: Callable[[], float] = time.time,
        observation_lease_secs: float = 30,
        submit_timeout_secs: float = 60,
    ) -> None:
        if not 0 < observation_lease_secs < float("inf"):
            raise ValueError("Observation lease must be positive and finite")
        if not 0 < submit_timeout_secs < float("inf"):
            raise ValueError("Submit timeout must be positive and finite")
        if not isinstance(bindings, (list, tuple, set, frozenset)) or not all(
            isinstance(binding, ConfiguredJobBinding) for binding in bindings
        ):
            raise ValueError(
                "Explicit hosted-job binding containers are required"
            )
        self.repository = repository
        self._submit_timeout_secs = submit_timeout_secs
        self._bindings: dict[tuple[str, int], ConfiguredJobBinding] = {}
        for configured in bindings:
            key = (configured.binding.id, configured.binding.version)
            if key in self._bindings:
                raise ValueError("Duplicate hosted-job binding version")
            self._bindings[key] = configured
        self._clock = clock
        self._lease_secs = observation_lease_secs

    @staticmethod
    def _authorize(principal: Principal, operation: str) -> None:
        # Explicit job scopes only; no expansion from sandbox legacy scopes.
        if (
            "*" not in principal.scopes
            and f"jobs:{operation}" not in principal.scopes
        ):
            raise AuthorizationError("Hosted-job scope required")

    def _adapter(
        self, principal: Principal, record: HostedJob
    ) -> HostedJobAdapter:
        configured = self._bindings.get(
            (record.binding.id, record.binding.version)
        )
        if (
            configured is None
            or configured.binding != record.binding
            or principal.organization_id not in configured.organizations
        ):
            raise ConflictError("Hosted-job binding is unavailable")
        try:
            return configured.adapter_factory()
        except Exception:
            raise ControlPlaneError(
                "Hosted-job provider is unavailable"
            ) from None

    def submit(
        self,
        principal: Principal,
        spec: JobSpec,
        *,
        idempotency_key: str,
        binding_id: str,
        binding_version: int,
    ) -> HostedJob:
        self._authorize(principal, "submit")
        if (
            not isinstance(idempotency_key, str)
            or not 0 < len(idempotency_key) <= 512
        ):
            raise ValueError("A bounded nonempty idempotency key is required")
        configured = self._bindings.get((binding_id, binding_version))
        if (
            configured is None
            or principal.organization_id not in configured.organizations
        ):
            raise AuthorizationError("Hosted-job binding not allowed")
        # Freeze the runtime request so caller mutation cannot change a dispatch
        # after its canonical HMAC was reserved. Only the HMAC reaches storage.
        try:
            runtime_spec = JobSpec(**copy.deepcopy(asdict(spec)))
            fingerprint = self.repository.digest(
                "submit-payload",
                {
                    "binding": asdict(configured.binding),
                    "spec": asdict(runtime_spec),
                },
            )
        except Exception:
            raise ValueError("Invalid hosted-job request") from None
        # Resource reservation is local. Factories must not perform network I/O.
        try:
            adapter = configured.adapter_factory()
            token = "hosted-" + uuid.uuid4().hex
            resource_name = adapter.resource_name(configured.binding, token)
        except Exception:
            raise ControlPlaneError(
                "Hosted-job provider is unavailable"
            ) from None
        # Invalid routing/ownership options must not burn an idempotency key.
        # Adapter validation is pure and must not construct a provider client.
        adapter.validate(runtime_spec, configured.binding)
        record = self.repository.reserve(
            principal.organization_id,
            idempotency_key,
            fingerprint,
            configured.binding,
            resource_name,
            uuid.uuid4().hex,
        )
        if (
            record.phase is JobPhase.RESERVED
            and self.repository.start_dispatch(
                record, self._clock(), self._submit_timeout_secs
            )
        ):
            # No code path ever changes a dispatched job back to reserved. A
            # process death even immediately before this RPC requires observation,
            # not a second attempt. A pre-dispatch reservation needs client replay.
            record = self.repository.get(principal.organization_id, record.id)
            try:
                accepted = adapter.submit(record, runtime_spec)
                if accepted != record.resource_name:
                    raise OwnershipConflict()
            except Exception:
                self.repository.dispatch_unconfirmed(
                    principal.organization_id, record.id
                )
        return self._refresh(
            principal, self.repository.get(principal.organization_id, record.id)
        )

    def get(self, principal: Principal, job_id: str) -> HostedJob:
        self._authorize(principal, "read")
        return self._refresh(
            principal, self.repository.get(principal.organization_id, job_id)
        )

    def cancel(self, principal: Principal, job_id: str) -> HostedJob:
        self._authorize(principal, "cancel")
        # Persist before any provider RPC, even if the binding is unavailable.
        record = self.repository.request_cancel(
            principal.organization_id, job_id
        )
        return self._refresh(principal, record)

    def result(self, principal: Principal, job_id: str) -> dict | None:
        self._authorize(principal, "result")
        record = self.repository.get(principal.organization_id, job_id)
        record = self._refresh(principal, record)
        return (
            copy.deepcopy(record.terminal_result)
            if record.phase is JobPhase.TERMINAL
            else None
        )

    def logs(
        self, principal: Principal, job_id: str, tail: int | None = None
    ) -> str:
        self._authorize(principal, "logs")
        record = self.repository.get(principal.organization_id, job_id)
        tail = bounded_tail(tail)
        record = self._refresh(principal, record)
        if record.uid is None:
            raise ConflictError("Hosted-job identity is not yet verified")
        adapter = self._adapter(principal, record)
        try:
            return bounded_log_text(adapter.logs(record, tail))
        except ProviderMissing:
            self._record_access_error(
                record, ObservationConfidence.MISSING, "provider_missing"
            )
        except OwnershipConflict:
            self._record_access_error(
                record,
                ObservationConfidence.OWNERSHIP_CONFLICT,
                "ownership_conflict",
            )
        except Exception:
            # Log transport/format failures say nothing about job lifecycle.
            # Keep the last verified status; return only a sanitized error.
            pass
        raise ControlPlaneError("Hosted-job logs are unavailable") from None

    def _record_access_error(
        self,
        record: HostedJob,
        confidence: ObservationConfidence,
        code: str,
    ) -> None:
        # A log RPC can lose the resource after its preflight status read.
        # Publish only if this exact nonterminal row is still current; never
        # invalidate a cached terminal result or a newer provider observation.
        claimed = self.repository.claim_observation(
            record, self._clock(), self._lease_secs
        )
        if claimed is not None:
            self.repository.complete_observation(
                claimed,
                now=self._clock(),
                confidence=confidence,
                error_code=code,
            )

    @staticmethod
    def _verify(record: HostedJob, observation: JobObservation) -> None:
        if (
            observation.status.job_id != record.resource_name
            or not isinstance(observation.uid, str)
            or not observation.uid
            or (record.uid is not None and record.uid != observation.uid)
        ):
            raise OwnershipConflict()
        if observation.status.state is JobState.UNKNOWN:
            raise ProviderMissing()

    def _refresh(self, principal: Principal, record: HostedJob) -> HostedJob:
        if record.phase in {JobPhase.RESERVED, JobPhase.TERMINAL}:
            return record
        adapter = self._adapter(principal, record)
        # Retry only a locally invalidated observation, never submission. A
        # cancellation intent arriving during status invalidates that status.
        for _ in range(2):
            claimed = self.repository.claim_observation(
                record, self._clock(), self._lease_secs
            )
            if claimed is None:
                break
            try:
                observation = adapter.observe(claimed)
                self._verify(claimed, observation)
                # Pin UID before a destructive call, and only with a live lease
                # and unchanged row. The next observation checks it again.
                applied = self._save(claimed, observation)
                record = self.repository.get(
                    principal.organization_id, record.id
                )
                if (
                    not applied
                    and observation.status.state in _TERMINAL
                    and record.cancel_requested
                    and not claimed.cancel_requested
                    and record.version == claimed.version + 1
                    and replace(
                        record,
                        version=claimed.version,
                        cancel_requested=claimed.cancel_requested,
                    )
                    == claimed
                ):
                    # Intent alone must not erase a verified completion that
                    # raced it. Never rebase over a changed UID, lease, binding,
                    # provider observation, or already terminal snapshot.
                    applied = self._save(record, observation)
                    record = self.repository.get(
                        principal.organization_id, record.id
                    )
                if (
                    applied
                    and record.cancel_requested
                    and record.phase is not JobPhase.TERMINAL
                ):
                    return self._cancel_verified(principal, adapter, record)
            except ProviderMissing:
                self.repository.complete_observation(
                    claimed,
                    now=self._clock(),
                    confidence=ObservationConfidence.MISSING,
                    error_code="provider_missing",
                )
            except OwnershipConflict:
                self.repository.complete_observation(
                    claimed,
                    now=self._clock(),
                    confidence=ObservationConfidence.OWNERSHIP_CONFLICT,
                    error_code="ownership_conflict",
                )
            except Exception:
                self.repository.complete_observation(
                    claimed,
                    now=self._clock(),
                    confidence=ObservationConfidence.UNAVAILABLE,
                    error_code="provider_unavailable",
                )
            # Intent changes invalidate the row version but not lease ownership.
            # Release only our own lease using its current row version.
            current = self.repository.get(principal.organization_id, claimed.id)
            if current.lease_token == claimed.lease_token:
                self.repository.release_observation(current)
            record = self.repository.get(principal.organization_id, record.id)
            if (
                record.phase is JobPhase.TERMINAL
                or record.version == claimed.version + 1
            ):
                break
        return self.repository.get(principal.organization_id, record.id)

    def _save(self, record: HostedJob, observation: JobObservation) -> bool:
        status = observation.status
        result = None
        if status.state in _TERMINAL:
            # No raw provider message, labels, details or output in snapshots.
            result = {
                "state": status.state.value,
                "exit_code": status.exit_code
                if isinstance(status.exit_code, int)
                else None,
                "reason": status.reason
                if status.reason in _SAFE_REASONS
                else None,
                "started_at": status.started_at.isoformat()
                if status.started_at
                else None,
                "finished_at": status.finished_at.isoformat()
                if status.finished_at
                else None,
            }
        return self.repository.complete_observation(
            record,
            now=self._clock(),
            uid=observation.uid,
            state=status.state.value,
            result=result,
        )

    def _cancel_verified(
        self,
        principal: Principal,
        adapter: HostedJobAdapter,
        record: HostedJob,
    ) -> HostedJob:
        claimed = self.repository.claim_observation(
            record, self._clock(), self._lease_secs
        )
        if claimed is None:
            return self.repository.get(principal.organization_id, record.id)
        try:
            # Adapters must re-observe actual ownership/UID before cancellation.
            observation = adapter.cancel(claimed)
            self._verify(claimed, observation)
            self._save(claimed, observation)
        except ProviderMissing:
            self.repository.complete_observation(
                claimed,
                now=self._clock(),
                confidence=ObservationConfidence.MISSING,
                error_code="provider_missing",
            )
        except OwnershipConflict:
            self.repository.complete_observation(
                claimed,
                now=self._clock(),
                confidence=ObservationConfidence.OWNERSHIP_CONFLICT,
                error_code="ownership_conflict",
            )
        except CancellationUnavailable:
            self.repository.complete_observation(
                claimed,
                now=self._clock(),
                confidence=ObservationConfidence.UNAVAILABLE,
                error_code="cancel_unavailable",
            )
        except Exception:
            self.repository.complete_observation(
                claimed,
                now=self._clock(),
                confidence=ObservationConfidence.UNAVAILABLE,
                error_code="cancel_unconfirmed",
            )
        current = self.repository.get(principal.organization_id, record.id)
        if current.lease_token == claimed.lease_token:
            self.repository.release_observation(current)
        return self.repository.get(principal.organization_id, record.id)
