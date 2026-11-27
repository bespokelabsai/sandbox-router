"""Credential-free HTTP tests against the real durable repository/service."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from bespokelabs.sandbox.control_plane.job_models import ConfiguredJobBinding
from bespokelabs.sandbox.control_plane.job_service import HostedJobService
from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from bespokelabs.sandbox.types import JobState
from tests.test_hosted_jobs import Harness, logging_response


class HTTPHarness(Harness):

    def __init__(self, path):
        super().__init__(path)
        self.store = SQLiteStore(path, key_pepper="stable-authority-key")
        self.key = self.store.issue_api_key(
            self.org, name="HTTP", scopes=("*",)
        )
        self.other_key = self.store.issue_api_key(
            self.other_org, name="Other", scopes=("*",)
        )
        self.admissions = []
        self.allowed = True
        self.configuration = self.config()
        self.client = self.restart()

    def config(self, **overrides):
        from bespokelabs.sandbox.control_plane.job_api import (
            HostedJobConfiguration,
        )

        values = {
            "bindings": (self.configured(),),
            "submit_bindings": {self.binding.id: self.binding.version},
            "hmac_secret": self.pepper,
            "admit": self.admit,
        }
        values.update(overrides)
        return HostedJobConfiguration(**values)

    def admit(self, principal, binding, spec):
        self.admissions.append((principal, binding, spec))
        return self.allowed

    def restart(self, configuration=None):
        from fastapi.testclient import TestClient

        from bespokelabs.sandbox.control_plane.api import create_app

        # Re-open the authority too: the HTTP host supplies no separate DB path.
        self.store = SQLiteStore(self.path, key_pepper="stable-authority-key")
        self.control = ControlPlane(
            self.store, resource_sampling_interval_secs=0
        )
        return TestClient(
            create_app(
                self.control,
                hosted_job_configuration=configuration or self.configuration,
            )
        )

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.key.secret}"}

    def post(
        self, *, body=None, key="http-operation", client=None, headers=None
    ):
        return (client or self.client).post(
            "/v1/jobs",
            headers={
                **(self.headers if headers is None else headers),
                "Idempotency-Key": key,
            },
            json=body
            if body is not None
            else {
                "binding_id": self.binding.id,
                "spec": {
                    "command": "printf request-command-secret",
                    "env_vars": {"TOKEN": "env-secret"},
                },
            },
        )

    def row(self):
        with self.store.connect() as connection:
            return dict(
                connection.execute("SELECT * FROM hosted_jobs").fetchone()
            )


@pytest.fixture
def h(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    return HTTPHarness(tmp_path / "authority.sqlite")


def test_real_routes_restart_one_dispatch_no_payload_persistence(h):
    response = h.post(key="header-operation-secret")
    assert response.status_code == 202, response.text
    assert response.headers["cache-control"] == "no-store"
    job = response.json()
    assert set(job) == {
        "id",
        "backend",
        "phase",
        "state",
        "confidence",
        "cancel_requested",
        "error_code",
        "result",
    }
    assert job["phase"] == "active" and job["state"] == "running"
    assert h.backend.submits == 1
    restarted = h.restart()
    assert (
        h.post(key="header-operation-secret", client=restarted).json()["id"]
        == job["id"]
    )
    assert h.backend.submits == 1
    row = h.row()
    assert row["organization_id"] == h.org
    serialized = json.dumps(row)
    for secret in (
        "header-operation-secret",
        "request-command-secret",
        "env-secret",
        h.pepper.hex(),
    ):
        assert secret not in serialized
    assert serialized.count("header-operation-secret") == 0
    # Durable foreign keys use the original authority, not a parallel database.
    with h.store.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE hosted_jobs SET organization_id='missing'"
            )
    conflict = h.post(
        key="header-operation-secret",
        body={"binding_id": "batch", "spec": {"command": "different"}},
    )
    assert conflict.status_code == 409
    assert h.backend.submits == 1


def test_http_reservation_precedes_rpc_and_concurrent_replays_dispatch_once(h):
    original = h.backend.submit_job

    def submit(spec):
        row = h.row()
        assert row["phase"] == "submitting"
        assert row["resource_name"] and row["ownership_marker"]
        assert row["organization_id"] == h.org
        return original(spec)

    h.backend.submit_job = submit
    clients = [h.restart() for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(lambda client: h.post(client=client), clients)
        )
    assert all(r.status_code == 202 for r in responses)
    assert len({r.json()["id"] for r in responses}) == 1
    assert h.backend.submits == 1


def test_configuration_is_lazy_and_validation_errors_do_not_reflect_secrets(h):
    def forbidden():
        raise AssertionError("provider-factory-secret")

    binding = replace(h.configured(), adapter_factory=forbidden)
    client = h.restart(h.config(bindings=(binding,)))
    # Startup and denied/invalid requests never need an adapter, SDK or client.
    assert client.get("/healthz").status_code == 200
    invalid = client.post(
        "/v1/jobs?secret=query-secret",
        headers={**h.headers, "Idempotency-Key": "header-secret"},
        content=b'{"binding_id":"batch","spec":{"command":"body-secret",',
    )
    assert invalid.status_code == 422
    unauthorized = h.post(
        client=client,
        headers={"Authorization": "Bearer header-credential-secret"},
    )
    assert unauthorized.status_code == 401
    unavailable = h.post(client=client)
    assert unavailable.status_code == 502
    for value in (
        "provider-factory-secret",
        "query-secret",
        "header-secret",
        "body-secret",
        "header-credential-secret",
    ):
        assert value not in invalid.text + unauthorized.text + unavailable.text
    assert h.backend.submits == 0


@pytest.mark.parametrize("mode", ["lost", "failed"])
def test_uncertain_submission_never_redispatches_after_restart(h, mode):
    if mode == "lost":
        h.backend.lost_response = True
    else:
        h.backend.submit_error = RuntimeError("provider-exception-secret")
    first = h.post()
    assert first.status_code == 202
    second = h.post(client=h.restart())
    assert (
        second.status_code == 202 and second.json()["id"] == first.json()["id"]
    )
    assert h.backend.submits == 1
    assert "provider-exception-secret" not in first.text + second.text
    if mode == "failed":
        assert first.json()["phase"] == "uncertain"
        assert first.json()["result"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"binding_version": 9},
        {"project": "spoof"},
        {"credentials": {"token": "credential-secret"}},
        {"spec": {"command": "x", "backend_options": {"project": "spoof"}}},
        {"spec": {"command": "x", "labels": {"HOSTED-JOB-OWNER": "spoof"}}},
        {"spec": {"command": "x", "cpu": True}},
        {"spec": {"command": "x", "memory_mb": 0}},
        {"spec": {"command": "x", "timeout_secs": 604801}},
        {"spec": {"command": "x", "env_vars": {"invalid-name": "env-secret"}}},
        {
            "spec": {
                "command": "x",
                "backend_options": {"nested": {"value": float("inf")}},
            }
        },
    ],
)
def test_invalid_request_does_not_consume_key(h, change):
    body = {"binding_id": "batch", "spec": {"command": "x"}, **change}
    # Raw JSON allows the deliberately invalid non-finite numeric case.
    response = h.client.post(
        "/v1/jobs",
        headers={**h.headers, "Idempotency-Key": "reusable"},
        content=json.dumps(body),
    )
    assert response.status_code == 422, response.text
    assert h.backend.submits == 0
    assert h.post(key="reusable").status_code == 202
    assert h.backend.submits == 1


_NEW_OPTIONS = {
    "runnable": "script",
    "secret_env_vars": {
        "API_KEY": "projects/example-project/secrets/api-key/versions/latest"
    },
    "boot_disk_image": "projects/example-project/global/images/worker",
    "boot_disk_type": "pd-balanced",
    "machine_type": "g2-standard-8",
    "install_gpu_drivers": True,
    "block_project_ssh_keys": True,
    "instance_labels": {"pool": "gpu"},
}


def _options_body(options, command="#!/bin/bash\necho hello\n"):
    return {
        "binding_id": "batch",
        "spec": {"command": command, "backend_options": options},
    }


def test_admission_sees_the_new_batch_options_and_can_reject_them(h):
    def policy(principal, binding, spec):
        h.admissions.append(spec)
        secrets = spec.backend_options.get("secret_env_vars", {})
        return (
            spec.backend_options.get("runnable") != "script"
            or h.allowed is True
        ) and all("/secrets/api-key/" in ref for ref in secrets.values())

    client = h.restart(h.config(admit=policy))
    accepted = h.post(body=_options_body(_NEW_OPTIONS), client=client)
    assert accepted.status_code == 202, accepted.text
    assert h.admissions[-1].backend_options == _NEW_OPTIONS
    assert h.backend.runtime_spec.backend_options == {
        **_NEW_OPTIONS,
        "project": h.binding.project,
        "region": h.binding.region,
        "job_id": h.backend.runtime_spec.backend_options["job_id"],
    }
    # Neither the script nor the secret reference is stored or returned.
    assert "api-key" not in accepted.text and "echo hello" not in accepted.text
    assert "api-key" not in repr(h.row()) and "echo hello" not in repr(h.row())

    other = {
        **_NEW_OPTIONS,
        "secret_env_vars": {
            "DB": "projects/example-project/secrets/database/versions/1"
        },
    }
    denied = h.post(
        body=_options_body(other), key="other-secret", client=client
    )
    assert denied.status_code == 403
    h.allowed = False
    denied = h.post(
        body=_options_body(_NEW_OPTIONS), key="script", client=client
    )
    assert denied.status_code == 403
    assert h.backend.submits == 1


@pytest.mark.parametrize(
    "options",
    [
        {
            "secret_env_vars": {
                "API_KEY": "projects/other-project/secrets/api-key/versions/1"
            }
        },
        {"secret_env_vars": {"API_KEY": "pasted-secret-value"}},
        {"instance_labels": {"hosted-job-owner": "override"}},
        {"runnable": "script", "project": "other-project"},
        {"runnable": "script", "container_options": "--privileged"},
        {"install_gpu_drivers": True},
        {"block_project_ssh_keys": 1},
    ],
)
def test_invalid_new_batch_options_are_422_and_keep_the_key(h, options):
    response = h.post(body=_options_body(options), key="reusable")
    assert response.status_code == 422, response.text
    assert "pasted-secret-value" not in response.text
    assert h.backend.submits == 0
    with h.store.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM hosted_jobs").fetchone()[0]
            == 0
        )
    assert h.post(key="reusable").status_code == 202


def test_a_script_counts_toward_the_request_size_cap(h):
    fits = h.post(body=_options_body({"runnable": "script"}, "x" * 60_000))
    assert fits.status_code == 202, fits.text
    assert len(h.backend.runtime_spec.command) == 60_000
    large = h.post(
        body=_options_body({"runnable": "script"}, "x" * 70_000), key="large"
    )
    assert large.status_code == 413
    assert h.backend.submits == 1


@pytest.mark.parametrize("key", [None, "", " ", "x" * 513])
def test_required_bounded_header_never_reflects_input(h, key):
    headers = h.headers
    if key is not None:
        headers["Idempotency-Key"] = key
    response = h.client.post(
        "/v1/jobs",
        headers=headers,
        json={"binding_id": "batch", "spec": {"command": "x"}},
    )
    assert response.status_code == 422
    assert h.backend.submits == 0


def test_oversized_body_rejected_before_admission_or_reservation(h):
    response = h.post(
        body={"binding_id": "batch", "spec": {"command": "x" * 65536}}
    )
    assert response.status_code == 413
    assert h.admissions == [] and h.backend.submits == 0
    assert h.post().status_code == 202


def test_admission_denied_and_audit_contains_no_body_header_query_secrets(h):
    h.allowed = False
    response = h.client.post(
        "/v1/jobs?credential=query-secret",
        headers={
            **h.headers,
            "Idempotency-Key": "idempotency-header-secret",
            "X-Private": "other-header-secret",
        },
        json={
            "binding_id": "batch",
            "spec": {
                "command": "command-secret",
                "env_vars": {"TOKEN": "env-secret"},
            },
        },
    )
    assert response.status_code == 403
    assert len(h.admissions) == 1 and h.backend.submits == 0
    audit, _ = h.store.list_audit(h.org, offset=0, limit=100)
    assert (
        audit[0]["action"] == "job.submit" and audit[0]["outcome"] == "denied"
    )
    serialized = json.dumps(audit) + response.text
    for secret in (
        "query-secret",
        "idempotency-header-secret",
        "other-header-secret",
        "command-secret",
        "env-secret",
        h.key.secret,
    ):
        assert secret not in serialized
    h.allowed = True
    assert h.post(key="idempotency-header-secret").status_code == 202


def test_admission_exceptions_and_nonboolean_results_fail_closed(h):
    for result in (None, 1, "allow"):
        client = h.restart(h.config(admit=lambda *_, value=result: value))
        assert h.post(client=client).status_code == 403

    def fail(*_):
        raise RuntimeError("policy-credential-secret")

    response = h.post(client=h.restart(h.config(admit=fail)))
    assert (
        response.status_code == 403
        and "policy-credential-secret" not in response.text
    )
    assert h.backend.submits == 0


@pytest.mark.parametrize(
    "operation,method,suffix",
    [
        ("read", "get", ""),
        ("result", "get", "/result"),
        ("logs", "get", "/logs"),
        ("cancel", "post", "/cancel"),
    ],
)
def test_auth_scopes_and_cross_tenant_404(h, operation, method, suffix):
    job_id = h.post().json()["id"]
    url = f"/v1/jobs/{job_id}{suffix}"
    assert getattr(h.client, method)(url).status_code == 401
    legacy = h.store.issue_api_key(
        h.org, name="Legacy", scopes=("sandboxes:write", "sandboxes:read")
    )
    assert (
        getattr(h.client, method)(
            url, headers={"Authorization": f"Bearer {legacy.secret}"}
        ).status_code
        == 403
    )
    scoped = h.store.issue_api_key(
        h.org, name="Scoped", scopes=(f"jobs:{operation}",)
    )
    assert (
        getattr(h.client, method)(
            url, headers={"Authorization": f"Bearer {scoped.secret}"}
        ).status_code
        == 200
    )
    other_headers = {"Authorization": f"Bearer {h.other_key.secret}"}
    assert (
        getattr(h.client, method)(url, headers=other_headers).status_code == 404
    )
    assert h.backend.submits == 1


def test_submit_auth_scope_and_explicit_empty_binding_tenants(h):
    assert h.post(headers={}).status_code == 401
    key = h.store.issue_api_key(h.org, name="Read", scopes=("jobs:read",))
    assert (
        h.post(headers={"Authorization": f"Bearer {key.secret}"}).status_code
        == 403
    )
    for config in (
        h.config(bindings=(), submit_bindings={}),
        h.config(
            bindings=(replace(h.configured(), organizations=frozenset()),)
        ),
    ):
        assert h.post(client=h.restart(config)).status_code == 403
    assert h.backend.submits == 0


def test_cookie_csrf_submit_and_cancel(h):
    session, csrf, _, _ = h.store.create_web_session(
        h.key.secret, ttl_seconds=3600
    )
    h.client.cookies.set("bespoke_dashboard_session", session)
    denied = h.post(headers={})
    assert denied.status_code == 403
    assert denied.headers["cache-control"] == "no-store"
    response = h.post(headers={"X-CSRF-Token": csrf})
    assert response.status_code == 202
    job_id = response.json()["id"]
    assert h.client.post(f"/v1/jobs/{job_id}/cancel").status_code == 403
    assert (
        h.client.post(
            f"/v1/jobs/{job_id}/cancel", headers={"X-CSRF-Token": csrf}
        ).status_code
        == 200
    )
    assert h.row()["cancel_requested"] == 1
    assert h.backend.cancels == 1  # Explicit CancelJob fake, never delete.
    assert h.row()["phase"] != "terminal"  # Intent is not completion.


def test_terminal_snapshot_dto_allowlist_and_restart_without_provider(h):
    job_id = h.post().json()["id"]
    row = h.row()
    status = h.backend.jobs[row["resource_name"]]
    status.state = JobState.SUCCEEDED
    status.exit_code = 0
    status.reason = "raw-provider-secret"
    status.details.update(
        message="metadata-secret", credentials="credential-secret"
    )
    result = h.client.get(f"/v1/jobs/{job_id}/result", headers=h.headers)
    assert (
        result.status_code == 200 and result.json()["result"]["reason"] is None
    )
    # Even an overpopulated historic snapshot cannot escape the HTTP DTO.
    snapshot = {
        "state": "succeeded",
        "exit_code": 0,
        "reason": "raw-provider-secret",
        "started_at": "date-secret",
        "uid": "uid-secret",
        "metadata": {"credentials": "credential-secret"},
    }
    with h.store.connect() as connection:
        connection.execute(
            "UPDATE hosted_jobs SET terminal_result=?", (json.dumps(snapshot),)
        )
    reads = h.backend.reads
    client = h.restart(h.config(bindings=(), submit_bindings={}))
    response = client.get(f"/v1/jobs/{job_id}", headers=h.headers)
    assert response.status_code == 200
    assert response.json()["result"] == {
        "state": "succeeded",
        "exit_code": 0,
        "reason": None,
        "started_at": None,
        "finished_at": None,
    }
    for value in (
        "raw-provider-secret",
        "date-secret",
        "uid-secret",
        "credential-secret",
        row["resource_name"],
        row["ownership_marker"],
        row["fingerprint"],
        row["binding_json"],
    ):
        assert value not in response.text
    assert h.backend.reads == reads


@pytest.mark.parametrize(
    "tail", ["-1", "1001", "query-secret", "9999999999999999999999999"]
)
def test_log_query_validation_is_bounded_and_redacted(h, tail):
    job_id = h.post().json()["id"]
    response = h.client.get(
        f"/v1/jobs/{job_id}/logs", params={"tail": tail}, headers=h.headers
    )
    assert response.status_code == 422 and "query-secret" not in response.text
    assert h.backend.log_reads == 0
    assert "query-secret" not in json.dumps(
        h.store.list_audit(h.org, offset=0, limit=100)
    )


def test_log_tail_zero_limit_and_failure_redaction(h):
    job_id = h.post().json()["id"]
    for tail in (0, 1000):
        response = h.client.get(
            f"/v1/jobs/{job_id}/logs?tail={tail}", headers=h.headers
        )
        assert response.status_code == 200
        if tail == 0:
            assert response.json() == {"logs": ""}
    h.logging.list_log_entries = lambda **_: logging_response(
        "z" * (2 * 1024 * 1024)
    )
    response = h.client.get(f"/v1/jobs/{job_id}/logs", headers=h.headers)
    assert (
        response.status_code == 200
        and len(response.json()["logs"].encode()) <= 1024 * 1024
    )

    def fail(**_):
        raise RuntimeError("sdk-provider-secret")

    h.logging.list_log_entries = fail
    response = h.client.get(
        f"/v1/jobs/{job_id}/logs?secret=query-secret",
        headers={**h.headers, "X-Private": "header-secret"},
    )
    assert response.status_code == 502
    for value in ("sdk-provider-secret", "query-secret", "header-secret"):
        assert value not in response.text + json.dumps(
            h.store.list_audit(h.org, offset=0, limit=100)
        )


def test_binding_version_rotation_retains_reads_and_cancel(h):
    job_id = h.post().json()["id"]
    old = h.configured()
    new = h.configured(replace(h.binding, version=2))
    client = h.restart(
        h.config(bindings=(old, new), submit_bindings={"batch": 2})
    )
    assert (
        client.get(f"/v1/jobs/{job_id}", headers=h.headers).status_code == 200
    )
    assert (
        client.post(f"/v1/jobs/{job_id}/cancel", headers=h.headers).status_code
        == 200
    )
    removed = h.restart(h.config(bindings=(new,), submit_bindings={"batch": 2}))
    assert (
        removed.get(f"/v1/jobs/{job_id}", headers=h.headers).status_code == 409
    )
    assert h.backend.submits == 1


@pytest.mark.parametrize(
    "organizations", ["org-string", None, {"org": True}, iter(["org"]), [1]]
)
def test_tenant_configuration_requires_real_containers(organizations):
    from bespokelabs.sandbox.control_plane.job_models import JobBinding

    binding = JobBinding(
        "batch", 1, "gcp_batch", "account", "project", "region"
    )
    with pytest.raises(ValueError):
        ConfiguredJobBinding(binding, organizations, lambda: None)


def test_configuration_requires_stable_secret_admission_and_versions(h):
    for overrides in (
        {"hmac_secret": b"short"},
        {"admit": None},
        {"bindings": iter(())},
        {"submit_bindings": {"batch": True}},
        {"submit_bindings": {"batch": 3}},
    ):
        with pytest.raises(ValueError):
            h.config(**overrides)
    with pytest.raises(ValueError):
        HostedJobService(h.repository, iter(()))


def test_default_app_has_no_jobs_and_sdk_import_stays_server_free(h):
    from fastapi.testclient import TestClient

    from bespokelabs.sandbox.control_plane.api import create_app

    client = TestClient(create_app(h.control))
    assert (
        client.post("/v1/jobs", headers=h.headers, json={}).status_code == 404
    )
    assert (
        client.get("/v1/jobs", headers=h.headers).status_code == 404
    )  # No list endpoint.
    script = """
