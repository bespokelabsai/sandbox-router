"""Offline durable native jobs with two independent repository instances."""

from __future__ import annotations

import copy
import hashlib
import hmac
import secrets
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bespokelabs.sandbox.control_plane.errors import (
    AuthorizationError,
    ConflictError,
    ControlPlaneError,
    NotFoundError,
)
from bespokelabs.sandbox.control_plane.job_adapters.gcp_batch import (
    OWNERSHIP_LABEL,
    GcpBatchHostedAdapter,
)
from bespokelabs.sandbox.control_plane.job_models import (
    ConfiguredJobBinding,
    JobBinding,
    JobPhase,
    ObservationConfidence,
    OwnershipConflict,
    ProviderMissing,
)
from bespokelabs.sandbox.control_plane.job_service import HostedJobService
from bespokelabs.sandbox.control_plane.job_store import HostedJobRepository
from bespokelabs.sandbox.control_plane.models import Principal
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from bespokelabs.sandbox.exceptions import SandboxNotFoundError
from bespokelabs.sandbox.types import JobSpec, JobState, JobStatus


class NativeBackend:
    """A direct SandboxJobBackend fake, not a hosted-adapter shortcut."""

    def __init__(self):
        self.submits = 0
        self.cancels = 0
        self.reads = 0
        self.log_reads = 0
        self.jobs = {}
        self.runtime_spec = None
        self.submit_entered = None
        self.submit_release = None
        self.lost_response = False
        self.submit_error = None
        self.status_hook = None
        self.cancel_hook = None

    def submit_job(self, spec):
        self.submits += 1
        self.runtime_spec = copy.deepcopy(spec)
        if self.submit_entered:
            self.submit_entered.set()
            assert self.submit_release.wait(5)
        if self.submit_error:
            raise self.submit_error
        options = spec.backend_options
        name = f"projects/{options['project']}/locations/{options['region']}/jobs/{options['job_id']}"
        self.jobs[name] = JobStatus(
            name,
            JobState.RUNNING,
            details={"uid": secrets.token_hex(16), "labels": dict(spec.labels)},
        )
        if self.lost_response:
            raise TimeoutError("Response lost")
        return name

    def job_status(self, job_id):
        self.reads += 1
        status = copy.deepcopy(
            self.jobs.get(
                job_id,
                JobStatus(
                    job_id,
                    JobState.UNKNOWN,
                    reason="job_not_found",
                ),
            )
        )
        hook, self.status_hook = self.status_hook, None
        if hook:
            hook(status)
        return status

    def cancel_job(self, job_id):
        self.cancels += 1
        if self.cancel_hook:
            self.cancel_hook()
        status = copy.deepcopy(self.jobs[job_id])
        if status.done:
            return status
        status.state = JobState.RUNNING
        status.reason = "cancelling"
        return status

    def job_logs(self, job_id, tail):
        self.log_reads += 1
        return "runtime output\n" if tail != 0 else ""


def logging_response(text):
    """SDK-free single-page response at the public Logging RPC boundary."""

    class TextEntry:

        text_payload = text

        def __contains__(self, field):
            return field == "text_payload"

    return SimpleNamespace(
        pages=iter([SimpleNamespace(entries=[TextEntry()], next_page_token="")])
    )


class Harness:

    def __init__(self, path):
        self.path = path
        self.pepper = secrets.token_bytes(32)
        authority = SQLiteStore(path, key_pepper=secrets.token_hex(32))
        self.org = authority.create_organization("Public example")["id"]
        self.other_org = authority.create_organization("Second example")["id"]
        self.principal = Principal(self.org, "example-key", frozenset({"*"}))
        self.other = Principal(self.other_org, "other-key", frozenset({"*"}))
        self.binding = JobBinding(
            "batch",
            1,
            "gcp_batch",
            "worker-account",
            "example-project",
            "us-central1",
        )
        self.backend = NativeBackend()
        self.logging = SimpleNamespace(list_log_entries=self.list_log_entries)
        self.now = 100.0
        self.repository = self.repo()
        self.service = self.service_for(self.repository)
        self.spec = JobSpec("printf hello")

    def repo(self):
        return HostedJobRepository(
            lambda: sqlite3.connect(self.path, timeout=5),
            lambda value: hmac.new(
                self.pepper, value, hashlib.sha256
            ).hexdigest(),
        )

    def configured(self, binding=None):
        binding = binding or self.binding
        return ConfiguredJobBinding(
            binding,
            frozenset({self.org, self.other_org}),
            lambda: GcpBatchHostedAdapter(
                binding,
                lambda: self.backend,
                cancel_client_factory=lambda: self.cancel_rpc(),
                logging_client_factory=lambda: self.logging,
            ),
        )

    def list_log_entries(self, *, request, retry, timeout):
        self.backend.log_reads += 1
        return logging_response("runtime output\n")

    def cancel_rpc(self):
        # Explicit SDK-shaped CancelJob-only handle; never a native fallback.
        backend = self.backend

        class CancelClient:

            def cancel_job(self, *, name, retry, timeout):
                return backend.cancel_job(name)

        return CancelClient()

    def service_for(self, repository):
        return HostedJobService(
            repository,
            [self.configured()],
            clock=lambda: self.now,
            observation_lease_secs=10,
        )

    def submit(
        self, spec=None, key="example-operation", principal=None, service=None
    ):
        return (service or self.service).submit(
            principal or self.principal,
            spec or self.spec,
            idempotency_key=key,
            binding_id=self.binding.id,
            binding_version=self.binding.version,
        )

    def finish(self, record, state=JobState.SUCCEEDED):
        status = self.backend.jobs[record.resource_name]
        status.state = state
        status.exit_code = 0 if state is JobState.SUCCEEDED else 1


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path / "authority.sqlite")


