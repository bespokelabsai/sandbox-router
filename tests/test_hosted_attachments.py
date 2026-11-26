"""Offline hosted attachment integration with no provider SDK or resources."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest

from bespokelabs.sandbox.control_plane import (
    attachments as attachment_module,
)
from bespokelabs.sandbox.control_plane.attachments import (
    AttachmentConflict,
    AttachmentCoordinator,
    AttachmentDescriptor,
    AttachmentError,
    BindingRegistry,
    ProviderBinding,
    SDKAttachmentAdapter,
    _BoundResumeBackend,
    release_owned_transport,
)
from bespokelabs.sandbox.control_plane.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from bespokelabs.sandbox.control_plane.reconciliation import ProviderObservation
from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from bespokelabs.sandbox.exceptions import (
    ErrorOutcome,
    FeatureNotSupportedError,
    SandboxCreationError,
    SandboxError,
)
from bespokelabs.sandbox.types import (
    AgentRunResult,
    NetworkPolicy,
    SandboxConfig,
    SandboxResult,
)


@pytest.fixture(params=["0.60.15", "1.6.4"])
def modal_client_contract(monkeypatch, request):
    default_url = (
        "https://api.modal.com"
        if request.param == "0.60.15"
        else "https://api.modal.com,https://api.modal2.com"
    )
    clients = []

    class Client:

        def __init__(self):
            self.exits = []
            self.closed = False
            self.cleanup_failure = False

        def __enter__(self):
            pytest.fail("An already-open factory client must not be reopened")

        def __exit__(self, *exc_info):
            self.exits.append(exc_info)
            self.closed = True
            if self.cleanup_failure:
                raise RuntimeError("cleanup-secret-placeholder")

    def create_client(**kwargs):
        assert kwargs == {
            "token_id": "token-placeholder",
            "token_secret": "secret-placeholder",
        }
        client = Client()
        clients.append(client)
        return client

    factory = mock.Mock(side_effect=create_client)
    terminate = mock.Mock(
        side_effect=AssertionError("Unverified handles must not be terminated")
    )
    remote = SimpleNamespace(object_id="resource-a", terminate=terminate)
    lookup = mock.Mock(return_value=remote)
    sdk = SimpleNamespace(
        Client=SimpleNamespace(from_credentials=factory),
        Sandbox=SimpleNamespace(from_id=lookup, terminate=terminate),
    )
    config = SimpleNamespace(
        config=SimpleNamespace(get=lambda key: default_url),
        DEFAULT_SERVER_URL=default_url,
    )
    monkeypatch.setitem(sys.modules, "modal", sdk)
    monkeypatch.setitem(sys.modules, "modal.config", config)
    monkeypatch.setattr(
        "bespokelabs.sandbox.control_plane.attachments.sdk_version",
        lambda name: request.param,
    )
    binding = ProviderBinding(
        id="binding-a",
        version=1,
        backend="modal",
        account_scope="account-a",
        organization_ids=["org-a"],
        credentials={
            "token_id": "token-placeholder",
            "token_secret": "secret-placeholder",
        },
        settings={"server_url": default_url},
    )
    return SimpleNamespace(
        binding=binding,
        descriptor=AttachmentDescriptor("modal", "binding-a", 1, "resource-a"),
        clients=clients,
        factory=factory,
        lookup=lookup,
        terminate=terminate,
        remote=remote,
    )


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_modal_failed_reconnect_closes_only_owned_client(
    modal_client_contract, cleanup_failure
):
    contract = modal_client_contract
    primary = RuntimeError("lookup-secret-placeholder")

    def reject(resource_id, *, client):
        assert resource_id == "resource-a"
        client.cleanup_failure = cleanup_failure
        raise primary

    contract.lookup.side_effect = reject
    for _ in range(3):
        with pytest.raises(AttachmentError) as error:
            SDKAttachmentAdapter().reconnect(
                contract.descriptor, contract.binding
            )
        assert str(error.value) == (
            "Provider SDK unavailable or runtime reconnect failed."
        )
        assert error.value.__context__ is None
        assert error.value.__cause__ is None
    assert len(contract.clients) == 3
    for client in contract.clients:
        assert client.closed
        assert len(client.exits) == 1
        exc_type, exc, traceback = client.exits[0]
        assert exc_type is RuntimeError
        assert exc is primary
        assert traceback is not None
    contract.terminate.assert_not_called()


def test_modal_client_cleanup_preserves_lookup_error(modal_client_contract):
    contract = modal_client_contract
    primary = RuntimeError("lookup-secret-placeholder")

    def reject(resource_id, *, client):
        client.cleanup_failure = True
        raise primary

    contract.lookup.side_effect = reject
    with pytest.raises(RuntimeError) as error:
        _BoundResumeBackend(contract.binding).resume(
            {"sandbox_id": "resource-a"}
        )
    assert error.value is primary
    assert "cleanup-secret-placeholder" not in str(error.value)
    assert len(contract.clients[0].exits) == 1
    contract.terminate.assert_not_called()


def test_modal_factory_failure_has_no_client_to_close(modal_client_contract):
    contract = modal_client_contract
    contract.factory.side_effect = RuntimeError("factory-secret-placeholder")
    with pytest.raises(AttachmentError) as error:
        SDKAttachmentAdapter().reconnect(contract.descriptor, contract.binding)
    assert "placeholder" not in str(error.value)
    assert error.value.__context__ is None
    assert contract.clients == []
    contract.lookup.assert_not_called()
    contract.terminate.assert_not_called()


def test_modal_success_keeps_client_and_existing_workload_close(
    modal_client_contract,
):
    contract = modal_client_contract

    def command(*args):
        assert not contract.clients[0].closed
        return SimpleNamespace(
            stdout=SimpleNamespace(read=lambda: "still usable"),
            stderr=SimpleNamespace(read=lambda: ""),
            wait=lambda: None,
            returncode=0,
        )

    contract.remote.exec = command
    attached = SDKAttachmentAdapter().reconnect(
        contract.descriptor, contract.binding
    )
    assert attached.provider_resource_id == "resource-a"
    assert attached.execute_command("echo").stdout == "still usable"
    client = contract.clients[0]
    assert client.exits == []
    contract.lookup.assert_called_once_with("resource-a", client=client)
    contract.terminate.assert_not_called()
    # A confirmed explicit destroy now consumes the hosted transport ownership.
    contract.terminate.side_effect = None
    attached.destroy()
    attached.destroy()
    contract.terminate.assert_called_once_with()
    assert client.closed
    assert client.exits == [(None, None, None)]


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_modal_owned_termination_failure_retains_handle_for_retry(
    modal_client_contract, cleanup_failure
):
    contract = modal_client_contract
    attached = SDKAttachmentAdapter().reconnect(
        contract.descriptor, contract.binding
    )
    client = contract.clients[0]
    client.cleanup_failure = cleanup_failure
    contract.terminate.side_effect = RuntimeError(
        "terminate-secret-placeholder"
    )
    with pytest.raises(SandboxError) as error:
        attached.destroy()
    assert error.value.outcome is ErrorOutcome.UNKNOWN
    assert error.value.backend == "modal"
    assert error.value.op == "destroy"
    assert "placeholder" not in str(error.value)
    assert error.value.__context__ is None
    assert error.value.__cause__ is None
    assert attached.is_alive
    assert attached.provider_resource_id == "resource-a"
    assert attached._session._sandbox is contract.remote
    assert attached._session._owned_client is client
    assert not client.closed
    assert client.exits == []
    contract.terminate.side_effect = None
    attached.destroy()
    attached.destroy()
    assert not attached.is_alive
    assert attached._session._sandbox is None
    assert attached._session._owned_client is None
    assert contract.terminate.call_count == 2
    assert client.exits == [(None, None, None)]


@pytest.mark.parametrize("failure", ["wrong_id", "state", "wrapper", "adapter"])
def test_modal_rejected_verification_releases_transport_without_termination(
    modal_client_contract, monkeypatch, failure
):
    from bespokelabs.sandbox.backends.modal import ModalSession
    from bespokelabs.sandbox.sandbox import Sandbox

    contract = modal_client_contract
    if failure == "wrong_id":
        contract.remote.object_id = "unverified-resource"
    elif failure == "state":
        monkeypatch.setattr(
            ModalSession,
            "session_state",
            mock.Mock(side_effect=RuntimeError("state-secret-placeholder")),
        )
    elif failure == "wrapper":
        monkeypatch.setattr(
            Sandbox,
            "_from_session",
            mock.Mock(side_effect=RuntimeError("wrapper-secret-placeholder")),
        )
    else:
        monkeypatch.setattr(
            Sandbox,
            "provider_resource_id",
            mock.PropertyMock(return_value="unverified-resource"),
        )
    with pytest.raises(AttachmentError) as error:
        SDKAttachmentAdapter().reconnect(contract.descriptor, contract.binding)
    assert "placeholder" not in str(error.value)
    assert error.value.__context__ is None
    assert error.value.__cause__ is None
    assert len(contract.clients) == 1
    assert contract.clients[0].closed
    assert len(contract.clients[0].exits) == 1
    contract.terminate.assert_not_called()


class FakeRuntime:
    backend_name = "e2b"

    def __init__(self, resource_id="resource-1"):
        self.provider_resource_id = resource_id
        self.executions = 0
        self.agents = 0
        self.destroyed = 0
        self.network = None

    def execute_code(self, code, language="python"):
        self.executions += 1
        return SandboxResult(stdout=self.provider_resource_id, exit_code=0)

    def execute_command(self, command, args=None):
        return self.execute_code(command)

    def run_agent(self, prompt, **options):
        self.agents += 1
        return AgentRunResult(text="completed", exit_code=0)

    def set_network(self, policy):
        self.network = policy

    def estimate_compute_cost(self, elapsed_secs):
        return elapsed_secs / 3600

    def destroy(self):
        self.destroyed += 1


class FakeAdapter(SDKAttachmentAdapter):

    def __init__(self):
        self.created = 0
        self.reconnected = 0
        self.runtime = None
        self.entered = None
        self.release = None
        self.failure = None
        self.creation_binding = None

    def create(self, binding, **config):
        assert binding.account_scope == "account-a"
        self.created += 1
        self.runtime = FakeRuntime()
        self.creation_binding = binding
        return self.runtime

    def verify_created(self, runtime, binding):
        if runtime is not self.runtime or binding is not self.creation_binding:
            raise AttachmentError("Bound creation provenance is unavailable.")

    def reconnect(self, descriptor, binding):
        self.reconnected += 1
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(10)
        if self.failure is not None:
            raise self.failure
        self.runtime = FakeRuntime(descriptor.provider_resource_id)
        return self.runtime


def binding_for(organization_id):
    return ProviderBinding(
        id="binding-a",
        version=1,
        backend="e2b",
        account_scope="account-a",
        organization_ids=frozenset({organization_id}),
        credentials={"api_key": "fake-credential"},
    )


def forbidden_factory(*args, **kwargs):
    raise AssertionError("ambient creation must never be called")


def service_for(store, binding, adapter, **kwargs):
    return ControlPlane(
        store,
        sandbox_factory=forbidden_factory,
        allowed_backends={"e2b"},
        attachment_bindings=(binding,),
        attachment_adapters={"e2b": adapter},
        resource_sampling_interval_secs=0,
        supervision_interval_secs=0,
        **kwargs,
    )


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "control.db"
    store = SQLiteStore(path, key_pepper="test-pepper")
    organization = store.create_organization("Example organization")
    key = store.issue_api_key(
        organization["id"], name="Operator", scopes=("*",)
    )
    principal = store.authenticate(key.secret)
    binding = binding_for(organization["id"])
    adapter = FakeAdapter()
    service = service_for(store, binding, adapter)
    return path, store, key, principal, binding, adapter, service


@pytest.mark.parametrize("cleanup", ["close", "terminate", "http"])
def test_modal_control_plane_does_not_confirm_failed_owned_termination(
    setup, modal_client_contract, cleanup
):
    _, store, _, principal, _, _, _ = setup
    contract = modal_client_contract
    binding = replace(
        contract.binding,
        id="modal-binding",
        organization_ids=[principal.organization_id],
    )
    attached_descriptor = replace(contract.descriptor, binding_id=binding.id)

    class ModalCreationAdapter(FakeAdapter):
        # Server-owned test provenance only; no provider create RPC is made.
        def create(self, binding, **config):
            self.runtime = SDKAttachmentAdapter().reconnect(
                attached_descriptor, binding
            )
            self.creation_binding = binding
            return self.runtime

    adapter = ModalCreationAdapter()
    service = ControlPlane(
        store,
        sandbox_factory=forbidden_factory,
        allowed_backends={"modal"},
        attachment_bindings=(binding,),
        attachment_adapters={"modal": adapter},
        resource_sampling_interval_secs=0,
        supervision_interval_secs=0,
    )
    record = service.create_sandbox(principal, "modal", {})
    runtime = adapter.runtime
    client = contract.clients[0]
    contract.terminate.side_effect = RuntimeError("rpc-secret-placeholder")
    if cleanup == "close":
        service.close()
    elif cleanup == "http":
        from fastapi.testclient import TestClient

        from bespokelabs.sandbox.control_plane.api import create_app

        key = store.issue_api_key(
            principal.organization_id, name="Owner", scopes=("*",)
        )
        response = TestClient(
            create_app(service, admin_token="test-admin")
        ).delete(
            f"/v1/sandboxes/{record.id}",
            headers={"Authorization": f"Bearer {key.secret}"},
        )
        assert response.status_code == 502
        assert "placeholder" not in response.text
        assert "secret" not in response.text
    else:
        with pytest.raises(SandboxError) as error:
            service.destroy_sandbox(principal, record.id)
        assert error.value.outcome is ErrorOutcome.UNKNOWN
        assert "placeholder" not in str(error.value)
    record = store.get_sandbox(principal.organization_id, record.id)
    assert record.status == "failed"
    assert runtime.is_alive
    assert runtime.provider_resource_id == "resource-a"
    assert client.closed
    assert client.exits == [(None, None, None)]
    contract.terminate.assert_called_once_with()
    assert store.resource_is_held(record.id)
    assert service.policy_summary(principal)["usage"]["active_sandboxes"] == 1
    assert record.id not in service._runtimes
    # Discarding API-level custody releases only the transport, once. It does
    # not promise a retained API retry handle or prove cloud termination.
    service.close()
    assert client.exits == [(None, None, None)]
    contract.terminate.assert_called_once_with()


@pytest.fixture
def modal_coordinator(setup, modal_client_contract):
    _, store, _, principal, _, _, _ = setup
    contract = modal_client_contract
    binding = replace(
        contract.binding,
        id="coordinator-modal",
        organization_ids=[principal.organization_id],
    )
    contract.descriptor = replace(contract.descriptor, binding_id=binding.id)
    registry = BindingRegistry((binding,))
    registry.attach_repository(store.attachment_repository)
    record, _ = store.claim_sandbox_creation(
        principal.organization_id, "modal", {}
    )
    store.complete_sandbox_creation(
        record.id, status="running", provider_resource_id="resource-a"
    )
    store.attachment_repository.publish(
        principal.organization_id,
        record.id,
        contract.descriptor,
        expected_generation=0,
    )
    coordinator = AttachmentCoordinator(store.attachment_repository, registry)
    return store, principal, record, coordinator, contract


@pytest.mark.parametrize(
    "rejection", ["lifecycle", "generation", "revocation", "install"]
)
def test_modal_coordinator_rejection_releases_transport_not_resource(
    modal_coordinator, rejection
):
    store, principal, record, coordinator, contract = modal_coordinator
    reconnect = coordinator.adapter.reconnect

    def rejected(descriptor, binding):
        runtime = reconnect(descriptor, binding)
        if rejection == "lifecycle":
            store.begin_termination(
                principal.organization_id,
                record.id,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        elif rejection == "generation":
            store.attachment_repository.publish(
                principal.organization_id,
                record.id,
                replace(descriptor, incarnation="replacement"),
                expected_generation=1,
            )
        elif rejection == "revocation":
            coordinator.bindings.revoke(binding.id)
        return runtime

    def install(*args):
        if rejection == "install":
            raise AttachmentConflict("install rejected")

    coordinator.adapter.reconnect = rejected
    with pytest.raises(AttachmentError):
        coordinator.resolve(principal, record.id, install=install)
    assert coordinator._cache == {}
    assert len(contract.clients) == 1
    assert contract.clients[0].exits == [(None, None, None)]
    contract.terminate.assert_not_called()
    coordinator.forget(principal.organization_id, record.id)
    assert contract.clients[0].exits == [(None, None, None)]


def test_modal_coordinator_concurrent_loser_and_overwrite_release_only_discarded(
    modal_coordinator,
):
    store, principal, record, coordinator, contract = modal_coordinator
    barrier = threading.Barrier(2)
    reconnect = coordinator.adapter.reconnect

    def parallel(descriptor, binding):
        runtime = reconnect(descriptor, binding)
        barrier.wait(timeout=10)
        return runtime

    coordinator.adapter.reconnect = parallel
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(coordinator.resolve, principal, record.id)
            for _ in range(2)
        ]
        runtimes = [future.result(timeout=10) for future in futures]
    assert runtimes[0] is runtimes[1]
    winner = runtimes[0]._session._owned_client
    assert not winner.closed
    assert len(contract.clients) == 2
    loser = next(client for client in contract.clients if client is not winner)
    assert loser.exits == [(None, None, None)]
    assert coordinator.resolve(principal, record.id) is runtimes[0]
    assert not winner.closed

    def rejected_install(*args):
        raise AttachmentConflict("caller install rejected")

    with pytest.raises(AttachmentConflict):
        coordinator.resolve(principal, record.id, install=rejected_install)
    assert coordinator.resolve(principal, record.id) is runtimes[0]
    assert not winner.closed
    # A descriptor generation replacement discards the prior cached custody.
    coordinator.adapter.reconnect = reconnect
    store.attachment_repository.publish(
        principal.organization_id,
        record.id,
        replace(contract.descriptor, incarnation="replacement"),
        expected_generation=1,
    )
    replacement = coordinator.resolve(principal, record.id)
    assert replacement is not runtimes[0]
    assert winner.exits == [(None, None, None)]
    assert not contract.clients[-1].closed
    coordinator.forget(principal.organization_id, record.id)
    assert all(
        client.exits == [(None, None, None)] for client in contract.clients
    )
    contract.terminate.assert_not_called()


def test_modal_delayed_displaced_disposal_rejects_three_generation_alias(
    modal_coordinator, monkeypatch
):
    store, principal, record, coordinator, contract = modal_coordinator
    original_reconnect = coordinator.adapter.reconnect
    original_release = attachment_module.release_owned_transport
    old = coordinator.resolve(principal, record.id)
    old_client = contract.clients[0]
    caller_thread = threading.get_ident()
    disposing, resume = threading.Event(), threading.Event()

    def delayed_release(runtime, **kwargs):
        if runtime is old and threading.get_ident() != caller_thread:
            disposing.set()
            assert resume.wait(10)
        original_release(runtime, **kwargs)

    def publish(generation):
        store.attachment_repository.publish(
            principal.organization_id,
            record.id,
            replace(
                contract.descriptor, incarnation=f"generation-{generation + 1}"
            ),
            expected_generation=generation,
        )

    monkeypatch.setattr(
        attachment_module, "release_owned_transport", delayed_release
    )
    publish(1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        replacement = pool.submit(coordinator.resolve, principal, record.id)
        try:
            assert disposing.wait(10)
            # SDK disposal is outside the cache/DB critical section. Generation
            # three can be published while generation one's exit is paused.
            assert coordinator._cache_lock.acquire(False)
            coordinator._cache_lock.release()
            publish(2)
            coordinator.adapter.reconnect = lambda *args: old
            rejected = False
            alias = None
            try:
                alias = coordinator.resolve(principal, record.id)
            except AttachmentConflict:
                rejected = True
            assert not old_client.closed
            resume.set()
            replacement.result(timeout=10)
            if alias is not None:
                assert (
                    not old_client.closed
                ), "successful alias winner closed by delayed disposal"
            assert rejected
            assert coordinator._cache == {}
            assert all(
                client.exits == [(None, None, None)]
                for client in contract.clients
            )
        finally:
            resume.set()
    coordinator.adapter.reconnect = original_reconnect
    fresh = coordinator.resolve(principal, record.id)
    assert fresh is not old
    assert not contract.clients[-1].closed
    coordinator.forget(principal.organization_id, record.id)
    assert all(
        client.exits == [(None, None, None)] for client in contract.clients
    )
    contract.terminate.assert_not_called()


def test_modal_concurrent_same_object_same_generation_preserves_winner(
    modal_coordinator,
):
    _, principal, record, coordinator, contract = modal_coordinator
    binding = coordinator.bindings.resolve(
        principal.organization_id, contract.descriptor
    )
    runtime = coordinator.adapter.reconnect(contract.descriptor, binding)
    barrier = threading.Barrier(2)

    def same_object(*args):
        barrier.wait(timeout=10)
        return runtime

    coordinator.adapter.reconnect = same_object
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(coordinator.resolve, principal, record.id)
            for _ in range(2)
        ]
        assert [f.result(timeout=10) for f in futures] == [runtime, runtime]
    assert contract.clients[0].exits == []
    assert coordinator.resolve(principal, record.id) is runtime
    coordinator.forget(principal.organization_id, record.id)
    assert contract.clients[0].exits == [(None, None, None)]
    contract.terminate.assert_not_called()


def test_modal_parallel_cross_coordinator_alias_claim_has_only_one_owner(
    modal_coordinator,
):
    store, principal, record, first, contract = modal_coordinator
    binding = first.bindings.resolve(
        principal.organization_id, contract.descriptor
    )
    runtime = first.adapter.reconnect(contract.descriptor, binding)
    second = AttachmentCoordinator(store.attachment_repository, first.bindings)
    barrier = threading.Barrier(2)

    def same_object(*args):
        barrier.wait(timeout=10)
        return runtime

    first.adapter.reconnect = same_object
    second.adapter.reconnect = same_object
    coordinators = [first, second]
    successes, rejected = [], []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            (c, pool.submit(c.resolve, principal, record.id))
            for c in coordinators
        ]
        for coordinator, future in futures:
            try:
                assert future.result(timeout=10) is runtime
                successes.append(coordinator)
            except AttachmentConflict:
                rejected.append(coordinator)
    assert len(successes) == len(rejected) == 1
    winner, loser = successes[0], rejected[0]
    loser.adapter.reconnect = lambda *args: runtime
    loser.forget(principal.organization_id, record.id)
    with pytest.raises(AttachmentConflict):
        loser.resolve(principal, record.id)
    assert contract.clients[0].exits == []
    assert winner.resolve(principal, record.id) is runtime
    winner.forget(principal.organization_id, record.id)
    with pytest.raises(AttachmentConflict):
        loser.resolve(principal, record.id)
    assert contract.clients[0].exits == [(None, None, None)]
    contract.terminate.assert_not_called()


def test_modal_unverified_creation_alias_cannot_dispose_cache_owner(
    modal_coordinator,
):
    store, principal, record, coordinator, contract = modal_coordinator
    runtime = coordinator.resolve(principal, record.id)
    binding = coordinator.bindings.resolve(
        principal.organization_id, contract.descriptor
    )

    class RejectedCreation(SDKAttachmentAdapter):

        def create(self, *args, **kwargs):
            return runtime

    service = ControlPlane(
        store,
        allowed_backends={"modal"},
        attachment_bindings=(binding,),
        attachment_adapters={"modal": RejectedCreation()},
        resource_sampling_interval_secs=0,
        supervision_interval_secs=0,
    )
    with pytest.raises(SandboxCreationError):
        service.create_sandbox(principal, "modal", {})
    assert service._runtimes == {}
    assert coordinator.resolve(principal, record.id) is runtime
    assert contract.clients[0].exits == []
    service.close()
    assert contract.clients[0].exits == []
    coordinator.forget(principal.organization_id, record.id)
    assert contract.clients[0].exits == [(None, None, None)]
    contract.terminate.assert_not_called()


def test_modal_same_object_new_generation_rejected_and_old_custody_disposed(
    modal_coordinator,
):
    store, principal, record, coordinator, contract = modal_coordinator
    runtime = coordinator.resolve(principal, record.id)
    store.attachment_repository.publish(
        principal.organization_id,
        record.id,
        replace(contract.descriptor, incarnation="replacement"),
        expected_generation=1,
    )
    coordinator.adapter.reconnect = lambda *args: runtime
    with pytest.raises(AttachmentConflict):
        coordinator.resolve(principal, record.id)
    assert coordinator._cache == {}
    assert contract.clients[0].exits == [(None, None, None)]
    contract.terminate.assert_not_called()


@pytest.mark.parametrize("failure", ["install", "commit", "interrupt"])
def test_modal_failed_claim_cannot_be_republished_after_displacement(
    modal_coordinator, monkeypatch, failure
):
    store, principal, record, coordinator, contract = modal_coordinator
    old = coordinator.resolve(principal, record.id)
    binding = coordinator.bindings.resolve(
        principal.organization_id, contract.descriptor
    )
    candidate = coordinator.adapter.reconnect(contract.descriptor, binding)
    store.attachment_repository.publish(
        principal.organization_id,
        record.id,
        replace(contract.descriptor, incarnation="replacement"),
        expected_generation=1,
    )
    coordinator.adapter.reconnect = lambda *args: candidate
    factory = store.attachment_repository.connection_factory
    connections = []

    class CommitFailure:

        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            if failure == "interrupt":
                raise KeyboardInterrupt("injected commit interruption")
            raise sqlite3.OperationalError("injected commit failure")

    def connection():
        conn = factory()
        connections.append(conn)
        return CommitFailure(conn) if len(connections) == 2 else conn

    def install(*args):
        if failure == "install":
            raise AttachmentConflict("injected install failure")

    if failure != "install":
        monkeypatch.setattr(
            store.attachment_repository, "connection_factory", connection
        )
    expected = (
        KeyboardInterrupt if failure == "interrupt" else AttachmentConflict
    )
    with pytest.raises(expected):
        coordinator.resolve(principal, record.id, install=install)
    assert coordinator._cache == {}
    assert old is not candidate
    assert all(
        client.exits == [(None, None, None)] for client in contract.clients
    )
    with pytest.raises(AttachmentConflict):
        coordinator.resolve(principal, record.id)
    coordinator.forget(principal.organization_id, record.id)
    assert all(
        client.exits == [(None, None, None)] for client in contract.clients
    )
    contract.terminate.assert_not_called()


@pytest.mark.parametrize("native", [False, True])
def test_alias_publication_restriction_leaves_unowned_and_custom_runtimes_unchanged(
    modal_coordinator, native
):
    from bespokelabs.sandbox.backends.modal import ModalSession
    from bespokelabs.sandbox.sandbox import Sandbox

    store, principal, record, coordinator, contract = modal_coordinator
    custom = SimpleNamespace(
        backend_name="modal",
        provider_resource_id="resource-a",
        destroy=mock.Mock(),
        _release_owned_client=mock.Mock(),
    )
    runtime = (
        Sandbox._from_session(
            "modal",
            ModalSession(sandbox=contract.remote),
            SandboxConfig(backend="modal"),
        )
        if native
        else custom
    )
    coordinator.adapter.reconnect = lambda *args: runtime
    assert coordinator.resolve(principal, record.id) is runtime
    store.attachment_repository.publish(
        principal.organization_id,
        record.id,
        replace(contract.descriptor, incarnation="replacement"),
        expected_generation=1,
    )
    assert coordinator.resolve(principal, record.id) is runtime
    other = AttachmentCoordinator(
        store.attachment_repository, coordinator.bindings
    )
    other.adapter.reconnect = lambda *args: runtime
    assert other.resolve(principal, record.id) is runtime
    coordinator.forget(principal.organization_id, record.id)
    other.forget(principal.organization_id, record.id)
    custom.destroy.assert_not_called()
    custom._release_owned_client.assert_not_called()
    assert contract.clients == []
    contract.terminate.assert_not_called()


def test_modal_rejected_concurrent_install_does_not_close_cached_winner(
    modal_coordinator,
):
    _, principal, record, coordinator, contract = modal_coordinator
    reconnect = coordinator.adapter.reconnect
    barrier = threading.Barrier(2)
    installs = []

    def parallel(descriptor, binding):
        runtime = reconnect(descriptor, binding)
        barrier.wait(timeout=10)
        return runtime

    def install(runtime, *args):
        installs.append(runtime)
        if len(installs) == 2:
            raise AttachmentConflict("late install rejected")

    coordinator.adapter.reconnect = parallel
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                coordinator.resolve, principal, record.id, install=install
            )
            for _ in range(2)
        ]
        successes = []
        rejected = []
        for future in futures:
            try:
                successes.append(future.result(timeout=10))
            except AttachmentConflict as error:
                rejected.append(error)
    assert len(successes) == len(rejected) == 1
    winner = successes[0]._session._owned_client
    assert not winner.closed
    assert coordinator.resolve(principal, record.id) is successes[0]
    loser = next(client for client in contract.clients if client is not winner)
    assert loser.exits == [(None, None, None)]
    contract.terminate.assert_not_called()
    coordinator.forget(principal.organization_id, record.id)
    assert winner.exits == [(None, None, None)]


@pytest.mark.parametrize(
    "failure", [sqlite3.OperationalError, KeyboardInterrupt]
)
def test_modal_provisional_cache_cannot_escape_a_failed_commit(
    modal_coordinator, monkeypatch, failure
):
    _, principal, record, coordinator, contract = modal_coordinator
    role = threading.local()
    rolled_back, allow_commit_error = threading.Event(), threading.Event()
    attempted_cache, disposing, allow_disposal = (
        threading.Event() for _ in range(3)
    )
    probe = []
    original_factory = coordinator.repository.connection_factory
    original_lock = coordinator._cache_lock
    original_release = attachment_module.release_owned_transport

    class CommitRejectingConnection:

        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            # Real rollback frees DB authority while the failing resolver is
            # paused before its exception handler can remove provisional cache.
            self.connection.rollback()
            rolled_back.set()
            assert allow_commit_error.wait(10)
            raise failure("injected commit failure")

    def factory():
        connection = original_factory()
        if getattr(role, "name", None) == "A":
            role.connections = getattr(role, "connections", 0) + 1
            if role.connections == 2:
                return CommitRejectingConnection(connection)
        return connection

    class ObservedLock:

        def acquire(self):
            if getattr(role, "name", None) == "B" and not probe:
                acquired = original_lock.acquire(False)
                probe.append(acquired)
                attempted_cache.set()
                if acquired:
                    return True
            return original_lock.acquire()

        def release(self):
            original_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *args):
            self.release()

    def release(runtime, **kwargs):
        if getattr(role, "name", None) == "A":
            disposing.set()
            assert allow_disposal.wait(10)
        original_release(runtime, **kwargs)

    def resolve(name):
        role.name = name
        return coordinator.resolve(principal, record.id)

    monkeypatch.setattr(coordinator.repository, "connection_factory", factory)
    monkeypatch.setattr(coordinator, "_cache_lock", ObservedLock())
    monkeypatch.setattr(attachment_module, "release_owned_transport", release)
    exit_observations = []
    winner_client = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(resolve, "A")
        try:
            assert rolled_back.wait(10)
            rejected_client = contract.clients[0]
            original_exit = rejected_client.__exit__

            def exit_rejected(*args):
                exit_observations.append(not winner_client[0].closed)
                original_exit(*args)
                exit_observations.append(not winner_client[0].closed)

            monkeypatch.setattr(rejected_client, "__exit__", exit_rejected)
            b = pool.submit(resolve, "B")
            assert attempted_cache.wait(10)
            if probe[0]:
                # Negative-control schedule: old code lets B successfully
                # acquire A's provisional runtime before A raises/cleans up.
                winner = b.result(timeout=10)
            allow_commit_error.set()
            assert disposing.wait(10)
            winner = b.result(timeout=10)
            winner_client.append(winner._session._owned_client)
            assert not winner_client[0].closed
            allow_disposal.set()
            expected_error = (
                AttachmentConflict
                if failure is sqlite3.OperationalError
                else failure
            )
            with pytest.raises(expected_error):
                a.result(timeout=10)
            assert exit_observations == [True, True]
            assert not winner_client[0].closed
            assert rejected_client is not winner_client[0]
            assert rejected_client.exits == [(None, None, None)]
            assert coordinator.resolve(principal, record.id) is winner
            contract.terminate.assert_not_called()
        finally:
            allow_commit_error.set()
            allow_disposal.set()
    coordinator.forget(principal.organization_id, record.id)
    assert all(
        client.exits == [(None, None, None)] for client in contract.clients
    )


def test_modal_failed_commit_rolls_back_control_plane_install(
    modal_coordinator, monkeypatch
):
    store, principal, record, coordinator, contract = modal_coordinator
    binding = coordinator.bindings.resolve(
        principal.organization_id, contract.descriptor
    )
    service = ControlPlane(
        store,
        allowed_backends={"modal"},
        attachment_bindings=(binding,),
        attachment_adapters={"modal": SDKAttachmentAdapter()},
        resource_sampling_interval_secs=0,
        supervision_interval_secs=0,
    )
    original_factory = store.attachment_repository.connection_factory
    connections = []

    class CommitRejectingConnection:

        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            raise sqlite3.OperationalError("injected commit failure")

    def factory():
        connection = original_factory()
        connections.append(connection)
        return (
            CommitRejectingConnection(connection)
            if len(connections) == 2
            else connection
        )

    monkeypatch.setattr(
        store.attachment_repository, "connection_factory", factory
    )
    with pytest.raises(ConflictError):
        service.attach_sandbox(principal, record.id)
    assert record.id not in service._runtimes
    assert record.id not in service._runtime_attachment_records
    assert service._attachment_coordinators["modal"]._cache == {}
    assert contract.clients[0].exits == [(None, None, None)]
    service.close()
    assert contract.clients[0].exits == [(None, None, None)]
    contract.terminate.assert_not_called()


def test_transport_disposal_ignores_native_and_unverified_callbacks(
    modal_client_contract,
):
    from bespokelabs.sandbox.backends.modal import ModalSession

    contract = modal_client_contract
    native = ModalSession(sandbox=contract.remote)
    unverified = SimpleNamespace(
        _session=native, _release_owned_client=mock.Mock(), destroy=mock.Mock()
    )
    release_owned_transport(native)
    release_owned_transport(unverified)
    unverified._release_owned_client.assert_not_called()
    unverified.destroy.assert_not_called()
    contract.terminate.assert_not_called()
    contract.factory.assert_not_called()
    assert native.provider_resource_id == "resource-a"


@pytest.mark.parametrize("confirmed", [False, True])
def test_disposed_owned_modal_session_cannot_switch_to_native_destroy(
    modal_client_contract, confirmed
):
    contract = modal_client_contract
    runtime = SDKAttachmentAdapter().reconnect(
        contract.descriptor, contract.binding
    )
    session = runtime._session
    if confirmed:
        contract.terminate.side_effect = None
        runtime.destroy()
    release_owned_transport(runtime)
    if confirmed:
        session.destroy()
        session.destroy()
        assert session.provider_resource_id is None
        contract.terminate.assert_called_once_with()
    else:
        for _ in range(2):
            with pytest.raises(SandboxError) as error:
                runtime.destroy()
            assert error.value.outcome is ErrorOutcome.UNKNOWN
            assert "placeholder" not in str(error.value)
            assert error.value.__context__ is None
            assert error.value.__cause__ is None
        assert runtime.is_alive
        assert runtime.provider_resource_id == "resource-a"
        contract.terminate.assert_not_called()
    assert contract.clients[0].exits == [(None, None, None)]


def test_modal_transport_ownership_is_consumed_once_under_concurrent_disposal(
    modal_client_contract,
):
    contract = modal_client_contract
    runtime = SDKAttachmentAdapter().reconnect(
        contract.descriptor, contract.binding
    )
    contract.clients[0].cleanup_failure = True
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(release_owned_transport, [runtime] * 8))
    assert contract.clients[0].exits == [(None, None, None)]
    contract.terminate.assert_not_called()
    assert runtime.provider_resource_id == "resource-a"


def descriptor(store, principal, sandbox_id):
    with store.attachment_repository.transaction() as connection:
        return store.attachment_repository.load(
            principal.organization_id, sandbox_id, connection=connection
        )


def fresh(setup, **kwargs):
    path, _, key, _, binding, _, _ = setup
    store = SQLiteStore(path, key_pepper="test-pepper")
    adapter = FakeAdapter()
    service = service_for(store, binding, adapter, **kwargs)
    return store, store.authenticate(key.secret), adapter, service


def test_restart_reuses_identity_and_execution_accounting_once(setup):
    _, store, _, principal, _, adapter, service = setup
    record = service.create_sandbox(
        principal, "e2b", {}, idempotency_key="create-a"
    )
    saved = descriptor(store, principal, record.id)
    assert saved.descriptor.provider_resource_id == record.provider_resource_id
    assert service.attach_sandbox(principal, record.id).id == record.id
    assert adapter.reconnected == 0
    second_store, second_principal, second_adapter, second = fresh(setup)
    first = second.execute(
        second_principal, record.id, code="pass", request_id="execute-a"
    )
    assert first == second.execute(
        second_principal, record.id, code="pass", request_id="execute-a"
    )
    assert first["stdout"] == record.provider_resource_id
    assert adapter.created == 1
    assert second_adapter.created == 0
    assert second_adapter.reconnected == 1
    assert second_adapter.runtime.executions == 1
    assert (
        len(
            second_store.sandbox_detail(principal.organization_id, record.id)[
                "executions"
            ]
        )
        == 1
    )
    assert len(second._resource_targets()) == 1
    assert (
        service.create_sandbox(
            principal, "e2b", {}, idempotency_key="create-a"
        ).id
        == record.id
    )
    assert adapter.created == 1
    before = second_store.summarize_costs(principal.organization_id)
    second.attach_sandbox(second_principal, record.id)
    assert second_store.summarize_costs(principal.organization_id) == before
    assert second_adapter.reconnected == 1
    second.destroy_sandbox(second_principal, record.id)
    assert descriptor(store, principal, record.id).descriptor is None
    assert (
        second.execute(
            second_principal, record.id, code="pass", request_id="execute-a"
        )
        == first
    )
    with pytest.raises(ConflictError):
        second.execute(
            second_principal, record.id, code="pass", request_id="execute-b"
        )
    # Even a process retaining the originally created handle cannot resurrect it.
    with pytest.raises(ConflictError):
        service.execute(
            principal, record.id, code="pass", request_id="execute-c"
        )
    assert second_adapter.runtime.destroyed == 1
    assert second_adapter.reconnected == 1
    _, third_principal, third_adapter, third = fresh(setup)
    assert (
        third.execute(
            third_principal, record.id, code="pass", request_id="execute-a"
        )
        == first
    )
    assert third_adapter.reconnected == 0


def test_fresh_interpreter_reattaches_without_creation(setup):
    path, _, key, principal, _, adapter, service = setup
    record = service.create_sandbox(principal, "e2b", {})
    script = """
