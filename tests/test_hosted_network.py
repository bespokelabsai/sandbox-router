"""Hosted network policy validation, authorization, persistence and transport."""

from __future__ import annotations

import io
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import timedelta
from unittest import mock

import pytest

from bespokelabs.sandbox import NetworkPolicy, Sandbox
from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from bespokelabs.sandbox.exceptions import (
    ErrorOutcome,
    FeatureNotSupportedError,
    SandboxConfigurationError,
    SandboxTimeoutError,
)
from bespokelabs.sandbox.remote import RemoteSandboxClient, RemoteSandboxError
from tests.test_control_plane import FakeSandbox
from tests.test_remote import FakeResponse

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from bespokelabs.sandbox.control_plane.api import create_app  # noqa: E402


@pytest.fixture
def hosted(tmp_path):
    store = SQLiteStore(tmp_path / "network.db", key_pepper="test-pepper")
    runtimes = []

    def factory(backend, **config):
        runtime = FakeSandbox(backend, **config)
        runtime.set_network = mock.Mock()
        runtimes.append(runtime)
        return runtime

    service = ControlPlane(
        store,
        sandbox_factory=factory,
        allowed_backends={"daytona", "docker", "local"},
        resource_sampling_interval_secs=0,
        supervision_interval_secs=0,
    )
    org, key = service.bootstrap_organization("Network tests")
    with TestClient(
        create_app(service), base_url="https://testserver"
    ) as client:
        yield service, client, org, key, runtimes
    service.close()


def headers(key):
    return {"Authorization": f"Bearer {key.secret}"}