def test_submit_preserves_native_defaults_and_runtime_only_payload(h):
    sentinel = secrets.token_hex(32)
    key = secrets.token_hex(32)
    spec = JobSpec(["printf", sentinel], env_vars={"TOKEN": sentinel})
    record = h.submit(spec, key)
    assert record.phase is JobPhase.ACTIVE
    assert record.uid
    assert h.backend.submits == 1
    actual = h.backend.runtime_spec
    assert actual.command == spec.command
    assert actual.env_vars == spec.env_vars
    assert (actual.cpu, actual.memory_mb, actual.timeout_secs) == (
        1,
        1024,
        3600,
    )
    assert (
        actual.backend_options["job_id"]
        == record.resource_name.rsplit("/", 1)[-1]
    )
    assert actual.labels[OWNERSHIP_LABEL] == record.ownership_marker
    assert spec.backend_options == {} and spec.labels == {}
    with sqlite3.connect(h.path) as connection:
        dump = "\n".join(connection.iterdump())
    assert sentinel not in dump and key not in dump
    assert sentinel.encode() not in h.path.read_bytes()


def test_two_instances_single_dispatch_during_inflight_replay(h):
    entered, release = threading.Event(), threading.Event()
    h.backend.submit_entered, h.backend.submit_release = entered, release
    second = h.service_for(h.repo())
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(h.submit)
        try:
            assert entered.wait(5)
            replay = h.submit(service=second)
            assert replay.phase is JobPhase.SUBMITTING
            assert replay.confidence is ObservationConfidence.UNOBSERVED
            assert replay.error_code is None
            assert h.backend.submits == 1
        finally:
            release.set()
        accepted = first.result(5)
    assert accepted.id == replay.id
    assert accepted.phase is JobPhase.ACTIVE
    assert h.backend.submits == 1


def test_lost_response_reopen_adopts_same_resource_without_dispatch(h):
    h.backend.lost_response = True
    accepted = h.submit()
    assert accepted.phase is JobPhase.ACTIVE
    reopened = h.service_for(h.repo())
    replay = h.submit(service=reopened)
    assert (replay.id, replay.uid) == (accepted.id, accepted.uid)
    assert h.backend.submits == 1


def test_failed_dispatch_is_never_retried_even_after_restart_and_lease_expiry(
    h,
):
    sentinel = secrets.token_hex(32)
    h.backend.submit_error = RuntimeError(sentinel)
    first = h.submit()
    assert first.phase is JobPhase.UNCERTAIN
    assert first.terminal_result is None
    h.now += 100
    again = h.submit(service=h.service_for(h.repo()))
    assert again.id == first.id and h.backend.submits == 1
    assert again.confidence is ObservationConfidence.MISSING
    with sqlite3.connect(h.path) as connection:
        assert sentinel not in "\n".join(connection.iterdump())


def test_reserved_process_loss_requires_matching_client_replay(h):
    with patch.object(h.repository, "start_dispatch", return_value=False):
        reserved = h.submit()
    assert reserved.phase is JobPhase.RESERVED
    assert h.backend.submits == 0
    assert h.service.get(h.principal, reserved.id).phase is JobPhase.RESERVED
    assert h.service.result(h.principal, reserved.id) is None
    with pytest.raises(ConflictError):
        h.submit(JobSpec("different command"))
    replay = h.submit(service=h.service_for(h.repo()))
    assert replay.id == reserved.id and h.backend.submits == 1


def test_crash_after_dispatch_start_before_rpc_does_not_redispatch(h):
    with patch.object(h.repository, "start_dispatch", return_value=False):
        reserved = h.submit()
    assert h.repository.start_dispatch(reserved, h.now, 60)
    h.now += 100
    replay = h.submit(service=h.service_for(h.repo()))
    assert replay.phase is JobPhase.UNCERTAIN
    assert h.backend.submits == 0


@pytest.mark.parametrize(
    "change",
    [
        {"command": "changed"},
        {"env_vars": {"EXAMPLE": "changed"}},
        {"cpu": 2},
        {"labels": {"team": "changed"}},
        {"backend_options": {"machine_type": "changed"}},
    ],
)
def test_changed_payload_conflicts_without_provider_access(h, change):
    h.submit()
    calls = h.backend.reads
    with pytest.raises(ConflictError):
        h.submit(replace(h.spec, **change))
    assert h.backend.submits == 1 and h.backend.reads == calls


def test_idempotency_scope_is_org_operation_not_api_key(h):
    first = h.submit()
    another_key = replace(h.principal, api_key_id="replacement-key")
    assert h.submit(principal=another_key).id == first.id
    other = h.submit(principal=h.other)
    assert other.id != first.id and other.resource_name != first.resource_name
    assert h.backend.submits == 2


def test_tenant_lookup_before_provider_or_cached_result(h):
    record = h.submit()
    h.finish(record)
    assert h.service.result(h.principal, record.id)["state"] == "succeeded"
    reads = h.backend.reads
    for operation in (
        h.service.get,
        h.service.cancel,
        h.service.result,
        h.service.logs,
    ):
        with pytest.raises(NotFoundError):
            operation(h.other, record.id)
    assert h.backend.reads == reads and h.backend.cancels == 0


@pytest.mark.parametrize(
    "scope,operation",
    [
        ("read", "get"),
        ("cancel", "cancel"),
        ("logs", "logs"),
        ("result", "result"),
    ],
)
def test_explicit_scopes_do_not_expand_legacy_permissions(h, scope, operation):
    record = h.submit()
    legacy = replace(
        h.principal, scopes=frozenset({"sandboxes:write", "sandboxes:read"})
    )
    with pytest.raises(AuthorizationError):
        getattr(h.service, operation)(legacy, record.id)
    scoped = replace(h.principal, scopes=frozenset({f"jobs:{scope}"}))
    getattr(h.service, operation)(scoped, record.id)
    assert h.backend.submits == 1