import json, sys
from tests.test_hosted_attachments import (
    SQLiteStore, FakeAdapter, binding_for, service_for,
)
data = json.load(sys.stdin)
store = SQLiteStore(data['path'], key_pepper='test-pepper')
principal = store.authenticate(data['key'])
adapter = FakeAdapter()
service = service_for(store, binding_for(principal.organization_id), adapter)
result = service.execute(principal, data['sandbox'], code='pass', request_id='process-a')
print(json.dumps([result['stdout'], adapter.created, adapter.reconnected]))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(
            {"path": str(path), "key": key.secret, "sandbox": record.id}
        ),
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
    )
    assert json.loads(completed.stdout) == [record.provider_resource_id, 0, 1]
    assert adapter.created == 1
    # Simulate a crash: neither interpreter calls close() or provider destroy.
    assert adapter.runtime.destroyed == 0


@pytest.mark.parametrize("operation", ["agent", "network"])
def test_lazy_agent_and_network_resolution(setup, operation):
    _, store, _, principal, _, _, service = setup
    record = service.create_sandbox(principal, "e2b", {})
    _, principal, adapter, second = fresh(setup)
    if operation == "agent":
        response = second.run_agent(
            principal, record.id, prompt="hello", request_id="agent-a"
        )
        assert response == second.run_agent(
            principal, record.id, prompt="hello", request_id="agent-a"
        )
        assert adapter.runtime.agents == 1
        second.destroy_sandbox(principal, record.id)
        assert response == second.run_agent(
            principal, record.id, prompt="hello", request_id="agent-a"
        )
    else:
        policy = NetworkPolicy(mode="none")
        second.set_network(principal, record.id, policy)
        assert adapter.runtime.network == policy
        assert (
            store.get_sandbox(principal.organization_id, record.id).config[
                "network"
            ]["mode"]
            == "none"
        )
        assert not store.sandbox_detail(principal.organization_id, record.id)[
            "executions"
        ]
    assert adapter.reconnected == 1
    assert adapter.created == 0