import sys
preloaded = set(sys.modules)  # SDK namespace .pth files can run at startup.
import bespokelabs.sandbox
assert 'fastapi' not in sys.modules
assert 'bespokelabs.sandbox.control_plane.job_api' not in sys.modules
from bespokelabs.sandbox.control_plane.api import create_app
from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import SQLiteStore
import tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as d:
    store = SQLiteStore(Path(d) / 'db', key_pepper='pepper')
    create_app(ControlPlane(store, resource_sampling_interval_secs=0))
    with store.connect() as c:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='hosted_jobs'").fetchone() is None
assert 'bespokelabs.sandbox.control_plane.job_api' not in sys.modules
assert not any(n.split('.')[0] in {'google', 'daytona', 'modal', 'tensorlake', 'e2b_code_interpreter'} for n in set(sys.modules) - preloaded)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(
                __import__("pathlib").Path(__file__).resolve().parents[1]
                / "src"
            ),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


# Cookie authentication is not replaced by the legacy bootstrap admin header.
def job_security_client(h, *, admin_token=None, cookie=True):
    from fastapi.testclient import TestClient

    from bespokelabs.sandbox.control_plane.api import create_app

    client = TestClient(
        create_app(
            h.control,
            admin_token=admin_token,
            hosted_job_configuration=h.configuration,
        )
    )
    session, csrf, session_id, principal = h.store.create_web_session(
        h.key.secret, ttl_seconds=3600
    )
    if cookie:
        client.cookies.set("bespoke_dashboard_session", session)
    return client, csrf, session_id, principal


