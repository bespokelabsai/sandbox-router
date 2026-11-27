"""Real loopback HTTP + SQLite tests; only provider RPCs are faked."""

from __future__ import annotations

import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from bespokelabs.sandbox import (
    JobSpec,
    JobState,
    RemoteJobClient,
    RemoteJobError,
)


@pytest.fixture
def http_jobs(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    uvicorn = pytest.importorskip("uvicorn")
    from tests.test_hosted_job_api import HTTPHarness

    harness = HTTPHarness(tmp_path / "authority.sqlite")
    ready = threading.Event()

    class Server(uvicorn.Server):

        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    server = Server(
        uvicorn.Config(
            harness.client.app,
            host="127.0.0.1",
            port=0,
            log_level="critical",
            access_log=False,
            ws="none",
        )
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        worker = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        worker.start()
        try:
            assert ready.wait(5), "Local HTTP server did not start"
            client = RemoteJobClient(
                origin, harness.key.secret, trust_env=False
            )
            yield harness, client, origin
        finally:
            server.should_exit = True
            worker.join(5)
            assert not worker.is_alive(), "Local HTTP server did not stop"
            harness.client.close()


def submit(client, key="synthetic-operation"):
    return client.submit(
        JobSpec(
            ["printf", "hello"], env_vars={"TOKEN": "synthetic-env-secret"}
        ),
        binding_id="batch",
        idempotency_key=key,
    )


def test_lifecycle_idempotency_audit_and_cached_terminal(http_jobs):
    h, client, origin = http_jobs
    job = submit(client)
    assert job.phase == "active" and job.state is JobState.RUNNING
    assert client.result(job.id) is None
    assert client.logs(job.id) == "runtime output\n"
    assert client.logs(job.id, tail=0) == ""
    # Distributed callers reconnect with the same explicit key, not a local map.
    another = RemoteJobClient(origin, h.key.secret, trust_env=False)
    assert submit(another).id == job.id and h.backend.submits == 1
    cancelled = another.cancel(job.id)
    assert cancelled.cancel_requested and cancelled.phase != "terminal"
    assert cancelled.state is JobState.RUNNING and h.backend.cancels == 1
    assert client.result(job.id) is None
    provider = h.backend.jobs[h.row()["resource_name"]]
    provider.state = JobState.SUCCEEDED  # Completion may win a cancel race.
    provider.exit_code = 0
    result = client.result(job.id)
    assert result.state == "succeeded" and result.exit_code == 0
    observed = client.status(job.id)
    assert observed.phase == "terminal" and observed.result == result
    reads = h.backend.reads
    h.backend.jobs.clear()
    assert client.result(job.id) == result and h.backend.reads == reads
    audit, _ = h.store.list_audit(h.org, offset=0, limit=100)
    assert {r["action"] for r in audit} == {
        "job.submit",
        "job.read",
        "job.result",
        "job.logs",
        "job.cancel",
    }
    serialized = json.dumps(audit) + json.dumps(h.row())
    assert "synthetic-env-secret" not in serialized
    assert "synthetic-operation" not in serialized
    assert h.key.secret not in serialized


def test_concurrent_callers_same_key_dispatch_once(http_jobs):
    h, _, origin = http_jobs

    def call(_):
        return submit(RemoteJobClient(origin, h.key.secret, trust_env=False)).id

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(call, range(4)))
    assert len(set(ids)) == 1 and h.backend.submits == 1


def test_transport_timeout_does_not_resubmit_or_cancel(http_jobs):
    h, client, origin = http_jobs
    h.backend.submit_entered = threading.Event()
    h.backend.submit_release = threading.Event()
    completed = threading.Event()
    original = h.backend.submit_job

    def delayed(spec):
        try:
            return original(spec)
        finally:
            completed.set()

    h.backend.submit_job = delayed
    impatient = RemoteJobClient(
        origin, h.key.secret, timeout_secs=0.1, trust_env=False
    )
    try:
        with pytest.raises(RemoteJobError) as caught:
            submit(impatient)
        assert (
            caught.value.outcome == "unknown"
            and caught.value.code == "connection"
        )
        # The client gives up after 0.1s; on a slow runner the server may not
        # have reached the backend yet, so wait for it rather than racing it.
        assert h.backend.submit_entered.wait(5)
        assert h.backend.submits == 1 and h.backend.cancels == 0
    finally:
        h.backend.submit_release.set()
    assert completed.wait(5)
    replay = submit(client)
    assert replay.id == h.row()["id"] and h.backend.submits == 1
    assert h.backend.cancels == 0 and not replay.cancel_requested


def test_provider_submit_failure_remains_uncertain(http_jobs):
    h, client, _ = http_jobs
    h.backend.submit_error = RuntimeError("synthetic-provider-secret")
    first = submit(client)
    assert first.phase == "uncertain" and first.result is None
    assert first.confidence == "missing"
    second = submit(client)
    assert second.id == first.id and second.phase == "uncertain"
    assert second.state is None and second.result is None
    assert h.backend.submits == 1 and h.backend.cancels == 0


def test_http_auth_scope_tenant_conflict_and_admission(http_jobs):
    h, client, origin = http_jobs
    invalid = RemoteJobClient(origin, "invalid-synthetic-key", trust_env=False)
    with pytest.raises(RemoteJobError) as caught:
        submit(invalid)
    assert caught.value.status_code == 401
    scoped = h.store.issue_api_key(h.org, name="Reader", scopes=("jobs:read",))
    reader = RemoteJobClient(origin, scoped.secret, trust_env=False)
    with pytest.raises(RemoteJobError) as caught:
        submit(reader)
    assert caught.value.status_code == 403
    job = submit(client)
    assert reader.status(job.id).id == job.id
    other = RemoteJobClient(origin, h.other_key.secret, trust_env=False)
    with pytest.raises(RemoteJobError) as caught:
        other.status(job.id)
    assert caught.value.status_code == 404
    with pytest.raises(RemoteJobError) as caught:
        client.submit(
            JobSpec("different"),
            binding_id="batch",
            idempotency_key="synthetic-operation",
        )
    assert caught.value.status_code == 409
    h.allowed = False
    with pytest.raises(RemoteJobError) as caught:
        submit(client, key="denied-operation")
    assert caught.value.status_code == 403
    assert h.backend.submits == 1
    audit, _ = h.store.list_audit(h.org, offset=0, limit=100)
    assert any(row["outcome"] == "denied" for row in audit)


def test_server_rejects_reserved_options_without_provider_access(http_jobs):
    h, client, _ = http_jobs
    with pytest.raises(RemoteJobError) as caught:
        client.submit(
            JobSpec("true", backend_options={"project": "synthetic-invalid"}),
            binding_id="batch",
            idempotency_key="reusable-key",
        )
    assert caught.value.status_code == 422
    assert h.backend.submits == 0
    assert submit(client, key="reusable-key").phase == "active"


def test_multi_task_result_and_capacity_reason_over_http(http_jobs):
    h, client, _ = http_jobs
    job = client.submit(
        JobSpec(
            ["printf", "hello"],
            backend_options={"task_count": 3, "parallelism": 2},
        ),
        binding_id="batch",
        idempotency_key="multi-task-operation",
    )
    assert h.backend.runtime_spec.backend_options["task_count"] == 3
    provider = h.backend.jobs[h.row()["resource_name"]]
    provider.state = JobState.FAILED
    provider.reason = "capacity_unavailable"
    provider.details["message"] = "zone-secret has no capacity"
    provider.details["tasks"] = {
        "count": 3,
        "succeeded": 1,
        "failed": 2,
        "other": 0,
        "failures": [
            {"index": 0, "exit_code": None, "reason": "capacity_unavailable"},
            {"index": 2, "exit_code": 50001, "reason": "vm_preempted"},
        ],
        "truncated": False,
    }
    result = client.result(job.id)
    assert result.state == "failed" and result.reason == "capacity_unavailable"
    assert (result.tasks.count, result.tasks.failed) == (3, 2)
    assert [(f.index, f.reason) for f in result.tasks.failures] == [
        (0, "capacity_unavailable"),
        (2, "vm_preempted"),
    ]
    assert "zone-secret" not in json.dumps(h.row())