@pytest.mark.parametrize("cleanup", ["close", "terminate"])
def test_creation_publication_rolls_back_together_and_preserves_allocation(
    setup, monkeypatch, cleanup
):
    _, store, _, principal, _, adapter, service = setup
    coordinator = service._attachment_coordinators["e2b"]
    original = coordinator.publish_created

    def fail_after_publish(*args, **kwargs):
        result = original(*args, **kwargs)
        connection = kwargs["connection"]
        assert connection.in_transaction
        assert (
            connection.execute(
                "SELECT status FROM sandboxes WHERE id=?", (result.sandbox_id,)
            ).fetchone()[0]
            == "running"
        )
        # A second store connection must not see either uncommitted write.
        visible = store.get_sandbox(
            principal.organization_id, result.sandbox_id
        )
        assert visible.status == "creating"
        with store.connect() as other:
            assert (
                other.execute(
                    "SELECT COUNT(*) FROM cp_runtime_attachments_v1"
                ).fetchone()[0]
                == 0
            )
        raise AttachmentError("opaque adapter error")

    monkeypatch.setattr(coordinator, "publish_created", fail_after_publish)
    with pytest.raises(SandboxCreationError) as error:
        service.create_sandbox(principal, "e2b", {}, idempotency_key="create-a")
    assert error.value.outcome is ErrorOutcome.UNKNOWN
    record = store.list_sandboxes(principal.organization_id)[0]
    assert record.status == "failed"
    assert record.provider_resource_id == adapter.runtime.provider_resource_id
    assert record.cleanup_status == "unknown"
    assert descriptor(store, principal, record.id).descriptor is None
    assert (
        service.create_sandbox(
            principal, "e2b", {}, idempotency_key="create-a"
        ).id
        == record.id
    )
    assert adapter.created == 1
    assert adapter.runtime.destroyed == 0
    assert service._runtimes[record.id] is adapter.runtime
    if cleanup == "close":
        service.close()
    else:
        service.destroy_sandbox(principal, record.id)
    assert adapter.runtime.destroyed == 1
    assert (
        store.get_sandbox(principal.organization_id, record.id).status
        == "destroyed"
    )