def job_mutation(h, client, operation, headers):
    if operation == "submit":
        return h.post(client=client, headers=headers)
    return client.post(
        "/v1/jobs/job-id-secret/cancel?private=query-secret",
        headers=headers,
        json={"command": "command-secret", "env": "env-secret"},
    )


@pytest.mark.parametrize("operation", ["submit", "cancel"])
@pytest.mark.parametrize(
    "configured,supplied",
    [
        (None, None),
        (None, "admin-header-secret"),
        ("configured-admin-secret", "wrong-admin-secret"),
        ("configured-admin-secret", "configured-admin-secret"),
    ],
)
def test_job_cookie_csrf_denial_ignores_admin_headers(
    h, operation, configured, supplied
):
    client, _, session_id, principal = job_security_client(
        h, admin_token=configured
    )
    headers = {"X-Private": "private-header-secret"}
    if supplied is not None:
        headers["X-Control-Plane-Admin"] = supplied
    response = job_mutation(h, client, operation, headers)
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert h.admissions == []
    assert (h.backend.submits, h.backend.reads, h.backend.cancels) == (0, 0, 0)
    with h.store.connect() as connection:
        rows = connection.execute("SELECT * FROM audit_log").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["organization_id"] == principal.organization_id == h.org
    assert row["api_key_id"] == principal.api_key_id == h.key.id
    assert row["session_id"] == session_id
    assert row["action"] == f"job.{operation}"
    assert row["resource_type"] == "http_route"
    assert row["resource_id"] == f"job.{operation}"
    assert row["outcome"] == "denied"
    assert json.loads(row["details_json"]) == {"status_code": 403}
    serialized = json.dumps(row) + response.text
    for secret in (
        "job-id-secret",
        "query-secret",
        "request-command-secret",
        "command-secret",
        "env-secret",
        "http-operation",
        "private-header-secret",
        "admin-header-secret",
        "configured-admin-secret",
        "wrong-admin-secret",
        h.key.secret,
    ):
        assert secret not in serialized