def test_submit_scope_and_binding_allowlist(h):
    denied = replace(h.principal, scopes=frozenset({"sandboxes:write"}))
    with pytest.raises(AuthorizationError):
        h.submit(principal=denied)
    service = HostedJobService(
        h.repository, [replace(h.configured(), organizations=frozenset())]
    )
    with pytest.raises(AuthorizationError):
        h.submit(service=service)
    assert h.backend.submits == 0


def test_cancel_reserved_is_local_terminal_and_replay_cannot_dispatch(h):
    with patch.object(h.repository, "start_dispatch", return_value=False):
        reserved = h.submit()
    cancelled = h.service.cancel(h.principal, reserved.id)
    assert cancelled.phase is JobPhase.TERMINAL
    assert h.submit().terminal_result["reason"] == "cancelled_before_dispatch"
    assert h.backend.submits == 0 and h.backend.cancels == 0


def test_async_cancel_ack_not_terminal_and_missing_is_uncertain(h):
    record = h.submit()
    cancelled = h.service.cancel(h.principal, record.id)
    assert cancelled.cancel_requested
    assert cancelled.phase is JobPhase.ACTIVE
    assert cancelled.terminal_result is None
    assert h.backend.cancels == 1
    del h.backend.jobs[record.resource_name]
    missing = h.service.get(h.principal, record.id)
    assert missing.phase is JobPhase.UNCERTAIN
    assert missing.uid == record.uid
    assert missing.confidence is ObservationConfidence.MISSING
    assert h.service.result(h.principal, record.id) is None
    assert h.backend.submits == 1


def test_late_submission_acceptance_honors_persisted_cancel(h):
    entered, release = threading.Event(), threading.Event()
    h.backend.submit_entered, h.backend.submit_release = entered, release
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(h.submit)
        try:
            assert entered.wait(5)
            with sqlite3.connect(h.path) as connection:
                job_id = connection.execute(
                    "SELECT id FROM hosted_jobs"
                ).fetchone()[0]
            pending = h.service_for(h.repo()).cancel(h.principal, job_id)
            assert pending.cancel_requested and pending.terminal_result is None
            assert h.backend.cancels == 0
        finally:
            release.set()
        accepted = future.result(5)
    assert accepted.cancel_requested and h.backend.cancels == 1
    assert accepted.phase is JobPhase.ACTIVE


def test_terminal_completion_racing_cancel_rpc_wins(h):
    record = h.submit()
    h.backend.cancel_hook = lambda: h.finish(record)
    result = h.service.cancel(h.principal, record.id)
    assert result.terminal_result["state"] == "succeeded"
    assert result.cancel_requested


def test_terminal_observation_racing_intent_is_preserved(h):
    record = h.submit()
    h.finish(record)
    second = h.repo()
    h.backend.status_hook = lambda status: second.request_cancel(
        h.org, record.id
    )
    result = h.service.get(h.principal, record.id)
    assert result.terminal_result["state"] == "succeeded"
    assert result.cancel_requested and h.backend.cancels == 0


def test_nonterminal_status_racing_intent_reobserves_then_cancels(h):
    record = h.submit()
    second = h.repo()
    h.backend.status_hook = lambda status: second.request_cancel(
        h.org, record.id
    )
    result = h.service.get(h.principal, record.id)
    assert result.cancel_requested and h.backend.cancels == 1


def test_terminal_snapshot_repeat_and_mutation_no_provider_access(h):
    record = h.submit()
    h.finish(record)
    first = h.service.result(h.principal, record.id)
    expected = copy.deepcopy(first)
    first["state"] = "changed locally"
    reads = h.backend.reads
    del h.backend.jobs[record.resource_name]
    reopened = h.service_for(h.repo())
    assert reopened.result(h.principal, record.id) == expected
    assert reopened.cancel(h.principal, record.id).terminal_result == expected
    assert h.backend.reads == reads and h.backend.submits == 1


def test_expired_lease_and_stale_row_cannot_write_or_redispatch(h):
    record = h.submit()
    first = h.repository.claim_observation(record, h.now, 10)
    assert first
    second_repo = h.repo()
    assert (
        second_repo.claim_observation(
            second_repo.get(h.org, record.id), h.now, 10
        )
        is None
    )
    h.now += 11
    second = second_repo.claim_observation(
        second_repo.get(h.org, record.id), h.now, 10
    )
    assert second and second.lease_token != first.lease_token
    assert not h.repository.complete_observation(
        first, now=h.now, state="failed", result={"state": "failed"}
    )
    assert not h.repository.release_observation(first)
    assert second_repo.complete_observation(
        second, now=h.now, uid=record.uid, state="running"
    )
    assert h.submit().id == record.id and h.backend.submits == 1


def test_inflight_expired_status_cannot_overwrite_new_terminal(h):
    record = h.submit()
    entered, release = threading.Event(), threading.Event()

    def pause(status):
        entered.set()
        assert release.wait(5)

    h.backend.status_hook = pause
    with ThreadPoolExecutor(max_workers=1) as executor:
        old = executor.submit(h.service.get, h.principal, record.id)
        try:
            assert entered.wait(5)
            h.now += 11
            h.finish(record)
            newer = h.service_for(h.repo()).get(h.principal, record.id)
            assert newer.phase is JobPhase.TERMINAL
        finally:
            release.set()
        assert old.result(5).terminal_result == newer.terminal_result
    assert h.backend.submits == 1


def test_stale_running_to_pending_does_not_regress(h):
    record = h.submit()
    h.backend.jobs[record.resource_name].state = JobState.PENDING
    assert h.service.get(h.principal, record.id).state == "running"