@pytest.mark.parametrize("bound", [False, True])
def test_ambiguous_creation_failure_does_not_grant_unverified_identity(
    setup, bound
):
    _, store, _, principal, _, adapter, service = setup

    def ambiguous(binding, **config):
        raise SandboxCreationError(
            "opaque failure",
            outcome=ErrorOutcome.UNKNOWN,
            context={
                "provider_resource_id": "resource-2",
                "cleanup_status": "unknown",
            },
        )

    if bound:
        adapter.create = ambiguous
    else:
        service = ControlPlane(
            store,
            sandbox_factory=ambiguous,
            allowed_backends={"e2b"},
            resource_sampling_interval_secs=0,
            supervision_interval_secs=0,
        )
    with pytest.raises(SandboxCreationError):
        service.create_sandbox(principal, "e2b", {})
    record = store.list_sandboxes(principal.organization_id)[0]
    assert record.status == "failed"
    assert record.provider_resource_id == (None if bound else "resource-2")
    assert record.cleanup_status == "unknown"
    assert record.retry_status == "blocked_cleanup_unknown"
    if bound:
        assert descriptor(store, principal, record.id).descriptor is None
        assert record.id not in service._runtimes
        with pytest.raises(ConflictError):
            service.destroy_sandbox(principal, record.id)
        service.close()
        assert service.get_sandbox(principal, record.id).status == "failed"