@pytest.mark.parametrize("operation", ["submit", "cancel"])
def test_job_cookie_csrf_denial_prevents_existing_job_cancel(h, operation):
    job_id = h.post().json()["id"]
    client, csrf, _, _ = job_security_client(
        h, admin_token="configured-admin-secret"
    )
    headers = {"X-Control-Plane-Admin": "configured-admin-secret"}
    before = (h.backend.submits, h.backend.reads, h.backend.cancels)
    path = "/v1/jobs" if operation == "submit" else f"/v1/jobs/{job_id}/cancel"
    denied = client.post(
        path,
        headers={**headers, "Idempotency-Key": "new-operation"},
        json={"binding_id": h.binding.id, "spec": {"command": "true"}},
    )
    assert denied.status_code == 403
    assert (h.backend.submits, h.backend.reads, h.backend.cancels) == before
    assert h.row()["cancel_requested"] == 0
    allowed = client.post(
        path,
        headers={
            **headers,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "new-operation",
        },
        json={"binding_id": h.binding.id, "spec": {"command": "true"}},
    )
    assert allowed.status_code == (202 if operation == "submit" else 200)
    audit, _ = h.store.list_audit(h.org, offset=0, limit=100)
    assert len(audit) == 3  # Initial submit, one denial, one success.
    assert sum(a["outcome"] == "denied" for a in audit) == 1
    assert sum(a["outcome"] == "success" for a in audit) == 2


