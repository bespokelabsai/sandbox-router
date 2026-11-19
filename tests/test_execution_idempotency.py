"""Offline hosted execution replay, isolation, and durable reservation tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from bespokelabs.sandbox.control_plane.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    provider_error_payload,
)
from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from bespokelabs.sandbox.exceptions import SandboxExecutionError
from bespokelabs.sandbox.types import AgentRunResult
from tests.test_control_plane import FakeSandbox

try:
    from fastapi.testclient import TestClient

    from bespokelabs.sandbox.control_plane.api import create_app
except ImportError:  # pragma: no cover - server extra is optional
    TestClient = create_app = None

CODE = {"code": "print('payload-secret')", "language": "python"}
COMMAND = {"command": "echo", "args": ["argument-secret", "two"]}


class ExecutionSandbox(FakeSandbox):

    def __init__(self, backend, **config):
        super().__init__(backend, **config)
        self.agent_calls = 0

    def run_agent(self, prompt, **options):
        self.agent_calls += 1
        return AgentRunResult(text="done")


@pytest.fixture
def hosted(tmp_path):
    path = tmp_path / "executions.db"
    store = SQLiteStore(path, key_pepper="test-pepper")
    runtime = ExecutionSandbox("local")
    service = ControlPlane(
        store,
        sandbox_factory=lambda *a, **kw: runtime,
        allowed_backends={"local"},
        resource_sampling_interval_secs=0,
        customer_markup=Decimal("1.5"),
    )
    org, key = service.bootstrap_organization("Example")
    principal = service.authenticate(key.secret, "sandboxes:execute")
    sandbox = service.create_sandbox(principal, "local", {})
    reopened = SQLiteStore(path, key_pepper="test-pepper")
    restarted = ControlPlane(
        reopened,
        sandbox_factory=Mock(
            side_effect=AssertionError("no provider creation")
        ),
        allowed_backends={"local"},
        resource_sampling_interval_secs=0,
    )
    state = SimpleNamespace(
        store=store,
        reopened=reopened,
        service=service,
        restarted=restarted,
        runtime=runtime,
        principal=principal,
        sandbox=sandbox,
        key=key,
        org=org,
    )
    yield state
    state.restarted.close()
    service.close()


def execute(hosted, payload, *, service=None, request_id="once"):
    return (service or hosted.service).execute(
        hosted.principal, hosted.sandbox.id, request_id=request_id, **payload
    )


def accounting(store):
    with store._connect() as connection:
        return {
            table: [
                tuple(row)
                for row in connection.execute(f"SELECT * FROM {table}")
            ]
            for table in ("executions", "usage_events", "ledger_entries")
        }


def fingerprint(payload):
    return hmac.new(
        b"test-pepper",
        json.dumps(
            {
                "operation": "execute",
                "code": None,
                "language": "python",
                "command": None,
                "args": None,
                **payload,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        hashlib.sha256,
    ).hexdigest()


def test_execution_fingerprint_is_keyed_and_canonical(tmp_path):
    payload = {"operation": "execute", **COMMAND}
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()
    digests = []
    for pepper in ("first-pepper", "second-pepper"):
        store = SQLiteStore(tmp_path / f"{pepper}.db", key_pepper=pepper)
        expected = hmac.new(
            pepper.encode(), canonical, hashlib.sha256
        ).hexdigest()
        assert store.fingerprint_execution_request(payload) == expected
        assert (
            store.fingerprint_execution_request(
                dict(reversed(list(payload.items())))
            )
            == expected
        )
        digests.append(expected)
    assert digests[0] != digests[1]


@pytest.mark.parametrize("payload", [CODE, COMMAND], ids=["code", "command"])
def test_replay_after_restart_and_termination_never_calls_provider_or_billing(
    hosted, payload
):
    first = execute(hosted, payload)
    assert first["request_id"] == "once"
    assert set(first) == {
        "request_id",
        "stdout",
        "stderr",
        "exit_code",
        "usage",
    }
    assert first["usage"]["provider_cost_usd"] == "0.25"
    assert first["usage"]["customer_cost_usd"] == "0.375"
    with hosted.store._connect() as connection:
        row = dict(connection.execute("SELECT * FROM executions").fetchone())
    saved = row["request_fingerprint"]
    assert saved == fingerprint(payload)
    assert re.fullmatch(r"[0-9a-f]{64}", saved)
    # Result/error storage is intentionally allowed to contain sensitive output.
    metadata = {
        key: value
        for key, value in row.items()
        if key not in {"response_json", "error"}
    }
    serialized = json.dumps(metadata)
    assert "payload-secret" not in serialized
    assert "argument-secret" not in serialized
    assert (
        not {"code", "command", "args", "payload", "request_json"} & row.keys()
    )

    # Re-open after completion, rather than reusing the original store object.
    hosted.restarted.close()
    hosted.reopened = SQLiteStore(hosted.store._path, key_pepper="test-pepper")
    hosted.restarted = ControlPlane(
        hosted.reopened,
        sandbox_factory=Mock(
            side_effect=AssertionError("no provider creation")
        ),
        allowed_backends={"local"},
        resource_sampling_interval_secs=0,
    )
    before = accounting(hosted.store)
    assert len(before["executions"]) == len(before["usage_events"]) == 1
    assert len(before["ledger_entries"]) == 2
    for service in (hosted.service, hosted.restarted):
        with patch.object(service, "_record_usage", side_effect=AssertionError):
            assert execute(hosted, payload, service=service) == first
    assert accounting(hosted.reopened) == before

    hosted.service.destroy_sandbox(hosted.principal, hosted.sandbox.id)
    assert hosted.runtime.destroyed
    before = accounting(hosted.store)
    with (
        patch.object(
            hosted.runtime, "execute_code", side_effect=AssertionError
        ),
        patch.object(
            hosted.runtime, "execute_command", side_effect=AssertionError
        ),
        patch.object(
            hosted.runtime, "estimate_compute_cost", side_effect=AssertionError
        ),
        patch.object(
            hosted.store, "record_runtime_usage", side_effect=AssertionError
        ),
        patch.object(
            hosted.reopened, "record_runtime_usage", side_effect=AssertionError
        ),
    ):
        for service in (hosted.service, hosted.restarted):
            assert execute(hosted, payload, service=service) == first
    assert accounting(hosted.store) == before
    assert hosted.runtime.calls == 1
    with pytest.raises(ConflictError, match="sandbox is destroyed"):
        execute(hosted, payload, request_id="new")


@pytest.mark.parametrize(
    ("original", "modified"),
    [
        (CODE, {**CODE, "code": "print(2)"}),
        (CODE, {**CODE, "language": "javascript"}),
        (COMMAND, {**COMMAND, "command": "printf"}),
        (COMMAND, {**COMMAND, "args": ["changed"]}),
        (COMMAND, {**COMMAND, "args": list(reversed(COMMAND["args"]))}),
        (CODE, COMMAND),
        (COMMAND, CODE),
    ],
)
def test_changed_payload_conflicts_even_without_a_runtime(
    hosted, original, modified
):
    execute(hosted, original)
    before = accounting(hosted.store)
    for service in (hosted.service, hosted.restarted):
        with pytest.raises(ConflictError, match="different request"):
            execute(hosted, modified, service=service)
    assert accounting(hosted.store) == before
    assert hosted.runtime.calls == 1


@pytest.mark.parametrize("agent_first", [False, True])
def test_agent_and_execute_keys_cannot_collide(hosted, agent_first):
    def agent():
        return hosted.service.run_agent(
            hosted.principal,
            hosted.sandbox.id,
            prompt="hello",
            request_id="once",
        )

    first, second = (
        (agent, lambda: execute(hosted, CODE))
        if agent_first
        else (lambda: execute(hosted, CODE), agent)
    )
    first()
    before = accounting(hosted.store)
    with pytest.raises(ConflictError, match="different request"):
        second()
    assert accounting(hosted.store) == before
    assert hosted.runtime.calls + hosted.runtime.agent_calls == 1


def test_scope_tenant_and_sandbox_ownership_are_checked_before_cache(hosted):
    execute(hosted, CODE)
    key = hosted.service.issue_api_key(
        hosted.principal, name="Inventory", scopes=["sandboxes:read"]
    )
    reader = hosted.store.authenticate(key.secret)
    _, other_key = hosted.service.bootstrap_organization("Other")
    other = hosted.store.authenticate(other_key.secret)
    with patch.object(
        hosted.store, "cached_execution", side_effect=AssertionError
    ):
        with pytest.raises(AuthorizationError):
            hosted.service.execute(
                reader, hosted.sandbox.id, code=CODE["code"], request_id="once"
            )
        with pytest.raises(NotFoundError):
            hosted.service.execute(
                other, hosted.sandbox.id, code=CODE["code"], request_id="once"
            )

    # The same request key is independent in another sandbox and tenant.
    second = hosted.service.create_sandbox(hosted.principal, "local", {})
    third = hosted.service.create_sandbox(other, "local", {})
    for principal, sandbox in ((hosted.principal, second), (other, third)):
        hosted.service.execute(
            principal, sandbox.id, request_id="once", **COMMAND
        )
    assert hosted.runtime.calls == 3
    rows = accounting(hosted.store)
    assert len(rows["executions"]) == len(rows["usage_events"]) == 3
    assert len(rows["ledger_entries"]) == 6
    assert (
        hosted.store.cached_execution(
            other.organization_id, hosted.sandbox.id, "once", fingerprint(CODE)
        )
        is None
    )
    assert (
        hosted.store.cached_execution(
            hosted.principal.organization_id,
            "unknown",
            "once",
            fingerprint(CODE),
        )
        is None
    )


@pytest.mark.parametrize("payload", [CODE, COMMAND], ids=["code", "command"])
def test_distinct_or_omitted_keys_run_and_charge_independently(hosted, payload):
    for request_id in ("one", "two", None, None):
        execute(hosted, payload, request_id=request_id)
    rows = accounting(hosted.store)
    assert hosted.runtime.calls == 4
    assert len(rows["executions"]) == len(rows["usage_events"]) == 4
    assert len(rows["ledger_entries"]) == 8


@pytest.mark.parametrize("payload", [CODE, COMMAND], ids=["code", "command"])
def test_failed_execution_is_never_rerun_or_charged_twice(hosted, payload):
    method = "execute_code" if "code" in payload else "execute_command"
    with patch.object(
        hosted.runtime, method, side_effect=SandboxExecutionError("failed")
    ) as call:
        with pytest.raises(SandboxExecutionError) as failure:
            execute(hosted, payload)
        expected = json.dumps(
            provider_error_payload(failure.value), sort_keys=True
        )
        with hosted.store._connect() as connection:
            assert (
                connection.execute("SELECT error FROM executions").fetchone()[0]
                == expected
            )
        before = accounting(hosted.store)
        for service in (hosted.service, hosted.restarted):
            with pytest.raises(ConflictError) as conflict:
                execute(hosted, payload, service=service)
            assert str(conflict.value) == expected
        call.assert_called_once()
    assert len(before["usage_events"]) == 1
    assert len(before["ledger_entries"]) == 2
    assert accounting(hosted.store) == before


@pytest.mark.parametrize("saved_error", [None, "saved interruption detail"])
def test_running_execution_preserves_prior_conflict_detail(hosted, saved_error):
    hosted.store.begin_execution(
        hosted.principal.organization_id,
        hosted.sandbox.id,
        "once",
        request_fingerprint=fingerprint(CODE),
    )
    with hosted.store._connect() as connection:
        connection.execute("UPDATE executions SET error = ?", (saved_error,))
    for service in (hosted.service, hosted.restarted):
        with pytest.raises(ConflictError) as conflict:
            execute(hosted, CODE, service=service)
        assert str(conflict.value) == (
            saved_error or "execution is already in progress"
        )
    assert hosted.runtime.calls == 0


@pytest.mark.skipif(TestClient is None, reason="server dependencies missing")
def test_http_failed_execution_preserves_stored_error_json_detail(hosted):
    path = f"/v1/sandboxes/{hosted.sandbox.id}/execute"
    headers = {
        "Authorization": f"Bearer {hosted.key.secret}",
        "Idempotency-Key": "once",
    }
    with patch.object(
        hosted.runtime,
        "execute_code",
        side_effect=SandboxExecutionError("failed"),
    ) as call:
        with pytest.raises(SandboxExecutionError):
            execute(hosted, CODE)
        with hosted.store._connect() as connection:
            expected = connection.execute(
                "SELECT error FROM executions"
            ).fetchone()[0]
        for service in (hosted.service, hosted.restarted):
            with (
                patch.object(service, "start_supervision"),
                TestClient(create_app(service)) as client,
            ):
                response = client.post(path, headers=headers, json=CODE)
                assert response.status_code == 409
                assert response.json() == {"detail": expected}
        call.assert_called_once()


@pytest.mark.parametrize("payload", [CODE, COMMAND], ids=["code", "command"])
def test_crash_after_provider_and_accounting_but_before_completion_is_ambiguous(
    hosted, payload
):
    with patch.object(
        hosted.store, "finish_execution", side_effect=KeyboardInterrupt
    ):
        with pytest.raises(KeyboardInterrupt):
            execute(hosted, payload)
    before = accounting(hosted.store)
    with hosted.store._connect() as connection:
        assert (
            connection.execute("SELECT status FROM executions").fetchone()[0]
            == "running"
        )
    assert len(before["usage_events"]) == 1
    for service in (hosted.service, hosted.restarted):
        with pytest.raises(
            ConflictError, match="^execution is already in progress$"
        ):
            execute(hosted, payload, service=service)
    assert hosted.runtime.calls == 1
    assert accounting(hosted.store) == before


@pytest.mark.parametrize("status", ["running", "failed", "completed"])
@pytest.mark.parametrize("payload", [CODE, COMMAND], ids=["code", "command"])
def test_legacy_null_fingerprints_fail_closed_without_claiming_payload_match(
    hosted, status, payload
):
    execution_id, _ = hosted.store.begin_execution(
        hosted.principal.organization_id, hosted.sandbox.id, "once"
    )
    if status == "failed":
        hosted.store.fail_execution(execution_id, "old failure")
    elif status == "completed":
        hosted.store.finish_execution(execution_id, {"stdout": "old result"})
    before = accounting(hosted.store)
    for service in (hosted.service, hosted.restarted):
        with pytest.raises(
            ConflictError, match="prior payload cannot be verified"
        ):
            execute(hosted, payload, service=service)
    # Even a legacy store caller must not replay an unverified result.
    with pytest.raises(ConflictError, match="prior payload cannot be verified"):
        hosted.store.begin_execution(
            hosted.principal.organization_id, hosted.sandbox.id, "once"
        )
    with pytest.raises(ConflictError, match="prior payload cannot be verified"):
        hosted.store.begin_execution(
            hosted.principal.organization_id,
            hosted.sandbox.id,
            "once",
            request_fingerprint=fingerprint(payload),
        )
    assert accounting(hosted.store) == before
    assert hosted.runtime.calls == 0


@pytest.mark.parametrize("payload", [CODE, COMMAND], ids=["code", "command"])
def test_begin_reservation_closes_cache_miss_race_without_duplicate_accounting(
    hosted, payload
):
    first = execute(hosted, payload)
    before = accounting(hosted.store)
    with patch.object(hosted.store, "cached_execution", return_value=None):
        assert execute(hosted, payload) == first
        with pytest.raises(ConflictError, match="different request"):
            execute(hosted, {"command": "different"})
    assert hosted.runtime.calls == 1
    assert accounting(hosted.store) == before


@pytest.mark.parametrize("payload", [CODE, COMMAND], ids=["code", "command"])
def test_two_control_planes_reserve_one_execution_with_deterministic_events(
    hosted, payload
):
    # Give both instances a fake runtime so the durable reservation, not runtime
    # attachment or an in-process lock, must prevent duplicate external work.
    hosted.restarted._runtimes[hosted.sandbox.id] = hosted.runtime
    both_missed = threading.Barrier(2)
    entered = threading.Event()
    release = threading.Event()
    method = "execute_code" if "code" in payload else "execute_command"
    original_execute = getattr(hosted.runtime, method)

    def blocked_execute(*args):
        result = original_execute(*args)
        entered.set()
        assert release.wait(timeout=10), "test did not release the provider"
        return result

    def synchronized_miss(store):
        original_lookup = store.cached_execution

        def lookup(*args):
            assert original_lookup(*args) is None
            both_missed.wait(timeout=10)
            return None

        return lookup

    with (
        patch.object(
            hosted.store,
            "cached_execution",
            side_effect=synchronized_miss(hosted.store),
        ),
        patch.object(
            hosted.reopened,
            "cached_execution",
            side_effect=synchronized_miss(hosted.reopened),
        ),
        patch.object(hosted.runtime, method, side_effect=blocked_execute),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        futures = [
            pool.submit(execute, hosted, payload, service=service)
            for service in (hosted.service, hosted.restarted)
        ]
        try:
            assert entered.wait(timeout=10)
            done, pending = wait(
                futures, timeout=10, return_when=FIRST_COMPLETED
            )
            assert len(done) == len(pending) == 1
            with pytest.raises(ConflictError, match="in progress"):
                next(iter(done)).result()
            assert hosted.runtime.calls == 1
        finally:
            release.set()
        first = next(iter(pending)).result(timeout=10)

    before = accounting(hosted.store)
    assert len(before["executions"]) == len(before["usage_events"]) == 1
    assert len(before["ledger_entries"]) == 2
    for service in (hosted.service, hosted.restarted):
        assert execute(hosted, payload, service=service) == first
    assert accounting(hosted.store) == before
    assert hosted.runtime.calls == 1


def test_cached_agent_execution_preserves_existing_fingerprint_and_errors(
    hosted,
):
    options = {
        "harness": "codex",
        "model": None,
        "command": None,
        "extra_args": None,
        "resume": False,
    }
    expected = hashlib.sha256(
        json.dumps(
            {"operation": "agent", "prompt": "hello", **options}, sort_keys=True
        ).encode()
    ).hexdigest()
    first = hosted.service.run_agent(
        hosted.principal,
        hosted.sandbox.id,
        prompt="hello",
        request_id="agent",
        **options,
    )
    assert (
        hosted.store.cached_agent_execution(
            hosted.principal.organization_id,
            hosted.sandbox.id,
            "agent",
            expected,
        )
        == first
    )
    execution_id, _ = hosted.store.begin_execution(
        hosted.principal.organization_id,
        hosted.sandbox.id,
        "unfinished",
        request_fingerprint=expected,
    )
    for status in ("running", "failed"):
        if status == "failed":
            hosted.store.fail_execution(execution_id, "failed")
        with pytest.raises(
            ConflictError, match="^agent execution has no saved result"
        ):
            hosted.store.cached_agent_execution(
                hosted.principal.organization_id,
                hosted.sandbox.id,
                "unfinished",
                expected,
            )
    hosted.store.begin_execution(
        hosted.principal.organization_id, hosted.sandbox.id, "legacy"
    )
    with pytest.raises(
        ConflictError,
        match="^idempotency key was used for a different request$",
    ):
        hosted.store.cached_agent_execution(
            hosted.principal.organization_id,
            hosted.sandbox.id,
            "legacy",
            expected,
        )


@pytest.mark.skipif(TestClient is None, reason="server dependencies missing")
@pytest.mark.parametrize("payload", [CODE, COMMAND], ids=["code", "command"])
def test_http_replay_requires_current_auth_scope_and_tenant_and_preserves_format(
    hosted, payload
):
    path = f"/v1/sandboxes/{hosted.sandbox.id}/execute"
    headers = {
        "Authorization": f"Bearer {hosted.key.secret}",
        "Idempotency-Key": "http",
    }
    with (
        patch.object(hosted.service, "start_supervision"),
        patch.object(hosted.restarted, "start_supervision"),
        TestClient(create_app(hosted.service)) as client,
    ):
        first = client.post(path, headers=headers, json=payload)
        assert first.status_code == 200
        assert first.json()["request_id"] == "http"
        before = accounting(hosted.store)
        assert (
            client.post(
                path, headers=headers, json={"command": "changed"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                path, headers={"Idempotency-Key": "http"}, json=payload
            ).status_code
            == 401
        )
        reader = hosted.service.issue_api_key(
            hosted.principal, name="Reader", scopes=["sandboxes:read"]
        )
        _, other = hosted.service.bootstrap_organization("Other")
        for secret, status in ((reader.secret, 403), (other.secret, 404)):
            assert (
                client.post(
                    path,
                    headers={**headers, "Authorization": f"Bearer {secret}"},
                    json=payload,
                ).status_code
                == status
            )
        assert accounting(hosted.store) == before

        hosted.service.destroy_sandbox(hosted.principal, hosted.sandbox.id)
        with TestClient(create_app(hosted.restarted)) as restarted:
            before = accounting(hosted.store)
            replay = restarted.post(path, headers=headers, json=payload)
            assert replay.status_code == 200
            assert replay.json() == first.json()
            hosted.store.revoke_api_key(
                hosted.principal.organization_id, hosted.key.id
            )
            assert (
                restarted.post(path, headers=headers, json=payload).status_code
                == 401
            )
            assert accounting(hosted.store) == before
    assert hosted.runtime.calls == 1