@pytest.mark.parametrize("tamper", ["uid", "labels", "resource"])
def test_actual_ownership_and_uid_required_before_cancel_and_logs(h, tamper):
    record = h.submit()
    actual = h.backend.jobs[record.resource_name]
    if tamper == "uid":
        actual.details["uid"] = secrets.token_hex(16)
    elif tamper == "labels":
        actual.details["labels"] = {OWNERSHIP_LABEL: "different-owner"}
    else:
        actual.job_id = "projects/another/locations/another/jobs/another"
    current = h.service.cancel(h.principal, record.id)
    assert current.phase is JobPhase.UNCERTAIN
    assert current.confidence is ObservationConfidence.OWNERSHIP_CONFLICT
    with pytest.raises(ControlPlaneError):
        h.service.logs(h.principal, record.id)
    assert h.backend.cancels == 0 and h.backend.log_reads == 0


def test_missing_uid_or_labels_cannot_be_adopted_after_lost_response(h):
    h.backend.lost_response = True
    original = h.backend.job_status

    def unverified(job_id):
        status = original(job_id)
        status.details = {}
        return status

    h.backend.job_status = unverified
    record = h.submit()
    assert record.uid is None
    assert record.confidence is ObservationConfidence.OWNERSHIP_CONFLICT
    assert record.terminal_result is None
    assert h.backend.cancels == 0


def test_binding_identity_changes_fail_closed_before_provider_access(h):
    record = h.submit()
    for changed in (
        replace(h.binding, account="other-account"),
        replace(h.binding, project="other-project"),
        replace(h.binding, region="us-east1"),
    ):
        configured = h.configured(changed)
        service = HostedJobService(h.repository, [configured])
        reads = h.backend.reads
        with pytest.raises(ConflictError):
            service.get(h.principal, record.id)
        assert h.backend.reads == reads
    assert not h.repository.complete_observation(
        replace(record, binding=replace(h.binding, account="other")), now=h.now
    )


def test_cancellation_intent_survives_unavailable_registry(h):
    record = h.submit()
    service = HostedJobService(h.repo(), [])
    with pytest.raises(ConflictError):
        service.cancel(h.principal, record.id)
    assert h.repository.get(h.org, record.id).cancel_requested
    h.service.get(h.principal, record.id)
    assert h.backend.cancels == 1


def test_sanitized_provider_errors_and_terminal_details_never_store_secrets(h):
    record = h.submit()
    sentinel = secrets.token_hex(32)

    def fail(status):
        raise RuntimeError(sentinel)

    h.backend.status_hook = fail
    failed = h.service.get(h.principal, record.id)
    assert failed.error_code == "provider_unavailable"
    h.finish(record, JobState.FAILED)
    status = h.backend.jobs[record.resource_name]
    status.reason = sentinel
    status.details["message"] = sentinel
    result = h.service.result(h.principal, record.id)
    assert result["reason"] is None
    with sqlite3.connect(h.path) as connection:
        assert sentinel not in "\n".join(connection.iterdump())

    def logs_fail(**kwargs):
        raise RuntimeError(sentinel)

    h.logging.list_log_entries = logs_fail
    with pytest.raises(ControlPlaneError) as error:
        h.service.logs(h.principal, record.id)
    assert sentinel not in str(error.value)


def test_adapter_scope_and_public_direct_api_contract(h):
    record = h.submit()
    adapter = GcpBatchHostedAdapter(h.binding, lambda: h.backend)
    assert adapter.observe(record).uid == record.uid
    assert adapter.logs(record, 0) == ""
    with pytest.raises(OwnershipConflict):
        adapter.cancel(replace(record, uid=None, cancel_requested=True))
    with pytest.raises(OwnershipConflict):
        adapter.observe(replace(record, binding=replace(h.binding, version=2)))
    h.backend.job_status = lambda name: (_ for _ in ()).throw(
        SandboxNotFoundError("gone")
    )
    with pytest.raises(ProviderMissing):
        adapter.observe(record)


_SECRET = "projects/example-project/secrets/api-key/versions/latest"


@pytest.mark.parametrize(
    "options",
    [
        {"instance_labels": {OWNERSHIP_LABEL: "override"}},
        {"instance_labels": {"Hosted-Job-Owner": "override"}},
        {"instance_labels": {"pool": 1}},
        {"secret_env_vars": {"API_KEY": "pasted-secret-value"}},
        {"secret_env_vars": {"TOKEN": _SECRET}},
        {
            "secret_env_vars": {
                "API_KEY": "projects/other-project/secrets/api-key/versions/1"
            }
        },
        {"runnable": "vm"},
        {"runnable": "script", "container_options": "--privileged"},
        {"boot_disk_type": ""},
        {"install_gpu_drivers": True},
        {"block_project_ssh_keys": "yes"},
        {"task_count": 0},
        {"task_count": True},
        {"task_count": "4"},
        {"task_count": 10_001},
        {"parallelism": 2},
        {"task_count": 4, "parallelism": 5},
        {"task_count": 4, "parallelism": 0},
    ],
)
def test_invalid_batch_options_fail_before_reservation(h, options):
    spec = JobSpec("true", env_vars={"TOKEN": "x"}, backend_options=options)
    with pytest.raises(ValueError) as error:
        h.submit(spec)
    # Fixed text only: a malformed reference may be a pasted secret value.
    assert "pasted-secret-value" not in str(error.value)
    assert error.value.__cause__ is None
    with sqlite3.connect(h.path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM hosted_jobs").fetchone()[0]
            == 0
        )
    assert h.backend.submits == 0