def create(hosted, **config):
    _, client, _, key, _ = hosted
    response = client.post(
        "/v1/sandboxes",
        json={"backend": "daytona", **config},
        headers=headers(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    "policy",
    [
        {"mode": "none"},
        {"mode": "public"},
        {"mode": "allowlist", "domains": ["api.example.com"]},
        {"mode": "allowlist", "cidrs": ["10.0.0.0/8", "2001:db8::/32"]},
    ],
)
def test_create_and_replace_round_trip(hosted, policy):
    _, client, _, key, runtimes = hosted
    record = create(hosted, network=policy)
    assert runtimes[0].config["network"] == NetworkPolicy(**policy)
    assert record["config"]["network"] == {"domains": [], "cidrs": [], **policy}
    response = client.put(
        f"/v1/sandboxes/{record['id']}/network",
        json=policy,
        headers=headers(key),
    )
    assert response.status_code == 200, response.text
    runtimes[0].set_network.assert_called_once_with(NetworkPolicy(**policy))
    updated = client.get(
        f"/v1/sandboxes/{record['id']}", headers=headers(key)
    ).json()
    assert updated["config"]["network"] == record["config"]["network"]
    assert updated["version"] == record["version"] + 1
    audits = client.get("/v1/audit", headers=headers(key)).json()
    assert any(
        row["action"] == "sandbox.set_network" for row in audits["items"]
    )


@pytest.mark.parametrize(
    "policy",
    [
        {"mode": "unknown"},
        {"mode": "allowlist"},
        {"mode": "allowlist", "cidrs": ["bad-cidr"]},
        {"mode": "none", "domains": ["api.example.com"]},
        {"mode": "public", "cidrs": ["10.0.0.0/8"]},
        {"mode": "allowlist", "domains": "api.example.com"},
        {"mode": "none", "typo": True},
        {"mode": "none", "cidrs": None},
        "none",
    ],
)
def test_invalid_policies_never_reach_provider(hosted, policy):
    _, client, _, key, runtimes = hosted
    response = client.post(
        "/v1/sandboxes",
        json={"backend": "daytona", "network": policy},
        headers=headers(key),
    )
    assert response.status_code == 422
    assert runtimes == []
    record = create(hosted)
    response = client.put(
        f"/v1/sandboxes/{record['id']}/network",
        json=policy,
        headers=headers(key),
    )
    assert response.status_code == 422
    runtimes[0].set_network.assert_not_called()


@pytest.mark.parametrize("legacy", [False, True])
def test_create_rejects_conflicting_flags(hosted, legacy):
    _, client, _, key, runtimes = hosted
    response = client.post(
        "/v1/sandboxes",
        json={
            "backend": "daytona",
            "network": {"mode": "none"},
            "allow_internet": legacy,
        },
        headers=headers(key),
    )
    assert response.status_code == 422
    assert runtimes == []


def test_legacy_flag_remains_supported_and_is_replaced_on_update(hosted):
    _, client, _, key, runtimes = hosted
    record = create(hosted, allow_internet=True)
    assert runtimes[0].config["allow_internet"] is True
    response = client.put(
        f"/v1/sandboxes/{record['id']}/network",
        json={"mode": "none"},
        headers=headers(key),
    )
    assert response.status_code == 200
    assert "allow_internet" not in response.json()["config"]
    assert response.json()["config"]["network"]["mode"] == "none"


def test_creation_idempotency_survives_policy_updates(hosted):
    _, client, _, key, runtimes = hosted
    auth = {**headers(key), "Idempotency-Key": "create-once"}
    payload = {"backend": "daytona", "network": {"mode": "none"}}
    record = client.post("/v1/sandboxes", json=payload, headers=auth).json()
    client.put(
        f"/v1/sandboxes/{record['id']}/network",
        json={"mode": "public"},
        headers=headers(key),
    ).raise_for_status()
    retried = client.post("/v1/sandboxes", json=payload, headers=auth)
    assert retried.status_code == 201
    assert retried.json()["id"] == record["id"]
    assert retried.json()["config"]["network"]["mode"] == "public"
    payload["network"] = {"mode": "public"}
    assert (
        client.post("/v1/sandboxes", json=payload, headers=auth).status_code
        == 409
    )
    assert len(runtimes) == 1
    assert runtimes[0].set_network.call_count == 1


def test_update_requires_explicit_scope_and_tenant_ownership(hosted):
    service, client, org, key, runtimes = hosted
    record = create(hosted)
    path = f"/v1/sandboxes/{record['id']}/network"
    assert client.put(path, json={"mode": "none"}).status_code == 401
    for scope in ("sandboxes:read", "sandboxes:execute", "sandboxes:write"):
        limited = service.store.issue_api_key(
            org["id"], name=scope, scopes=[scope]
        )
        assert (
            client.put(
                path, json={"mode": "none"}, headers=headers(limited)
            ).status_code
            == 403
        )
    _, other = service.bootstrap_organization("Other tenant")
    assert (
        client.put(
            path, json={"mode": "none"}, headers=headers(other)
        ).status_code
        == 404
    )
    runtimes[0].set_network.assert_not_called()
    writer = service.store.issue_api_key(
        org["id"], name="Network", scopes=["sandboxes:network"]
    )
    assert (
        client.put(
            path, json={"mode": "none"}, headers=headers(writer)
        ).status_code
        == 200
    )


def test_update_rejects_detached_and_stopped_sandboxes(hosted):
    service, client, _, key, runtimes = hosted
    record = create(hosted)
    path = f"/v1/sandboxes/{record['id']}/network"
    runtime = service._runtimes.pop(record["id"])
    assert (
        client.put(
            path, json={"mode": "none"}, headers=headers(key)
        ).status_code
        == 409
    )
    service._runtimes[record["id"]] = runtime
    client.delete(
        f"/v1/sandboxes/{record['id']}", headers=headers(key)
    ).raise_for_status()
    assert (
        client.put(
            path, json={"mode": "none"}, headers=headers(key)
        ).status_code
        == 409
    )
    runtimes[0].set_network.assert_not_called()


@pytest.mark.parametrize(
    "error,status,code,outcome",
    [
        (
            FeatureNotSupportedError("not supported"),
            422,
            "feature_not_supported",
            "failed",
        ),
        (
            SandboxTimeoutError("secret token", outcome=ErrorOutcome.UNKNOWN),
            504,
            "timeout",
            "unknown",
        ),
        (RuntimeError("secret token"), 502, "execution_failed", "unknown"),
    ],
)
def test_failed_update_preserves_record_and_structured_errors(
    hosted, error, status, code, outcome
):
    _, client, _, key, runtimes = hosted
    record = create(hosted, network={"mode": "none"})
    runtimes[0].set_network.side_effect = error
    response = client.put(
        f"/v1/sandboxes/{record['id']}/network",
        json={"mode": "public"},
        headers=headers(key),
    )
    assert response.status_code == status
    detail = response.json()["detail"]
    assert (
        detail["code"],
        detail["backend"],
        detail["op"],
        detail["outcome"],
    ) == (
        code,
        "daytona",
        "set_network",
        outcome,
    )
    assert "secret token" not in response.text
    assert (
        client.get(f"/v1/sandboxes/{record['id']}", headers=headers(key)).json()
        == record
    )


def test_runtime_without_setter_returns_unsupported(hosted):
    _, client, _, key, runtimes = hosted
    record = create(hosted)
    del runtimes[0].set_network
    response = client.put(
        f"/v1/sandboxes/{record['id']}/network",
        json={"mode": "none"},
        headers=headers(key),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "feature_not_supported"


def test_unsupported_create_uses_sdk_enforcement_before_provisioning(hosted):
    service, client, _, key, runtimes = hosted
    backend = mock.Mock(
        network_modes={"public", "none"}, network_option_keys=set()
    )
    service._sandbox_factory = lambda name, **config: Sandbox(
        name, _backend_client=backend, **config
    )
    response = client.post(
        "/v1/sandboxes",
        json={
            "backend": "docker",
            "network": {"mode": "allowlist", "domains": ["example.com"]},
        },
        headers=headers(key),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "feature_not_supported"
    backend.create.assert_not_called()
    assert not runtimes


def test_cookie_network_updates_require_csrf(hosted):
    _, client, _, key, runtimes = hosted
    record = create(hosted)
    login = client.post("/v1/dashboard/session", json={"api_key": key.secret})
    assert login.status_code == 200
    path = f"/v1/sandboxes/{record['id']}/network"
    assert client.put(path, json={"mode": "none"}).status_code == 403
    runtimes[0].set_network.assert_not_called()
    assert (
        client.put(
            path,
            json={"mode": "none"},
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
        ).status_code
        == 200
    )


def test_remote_client_through_http_api(hosted):
    _, http, _, key, runtimes = hosted

    def urlopen(request, **kwargs):
        response = http.request(
            request.get_method(),
            request.full_url,
            content=request.data,
            headers=dict(request.header_items()),
        )
        if response.is_error:
            raise urllib.error.HTTPError(
                request.full_url,
                response.status_code,
                response.reason_phrase,
                {},
                io.BytesIO(response.content),
            )
        return FakeResponse(response.json())

    with mock.patch(
        "bespokelabs.sandbox.remote.urllib.request.urlopen", side_effect=urlopen
    ) as transport:
        client = RemoteSandboxClient("https://testserver", key.secret)
        policy = NetworkPolicy(mode="allowlist", domains=["api.example.com"])
        with client.create(
            "daytona", network=policy, idempotency_key="remote-create"
        ) as sandbox:
            assert runtimes[0].config["network"] == policy
            sandbox.set_network(NetworkPolicy(mode="none"))
            assert client.get(sandbox.id)["config"]["network"]["mode"] == "none"
            runtimes[0].set_network.side_effect = FeatureNotSupportedError(
                "unsupported"
            )
            with pytest.raises(RemoteSandboxError) as caught:
                sandbox.set_network(policy)
            assert caught.value.status_code == 422
            assert caught.value.code == "feature_not_supported"
            assert caught.value.op == "set_network"
        transport.reset_mock()
        with pytest.raises(RemoteSandboxError, match="destroyed"):
            sandbox.set_network(policy)
        transport.assert_not_called()


def test_remote_client_rejects_wrong_types_and_conflicting_flags():
    with mock.patch(
        "bespokelabs.sandbox.remote.urllib.request.urlopen"
    ) as transport:
        client = RemoteSandboxClient("https://example.com", "key")
        with pytest.raises(SandboxConfigurationError):
            client.create("daytona", network={"mode": "none"})
        with pytest.raises(SandboxConfigurationError):
            client.create(
                "daytona",
                network=NetworkPolicy(mode="none"),
                allow_internet=False,
            )
        transport.assert_not_called()
        transport.return_value = FakeResponse(
            {"id": "sbx", "backend": "daytona"}
        )
        sandbox = client.create("daytona")
        transport.reset_mock()
        with pytest.raises(SandboxConfigurationError):
            sandbox.set_network({"mode": "none"})
        transport.assert_not_called()


@pytest.mark.parametrize("termination", ["api", "shutdown"])
@pytest.mark.parametrize("phase", ["provider", "persistence"])
def test_termination_waits_for_network_update(hosted, termination, phase):
    service, _, org, key, runtimes = hosted
    record = create(hosted, network={"mode": "public"})
    principal = service.store.authenticate(key.secret)
    entered = threading.Event()
    release = threading.Event()
    target, method = (
        (runtimes[0], "set_network")
        if phase == "provider"
        else (service.store, "update_sandbox_network")
    )
    original = getattr(target, method)

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5), "network update was not released"
        return original(*args, **kwargs)

    def stop():
        if termination == "api":
            return service.destroy_sandbox(principal, record["id"])
        return service.close()

    with mock.patch.object(target, method, side_effect=blocked):
        with ThreadPoolExecutor(max_workers=2) as executor:
            update = executor.submit(
                service.set_network,
                principal,
                record["id"],
                NetworkPolicy(mode="none"),
            )
            try:
                assert entered.wait(timeout=2)
                stopped = executor.submit(stop)
                with pytest.raises(TimeoutError):
                    stopped.result(timeout=0.1)
                assert (
                    service.store.get_sandbox(org["id"], record["id"]).status
                    == "running"
                )
                assert not runtimes[0].destroyed
            finally:
                release.set()
            assert update.result(timeout=2).config["network"]["mode"] == "none"
            stopped.result(timeout=2)
    final = service.store.get_sandbox(org["id"], record["id"])
    assert final.status == "destroyed"
    assert final.config["network"]["mode"] == "none"


def test_watchdog_defers_busy_network_update_but_cleans_other_sandboxes(hosted):
    service, _, org, key, runtimes = hosted
    busy = create(hosted, timeout_secs=1, network={"mode": "public"})
    idle = create(hosted, timeout_secs=1)
    principal = service.store.authenticate(key.secret)
    entered = threading.Event()
    release = threading.Event()

    def blocked(policy):
        entered.set()
        assert release.wait(timeout=5)

    runtimes[0].set_network.side_effect = blocked
    with ThreadPoolExecutor(max_workers=1) as executor:
        update = executor.submit(
            service.set_network,
            principal,
            busy["id"],
            NetworkPolicy(mode="none"),
        )
        try:
            assert entered.wait(timeout=2)
            future = service._now() + timedelta(seconds=2)
            service._now = lambda: future
            result = service.run_watchdog_once()
            assert result["expired_claimed"] == 1
            assert result["expired_terminated"] == 1
            assert (
                service.store.get_sandbox(org["id"], busy["id"]).status
                == "running"
            )
            assert (
                service.store.get_sandbox(org["id"], idle["id"]).status
                == "destroyed"
            )
            assert not runtimes[0].destroyed
        finally:
            release.set()
        update.result(timeout=2)
    result = service.run_watchdog_once()
    assert result["expired_terminated"] == 1
    final = service.store.get_sandbox(org["id"], busy["id"])
    assert final.status == "destroyed"
    assert final.config["network"]["mode"] == "none"