@pytest.mark.parametrize("operation", ["submit", "cancel"])
@pytest.mark.parametrize("cookie", [False, True])
@pytest.mark.parametrize("credential", ["valid", "invalid", "wrong-scope"])
def test_job_bearer_authentication_takes_precedence_over_cookie(
    h, operation, cookie, credential
):
    job_id = h.post().json()["id"] if operation == "cancel" else None
    client, _, _, _ = job_security_client(h, cookie=cookie)
    scoped = h.store.issue_api_key(
        h.org, name="Read only", scopes=("jobs:read",)
    )
    secret = {
        "valid": h.key.secret,
        "invalid": "invalid-bearer-secret",
        "wrong-scope": scoped.secret,
    }[credential]
    headers = {"Authorization": f"Bearer {secret}"}
    before = (h.backend.submits, h.backend.reads, h.backend.cancels)
    if operation == "submit":
        response = h.post(client=client, headers=headers)
    else:
        response = client.post(f"/v1/jobs/{job_id}/cancel", headers=headers)
    expected = {
        "valid": 202 if operation == "submit" else 200,
        "invalid": 401,
        "wrong-scope": 403,
    }[credential]
    assert response.status_code == expected
    if credential != "valid":
        assert (h.backend.submits, h.backend.reads, h.backend.cancels) == before
    if credential == "wrong-scope":
        audit, _ = h.store.list_audit(h.org, offset=0, limit=100)
        denied = [a for a in audit if a["outcome"] == "denied"]
        assert len(denied) == 1 and denied[0]["api_key_id"] == scoped.id


