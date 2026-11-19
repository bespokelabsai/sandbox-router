"""Offline contracts against installed provider SDKs, not invented SDK mocks.

CI sets ATTACHMENT_CONTRACT_PROVIDER: its SDK must import and may not skip.
No credentials, accounts, or provider network access are needed.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import socket
from dataclasses import replace
from importlib.metadata import version
from types import SimpleNamespace
from unittest import mock

import pytest

from bespokelabs.sandbox.control_plane.attachments import (
    AttachmentDescriptor,
    AttachmentError,
    ProviderBinding,
    SDKAttachmentAdapter,
)
from bespokelabs.sandbox.exceptions import ErrorOutcome, SandboxError

_PROVIDER = os.environ.get("ATTACHMENT_CONTRACT_PROVIDER")
_PROVIDERS = (
    [_PROVIDER] if _PROVIDER else ["modal", "daytona", "tensorlake", "e2b"]
)


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def deny(*args, **kwargs):
        raise AssertionError(
            "Provider network access is forbidden in contracts."
        )

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    for key in (
        "MODAL_SERVER_URL",
        "DAYTONA_API_URL",
        "TENSORLAKE_API_URL",
        "TENSORLAKE_SANDBOX_PROXY_URL",
        "E2B_API_URL",
    ):
        monkeypatch.setenv(key, "https://ambient.invalid")
    for key in ("DAYTONA_TARGET", "TENSORLAKE_NAMESPACE"):
        monkeypatch.setenv(key, "ambient")
    for key in (
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "DAYTONA_API_KEY",
        "DAYTONA_JWT_TOKEN",
        "DAYTONA_ORGANIZATION_ID",
        "TENSORLAKE_API_KEY",
        "TENSORLAKE_ORGANIZATION_ID",
        "TENSORLAKE_PROJECT_ID",
        "E2B_API_KEY",
    ):
        monkeypatch.setenv(key, "ambient-placeholder")
    (tmp_path / ".env").write_text(
        "DAYTONA_API_URL=https://dotenv.invalid/api\nDAYTONA_TARGET=dotenv\n"
    )
    # Only this isolated offline test changes cwd; production never does.
    monkeypatch.chdir(tmp_path)


def _sdk(name):
    if _PROVIDER:
        return importlib.import_module(name)
    return pytest.importorskip(name)


def _binding(backend):
    settings = {
        "modal": {"server_url": "https://modal.explicit.invalid"},
        "daytona": {
            "api_url": "https://daytona.explicit.invalid/api",
            "target": "bound",
        },
        "tensorlake": {
            "api_url": "https://tensorlake.explicit.invalid",
            "proxy_url": "https://proxy.explicit.invalid",
            "organization_id": "org-bound",
            "project_id": "project-bound",
            "namespace": "bound",
        },
        "e2b": {},
    }[backend]
    return ProviderBinding(
        id="binding",
        version=1,
        backend=backend,
        account_scope="account",
        organization_ids=["org"],
        settings=settings,
        credentials=(
            {
                "token_id": "token-placeholder",
                "token_secret": "secret-placeholder",
            }
            if backend == "modal"
            else {"api_key": "key-placeholder"}
        ),
    )


@pytest.fixture
def modal_transport(monkeypatch):
    if _PROVIDER and _PROVIDER != "modal":
        pytest.skip("Modal-only lifecycle contract")
    sdk = _sdk("modal")
    floor = version("modal") == "0.60.15"
    assert version("modal") in {"0.60.15", "1.6.4"}
    default_url = (
        "https://api.modal.com"
        if floor
        else "https://api.modal.com,https://api.modal2.com"
    )
    config_module = importlib.import_module("modal.config")
    client_module = importlib.import_module("modal.client")
    async_utils = client_module.async_utils
    monkeypatch.setattr(config_module, "_profile", "contract")
    monkeypatch.setattr(config_module, "_user_config", {})
    monkeypatch.setenv("MODAL_SERVER_URL", default_url)
    channels, opened, exited = [], [], []
    configure_stub = []
    if floor:
        channel_module = client_module
    else:
        channel_module = importlib.import_module("modal._utils.grpc_utils")
    real_channel = channel_module.create_channel

    def channel_offline(server_url, *args, **kwargs):
        # Construct a real, lazy transport but never connect/probe any endpoint.
        channel = real_channel(server_url.split(",")[0], *args, **kwargs)
        monkeypatch.setattr(channel, "close", mock.Mock(wraps=channel.close))
        channels.append(channel)
        return channel

    if floor:
        monkeypatch.setattr(channel_module, "create_channel", channel_offline)
    else:

        async def fallback_offline(*args, **kwargs):
            return channel_offline(*args, **kwargs)

        monkeypatch.setattr(
            channel_module, "create_channel_with_fallbacks", fallback_offline
        )
    real_open = client_module._Client._open
    real_exit = client_module._Client.__aexit__
    close_method = "_close" if floor else "close"
    real_close = getattr(client_module._Client, close_method)

    async def open_spy(client):
        await real_open(client)
        for configure in configure_stub:
            configure(client.stub)
        opened.append(client)

    async def close_spy(client, *args, **kwargs):
        exited.append(client)
        await real_close(client, *args, **kwargs)

    monkeypatch.setattr(client_module._Client, "_open", open_spy)
    monkeypatch.setattr(client_module._Client, close_method, close_spy)
    initial_hooks = set(async_utils._shutdown_tasks)
    binding = replace(_binding("modal"), settings={"server_url": default_url})
    try:
        yield SimpleNamespace(
            sdk=sdk,
            binding=binding,
            opened=opened,
            exited=exited,
            channels=channels,
            hooks=async_utils._shutdown_tasks,
            initial_hooks=initial_hooks,
            floor=floor,
            configure_stub=configure_stub,
        )
    finally:
        # Test isolation only: floor has no public shutdown-hook unregister.
        # Never copy this SDK-internal bookkeeping into the hosted connector.
        async def cleanup():
            for client in opened:
                await real_exit(client, None, None, None)
            added = set(async_utils._shutdown_tasks) - initial_hooks
            for task in added:
                task.cancel()
            await asyncio.gather(*added, return_exceptions=True)
            if floor:
                for task in added:
                    async_utils._shutdown_tasks.remove(task)

        async_utils.synchronizer.create_blocking(cleanup)()


def test_modal_public_factory_context_contract(modal_transport):
    transport = modal_transport
    client = transport.sdk.Client.from_credentials(
        token_id="token-placeholder", token_secret="secret-placeholder"
    )
    assert len(transport.opened) == 1
    assert transport.exited == []
    # Both reviewed SDKs expose this public exit on an already-open client.
    # Entering again would reopen the floor transport, so do not call __enter__.
    client.__exit__(None, None, None)
    assert transport.exited == transport.opened
    transport.channels[0].close.assert_called_once_with()


def test_modal_real_success_retains_factory_client(
    modal_transport, monkeypatch
):
    transport = modal_transport
    terminate = mock.Mock()
    remote = SimpleNamespace(object_id="resource-a", terminate=terminate)
    lookup = mock.create_autospec(
        transport.sdk.Sandbox.from_id, return_value=remote
    )
    monkeypatch.setattr(transport.sdk.Sandbox, "from_id", lookup)
    attached = SDKAttachmentAdapter().reconnect(
        AttachmentDescriptor("modal", "binding", 1, "resource-a"),
        transport.binding,
    )
    assert attached.provider_resource_id == "resource-a"
    assert attached._session._sandbox is remote
    assert len(transport.opened) == 1
    assert transport.exited == []
    assert len(transport.channels) == 1
    transport.channels[0].close.assert_not_called()
    client = lookup.call_args.kwargs["client"]
    if not transport.floor:
        assert not client.is_closed()
    attached.destroy()
    attached.destroy()
    terminate.assert_called_once_with()
    _assert_modal_owned_transport_released(transport)


def _assert_modal_owned_transport_released(transport):
    assert transport.exited == transport.opened
    for channel in transport.channels:
        channel.close.assert_called_once_with()
    if transport.floor:
        # Known unresolved floor capability: public exit closes channels but
        # retains one inert SDK shutdown coroutine per client. Do not waive it.
        assert len(set(transport.hooks) - transport.initial_hooks) == len(
            transport.opened
        )
    else:
        assert set(transport.hooks) == transport.initial_hooks
        for client in transport.opened:
            assert client.is_closed()
            assert client._connection_manager is None
            assert client._cancellation_context is None
            assert client._shutdown_unregister is None


@pytest.mark.parametrize("fail_terminate", [False, True])
def test_modal_real_sdk_terminate_rpc_and_owned_lifetime(
    modal_transport, monkeypatch, fail_terminate
):
    from google.protobuf.empty_pb2 import Empty
    from modal_proto import api_pb2

    transport = modal_transport
    # Use real SDK from_id and terminate methods. Replace only RPC boundaries,
    # before they can contact the lazy transport (also guarded by socket denial).
    terminate = mock.AsyncMock(return_value=Empty())
    if fail_terminate:
        terminate.side_effect = RuntimeError("rpc-secret-placeholder")
    terminal = api_pb2.SandboxWaitResponse(
        result=api_pb2.GenericResult(
            status=api_pb2.GenericResult.GENERIC_STATUS_TERMINATED
        )
    )
    wait = mock.AsyncMock(return_value=terminal)

    def configure(stub):
        monkeypatch.setattr(stub, "SandboxTerminate", terminate)
        monkeypatch.setattr(stub, "SandboxWait", wait)
        if transport.floor:
            lookup = mock.AsyncMock(
                return_value=api_pb2.AppLookupObjectResponse()
            )
            monkeypatch.setattr(stub, "AppLookupObject", lookup)
        else:
            wait.side_effect = [api_pb2.SandboxWaitResponse(), terminal]

    transport.configure_stub.append(configure)
    # A V1-format ID uses the same RPC names in both reviewed SDK generations.
    resource_id = "sb-" + "A" * 22
    attached = SDKAttachmentAdapter().reconnect(
        AttachmentDescriptor("modal", "binding", 1, resource_id),
        transport.binding,
    )
    assert attached.provider_resource_id == resource_id
    assert len(transport.opened) == 1
    assert transport.exited == []
    transport.channels[0].close.assert_not_called()
    terminate.assert_not_awaited()
    if fail_terminate:
        with pytest.raises(SandboxError) as error:
            attached.destroy()
        assert error.value.outcome is ErrorOutcome.UNKNOWN
        assert "placeholder" not in str(error.value)
        assert error.value.__context__ is None
        assert error.value.__cause__ is None
        assert attached.is_alive
        assert attached.provider_resource_id == resource_id
        assert transport.exited == []
        transport.channels[0].close.assert_not_called()
        terminate.side_effect = None
    attached.destroy()
    attached.destroy()
    assert not attached.is_alive
    assert terminate.await_count == (2 if fail_terminate else 1)
    for call in terminate.await_args_list:
        assert call.args[0].sandbox_id == resource_id
    _assert_modal_owned_transport_released(transport)


@pytest.mark.parametrize("cleanup", ["destroy", "close"])
def test_modal_real_control_plane_unknown_termination_keeps_accounting_hold(
    modal_transport, monkeypatch, tmp_path, cleanup
):
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from google.protobuf.empty_pb2 import Empty
    from modal_proto import api_pb2

    from bespokelabs.sandbox.control_plane.service import ControlPlane
    from bespokelabs.sandbox.control_plane.store import SQLiteStore

    transport = modal_transport
    terminate = mock.AsyncMock(
        return_value=Empty(), side_effect=RuntimeError("rpc-secret-placeholder")
    )

    def configure(stub):
        monkeypatch.setattr(stub, "SandboxTerminate", terminate)
        monkeypatch.setattr(
            stub,
            "SandboxWait",
            mock.AsyncMock(return_value=api_pb2.SandboxWaitResponse()),
        )
        if transport.floor:
            monkeypatch.setattr(
                stub,
                "AppLookupObject",
                mock.AsyncMock(return_value=api_pb2.AppLookupObjectResponse()),
            )

    # Install the RPC boundary stubs BEFORE the eager public client factory.
    transport.configure_stub.append(configure)
    store = SQLiteStore(tmp_path / "termination.db", key_pepper="test")
    org = store.create_organization("Modal transport")
    key = store.issue_api_key(org["id"], name="Owner", scopes=("*",))
    principal = store.authenticate(key.secret)
    binding = replace(transport.binding, organization_ids=[org["id"]])
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    service = ControlPlane(
        store,
        allowed_backends={"modal"},
        attachment_bindings=(binding,),
        attachment_adapters={"modal": SDKAttachmentAdapter()},
        now=lambda: clock[0],
        resource_sampling_interval_secs=0,
        supervision_interval_secs=0,
    )
    resource_id = "sb-" + "A" * 22
    record, _ = store.claim_sandbox_creation(
        org["id"],
        "modal",
        {},
        timestamp=clock[0].isoformat(),
        hourly_rate_usd=Decimal(1),
    )
    store.complete_sandbox_creation(
        record.id,
        status="running",
        provider_resource_id=resource_id,
        timestamp=clock[0].isoformat(),
    )
    store.attachment_repository.publish(
        org["id"],
        record.id,
        AttachmentDescriptor("modal", "binding", 1, resource_id),
        expected_generation=0,
    )
    service.attach_sandbox(principal, record.id)
    assert transport.exited == []
    clock[0] += timedelta(seconds=60)
    if cleanup == "close":
        service.close()
    else:
        with pytest.raises(SandboxError) as error:
            service.destroy_sandbox(principal, record.id)
        assert error.value.outcome is ErrorOutcome.UNKNOWN
        assert "placeholder" not in str(error.value)
        assert error.value.__context__ is None
        assert error.value.__cause__ is None
    assert terminate.await_count == 1
    assert terminate.await_args.args[0].sandbox_id == resource_id
    _assert_modal_owned_transport_released(transport)
    failed = store.get_sandbox(org["id"], record.id)
    assert failed.status == "failed"
    assert failed.terminated_at is None
    assert record.id not in service._runtimes
    clock[0] = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    assert service.policy_summary(principal)["usage"]["active_sandboxes"] == 1
    assert Decimal(
        service.cost_summary(principal)[0]["runtime_seconds"]
    ) == Decimal(3600)
    restarted = SQLiteStore(store._path, key_pepper="test")
    assert restarted.resource_is_held(record.id)
    service.close()
    assert terminate.await_count == 1
    _assert_modal_owned_transport_released(transport)


@pytest.mark.parametrize("confirmed", [False, True])
def test_modal_real_disposed_owned_session_stays_strict(
    modal_transport, monkeypatch, confirmed
):
    from google.protobuf.empty_pb2 import Empty
    from modal_proto import api_pb2

    from bespokelabs.sandbox.control_plane.attachments import (
        release_owned_transport,
    )

    transport = modal_transport
    terminate = mock.AsyncMock(return_value=Empty())
    terminal = api_pb2.SandboxWaitResponse(
        result=api_pb2.GenericResult(
            status=api_pb2.GenericResult.GENERIC_STATUS_TERMINATED
        )
    )

    def configure(stub):
        monkeypatch.setattr(stub, "SandboxTerminate", terminate)
        wait = mock.AsyncMock(return_value=terminal)
        monkeypatch.setattr(stub, "SandboxWait", wait)
        if transport.floor:
            monkeypatch.setattr(
                stub,
                "AppLookupObject",
                mock.AsyncMock(return_value=api_pb2.AppLookupObjectResponse()),
            )
        else:
            wait.side_effect = [api_pb2.SandboxWaitResponse(), terminal]

    transport.configure_stub.append(configure)
    resource_id = "sb-" + "A" * 22
    attached = SDKAttachmentAdapter().reconnect(
        AttachmentDescriptor("modal", "binding", 1, resource_id),
        transport.binding,
    )
    session = attached._session
    if confirmed:
        attached.destroy()
    release_owned_transport(attached)
    _assert_modal_owned_transport_released(transport)
    if confirmed:
        session.destroy()
        session.destroy()
        assert session.provider_resource_id is None
        assert terminate.await_count == 1
    else:
        terminate.side_effect = RuntimeError("must-not-call-released-client")
        for _ in range(2):
            with pytest.raises(SandboxError) as error:
                attached.destroy()
            assert error.value.outcome is ErrorOutcome.UNKNOWN
            assert error.value.__cause__ is None
            assert error.value.__context__ is None
        terminate.assert_not_awaited()
        assert attached.is_alive
        assert attached.provider_resource_id == resource_id
    _assert_modal_owned_transport_released(transport)


@pytest.mark.parametrize("failure", ["wrong_id", "wrapper", "adapter"])
def test_modal_real_rejected_reconnect_releases_only_transport(
    modal_transport, monkeypatch, failure
):
    from bespokelabs.sandbox.sandbox import Sandbox

    transport = modal_transport
    terminate = mock.Mock(
        side_effect=AssertionError("Rejected handles must not be terminated")
    )
    remote = SimpleNamespace(
        object_id="wrong-id" if failure == "wrong_id" else "resource-a",
        terminate=terminate,
    )
    lookup = mock.create_autospec(
        transport.sdk.Sandbox.from_id, return_value=remote
    )
    monkeypatch.setattr(transport.sdk.Sandbox, "from_id", lookup)
    if failure == "wrapper":
        monkeypatch.setattr(
            Sandbox,
            "_from_session",
            mock.Mock(side_effect=RuntimeError("wrapper-secret-placeholder")),
        )
    elif failure == "adapter":
        monkeypatch.setattr(
            Sandbox,
            "provider_resource_id",
            mock.PropertyMock(return_value="wrong-id"),
        )
    with pytest.raises(AttachmentError) as error:
        SDKAttachmentAdapter().reconnect(
            AttachmentDescriptor("modal", "binding", 1, "resource-a"),
            transport.binding,
        )
    assert error.value.__context__ is None
    assert "placeholder" not in str(error.value)
    assert len(transport.opened) == 1
    terminate.assert_not_called()
    _assert_modal_owned_transport_released(transport)


def test_modal_real_failed_reconnect_transport_lifetime(
    modal_transport, monkeypatch
):
    transport = modal_transport
    sdk = transport.sdk
    lookup = mock.create_autospec(
        sdk.Sandbox.from_id,
        side_effect=RuntimeError("lookup-secret-placeholder"),
    )
    terminate = mock.Mock(
        side_effect=AssertionError("Unverified handles must not be terminated")
    )
    monkeypatch.setattr(sdk.Sandbox, "from_id", lookup)
    monkeypatch.setattr(sdk.Sandbox, "terminate", terminate)
    descriptor = AttachmentDescriptor("modal", "binding", 1, "resource-a")
    for _ in range(3):
        with pytest.raises(AttachmentError) as error:
            SDKAttachmentAdapter().reconnect(descriptor, transport.binding)
        assert "placeholder" not in str(error.value)
        assert error.value.__context__ is None
    assert len(transport.opened) == 3
    assert transport.exited == transport.opened
    assert len(transport.channels) == 3
    for channel in transport.channels:
        channel.close.assert_called_once_with()
    terminate.assert_not_called()
    if transport.floor:
        # 0.60.15 closes transports but retains its shutdown coroutines even
        # after public context exit. There is no public unregister operation.
        assert len(set(transport.hooks) - transport.initial_hooks) == 3
    else:
        assert set(transport.hooks) == transport.initial_hooks
        for client in transport.opened:
            assert client.is_closed()
            assert client._connection_manager is None
            assert client._cancellation_context is None
            assert client._shutdown_unregister is None


@pytest.mark.parametrize("backend", _PROVIDERS)
def test_real_sdk_attachment_contract(backend, monkeypatch):
    sdk = _sdk(
        {"tensorlake": "tensorlake.sandbox", "e2b": "e2b_code_interpreter"}.get(
            backend, backend
        )
    )
    distribution = "e2b-code-interpreter" if backend == "e2b" else backend
    tested = {
        "modal": {"0.60.15", "1.6.4"},
        "daytona": {"0.207.0", "0.232.0"},
        "tensorlake": {"0.3.12", "0.5.165"},
        "e2b": {"1.0.0", "2.11.10"},
    }
    assert version(distribution) in tested[backend]
    binding = _binding(backend)
    descriptor = AttachmentDescriptor(
        backend, binding.id, binding.version, "resource-a"
    )
    adapter = SDKAttachmentAdapter()
    remote = SimpleNamespace(
        id="resource-a", object_id="resource-a", sandbox_id="resource-a"
    )
    if backend == "e2b":
        # Both tested generations are deliberately disabled in the stock hosted
        # adapter. No GET-then-POST workaround can fence modern auto-resume.
        connect = mock.create_autospec(sdk.Sandbox.connect)
        monkeypatch.setattr(sdk.Sandbox, "connect", connect)
        with pytest.raises(AttachmentError):
            adapter.reconnect(descriptor, binding)
        connect.assert_not_called()
    elif backend == "modal":
        config_module = importlib.import_module("modal.config")
        client_module = importlib.import_module("modal.client")
        default_url = {
            "0.60.15": "https://api.modal.com",
            "1.6.4": "https://api.modal.com,https://api.modal2.com",
        }[version("modal")]
        if version("modal") == "1.6.4":
            assert config_module.DEFAULT_SERVER_URL == default_url
        binding = replace(binding, settings={"server_url": default_url})
        opened = []

        async def open_offline(client):
            # Test-only interception BEFORE the real public factory can perform
            # RPCs (the floor factory eagerly opens). No private runtime path.
            assert client.server_url == default_url
            credentials = getattr(
                client, "_credentials", getattr(client, "credentials", None)
            )
            assert credentials == ("token-placeholder", "secret-placeholder")
            opened.append(client)

        async def close_offline(client, *args, **kwargs):
            pass

        monkeypatch.setattr(client_module._Client, "_open", open_offline)
        if version("modal") == "0.60.15":
            monkeypatch.setattr(client_module._Client, "_close", close_offline)
        public_factory = sdk.Client.from_credentials
        constructor = mock.Mock(
            side_effect=AssertionError(
                "Direct public Client construction is forbidden"
            )
        )
        monkeypatch.setattr(sdk.Client, "__init__", constructor)

        def factory_spy(cls, *, token_id, token_secret):
            assert cls is sdk.Client
            return public_factory(token_id=token_id, token_secret=token_secret)

        factory = mock.Mock(side_effect=factory_spy)
        monkeypatch.setattr(
            sdk.Client, "from_credentials", classmethod(factory)
        )
        from_id = mock.create_autospec(sdk.Sandbox.from_id, return_value=remote)
        monkeypatch.setattr(sdk.Sandbox, "from_id", from_id)
        monkeypatch.setattr(config_module, "_profile", "contract")
        monkeypatch.setattr(config_module, "_user_config", {})
        before_env, before_cwd = dict(os.environ), os.getcwd()
        # Custom ambient routing must be rejected before calling the eager
        # credentials factory, even if the binding requests the SDK default.
        with pytest.raises(AttachmentError):
            adapter.reconnect(descriptor, binding)
        assert dict(os.environ) == before_env
        assert os.getcwd() == before_cwd
        factory.assert_not_called()
        from_id.assert_not_called()
        assert opened == []
        monkeypatch.setenv("MODAL_SERVER_URL", default_url)
        with pytest.raises(AttachmentError):
            adapter.reconnect(
                descriptor,
                replace(
                    binding, settings={"server_url": "https://custom.invalid"}
                ),
            )
        factory.assert_not_called()
        assert opened == []
        # Effective config-file routing is checked too, not just the env var.
        monkeypatch.delenv("MODAL_SERVER_URL")
        monkeypatch.setattr(
            config_module,
            "_user_config",
            {"contract": {"server_url": "https://config.invalid"}},
        )
        before_env = dict(os.environ)
        with pytest.raises(AttachmentError):
            adapter.reconnect(descriptor, binding)
        assert dict(os.environ) == before_env
        assert os.getcwd() == before_cwd
        factory.assert_not_called()
        from_id.assert_not_called()
        assert opened == []
        with monkeypatch.context() as unavailable:
            unavailable.setattr(
                config_module.config,
                "get",
                mock.Mock(side_effect=KeyError("server_url")),
            )
            with pytest.raises(AttachmentError):
                adapter.reconnect(descriptor, binding)
        if version("modal") == "1.6.4":
            with monkeypatch.context() as unavailable:
                unavailable.delattr(config_module, "DEFAULT_SERVER_URL")
                with pytest.raises(AttachmentError):
                    adapter.reconnect(descriptor, binding)
        factory.assert_not_called()
        assert opened == []
        monkeypatch.setattr(config_module, "_user_config", {})
        # No env/config override: the real SDK default and real public factory.
        attached = adapter.reconnect(descriptor, binding)
        factory.assert_called_once_with(
            sdk.Client,
            token_id="token-placeholder",
            token_secret="secret-placeholder",
        )
        assert len(opened) == 1
        client = from_id.call_args.kwargs["client"]
        from_id.assert_called_once_with("resource-a", client=client)
        assert attached.provider_resource_id == "resource-a"
        assert dict(os.environ) == before_env
        assert os.getcwd() == before_cwd
        # An explicitly configured default is allowed, with explicit tokens.
        monkeypatch.setenv("MODAL_SERVER_URL", default_url)
        adapter.reconnect(descriptor, binding)
        assert factory.call_count == 2
        assert len(opened) == 2
        constructor.assert_not_called()
        from_id.side_effect = RuntimeError("provider-secret-placeholder")
        with pytest.raises(AttachmentError) as error:
            adapter.reconnect(descriptor, binding)
        assert "provider-secret-placeholder" not in str(error.value)
        assert error.value.__context__ is None
    elif backend == "daytona":
        client = sdk.Daytona(
            sdk.DaytonaConfig(
                api_key="key-placeholder", **dict(binding.settings)
            )
        )
        assert client._api_url == binding.settings["api_url"]
        assert client._target == "bound"
        assert client._api_key == "key-placeholder"
        assert client._jwt_token is None
        assert client._organization_id is None
        constructor = mock.create_autospec(sdk.Daytona, return_value=client)
        get = mock.create_autospec(client.get, return_value=remote)
        monkeypatch.setattr(client, "get", get)
        monkeypatch.setattr(sdk, "Daytona", constructor)
        attached = adapter.reconnect(descriptor, binding)
        get.assert_called_once_with("resource-a", request_timeout=30)
        assert attached.provider_resource_id == "resource-a"
        get.side_effect = RuntimeError("provider-secret-placeholder")
        with pytest.raises(AttachmentError) as error:
            adapter.reconnect(descriptor, binding)
        assert "provider-secret-placeholder" not in str(error.value)
        assert error.value.__context__ is None
    else:
        # Modern ID verification performs a read-only GET; floor construction
        # is entirely local. Autospec the real public getter, not SDK classes
        # invented for a fake API. No pending wait or resume is invoked.
        get = mock.create_autospec(
            sdk.SandboxClient.get, return_value=SimpleNamespace(value=remote)
        )
        monkeypatch.setattr(sdk.SandboxClient, "get", get)
        attached = adapter.reconnect(descriptor, binding)
        client = attached._session._client
        assert client._api_url == binding.settings["api_url"]
        assert client._api_key == "key-placeholder"
        assert client._organization_id == "org-bound"
        assert client._project_id == "project-bound"
        assert client._namespace == "bound"
        assert attached.provider_resource_id == "resource-a"
        sandbox = attached._session._sandbox
        if version("tensorlake") == "0.5.165":
            assert sandbox._proxy_url == binding.settings["proxy_url"]
            get.assert_called_once()
            assert client._request_timeout == 30
        else:
            assert "proxy.explicit.invalid" in str(sandbox._client.base_url)
            get.assert_not_called()