@pytest.mark.parametrize(
    "mode", ["binding-version", "membership", "sdk", "adapter"]
)
def test_unavailable_bindings_and_adapters_are_safe(setup, mode):
    path, _, key, principal, binding, _, service = setup
    record = service.create_sandbox(principal, "e2b", {})
    store = SQLiteStore(path, key_pepper="test-pepper")
    adapter = FakeAdapter()
    if mode == "binding-version":
        binding = replace(binding, version=2)
    elif mode == "membership":
        binding = replace(
            binding,
            version=2,
            organization_ids=frozenset({"another-organization"}),
        )
    elif mode == "sdk":
        adapter.failure = ImportError("opaque sdk failure")
    second = service_for(store, binding, adapter)
    if mode == "adapter":
        second = ControlPlane(
            store,
            attachment_bindings=(binding,),
            resource_sampling_interval_secs=0,
        )
    with pytest.raises(FeatureNotSupportedError) as error:
        second.attach_sandbox(store.authenticate(key.secret), record.id)
    assert "opaque" not in str(error.value)
    assert "fake-credential" not in str(error.value)
    assert record.id not in second._runtimes
    assert adapter.created == 0


def test_shared_revocation_blocks_other_worker_local_handle_and_restart(setup):
    _, _, _, principal, binding, _, service = setup
    record = service.create_sandbox(principal, "e2b", {})
    store, other_principal, adapter, second = fresh(setup)
    second.attach_sandbox(other_principal, record.id)
    retained = second._runtimes[record.id]
    service._binding_registry.revoke(binding.id)
    with pytest.raises(FeatureNotSupportedError):
        second.attach_sandbox(other_principal, record.id)
    assert retained.destroyed == 0
    assert adapter.reconnected == 1
    with pytest.raises(AttachmentError):
        service_for(store, binding, FakeAdapter())
    with pytest.raises(AttachmentError):
        service_for(store, replace(binding, version=0), FakeAdapter())
    replacement = service_for(store, replace(binding, version=2), FakeAdapter())
    with pytest.raises(FeatureNotSupportedError):
        replacement.attach_sandbox(other_principal, record.id)


