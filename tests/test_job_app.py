"""The jobs-only app on SQLite stores, with a fake provider and no network.

Tests taking the ``h`` fixture assert behaviour that must not depend on the
database; ``tests/test_job_app_postgres.py`` reruns them on PostgreSQL.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from bespokelabs.sandbox.control_plane.job_interfaces import (
    AuthorityStore,
    SeedableAuthorityStore,
)
from bespokelabs.sandbox.control_plane.job_store import (
    HostedJobRepository,
    hosted_job_hmac,
)
from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from tests.test_hosted_jobs import Harness

ROOT = Path(__file__).resolve().parents[1]
JOB_ROUTES = {
    ("POST", "/v1/jobs"),
    ("GET", "/v1/jobs/{job_id}"),
    ("GET", "/v1/jobs/{job_id}/result"),
    ("GET", "/v1/jobs/{job_id}/logs"),
    ("POST", "/v1/jobs/{job_id}/cancel"),
}
PEPPER = "stable-authority-key"


class JobAppMixin:
    """HTTP layer over a storage harness; each ``new_app`` is one instance."""

    def start(self):
        self.key = self.authority.issue_api_key(
            self.org, name="Primary", scopes=("*",)
        )
        self.other_key = self.authority.issue_api_key(
            self.other_org, name="Other", scopes=("*",)
        )
        self.admissions = []
        self.allowed = True
        self.client = self.new_client()

    def configuration(self):
        from bespokelabs.sandbox.control_plane.job_api import (
            HostedJobConfiguration,
        )

        return HostedJobConfiguration(
            bindings=(self.configured(),),
            submit_bindings={self.binding.id: self.binding.version},
            hmac_secret=self.pepper,
            admit=self.admit,
        )

    def admit(self, principal, binding, spec):
        self.admissions.append((principal, binding, spec))
        return self.allowed

    def instance_stores(self):
        """Return fresh stores, as a separately started instance would have."""
        return self.new_authority(), self.repo()

    def new_app(self, **options):
        from bespokelabs.sandbox.control_plane.job_app import create_job_app

        authority, repository = self.instance_stores()
        return create_job_app(
            authority=authority,
            repository=repository,
            configuration=self.configuration(),
            **options,
        )

    def new_client(self, **options):
        from fastapi.testclient import TestClient

        return TestClient(self.new_app(**options))

    def bearer(self, issued=None):
        return {"Authorization": f"Bearer {(issued or self.key).secret}"}

    def scoped(self, *scopes):
        return self.bearer(
            self.authority.issue_api_key(self.org, name="Scoped", scopes=scopes)
        )

    def post(self, *, key="operation", headers=None, client=None, body=None):
        return (client or self.client).post(
            "/v1/jobs",
            headers={
                **(self.bearer() if headers is None else headers),
                "Idempotency-Key": key,
            },
            json=body
            if body is not None
            else {
                "binding_id": self.binding.id,
                "spec": {"command": "printf request-command-secret"},
            },
        )


class SQLiteAppHarness(JobAppMixin, Harness):

    def __init__(self, path):
        super().__init__(path)
        self.authority = self.new_authority()
        self.start()

    def new_authority(self):
        return SQLiteStore(self.path, key_pepper=PEPPER)

    def audit(self):
        with sqlite3.connect(self.path) as connection:
            return connection.execute(
                "SELECT organization_id, api_key_id, action, outcome, "
                "resource_type, resource_id, details_json FROM audit_log "
                "ORDER BY created_at, id"
            ).fetchall()


@pytest.fixture
def h(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    return SQLiteAppHarness(tmp_path / "authority.sqlite")


def job_requests(client, job_id, headers):
    return [
        client.get(f"/v1/jobs/{job_id}", headers=headers),
        client.get(f"/v1/jobs/{job_id}/result", headers=headers),
        client.get(f"/v1/jobs/{job_id}/logs", headers=headers),
        client.post(f"/v1/jobs/{job_id}/cancel", headers=headers),
    ]


def test_job_round_trip_with_headers(h):
    submitted = h.post()
    assert submitted.status_code == 202, submitted.text
    job = submitted.json()
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
    assert job["phase"] == "active" and h.backend.submits == 1
    assert h.post().json()["id"] == job["id"] and h.backend.submits == 1
    read, result, logs, cancelled = job_requests(
        h.client, job["id"], h.bearer()
    )
    assert read.json()["id"] == job["id"]
    assert result.json() == {"result": None}
    assert logs.json() == {"logs": "runtime output\n"}
    assert cancelled.json()["cancel_requested"] is True
    for response in (submitted, read, result, logs, cancelled):
        assert response.status_code < 300, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert (
            "default-src 'none'" in response.headers["content-security-policy"]
        )
        assert "max-age" in response.headers["strict-transport-security"]


def test_authentication_is_required_and_failures_are_not_audited(h):
    job_id = h.post().json()["id"]
    before = (h.backend.submits, h.backend.reads, len(h.audit()))
    for headers in (
        {},
        {"Authorization": "Bearer not-a-key"},
        {"Authorization": "Bearer bsk_live_" + "x" * 43},
        {"Authorization": h.key.secret},
        {"X-Control-Plane-Admin": "any-admin-token"},
    ):
        responses = job_requests(h.client, job_id, headers)
        responses.append(h.post(key="unauthenticated", headers=headers))
        for response in responses:
            assert response.status_code == 401, response.text
            assert response.headers["cache-control"] == "no-store"
            assert h.key.secret not in response.text
    assert (h.backend.submits, h.backend.reads, len(h.audit())) == before


def test_each_route_requires_its_own_scope(h):
    job_id = h.post().json()["id"]
    scopes = ["jobs:read", "jobs:result", "jobs:logs", "jobs:cancel"]
    for index, scope in enumerate(scopes):
        responses = job_requests(h.client, job_id, h.scoped(scope))
        assert [r.status_code for r in responses] == [
            200 if position == index else 403 for position in range(4)
        ]
        assert h.post(key="other", headers=h.scoped(scope)).status_code == 403
    submit_only = h.scoped("jobs:submit")
    assert h.post(key="scoped", headers=submit_only).status_code == 202
    assert {
        r.status_code for r in job_requests(h.client, job_id, submit_only)
    } == {403}
    # Sandbox scopes, including the legacy write scope, grant no job access.
    legacy = h.scoped("sandboxes:write", "sandboxes:read", "keys:write")
    assert h.post(key="legacy", headers=legacy).status_code == 403
    assert {r.status_code for r in job_requests(h.client, job_id, legacy)} == {
        403
    }
    assert h.backend.submits == 2


def test_another_organization_cannot_see_or_cancel_a_job(h):
    job_id = h.post().json()["id"]
    reads = (h.backend.reads, h.backend.cancels, h.backend.log_reads)
    responses = job_requests(h.client, job_id, h.bearer(h.other_key))
    for response in responses:
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": "Hosted job not found"}
    assert (h.backend.reads, h.backend.cancels, h.backend.log_reads) == reads
    # The same idempotency key belongs to each organization separately.
    other = h.post(headers=h.bearer(h.other_key))
    assert other.status_code == 202 and other.json()["id"] != job_id
    assert h.client.get(f"/v1/jobs/{job_id}", headers=h.bearer()).json()[
        "cancel_requested"
    ] is (False)


def test_job_requests_are_audited_by_fixed_names(h):
    job_id = h.post(key="audited-operation-key").json()["id"]
    job_requests(h.client, job_id, h.bearer())
    h.client.get(f"/v1/jobs/{job_id}", headers=h.scoped("jobs:logs"))
    h.client.get("/v1/jobs/hjob_missing", headers=h.bearer(h.other_key))
    h.client.get(f"/v1/jobs/{job_id}/unknown", headers=h.bearer())
    h.client.get("/healthz", headers=h.bearer())
    h.client.get("/readyz", headers=h.bearer())
    rows = h.audit()
    assert [(row[2], row[3], json.loads(row[6])) for row in rows] == [
        ("job.submit", "success", {"status_code": 202}),
        ("job.read", "success", {"status_code": 200}),
        ("job.result", "success", {"status_code": 200}),
        ("job.logs", "success", {"status_code": 200}),
        ("job.cancel", "success", {"status_code": 200}),
        ("job.read", "denied", {"status_code": 403}),
        ("job.read", "denied", {"status_code": 404}),
    ]
    assert {row[0] for row in rows[:6]} == {h.org}
    assert rows[6][0] == h.other_org and rows[6][1] == h.other_key.id
    assert {row[1] for row in rows[:5]} == {h.key.id}
    assert all(row[4] == "http_route" and row[5] == row[2] for row in rows)
    serialized = json.dumps(rows)
    for private in (
        job_id,
        h.key.secret,
        "audited-operation-key",
        "request-command-secret",
    ):
        assert private not in serialized


def test_request_limits_and_fixed_error_responses(h):
    oversized = h.client.post(
        "/v1/jobs",
        headers={**h.bearer(), "Idempotency-Key": "large"},
        content=b'{"binding_id": "' + b"a" * (64 * 1024) + b'"}',
    )
    assert oversized.status_code == 413
    invalid = [
        h.post(key="bad", body={"binding_id": h.binding.id}),
        h.post(key="bad", body={"binding_id": h.binding.id, "spec": {}}),
        h.post(key=" "),
        h.client.get("/v1/jobs/x/logs?tail=1001", headers=h.bearer()),
    ]
    assert [r.status_code for r in invalid] == [422] * 4
    unknown = h.post(key="bad", body={"binding_id": "none", "spec": {}})
    assert unknown.status_code == 422
    h.allowed = False
    denied = h.post(key="denied")
    assert denied.status_code == 403
    assert denied.json() == {"detail": "Hosted-job access denied"}
    for response in (oversized, *invalid, denied):
        assert "request-command-secret" not in response.text
        assert response.headers["x-content-type-options"] == "nosniff"
    assert h.backend.submits == 0
    h.allowed = True
    h.backend.submit_error = RuntimeError("provider-internal-detail")
    failed = h.post(key="provider-failure")
    assert "provider-internal-detail" not in failed.text
    assert h.audit()[-1][2] == "job.submit"


def test_health_and_readiness_need_no_credentials(h):
    for path, body in (
        ("/healthz", {"status": "ok"}),
        ("/readyz", {"status": "ready"}),
    ):
        response = h.client.get(path)
        assert response.status_code == 200 and response.json() == body
        assert response.headers["x-frame-options"] == "DENY"
    assert h.backend.reads == 0 and h.audit() == []


def test_readiness_reports_503_without_detail_when_the_check_fails(h):
    def unavailable():
        raise RuntimeError("database-internal-detail")

    client = h.new_client(readiness_check=unavailable)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert client.get("/healthz").status_code == 200


def test_readiness_stops_waiting_for_a_check_that_never_answers(h):
    from fastapi.testclient import TestClient

    entered, release = threading.Event(), threading.Event()
    calls = []

    def stuck():
        calls.append(True)
        entered.set()
        assert release.wait(30)

    app = h.new_app(readiness_check=stuck, readiness_timeout_secs=0.2)
    with TestClient(app) as client:
        try:
            for _ in range(3):
                response = client.get("/readyz")
                assert response.status_code == 503
                assert response.json() == {"status": "unavailable"}
            # Repeated probes share the one stuck check instead of piling up.
            assert entered.is_set() and calls == [True]
            assert client.get("/healthz").status_code == 200
        finally:
            release.set()
        entered.clear()
        assert client.get("/readyz").status_code == 200
        assert len(calls) == 2


def test_only_job_and_health_routes_exist(h):
    app = h.new_app()
    routes = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", ()) - {"HEAD"}
    }
    assert routes == JOB_ROUTES | {("GET", "/healthz"), ("GET", "/readyz")}
    assert all(hasattr(route, "methods") for route in app.routes)
    admin = {**h.bearer(), "X-Control-Plane-Admin": "any-admin-token"}
    for method, path in (
        ("GET", "/"),
        ("GET", "/dashboard"),
        ("GET", "/dashboard/local"),
        ("GET", "/dashboard/assets/dashboard.js"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("GET", "/v1/jobs"),
        ("POST", "/v1/organizations"),
        ("POST", "/v1/api-keys"),
        ("GET", "/v1/api-keys"),
        ("POST", "/v1/dashboard/session"),
        ("GET", "/v1/sandboxes"),
        ("POST", "/v1/sandboxes"),
        ("GET", "/v1/audit"),
    ):
        response = h.client.request(method, path, headers=admin, json={})
        assert response.status_code in {404, 405}, (method, path)
    assert h.client.get("/v1/jobs", headers=admin).status_code == 405


def test_two_instances_share_one_database(h):
    first, second = h.client, h.new_client()
    job_id = h.post(client=first).json()["id"]
    assert h.post(client=second).json()["id"] == job_id
    assert h.backend.submits == 1
    read = second.get(f"/v1/jobs/{job_id}", headers=h.bearer())
    assert read.status_code == 200 and read.json()["phase"] == "active"
    cancelled = second.post(f"/v1/jobs/{job_id}/cancel", headers=h.bearer())
    assert cancelled.status_code == 200
    assert cancelled.json()["cancel_requested"] is True
    seen = first.get(f"/v1/jobs/{job_id}", headers=h.bearer()).json()
    assert seen["cancel_requested"] is True
    # A poll while cancellation is pending may repeat the provider request.
    assert h.backend.cancels >= 1
    assert [row[2] for row in h.audit()] == [
        "job.submit",
        "job.submit",
        "job.read",
        "job.cancel",
        "job.read",
    ]


def test_app_starts_no_thread_and_needs_no_control_plane(h):
    from fastapi.testclient import TestClient

    from bespokelabs.sandbox.control_plane.job_app import create_job_app

    closed = []
    # The host owns its stores (and any pool threads); only the app is checked.
    authority, repository = h.instance_stores()

    def forbidden(*_, **__):
        raise AssertionError("the jobs-only app must not use a control plane")

    with (
        patch.object(ControlPlane, "__init__", forbidden),
        patch.object(ControlPlane, "start_supervision", forbidden),
        patch.object(threading.Thread, "start", forbidden),
    ):
        app = create_job_app(
            authority=authority,
            repository=repository,
            configuration=h.configuration(),
            on_shutdown=lambda: closed.append(True),
        )
    before = set(threading.enumerate())
    with patch.object(ControlPlane, "__init__", forbidden):
        with TestClient(app) as client:
            assert h.post(client=client).status_code == 202
            assert client.get("/readyz").status_code == 200
            assert closed == []
    assert closed == [True]
    # Only the server's own request workers may outlive the requests.
    assert all(
        "anyio" in thread.name.lower() or "asyncio" in thread.name.lower()
        for thread in set(threading.enumerate()) - before
        if thread.is_alive()
    )


def test_seeding_is_idempotent_and_shows_the_key_once(h):
    from bespokelabs.sandbox.control_plane.job_seed import (
        JOB_SCOPES,
        seed_job_access,
    )

    authority = h.new_authority()
    first = seed_job_access(authority, organization=" Seeded ", key_name="ci")
    again = seed_job_access(authority, organization="Seeded", key_name=" ci ")
    assert first.issued is not None and again.issued is None
    assert first.organization_id == again.organization_id
    assert first.api_key_id == again.api_key_id == first.issued.id
    assert first.organization_id not in {h.org, h.other_org}
    keys = authority.list_api_keys(first.organization_id)
    assert [(key["name"], key["scopes"]) for key in keys] == [
        ("ci", sorted(JOB_SCOPES))
    ]
    assert first.issued.secret not in json.dumps(keys)
    second = seed_job_access(
        authority,
        organization="Seeded",
        key_name="reader",
        scopes=["jobs:read"],
    )
    assert second.organization_id == first.organization_id
    assert second.issued.scopes == ("jobs:read",)
    assert seed_job_access(
        h.new_authority(), organization="Seeded", key_name="ci"
    ) == (again)
    for call in (
        lambda: seed_job_access(authority, organization=" ", key_name="ci"),
        lambda: seed_job_access(authority, organization="Seeded", key_name=""),
        lambda: seed_job_access(
            authority, organization="Seeded", key_name="none", scopes=()
        ),
    ):
        with pytest.raises(ValueError):
            call()

    # The seeded key works on a running instance, limited to its scopes and
    # to the bindings its organization was given (none here).
    headers = h.bearer(first.issued)
    missing = h.client.get("/v1/jobs/hjob_missing", headers=headers)
    assert missing.status_code == 404
    assert h.post(headers=headers).status_code == 403
    reader = h.bearer(second.issued)
    assert h.post(headers=reader).status_code == 403
    assert h.backend.submits == 0


def test_seed_command_prints_a_new_key_exactly_once(
    tmp_path, monkeypatch, capsys
):
    from bespokelabs.sandbox.control_plane import job_seed

    database = tmp_path / "authority.sqlite"
    (tmp_path / "seed_host_example.py").write_text(
        "from bespokelabs.sandbox.control_plane.store import SQLiteStore\n"
        f"authority = SQLiteStore({str(database)!r}, key_pepper={PEPPER!r})\n"
        "app = object()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    arguments = [
        "seed_host_example:authority",
        "--organization",
        "Seeded",
        "--key-name",
        "ci",
        "--scope",
        "jobs:read",
    ]
    assert job_seed.main(arguments) == 0
    first = dict(
        line.split("=", 1) for line in capsys.readouterr().out.splitlines()
    )
    assert set(first) == {"organization_id", "api_key_id", "api_key"}
    principal = SQLiteStore(database, key_pepper=PEPPER).authenticate(
        first["api_key"]
    )
    assert principal.organization_id == first["organization_id"]
    assert principal.scopes == frozenset({"jobs:read"})

    assert job_seed.main(arguments) == 0
    repeated = capsys.readouterr()
    assert "api_key=" not in repeated.out and "bsk_live_" not in repeated.out
    assert f"api_key_id={first['api_key_id']}" in repeated.out
    assert "already exists" in repeated.err
    for target in ("seed_host_example", "seed_host_example:app"):
        with pytest.raises(SystemExit) as raised:
            job_seed.main([target, "--organization", "x", "--key-name", "y"])
        assert raised.value.code not in (0, None)


def test_default_readiness_makes_a_real_database_round_trip(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from bespokelabs.sandbox.control_plane.job_api import (
        HostedJobConfiguration,
    )
    from bespokelabs.sandbox.control_plane.job_app import create_job_app

    store = SQLiteStore(tmp_path / "authority.sqlite", key_pepper=PEPPER)
    assert isinstance(store, AuthorityStore)
    assert isinstance(store, SeedableAuthorityStore)
    connections = []
    available = [True]

    def connect():
        if not available[0]:
            raise sqlite3.OperationalError("unable to open database file")
        connections.append(True)
        return store.connect()

    secret = b"k" * 32
    configuration = HostedJobConfiguration(
        bindings=(),
        submit_bindings={},
        hmac_secret=secret,
        admit=lambda *_: False,
    )
    client = TestClient(
        create_job_app(
            authority=store,
            repository=HostedJobRepository(connect, hosted_job_hmac(secret)),
            configuration=configuration,
        )
    )
    opened = len(connections)
    assert client.get("/healthz").status_code == 200
    assert len(connections) == opened
    assert client.get("/readyz").status_code == 200
    assert len(connections) == opened + 1
    available[0] = False
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert client.get("/healthz").json() == {"status": "ok"}
    with pytest.raises(ValueError, match="configuration"):
        create_job_app(authority=store, repository=None, configuration=None)


def test_service_image_is_unprivileged_and_never_published():
    dockerfile = (ROOT / "images/service/Dockerfile").read_text()
    assert '"/build[server,postgres,gcp-batch]"' in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "--host 0.0.0.0" in dockerfile and "${PORT:-8080}" in dockerfile
    assert "APP_MODULE" in dockerfile
    workflow = (ROOT / ".github/workflows/build-images.yml").read_text()
    assert workflow.count('. != "service"') == 2
    assert "images/service is not published" in workflow