def test_batch_options_reach_native_submit_with_server_routing(h):
    options = {
        "runnable": "script",
        "secret_env_vars": {"API_KEY": _SECRET},
        "boot_disk_image": "projects/example-project/global/images/worker",
        "boot_disk_type": "pd-balanced",
        "machine_type": "g2-standard-8",
        "install_gpu_drivers": True,
        "block_project_ssh_keys": True,
        "instance_labels": {"pool": "gpu"},
    }
    spec = JobSpec("#!/bin/bash\necho hello\n", backend_options=dict(options))
    record = h.submit(spec)
    assert record.phase is JobPhase.ACTIVE
    actual = h.backend.runtime_spec
    assert actual.command == spec.command and actual.image is None
    assert actual.backend_options == {
        **options,
        "project": h.binding.project,
        "region": h.binding.region,
        "job_id": record.resource_name.rsplit("/", 1)[-1],
    }
    # The marker stays a job label (so also a VM label), never instance-only.
    assert actual.labels == {OWNERSHIP_LABEL: record.ownership_marker}
    # A changed secret reference is a different payload for the same key.
    changed = copy.deepcopy(spec)
    changed.backend_options["secret_env_vars"]["API_KEY"] = _SECRET[:-6] + "2"
    with pytest.raises(ConflictError):
        h.submit(changed)
    assert h.backend.submits == 1


@pytest.mark.parametrize(
    "spec",
    [
        JobSpec("true", backend_options={"project": "other"}),
        JobSpec("true", backend_options={"job_id": "override"}),
        JobSpec("true", labels={OWNERSHIP_LABEL: "override"}),
        JobSpec("true", labels={"HOSTED-JOB-OWNER": "override"}),
    ],
)
def test_reserved_routing_and_marker_cannot_be_overridden(h, spec):
    with pytest.raises(ValueError):
        h.submit(spec)
    with sqlite3.connect(h.path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM hosted_jobs").fetchone()[0]
            == 0
        )
    assert h.backend.submits == 0
    record = h.submit()
    assert record.phase is JobPhase.ACTIVE
    assert h.submit().id == record.id
    assert h.backend.submits == 1


def test_namespaced_schema_and_same_authority_database(h, tmp_path):
    with sqlite3.connect(h.path) as connection:
        assert connection.execute(
            "SELECT version FROM hosted_jobs_schema_migrations"
        ).fetchall() == [(1,), (2,)]
        versions = connection.execute(
            "SELECT * FROM schema_migrations"
        ).fetchall()
    h.repo()
    with sqlite3.connect(h.path) as connection:
        assert (
            connection.execute("SELECT * FROM schema_migrations").fetchall()
            == versions
        )
    with pytest.raises(ValueError, match="organization authority"):
        HostedJobRepository(
            lambda: sqlite3.connect(tmp_path / "unrelated.sqlite"),
            lambda value: "a" * 64,
        )


def test_repository_fences_uid_binding_version_and_terminal_snapshot(h):
    record = h.submit()
    claimed = h.repository.claim_observation(record, h.now, 10)
    assert claimed
    assert not h.repository.complete_observation(
        replace(claimed, uid="wrong"), now=h.now, state="failed"
    )
    assert not h.repository.release_observation(
        replace(claimed, binding=replace(h.binding, account="wrong"))
    )
    assert h.repository.complete_observation(
        claimed,
        now=h.now,
        uid=record.uid,
        state="succeeded",
        result={"state": "succeeded"},
    )
    terminal = h.repository.get(h.org, record.id)
    assert not h.repository.release_observation(terminal)
    assert not h.repository.start_dispatch(terminal, h.now, 60)
    assert terminal.terminal_result == {"state": "succeeded"}


def test_simultaneous_first_submissions_reserve_once(h):
    second_repo = h.repo()
    second_service = h.service_for(second_repo)
    barrier = threading.Barrier(2, timeout=5)
    first_reserve = h.repository.reserve
    second_reserve = second_repo.reserve

    def synchronized(reserve):
        def call(*args, **kwargs):
            barrier.wait()
            return reserve(*args, **kwargs)

        return call

    with (
        patch.object(h.repository, "reserve", synchronized(first_reserve)),
        patch.object(second_repo, "reserve", synchronized(second_reserve)),
    ):
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(h.submit)
            second = executor.submit(h.submit, service=second_service)
            first_result, second_result = first.result(5), second.result(5)
    assert first_result.id == second_result.id
    assert h.backend.submits == 1
    assert h.service.get(h.principal, first_result.id).phase is JobPhase.ACTIVE


def test_crash_after_acceptance_before_response_reopens_and_adopts(h):
    class ProcessLost(BaseException):
        pass

    direct_submit = h.backend.submit_job

    def accepted_then_crash(spec):
        direct_submit(spec)
        raise ProcessLost()

    h.backend.submit_job = accepted_then_crash
    with pytest.raises(ProcessLost):
        h.submit()
    with sqlite3.connect(h.path) as connection:
        job_id = connection.execute("SELECT id FROM hosted_jobs").fetchone()[0]
    assert h.repository.get(h.org, job_id).phase is JobPhase.SUBMITTING
    reopened = h.service_for(h.repo())
    adopted = reopened.get(h.principal, job_id)
    assert adopted.phase is JobPhase.ACTIVE and adopted.uid
    assert h.submit(service=reopened).id == job_id
    assert h.backend.submits == 1


def test_lost_response_and_failed_observation_recover_on_reopen(h):
    h.backend.lost_response = True

    def fail(status):
        raise TimeoutError("Observation unavailable")

    h.backend.status_hook = fail
    uncertain = h.submit()
    assert uncertain.phase is JobPhase.UNCERTAIN and uncertain.uid is None
    adopted = h.submit(service=h.service_for(h.repo()))
    assert adopted.id == uncertain.id and adopted.uid
    assert adopted.phase is JobPhase.ACTIVE
    assert h.backend.submits == 1