@pytest.mark.parametrize(
    "mode", ["unknown", "wrong-account", "wrong-id", "stock"]
)
@pytest.mark.parametrize("cleanup", ["close", "terminate"])
def test_unverified_creation_never_grants_cleanup_authority(
    setup, monkeypatch, mode, cleanup
):
    _, store, _, principal, binding, adapter, _ = setup
    runtime = FakeRuntime("foreign-resource")
    calls = []

    class UnverifiedAdapter(SDKAttachmentAdapter):

        def create(self, selected_binding, **config):
            assert selected_binding is binding
            return runtime

        def reconnect(self, *args):
            calls.append("reconnect")
            raise AssertionError("Unverified handles must not reconnect")

    configured = UnverifiedAdapter()
    if mode != "stock":

        def reject(returned, selected_binding):
            assert returned is runtime
            assert selected_binding is binding
            # Structured errors cannot supply authority after provenance rejection.
            raise SandboxCreationError(
                "opaque-token-placeholder",
                context={
                    "provider_resource_id": "foreign-resource",
                    "cleanup_status": "deleted",
                },
            )

        monkeypatch.setattr(configured, "verify_created", reject)
    if mode == "wrong-id":
        runtime.provider_resource_id = "another-resource"
    elif mode == "wrong-account":
        runtime.account_scope = "another-account"

    def forbidden(*args, **kwargs):
        calls.append("provider-operation")
        raise AssertionError("Unverified runtime must not be used")

    def estimate(elapsed):
        calls.append("cost")
        return 1

    monkeypatch.setattr(runtime, "estimate_compute_cost", estimate)
    runtime.close = forbidden
    runtime.destroy = forbidden

    class ObservationOnly:

        def list_resources(self, organization_id):
            assert organization_id == principal.organization_id
            return [
                ProviderObservation(
                    "foreign-observation",
                    runtime.provider_resource_id,
                    "running",
                    datetime.now(timezone.utc).isoformat(),
                    provider_cost_usd=Decimal("17"),
                    organization_id=organization_id,
                    backend="e2b",
                )
            ]

        get_resource = staticmethod(forbidden)

    service = service_for(
        store,
        binding,
        configured,
        provider_terminators={"e2b": forbidden},
        provider_reconcilers={"e2b": ObservationOnly()},
    )
    with pytest.raises(SandboxCreationError) as error:
        service.create_sandbox(principal, "e2b", {}, idempotency_key="rejected")
    assert error.value.outcome is ErrorOutcome.UNKNOWN
    assert "opaque-token-placeholder" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    record = service.list_sandboxes(principal)[0]
    assert service.get_sandbox(principal, record.id) == record
    assert record.status == "failed"
    assert record.provider_resource_id is None
    assert record.cleanup_status == "unknown"
    assert record.retry_status == "blocked_cleanup_unknown"
    assert record.id not in service._runtimes
    assert record.id not in service._runtime_attachment_records
    assert descriptor(store, principal, record.id).descriptor is None
    assert (
        service.create_sandbox(principal, "e2b", {}, idempotency_key="rejected")
        == record
    )
    detail = service.sandbox_detail(principal, record.id)
    serialized = json.dumps(detail, default=str)
    for forbidden_value in (
        runtime.provider_resource_id,
        "opaque-token-placeholder",
        "another-account",
    ):
        assert forbidden_value not in serialized
    with store.connect() as connection:
        attempt = connection.execute(
            "SELECT provider_resource_id, error_json "
            "FROM sandbox_creation_attempts WHERE sandbox_id=?",
            (record.id,),
        ).fetchone()
    assert attempt[0] is None
    assert runtime.provider_resource_id not in attempt[1]
    readonly = store.issue_api_key(
        principal.organization_id, name="Reader", scopes=("sandboxes:read",)
    )
    assert (
        service.get_sandbox(store.authenticate(readonly.secret), record.id).id
        == record.id
    )
    with pytest.raises(AuthorizationError):
        service.destroy_sandbox(store.authenticate(readonly.secret), record.id)
    other = store.create_organization("Other organization")
    other_key = store.issue_api_key(other["id"], name="Operator", scopes=("*",))
    with pytest.raises(NotFoundError):
        service.destroy_sandbox(store.authenticate(other_key.secret), record.id)
    with pytest.raises(ConflictError):
        service.attach_sandbox(principal, record.id)
    with pytest.raises(ConflictError):
        service.execute(
            principal, record.id, code="pass", request_id="rejected"
        )
    summary = service.reconcile(principal, "e2b")
    assert summary["orphans"] == 1
    assert summary["updated"] == 0
    assert (
        service.get_sandbox(principal, record.id).provider_resource_id is None
    )
    if cleanup == "close":
        service.close()
    else:
        with pytest.raises(ConflictError):
            service.destroy_sandbox(principal, record.id)
        service.close()
    assert service.get_sandbox(principal, record.id).status == "failed"
    assert descriptor(store, principal, record.id).descriptor is None
    assert calls == []
    assert adapter.created == 0