@pytest.mark.parametrize("operation", ["submit", "cancel"])
def test_job_configured_admin_header_without_principal_is_unauthorized(
    h, operation
):
    client, _, _, _ = job_security_client(
        h, admin_token="configured-admin-secret", cookie=False
    )
    response = job_mutation(
        h,
        client,
        operation,
        {"X-Control-Plane-Admin": "configured-admin-secret"},
    )
    assert response.status_code == 401
    assert h.admissions == []
    assert (h.backend.submits, h.backend.reads, h.backend.cancels) == (0, 0, 0)


@pytest.mark.parametrize(
    "configured,supplied,expected",
    [
        (None, "arbitrary-admin", 403),
        ("admin", "wrong", 403),
        ("admin", "admin", 201),
    ],
)
def test_only_configured_bootstrap_admin_can_exempt_cookie_csrf(
    h, configured, supplied, expected
):
    client, _, _, _ = job_security_client(h, admin_token=configured)
    response = client.post(
        "/v1/organizations",
        headers={"X-Control-Plane-Admin": supplied},
        json={"name": "Bootstrap"},
    )
    assert response.status_code == expected
    if expected == 201:
        # This header is not an authentication mechanism for principal routes.
        response = client.post(
            "/v1/api-keys",
            headers={"X-Control-Plane-Admin": supplied},
            json={"name": "Must require CSRF", "scopes": ["jobs:read"]},
        )
        assert response.status_code == 403