def test_binding_version_cannot_reuse_an_existing_submit_key(h):
    record = h.submit()
    changed = replace(h.binding, version=2)
    service = HostedJobService(h.repository, [h.configured(changed)])
    reads = h.backend.reads
    with pytest.raises(ConflictError):
        service.submit(
            h.principal,
            h.spec,
            idempotency_key="example-operation",
            binding_id=changed.id,
            binding_version=changed.version,
        )
    assert h.backend.reads == reads and h.backend.submits == 1
    assert h.repository.get(h.org, record.id).binding == h.binding


def test_full_binding_and_phase_are_database_compare_and_swap_fences(h):
    record = h.submit()
    claimed = h.repository.claim_observation(record, h.now, 10)
    assert claimed
    for identity in (
        replace(h.binding, id="another-binding"),
        replace(h.binding, version=2),
        replace(h.binding, account="another-account"),
        replace(h.binding, project="another-project"),
        replace(h.binding, region="us-east1"),
    ):
        assert not h.repository.complete_observation(
            replace(claimed, binding=identity),
            now=h.now,
            uid=record.uid,
            state="succeeded",
            result={"state": "succeeded"},
        )
    assert not h.repository.start_dispatch(
        replace(claimed, phase=JobPhase.RESERVED), h.now, 60
    )
    assert h.repository.release_observation(claimed)


def test_typed_missing_during_cancel_does_not_invent_terminal_result(h):
    record = h.submit()

    def missing(job_id):
        raise SandboxNotFoundError("Resource disappeared")

    h.backend.cancel_job = missing
    current = h.service.cancel(h.principal, record.id)
    assert current.cancel_requested
    assert current.phase is JobPhase.UNCERTAIN
    assert current.confidence is ObservationConfidence.MISSING
    assert current.terminal_result is None and current.uid == record.uid
    assert h.backend.submits == 1


def test_uid_replacement_between_observation_and_cancel_blocks_rpc(h):
    record = h.submit()

    def replace_after_read(status):
        h.backend.jobs[record.resource_name].details["uid"] = secrets.token_hex(
            16
        )

    h.backend.status_hook = replace_after_read
    current = h.service.cancel(h.principal, record.id)
    assert current.confidence is ObservationConfidence.OWNERSHIP_CONFLICT
    assert current.uid == record.uid and h.backend.cancels == 0


def test_typed_missing_during_logs_is_uncertain_not_fake_terminal(h):
    record = h.submit()

    def missing(**kwargs):
        # Log availability alone does not prove job absence. The authoritative
        # Batch postflight observes that the resource disappeared during logs.
        h.backend.jobs.pop(record.resource_name)
        return logging_response("")

    h.logging.list_log_entries = missing
    with pytest.raises(ControlPlaneError):
        h.service.logs(h.principal, record.id)
    current = h.repository.get(h.org, record.id)
    assert current.phase is JobPhase.UNCERTAIN
    assert current.confidence is ObservationConfidence.MISSING
    assert current.terminal_result is None


def test_server_factory_credentials_are_not_persisted_or_exposed(h):
    sentinel = secrets.token_hex(32)

    def credential_client_factory():
        # A real factory would resolve server-side credentials here.
        assert sentinel
        return h.backend

    configured = replace(
        h.configured(),
        adapter_factory=lambda: GcpBatchHostedAdapter(
            h.binding, credential_client_factory
        ),
    )
    service = HostedJobService(h.repository, [configured])
    record = h.submit(service=service)
    with sqlite3.connect(h.path) as connection:
        assert sentinel not in "\n".join(connection.iterdump())

    def failed_factory():
        raise RuntimeError(sentinel)

    failed = HostedJobService(
        h.repository, [replace(configured, adapter_factory=failed_factory)]
    )
    with pytest.raises(ControlPlaneError) as error:
        failed.get(h.principal, record.id)
    assert sentinel not in str(error.value)


def test_bound_factory_required_and_pure_validation_does_not_construct_clients(
    h,
):
    with pytest.raises(TypeError):
        GcpBatchHostedAdapter(h.binding)
    with pytest.raises(ValueError):
        GcpBatchHostedAdapter(h.binding, None)

    def forbidden_factory():
        pytest.fail("Pure validation must not construct a client")

    adapter = GcpBatchHostedAdapter(h.binding, forbidden_factory)
    adapter.validate(h.spec, h.binding)
    with pytest.raises(ValueError):
        adapter.validate(
            replace(h.spec, backend_options={"job_id": "bad"}), h.binding
        )
    with pytest.raises(OwnershipConflict):
        adapter.resource_name(h.binding, "trailing-")
    assert adapter.resource_name(h.binding, "a").endswith("/a")
    assert adapter.resource_name(h.binding, "a" * 63).endswith("/" + "a" * 63)


def test_changed_validation_after_authorization_never_redispatches(h):
    original = GcpBatchHostedAdapter.validate
    calls = 0

    def changed(adapter, spec, binding):
        nonlocal calls
        calls += 1
        original(adapter, spec, binding)
        if calls == 2:
            raise ValueError("Policy changed before dispatch")

    with patch.object(GcpBatchHostedAdapter, "validate", changed):
        record = h.submit()
    assert record.phase is JobPhase.UNCERTAIN
    assert record.error_code == "provider_missing"
    assert h.backend.submits == 0
    assert h.submit().id == record.id
    assert h.backend.submits == 0