def test_scope_and_ownership_precede_reconnect(setup):
    _, store, _, principal, _, _, service = setup
    record = service.create_sandbox(principal, "e2b", {})
    _, _, adapter, second = fresh(setup)
    readonly = store.issue_api_key(
        principal.organization_id, name="Reader", scopes=("sandboxes:read",)
    )
    with pytest.raises(AuthorizationError):
        second.attach_sandbox(store.authenticate(readonly.secret), record.id)
    other = store.create_organization("Another organization")
    other_key = store.issue_api_key(other["id"], name="Operator", scopes=("*",))
    with pytest.raises(NotFoundError):
        second.attach_sandbox(store.authenticate(other_key.secret), record.id)
    assert adapter.reconnected == 0


def test_cross_store_attach_cannot_install_after_stopping(setup):
    _, store, _, principal, _, _, service = setup
    record = service.create_sandbox(principal, "e2b", {})
    _, other_principal, adapter, second = fresh(setup)
    adapter.entered = threading.Event()
    adapter.release = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(second.attach_sandbox, other_principal, record.id)
        assert adapter.entered.wait(10)
        try:
            service.destroy_sandbox(principal, record.id)
            assert descriptor(store, principal, record.id).descriptor is None
        finally:
            adapter.release.set()
        with pytest.raises(ConflictError):
            future.result(timeout=10)
    assert record.id not in second._runtimes
    assert adapter.runtime.destroyed == 0


@pytest.mark.parametrize(
    "path", ["begin", "mark", "update", "watchdog", "reconcile"]
)
def test_all_terminal_paths_invalidate_without_resurrection(setup, path):
    _, store, _, principal, _, _, service = setup
    record = service.create_sandbox(principal, "e2b", {"timeout_secs": 1})
    old = descriptor(store, principal, record.id)
    timestamp = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    if path == "begin":
        store.begin_termination(
            principal.organization_id, record.id, timestamp=timestamp
        )
    elif path == "mark":
        store.mark_lifecycle(record.id, "destroyed", timestamp)
    elif path == "update":
        store.update_sandbox_status(record.id, "failed")
    elif path == "watchdog":
        store.claim_expired_sandboxes(
            timestamp=timestamp, owner="worker-a", stale_before=timestamp
        )
    else:
        observation = ProviderObservation(
            observation_id="observation-a",
            provider_resource_id=record.provider_resource_id,
            status="terminated",
            observed_at=timestamp,
        )
        assert store.reconcile_provider_observation(
            principal.organization_id,
            record.id,
            backend="e2b",
            observation=observation,
            customer_markup=Decimal(1),
        )
    current = descriptor(store, principal, record.id)
    assert current.descriptor is None
    assert current.generation > old.generation
    assert not service._resource_targets()
    # Even a subsequent running observation cannot restore a descriptor.
    store.apply_provider_observation(
        principal.organization_id,
        record.id,
        status="running",
        observed_at=timestamp,
    )
    assert not service._resource_targets()
    _, other_principal, adapter, second = fresh(setup)
    with pytest.raises((ConflictError, FeatureNotSupportedError)):
        second.attach_sandbox(other_principal, record.id)
    assert adapter.reconnected == 0


