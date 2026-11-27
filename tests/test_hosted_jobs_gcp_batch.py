"""Real SDK/protobuf contracts with synthetic responses, never cloud RPCs."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import create_autospec, patch

import pytest

from bespokelabs.sandbox.control_plane.errors import ControlPlaneError
from bespokelabs.sandbox.control_plane.job_adapters.gcp_batch import (
    OWNERSHIP_LABEL,
    GcpBatchHostedAdapter,
)
from bespokelabs.sandbox.control_plane.job_models import (
    MAX_LOG_BYTES,
    JobPhase,
    ObservationConfidence,
    OwnershipConflict,
)
from bespokelabs.sandbox.control_plane.job_service import HostedJobService
from bespokelabs.sandbox.types import JobState
from tests.test_hosted_jobs import Harness

batch = pytest.importorskip("google.cloud.batch_v1")


@pytest.fixture
def sdk_harness(tmp_path):
    from google.auth.credentials import AnonymousCredentials

    from bespokelabs.sandbox.backends.gcp_batch import GcpBatchClient

    h = Harness(tmp_path / "authority.sqlite")
    h.spec.image = "python:3.12"
    # Construct an actual SDK client with synthetic explicit credentials. No
    # method is invoked: the network boundary below is a class-spec autospec.
    actual = batch.BatchServiceClient(credentials=AnonymousCredentials())
    actual.transport.close()
    rpc = create_autospec(
        batch.BatchServiceClient, instance=True, spec_set=True
    )
    jobs = {}

    def create(*, request):
        assert isinstance(request, batch.CreateJobRequest)
        name = f"{request.parent}/jobs/{request.job_id}"
        job = batch.Job(
            name=name,
            uid="provider-incarnation",
            labels=dict(request.job.labels),
            status=batch.JobStatus(state=batch.JobStatus.State.QUEUED),
        )
        jobs[name] = job
        return job

    rpc.create_job.side_effect = create
    rpc.get_job.side_effect = lambda *, name: jobs[name]
    rpc.list_tasks.return_value = iter(())
    h.backend = GcpBatchClient()
    # Test-only injection at the existing direct client's transport boundary.
    h.backend._client = rpc
    adapter = GcpBatchHostedAdapter(
        h.binding,
        lambda: h.backend,
        cancel_client_factory=lambda: rpc,
        logging_client_factory=lambda: h.logging,
    )
    configured = replace(h.configured(), adapter_factory=lambda: adapter)
    h.service = HostedJobService(
        h.repository, [configured], clock=lambda: h.now
    )
    return h, rpc, jobs


def test_real_public_client_receives_reserved_name_and_reports_actual_ownership(
    sdk_harness,
):
    h, rpc, jobs = sdk_harness
    record = h.submit()
    assert record.phase is JobPhase.ACTIVE
    assert record.state == JobState.PENDING.value
    assert record.uid == "provider-incarnation"
    request = rpc.create_job.call_args.kwargs["request"]
    assert f"{request.parent}/jobs/{request.job_id}" == record.resource_name
    assert request.job.labels[OWNERSHIP_LABEL] == record.ownership_marker
    jobs[record.resource_name].status.state = batch.JobStatus.State.SUCCEEDED
    result = h.service.result(h.principal, record.id)
    assert result["state"] == "succeeded" and result["exit_code"] == 0
    h.submit()
    rpc.create_job.assert_called_once()


def test_real_batch_request_carries_the_new_options(sdk_harness):
    h, rpc, _ = sdk_harness
    secret = "projects/example-project/secrets/api-key/versions/latest"
    image = "projects/example-project/global/images/worker"
    h.spec = replace(
        h.spec,
        command="#!/bin/bash\necho hello\n",
        image=None,
        env_vars={"MODE": "fast"},
        disk_mb=100 * 1024,
        labels={"team": "evals"},
        backend_options={
            "runnable": "script",
            "secret_env_vars": {"API_KEY": secret},
            "boot_disk_image": image,
            "boot_disk_type": "pd-balanced",
            "machine_type": "g2-standard-8",
            "install_gpu_drivers": True,
            "instance_labels": {"pool": "gpu"},
        },
    )
    record = h.submit()
    assert record.phase is JobPhase.ACTIVE
    request = rpc.create_job.call_args.kwargs["request"]
    # Routing is still the binding's, whatever the options carry.
    assert request.parent == (
        f"projects/{h.binding.project}/locations/{h.binding.region}"
    )
    task = request.job.task_groups[0].task_spec
    assert task.runnables[0].script.text == h.spec.command
    assert "container" not in task.runnables[0]
    assert dict(task.environment.variables) == {"MODE": "fast"}
    assert dict(task.environment.secret_variables) == {"API_KEY": secret}
    instances = request.job.allocation_policy.instances[0]
    assert instances.install_gpu_drivers is True
    assert instances.policy.machine_type == "g2-standard-8"
    disk = instances.policy.boot_disk
    assert (disk.size_gb, disk.image, disk.type_) == (100, image, "pd-balanced")
    marker = {OWNERSHIP_LABEL: record.ownership_marker}
    assert dict(request.job.labels) == {"team": "evals", **marker}
    assert dict(request.job.allocation_policy.labels) == {
        "team": "evals",
        "pool": "gpu",
        **marker,
    }


def test_real_batch_request_blocks_project_ssh_keys(sdk_harness):
    policy = batch.AllocationPolicy.InstancePolicyOrTemplate
    if "block_project_ssh_keys" not in policy.meta.fields:
        pytest.skip("this google-cloud-batch has no block_project_ssh_keys")
    h, rpc, _ = sdk_harness
    h.spec = replace(h.spec, backend_options={"block_project_ssh_keys": True})
    assert h.submit().phase is JobPhase.ACTIVE
    request = rpc.create_job.call_args.kwargs["request"]
    instances = request.job.allocation_policy.instances[0]
    assert instances.block_project_ssh_keys is True


@pytest.mark.parametrize("change", ["uid", "owner"])
def test_real_status_details_not_request_labels_gate_cancel(
    sdk_harness, change
):
    h, rpc, jobs = sdk_harness
    record = h.submit()
    actual = jobs[record.resource_name]
    if change == "uid":
        actual.uid = "replacement-incarnation"
    else:
        actual.labels[OWNERSHIP_LABEL] = "unrelated-owner"
    current = h.service.cancel(h.principal, record.id)
    assert current.phase is JobPhase.UNCERTAIN
    assert current.confidence is ObservationConfidence.OWNERSHIP_CONFLICT
    if hasattr(rpc, "cancel_job"):
        rpc.cancel_job.assert_not_called()
    rpc.delete_job.assert_not_called()


def test_real_created_name_empty_normalizes_to_reserved_resource(sdk_harness):
    h, rpc, _ = sdk_harness
    create = rpc.create_job.side_effect

    def unnamed(*, request):
        response = create(request=request)
        return batch.Job(uid=response.uid, labels=dict(response.labels))

    rpc.create_job.side_effect = unnamed
    record = h.submit()
    assert record.phase is JobPhase.ACTIVE
    assert record.uid == "provider-incarnation"
    request = rpc.create_job.call_args.kwargs["request"]
    assert record.resource_name == f"{request.parent}/jobs/{request.job_id}"
    rpc.create_job.assert_called_once()


def test_unexpected_created_name_is_not_accepted_by_adapter(sdk_harness):
    h, rpc, _ = sdk_harness
    record = h.submit()
    rpc.create_job.return_value = batch.Job(
        name="projects/other/locations/us-central1/jobs/unrelated"
    )
    rpc.create_job.side_effect = None
    adapter = GcpBatchHostedAdapter(h.binding, lambda: h.backend)
    with pytest.raises(OwnershipConflict):
        adapter.submit(record, h.spec)


def test_sdk_cancel_contract_no_delete_fallback(sdk_harness):
    h, rpc, _ = sdk_harness
    record = h.submit()
    supported = hasattr(batch.BatchServiceClient, "cancel_job")
    if supported:
        from google.api_core.operation import Operation
        from google.api_core.operations_v1 import OperationsClient
        from google.longrunning import operations_pb2

        operations = create_autospec(OperationsClient, instance=True)
        rpc.cancel_job.return_value = Operation(
            operations_pb2.Operation(name="operations/synthetic-cancel"),
            operations.get_operation,
            operations.cancel_operation,
            batch.CancelJobResponse,
        )
    current = h.service.cancel(h.principal, record.id)
    assert current.cancel_requested and current.terminal_result is None
    if supported:
        rpc.cancel_job.assert_called_once_with(
            name=record.resource_name, retry=None, timeout=30
        )
        assert current.phase is JobPhase.ACTIVE
        assert current.confidence is ObservationConfidence.VERIFIED
    else:
        assert current.phase is JobPhase.UNCERTAIN
        assert current.error_code == "cancel_unavailable"
    rpc.delete_job.assert_not_called()


@pytest.mark.parametrize("error", ["unimplemented", "missing"])
def test_real_cancel_rpc_errors_fail_closed(sdk_harness, error):
    from google.api_core import exceptions

    h, rpc, _ = sdk_harness
    record = h.submit()
    if hasattr(rpc, "cancel_job"):
        rpc.cancel_job.side_effect = (
            exceptions.MethodNotImplemented("Synthetic unsupported RPC")
            if error == "unimplemented"
            else exceptions.NotFound("Synthetic absence")
        )
    current = h.service.cancel(h.principal, record.id)
    assert current.cancel_requested
    assert current.phase is JobPhase.UNCERTAIN
    assert current.terminal_result is None and current.uid == record.uid
    expected = "cancel_unavailable"
    if error == "missing" and hasattr(rpc, "cancel_job"):
        expected = "provider_missing"
    assert current.error_code == expected
    rpc.delete_job.assert_not_called()


@pytest.fixture
def logging_rpc(sdk_harness):
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.logging_v2.services.logging_service_v2 import (
        LoggingServiceV2Client,
    )
    from google.cloud.logging_v2.services.logging_service_v2.pagers import (
        ListLogEntriesPager,
    )
    from google.cloud.logging_v2.types import (
        ListLogEntriesRequest,
        ListLogEntriesResponse,
        LogEntry,
    )

    h, _, _ = sdk_harness
    client = LoggingServiceV2Client(credentials=AnonymousCredentials())
    calls = []
    state = {"page_lengths": [], "end": False, "entries": None}
    fetched = []

    def fetch(request, *, retry, timeout, metadata):
        assert retry is None and timeout == 30
        calls.append(ListLogEntriesRequest(request))
        index = len(calls) - 1
        assert request.page_token == (str(index) if index else "")
        lengths = state["page_lengths"]
        count = lengths[index] if index < len(lengths) else 50
        entries = state["entries"]
        if entries is None:
            entries = [
                LogEntry(text_payload=f"line-{len(fetched) + i}")
                for i in range(min(count, request.page_size))
            ]
        fetched.extend(entries)
        return ListLogEntriesResponse(
            entries=entries,
            next_page_token="" if state["end"] else str(index + 1),
        )

    # Test-only synthetic transport injection. Exercise the actual public GAPIC
    # method, request coercion and official pager, including automatic next-page
    # fetches if production accidentally exhausts it. No cloud RPCs or ADC.
    client.transport._wrapped_methods[client.transport.list_log_entries] = fetch
    original = client.list_log_entries

    def list_log_entries(**kwargs):
        pager = original(**kwargs)
        assert isinstance(pager, ListLogEntriesPager)
        return pager

    with patch.object(client, "list_log_entries", side_effect=list_log_entries):
        h.logging = client
        yield h, calls, fetched, state
    client.transport.close()


@pytest.mark.parametrize(
    "tail,expected_sizes",
    [
        (1, [1]),
        (None, list(range(1000, 0, -50))),
        (1000000, list(range(1000, 0, -50))),
        (103, [103, 53, 3]),
    ],
)
def test_real_logging_pager_stops_before_extra_fetch(
    logging_rpc, tail, expected_sizes
):
    h, calls, fetched, _ = logging_rpc
    record = h.submit()
    with patch.object(h.backend, "job_logs", side_effect=AssertionError):
        result = h.service.logs(h.principal, record.id, tail)
    assert [call.page_size for call in calls] == expected_sizes
    assert len(calls) == len(expected_sizes)
    expected_count = 1000 if tail is None else min(tail, 1000)
    assert len(fetched) == expected_count
    assert result == "".join(
        f"line-{index}\n" for index in reversed(range(expected_count))
    )
    for call in calls:
        assert list(call.resource_names) == [f"projects/{h.binding.project}"]
        assert call.filter == (
            f'logName="projects/{h.binding.project}/logs/batch_task_logs" '
            f'AND labels.job_uid="{record.uid}"'
        )
        assert call.order_by == "timestamp desc"


def test_real_logging_partial_and_empty_pages_shrink_remaining_budget(
    logging_rpc,
):
    h, calls, fetched, state = logging_rpc
    state["page_lengths"] = [0, 40, 0, 30]
    record = h.submit()
    result = h.service.logs(h.principal, record.id, 103)
    assert [call.page_size for call in calls] == [103, 103, 63, 63, 33]
    assert len(calls) == 5 and len(fetched) == 103
    assert len(result.splitlines()) == 103


def test_real_logging_empty_page_tokens_have_a_request_limit(logging_rpc):
    h, calls, fetched, state = logging_rpc
    state["page_lengths"] = [0] * 21
    record = h.submit()
    assert h.service.logs(h.principal, record.id, 1) == ""
    assert len(calls) == 20 and fetched == []
    assert all(call.page_size == 1 for call in calls)


def test_real_logging_exhaustion_stops_without_an_extra_rpc(logging_rpc):
    h, calls, fetched, state = logging_rpc
    state.update(page_lengths=[3], end=True)
    record = h.submit()
    assert h.service.logs(h.principal, record.id) == "line-2\nline-1\nline-0\n"
    assert len(calls) == 1 and len(fetched) == 3


def test_real_logging_client_receives_bounded_tail_and_caps_utf8(logging_rpc):
    from google.cloud.logging_v2.types import LogEntry

    h, calls, _, state = logging_rpc
    state.update(entries=[LogEntry(text_payload="€" * MAX_LOG_BYTES)], end=True)
    record = h.submit()
    result = h.service.logs(h.principal, record.id)
    assert len(result.encode("utf-8")) <= MAX_LOG_BYTES
    assert "�" not in result
    assert result == "€" * (MAX_LOG_BYTES // 3)
    assert len(calls) == 1 and calls[0].page_size == 1000


def test_real_logging_byte_cap_is_a_chronological_prefix(logging_rpc):
    from google.cloud.logging_v2.types import LogEntry

    h, calls, _, state = logging_rpc
    state.update(
        entries=[
            LogEntry(text_payload="€" * MAX_LOG_BYTES),
            LogEntry(text_payload="oldest"),
        ],
        end=True,
    )
    record = h.submit()
    expected = ("oldest\n" + "€" * MAX_LOG_BYTES).encode("utf-8")
    assert h.service.logs(h.principal, record.id) == expected[
        :MAX_LOG_BYTES
    ].decode("utf-8", errors="ignore")
    assert len(calls) == 1


def test_real_logging_not_found_does_not_claim_batch_job_missing(logging_rpc):
    from google.api_core import exceptions

    h, calls, _, _ = logging_rpc
    record = h.submit()
    h.logging.list_log_entries.side_effect = exceptions.NotFound(
        "Log unavailable"
    )
    with pytest.raises(ControlPlaneError, match="logs are unavailable"):
        h.service.logs(h.principal, record.id)
    current = h.repository.get(h.org, record.id)
    assert calls == []
    assert current.phase is JobPhase.ACTIVE
    assert current.confidence is ObservationConfidence.VERIFIED
    assert current.uid == record.uid and current.state == record.state
    assert current.error_code is None


def test_real_logging_oversized_entry_count_fails_closed(logging_rpc):
    from google.cloud.logging_v2.types import LogEntry

    h, calls, fetched, state = logging_rpc
    state["entries"] = [LogEntry(text_payload="extra")] * 2
    record = h.submit()
    with pytest.raises(ControlPlaneError, match="logs are unavailable"):
        h.service.logs(h.principal, record.id, 1)
    assert len(calls) == 1 and calls[0].page_size == 1
    # A malformed provider response is already on the wire; we reject it rather
    # than claiming page_size prevents oversized responses at the transport.
    assert len(fetched) == 2


def test_real_logging_payload_formats_and_chronological_prefix(logging_rpc):
    from google.cloud.logging_v2.types import LogEntry

    h, calls, _, state = logging_rpc
    state.update(
        entries=[
            LogEntry(text_payload="newest\n"),
            LogEntry(json_payload={"message": "middle"}),
            LogEntry(json_payload={"count": 1}),
            LogEntry(text_payload=""),
        ],
        end=True,
    )
    record = h.submit()
    assert h.service.logs(h.principal, record.id) == (
        "\n{'count': 1.0}\nmiddle\nnewest\n"
    )
    assert len(calls) == 1


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("change", ["uid", "owner", "missing"])
def test_real_logging_identity_checks_fail_closed(
    logging_rpc, sdk_harness, when, change
):
    from google.api_core import exceptions

    h, calls, _, state = logging_rpc
    _, rpc, jobs = sdk_harness
    record = h.submit()

    def change_job():
        if change == "uid":
            jobs[record.resource_name].uid = "replacement-incarnation"
        elif change == "owner":
            jobs[record.resource_name].labels[OWNERSHIP_LABEL] = "other-owner"
        else:
            rpc.get_job.side_effect = exceptions.NotFound("Synthetic absence")

    if when == "before":
        change_job()
    else:
        state["end"] = True
        original = h.logging.list_log_entries.side_effect

        def replace_after_fetch(**kwargs):
            pager = original(**kwargs)
            change_job()
            return pager

        h.logging.list_log_entries.side_effect = replace_after_fetch
    with pytest.raises(ControlPlaneError):
        h.service.logs(h.principal, record.id, 1)
    assert len(calls) == (0 if when == "before" else 1)
    current = h.repository.get(h.org, record.id)
    assert current.confidence is (
        ObservationConfidence.MISSING
        if change == "missing"
        else ObservationConfidence.OWNERSHIP_CONFLICT
    )
    assert current.terminal_result is None


def test_logging_factory_missing_fails_closed_without_native_fallback(
    sdk_harness,
):
    h, _, _ = sdk_harness
    record = h.submit()
    adapter = GcpBatchHostedAdapter(h.binding, lambda: h.backend)
    with patch.object(
        h.backend, "job_logs", side_effect=AssertionError
    ) as native:
        assert adapter.logs(record, 0) == ""
        with pytest.raises(
            RuntimeError, match="explicit bound logging factory"
        ):
            adapter.logs(record, 1)
        native.assert_not_called()


def test_real_batch_request_carries_task_count_and_parallelism(sdk_harness):
    h, rpc, _ = sdk_harness
    h.spec = replace(
        h.spec, backend_options={"task_count": 6, "parallelism": 2}
    )
    assert h.submit().phase is JobPhase.ACTIVE
    group = rpc.create_job.call_args.kwargs["request"].job.task_groups[0]
    assert (group.task_count, group.parallelism) == (6, 2)


def _real_task(name, index, state, exit_code=None, text=""):
    event = batch.StatusEvent(
        task_state=batch.TaskStatus.State[state], description=text
    )
    if exit_code is not None:
        event.task_execution = batch.TaskExecution(exit_code=exit_code)
    return batch.Task(
        name=f"{name}/taskGroups/group0/tasks/{index}",
        status=batch.TaskStatus(
            state=batch.TaskStatus.State[state], status_events=[event]
        ),
    )


def test_real_multi_task_status_becomes_a_sanitized_result(sdk_harness):
    h, rpc, jobs = sdk_harness
    h.spec = replace(h.spec, backend_options={"task_count": 4})
    record = h.submit()
    job = jobs[record.resource_name]
    job.task_groups = [batch.TaskGroup(task_count=4)]
    job.status.state = batch.JobStatus.State.FAILED
    stockout = (
        "Batch Error: code - CODE_GCE_ZONE_RESOURCE_POOL_EXHAUSTED, "
        "description - The zone does not have enough resources available"
    )
    rpc.list_tasks.return_value = iter(
        [
            _real_task(job.name, 0, "SUCCEEDED"),
            _real_task(job.name, 1, "FAILED", 9, "exit text"),
            _real_task(job.name, 2, "FAILED", None, stockout),
            _real_task(job.name, 3, "UNEXECUTED"),
        ]
    )
    result = h.service.result(h.principal, record.id)
    assert result == {
        "state": "failed",
        "exit_code": 9,
        "reason": "exit_code",
        "started_at": None,
        "finished_at": None,
        "tasks": {
            "count": 4,
            "succeeded": 1,
            "failed": 2,
            "other": 1,
            "failures": [
                {"index": 1, "exit_code": 9, "reason": "exit_code"},
                {
                    "index": 2,
                    "exit_code": None,
                    "reason": "capacity_unavailable",
                },
            ],
            "truncated": False,
        },
    }
    parent = rpc.list_tasks.call_args.kwargs["request"].parent
    assert parent == f"{record.resource_name}/taskGroups/group0"


@pytest.mark.parametrize(
    "text,reason",
    [
        (
            "Job failed due to: Batch Error: code - CODE_GCE_QUOTA_EXCEEDED, "
            "description - Quota 'CPUS' exceeded.  Limit: 24.0 in region "
            "us-central1.",
            "quota_exceeded",
        ),
        (
            "Job failed due to: Batch Error: code - "
            "CODE_GCE_ZONE_RESOURCE_POOL_EXHAUSTED, description - The zone "
            "does not have enough resources available to fulfill the request.",
            "capacity_unavailable",
        ),
        ("Job failed: the image could not be pulled", "batch_failed"),
    ],
)
def test_real_capacity_failures_have_distinct_hosted_reasons(
    sdk_harness, text, reason
):
    h, _, jobs = sdk_harness
    record = h.submit()
    job = jobs[record.resource_name]
    job.status.state = batch.JobStatus.State.FAILED
    job.status.status_events = [batch.StatusEvent(description=text)]
    result = h.service.result(h.principal, record.id)
    assert result == {
        "state": "failed",
        "exit_code": None,
        "reason": reason,
        "started_at": None,
        "finished_at": None,
    }
    # The provider's message is neither stored nor returned.
    assert "Limit: 24.0" not in repr(h.service.get(h.principal, record.id))