def test_blocked_submit_concurrent_get_preserves_submitting(h):
    entered, release = threading.Event(), threading.Event()
    h.backend.submit_entered, h.backend.submit_release = entered, release
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(h.submit)
        try:
            assert entered.wait(5)
            with sqlite3.connect(h.path) as connection:
                job_id = connection.execute(
                    "SELECT id FROM hosted_jobs"
                ).fetchone()[0]
            seen = h.service_for(h.repo()).get(h.principal, job_id)
            assert seen.phase is JobPhase.SUBMITTING
            assert seen.confidence is ObservationConfidence.UNOBSERVED
            assert seen.error_code is None and seen.terminal_result is None
            assert seen.submit_deadline == h.now + 60

            def unavailable(status):
                raise TimeoutError("Observation transport unavailable")

            h.backend.status_hook = unavailable
            still_pending = h.service.get(h.principal, job_id)
            assert still_pending.phase is JobPhase.SUBMITTING
            assert still_pending.confidence is ObservationConfidence.UNOBSERVED
            assert still_pending.error_code is None
        finally:
            release.set()
        assert future.result(5).phase is JobPhase.ACTIVE
    assert h.backend.submits == 1


def test_crash_deadline_reconciliation_never_resubmits(h):
    with patch.object(h.repository, "start_dispatch", return_value=False):
        reserved = h.submit()
    assert h.repository.start_dispatch(reserved, h.now, 60)
    reopened = h.service_for(h.repo())
    h.now += 59
    pending = reopened.get(h.principal, reserved.id)
    assert pending.phase is JobPhase.SUBMITTING
    assert pending.confidence is ObservationConfidence.UNOBSERVED
    h.now += 1
    expired = reopened.get(h.principal, reserved.id)
    assert expired.phase is JobPhase.UNCERTAIN
    assert expired.confidence is ObservationConfidence.MISSING
    assert expired.terminal_result is None
    assert h.submit(service=reopened).id == reserved.id
    assert h.backend.submits == 0