@pytest.mark.parametrize("operation", ["submit", "cancel"])
def test_job_csrf_denial_resets_request_context_when_auditing_raises(
    h, monkeypatch, operation
):
    from contextvars import ContextVar

    from fastapi.testclient import TestClient

    from bespokelabs.sandbox.control_plane import api

    class TrackingContext:

        def __init__(self):
            self.context = ContextVar("tracked-request-auth")
            self.resets = []

        def set(self, value):
            return self.context.set(value)

        def get(self, default):
            return self.context.get(default)

        def reset(self, token):
            self.context.reset(token)
            self.resets.append(self.context.get(None))

    context = TrackingContext()
    monkeypatch.setattr(api, "ContextVar", lambda _: context)
    app = api.create_app(h.control, hosted_job_configuration=h.configuration)
    client = TestClient(app, raise_server_exceptions=False)
    session, _, _, _ = h.store.create_web_session(
        h.key.secret, ttl_seconds=3600
    )
    client.cookies.set("bespoke_dashboard_session", session)
    calls = []

    def fail_audit(principal, **kwargs):
        calls.append((principal, kwargs))
        raise RuntimeError("audit-storage-secret")

    monkeypatch.setattr(h.control, "record_audit", fail_audit)
    response = job_mutation(h, client, operation, {})
    assert len(calls) == 1
    assert calls[0][1]["action"] == f"job.{operation}"
    assert calls[0][1]["details"] == {"status_code": 403}
    assert context.resets == [None]
    assert response.status_code == 500
    assert "audit-storage-secret" not in response.text
    assert h.admissions == []
    assert (h.backend.submits, h.backend.reads, h.backend.cancels) == (0, 0, 0)