@pytest.mark.parametrize(
    "path", ["begin", "mark", "watchdog", "reconcile", "update", "fail"]
)
def test_invalidation_rolls_back_with_stopping_intent(setup, monkeypatch, path):
    _, store, _, principal, _, _, service = setup
    record = service.create_sandbox(principal, "e2b", {})
    old = descriptor(store, principal, record.id)
    original = store.attachment_repository.invalidate

    def fail(*args, **kwargs):
        assert kwargs["connection"].in_transaction
        original(*args, **kwargs)
        raise RuntimeError("transaction failure")

    monkeypatch.setattr(store.attachment_repository, "invalidate", fail)
    timestamp = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    with pytest.raises(RuntimeError):
        if path == "begin":
            store.begin_termination(
                principal.organization_id, record.id, timestamp=timestamp
            )
        elif path == "mark":
            store.mark_lifecycle(record.id, "destroyed", timestamp)
        elif path == "watchdog":
            # Include this record in the expiry selection without changing its
            # running authority or attachment.
            with store.connect() as connection:
                connection.execute(
                    "UPDATE sandboxes SET expires_at=? WHERE id=?",
                    (timestamp, record.id),
                )
            store.claim_expired_sandboxes(
                timestamp=timestamp, owner="worker-a", stale_before=timestamp
            )
        elif path == "reconcile":
            store.reconcile_provider_observation(
                principal.organization_id,
                record.id,
                backend="e2b",
                observation=ProviderObservation(
                    observation_id="observation-a",
                    provider_resource_id=record.provider_resource_id,
                    status="terminated",
                    observed_at=timestamp,
                ),
                customer_markup=Decimal(1),
            )
        elif path == "update":
            store.update_sandbox_status(record.id, "failed")
        else:
            store.fail_termination(record.id, timestamp=timestamp, error={})
    assert (
        store.get_sandbox(principal.organization_id, record.id).status
        == "running"
    )
    assert descriptor(store, principal, record.id) == old


def test_close_and_watchdog_keep_existing_fake_cleanup_semantics(setup):
    _, store, _, principal, _, _, service = setup
    first = service.create_sandbox(principal, "e2b", {})
    _, other_principal, adapter, second = fresh(setup)
    second.attach_sandbox(other_principal, first.id)
    second.close()
    assert adapter.runtime.destroyed == 1
    assert (
        store.get_sandbox(principal.organization_id, first.id).status
        == "destroyed"
    )
    assert descriptor(store, principal, first.id).descriptor is None
    expiring = service.create_sandbox(principal, "e2b", {"timeout_secs": 1})
    service._now = lambda: datetime.now(timezone.utc) + timedelta(seconds=10)
    assert service.run_watchdog_once()["expired_terminated"] == 1
    assert descriptor(store, principal, expiring.id).descriptor is None


def test_http_attach_contract_auth_csrf_audit_and_no_provider_payload(setup):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from bespokelabs.sandbox.control_plane.api import create_app

    _, store, key, principal, _, _, service = setup
    record = service.create_sandbox(principal, "e2b", {})
    _, _, adapter, second = fresh(setup)
    client = TestClient(create_app(second, admin_token="test-admin"))
    url = f"/v1/sandboxes/{record.id}/attach"
    headers = {"Authorization": f"Bearer {key.secret}"}
    assert client.post(url).status_code == 401
    readonly = store.issue_api_key(
        principal.organization_id, name="Reader", scopes=("sandboxes:read",)
    )
    assert (
        client.post(
            url, headers={"Authorization": f"Bearer {readonly.secret}"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            url, headers=headers, json={"provider_resource_id": "resource-2"}
        ).status_code
        == 422
    )
    rejected = client.post(
        url, headers=headers, json={"credentials": "rejected-value"}
    )
    assert rejected.status_code == 422
    assert "rejected-value" not in rejected.text
    response = client.post(url, headers=headers)
    assert response.status_code == 200
    assert (
        response.json()
        == client.get(f"/v1/sandboxes/{record.id}", headers=headers).json()
    )
    assert "fake-credential" not in response.text
    assert adapter.reconnected == 1
    schema = client.get("/openapi.json").json()
    operation_ids = [
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    assert len(operation_ids) == len(set(operation_ids))
    assert "attach_hosted_sandbox" in operation_ids
    assert any(
        row["action"] == "sandbox.attach" and row["outcome"] == "success"
        for row in store.list_audit(
            principal.organization_id, offset=0, limit=100
        )[0]
    )
    session, csrf, _, _ = store.create_web_session(
        key.secret,
        ttl_seconds=300,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    client.cookies.set("bespoke_dashboard_session", session)
    assert client.post(url).status_code == 403
    assert client.post(url, headers={"X-CSRF-Token": csrf}).status_code == 200
    second.destroy_sandbox(principal, record.id)
    assert client.post(url, headers=headers).status_code == 409
    client.close()


def test_late_allocation_never_overwrites_stopping_authority(setup):
    _, store, _, principal, _, adapter, service = setup
    original = adapter.create

    def stop_during_create(binding, **config):
        record = store.list_sandboxes(principal.organization_id)[0]
        store.begin_termination(
            principal.organization_id,
            record.id,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        return original(binding, **config)

    adapter.create = stop_during_create
    with pytest.raises(SandboxCreationError):
        service.create_sandbox(principal, "e2b", {}, idempotency_key="create-a")
    record = store.list_sandboxes(principal.organization_id)[0]
    assert record.status == "stopping"
    assert record.provider_resource_id == adapter.runtime.provider_resource_id
    assert record.cleanup_status == "unknown"
    assert descriptor(store, principal, record.id).descriptor is None
    assert (
        service.create_sandbox(
            principal, "e2b", {}, idempotency_key="create-a"
        ).status
        == "stopping"
    )
    assert adapter.created == 1


def test_concurrent_execution_key_has_one_execution_and_usage_event(setup):
    _, store, _, principal, _, _, service = setup
    record = service.create_sandbox(principal, "e2b", {})
    _, other_principal, adapter, second = fresh(setup)
    second.attach_sandbox(other_principal, record.id)
    entered = threading.Event()
    release = threading.Event()
    original = adapter.runtime.execute_code

    def blocked(code, language="python"):
        entered.set()
        assert release.wait(10)
        return original(code, language)

    adapter.runtime.execute_code = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            second.execute,
            other_principal,
            record.id,
            code="pass",
            request_id="execute-a",
        )
        assert entered.wait(10)
        try:
            with pytest.raises(ConflictError):
                second.execute(
                    other_principal,
                    record.id,
                    code="pass",
                    request_id="execute-a",
                )
        finally:
            release.set()
        response = future.result(timeout=10)
    assert (
        second.execute(
            other_principal, record.id, code="pass", request_id="execute-a"
        )
        == response
    )
    assert adapter.runtime.executions == 1
    assert adapter.reconnected == 1
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM executions WHERE sandbox_id=?",
                (record.id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM usage_events WHERE sandbox_id=? AND execution_id IS NOT NULL",
                (record.id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM ledger_entries WHERE usage_event_id IN (SELECT id FROM usage_events WHERE sandbox_id=? AND execution_id IS NOT NULL)",
                (record.id,),
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("mode", ["sdk", "unconfigured"])
def test_http_unsupported_attachment_is_sanitized(setup, mode):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from bespokelabs.sandbox.control_plane.api import create_app

    path, _, key, principal, _, _, service = setup
    record = service.create_sandbox(principal, "e2b", {})
    if mode == "sdk":
        _, _, adapter, second = fresh(setup)
        adapter.failure = ImportError("opaque fake-credential failure")
    else:
        second = ControlPlane(
            SQLiteStore(path, key_pepper="test-pepper"),
            sandbox_factory=forbidden_factory,
            resource_sampling_interval_secs=0,
            supervision_interval_secs=0,
        )
    client = TestClient(create_app(second, admin_token="test-admin"))
    response = client.post(
        f"/v1/sandboxes/{record.id}/attach",
        headers={"Authorization": f"Bearer {key.secret}"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "feature_not_supported"
    assert "opaque" not in response.text
    assert "fake-credential" not in response.text
    assert record.id not in second._runtimes
    client.close()