@pytest.mark.parametrize(
    "tail,expected", [(None, 1000), (1000000, 1000), (1, 1), (0, 0)]
)
def test_logs_have_bounded_provider_tail_and_utf8_response(h, tail, expected):
    from bespokelabs.sandbox.control_plane.job_models import MAX_LOG_BYTES

    record = h.submit()
    seen = []

    def logs(*, request, retry, timeout):
        seen.append(request["page_size"])
        return logging_response("€" * MAX_LOG_BYTES)

    h.logging.list_log_entries = logs
    result = h.service.logs(h.principal, record.id, tail)
    assert len(result.encode("utf-8")) <= MAX_LOG_BYTES
    assert "�" not in result
    if expected == 0:
        assert result == "" and seen == []
    else:
        assert seen == [expected]
        assert result == "€" * (MAX_LOG_BYTES // 3)


@pytest.mark.parametrize("tail", [-1, True, False, 1.5, "10"])
def test_invalid_log_tails_rejected_before_provider_access(h, tail):
    record = h.submit()
    reads = h.backend.reads
    with pytest.raises(ValueError):
        h.service.logs(h.principal, record.id, tail)
    adapter = GcpBatchHostedAdapter(h.binding, lambda: h.backend)
    with pytest.raises(ValueError):
        adapter.logs(record, tail)
    assert h.backend.reads == reads and h.backend.log_reads == 0


def test_transient_log_failure_preserves_verified_lifecycle(h):
    record = h.submit()
    sentinel = secrets.token_hex(32)

    def fail(**kwargs):
        raise RuntimeError(sentinel)

    h.logging.list_log_entries = fail
    with pytest.raises(ControlPlaneError) as error:
        h.service.logs(h.principal, record.id)
    current = h.repository.get(h.org, record.id)
    assert current.phase is JobPhase.ACTIVE
    assert current.confidence is ObservationConfidence.VERIFIED
    assert current.state == record.state and current.uid == record.uid
    assert current.error_code is None
    assert sentinel not in str(error.value)
    with sqlite3.connect(h.path) as connection:
        assert sentinel not in "\n".join(connection.iterdump())


@pytest.mark.parametrize(
    "operation,scope", [("get", "read"), ("result", "result")]
)
def test_read_operations_resume_only_previously_authorized_cancel_intent(
    h, operation, scope
):
    record = h.submit()
    reader = replace(h.principal, scopes=frozenset({f"jobs:{scope}"}))
    getattr(h.service, operation)(reader, record.id)
    assert h.backend.cancels == 0
    lease = h.repository.claim_observation(
        h.repository.get(h.org, record.id), h.now, 10
    )
    canceller = replace(h.principal, scopes=frozenset({"jobs:cancel"}))
    assert h.service.cancel(canceller, record.id).cancel_requested
    assert h.backend.cancels == 0
    assert h.repository.release_observation(h.repository.get(h.org, lease.id))
    getattr(h.service, operation)(reader, record.id)
    assert h.backend.cancels == 1


@pytest.mark.parametrize("mode", ["missing", "unimplemented"])
def test_cancel_unavailable_never_uses_native_cancel_or_delete(h, mode):
    record = h.submit()

    class CancelClient:

        def cancel_job(self, **kwargs):
            raise NotImplementedError("Unsupported")

        def delete_job(self, **kwargs):
            pytest.fail("Hosted cancellation must never delete")

    adapter = GcpBatchHostedAdapter(
        h.binding,
        lambda: h.backend,
        cancel_client_factory=(lambda: CancelClient())
        if mode == "unimplemented"
        else None,
    )
    configured = replace(h.configured(), adapter_factory=lambda: adapter)
    service = HostedJobService(h.repository, [configured], clock=lambda: h.now)
    current = service.cancel(h.principal, record.id)
    assert current.cancel_requested and current.uid == record.uid
    assert current.phase is JobPhase.UNCERTAIN
    assert current.error_code == "cancel_unavailable"
    assert current.terminal_result is None
    assert h.backend.cancels == 0


def test_plain_repository_get_does_not_acquire_write_lock(h):
    record = h.submit()
    with sqlite3.connect(h.path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        assert h.repository.get(h.org, record.id).id == record.id


def test_older_durable_rows_reopen_without_losing_dispatch_fence(h):
    record = h.submit()
    with sqlite3.connect(h.path) as connection:
        connection.execute(
            "ALTER TABLE hosted_jobs DROP COLUMN submit_deadline"
        )
        connection.execute(
            "DELETE FROM hosted_jobs_schema_migrations WHERE version=2"
        )
        connection.execute(
            "UPDATE hosted_jobs SET phase='submitting', uid=NULL WHERE id=?",
            (record.id,),
        )
    reopened = h.service_for(h.repo())
    accepted = h.submit(service=reopened)
    assert accepted.id == record.id and accepted.uid == record.uid
    assert accepted.phase is JobPhase.ACTIVE
    assert h.backend.submits == 1
    with sqlite3.connect(h.path) as connection:
        assert connection.execute(
            "SELECT version FROM hosted_jobs_schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,)]


def test_update_rejects_a_change_key_that_is_not_a_column_name():
    import pytest

    from bespokelabs.sandbox.control_plane.job_store import HostedJobRepository

    repository = HostedJobRepository.__new__(HostedJobRepository)
    with pytest.raises(ValueError, match="not a hosted_jobs column name"):
        repository._update(None, {"phase=phase, organization_id": "x"})


def test_task_options_reach_native_submit_and_the_fingerprint(h):
    options = {"task_count": 4, "parallelism": 2}
    spec = JobSpec("printf hello", backend_options=dict(options))
    record = h.submit(spec)
    assert record.phase is JobPhase.ACTIVE
    assert h.backend.runtime_spec.backend_options == {
        **options,
        "project": h.binding.project,
        "region": h.binding.region,
        "job_id": record.resource_name.rsplit("/", 1)[-1],
    }
    # Replaying the same payload adopts the job; a changed count conflicts.
    assert h.submit(spec).id == record.id
    for changed in ({"task_count": 5, "parallelism": 2}, {"task_count": 4}):
        with pytest.raises(ConflictError):
            h.submit(JobSpec("printf hello", backend_options=changed))
    assert h.backend.submits == 1


_FIVE_KEYS = {"state", "exit_code", "reason", "started_at", "finished_at"}


def test_single_task_result_shape_is_unchanged(h):
    record = h.submit()
    h.finish(record, JobState.FAILED)
    result = h.service.result(h.principal, record.id)
    assert set(result) == _FIVE_KEYS


@pytest.mark.parametrize("reason", ["quota_exceeded", "capacity_unavailable"])
def test_capacity_reasons_reach_the_terminal_result(h, reason):
    record = h.submit()
    status = h.backend.jobs[record.resource_name]
    status.state = JobState.FAILED
    status.reason = reason
    status.details["message"] = "Quota 'CPUS' exceeded.  Limit: 24.0"
    result = h.service.result(h.principal, record.id)
    assert result == {
        "state": "failed",
        "exit_code": None,
        "reason": reason,
        "started_at": None,
        "finished_at": None,
    }
    with sqlite3.connect(h.path) as connection:
        assert "Limit: 24.0" not in "\n".join(connection.iterdump())


def test_multi_task_result_is_bounded_and_sanitized(h):
    sentinel = secrets.token_hex(16)
    record = h.submit(
        JobSpec("printf hello", backend_options={"task_count": 40})
    )
    h.finish(record, JobState.FAILED)
    status = h.backend.jobs[record.resource_name]
    status.reason = "exit_code"
    failures = [
        {
            "index": index,
            "exit_code": 1,
            "reason": "exit_code",
            "message": sentinel,
        }
        for index in range(1, 26)
    ]
    failures[1].update(exit_code=None, reason="capacity_unavailable")
    failures[2].update(exit_code=2**40, reason=sentinel)
    status.details["tasks"] = {
        "count": 40,
        "succeeded": 10,
        "failed": 25,
        "other": 5,
        "failures": failures,
        "truncated": False,
        "private": sentinel,
    }
    result = h.service.result(h.principal, record.id)
    assert set(result) == _FIVE_KEYS | {"tasks"}
    tasks = result["tasks"]
    assert set(tasks) == {
        "count",
        "succeeded",
        "failed",
        "other",
        "failures",
        "truncated",
    }
    assert (tasks["count"], tasks["succeeded"]) == (40, 10)
    assert (tasks["failed"], tasks["other"]) == (25, 5)
    # More failures than the bound: cut, and said so.
    assert len(tasks["failures"]) == 20 and tasks["truncated"] is True
    assert tasks["failures"][:3] == [
        {"index": 1, "exit_code": 1, "reason": "exit_code"},
        {"index": 2, "exit_code": None, "reason": "capacity_unavailable"},
        {"index": 3, "exit_code": None, "reason": None},
    ]
    with sqlite3.connect(h.path) as connection:
        assert sentinel not in "\n".join(connection.iterdump())


@pytest.mark.parametrize(
    "summary",
    [
        "raw text",
        {"count": "4", "succeeded": 0, "failed": 0, "other": 4},
        {"count": 4, "succeeded": True, "failed": 0, "other": 4},
        {"count": 4, "succeeded": 0, "failed": -1, "other": 4},
        {
            "count": 4,
            "succeeded": 0,
            "failed": 1,
            "other": 3,
            "failures": [{"index": "0"}],
        },
    ],
)
def test_malformed_task_summary_is_dropped_whole(h, summary):
    record = h.submit()
    h.finish(record, JobState.FAILED)
    h.backend.jobs[record.resource_name].details["tasks"] = summary
    assert set(h.service.result(h.principal, record.id)) == _FIVE_KEYS
