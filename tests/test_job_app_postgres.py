"""The jobs-only app on PostgreSQL stores, including several instances.

Database tests need ``SANDBOX_TEST_POSTGRES_DSN`` and are skipped without it.
The storage-independent cases of ``tests/test_job_app.py`` are rerun here.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import pytest

from bespokelabs.sandbox.control_plane.job_interfaces import (
    SeedableAuthorityStore,
)
from bespokelabs.sandbox.control_plane.job_store import hosted_job_hmac
from bespokelabs.sandbox.control_plane.job_store_postgres import (
    PostgresHostedJobRepository,
)
from bespokelabs.sandbox.control_plane.models import Principal
from bespokelabs.sandbox.control_plane.store_postgres import (
    PostgresAuthorityStore,
)
from bespokelabs.sandbox.types import JobSpec
from tests import test_job_app as sqlite_suite
from tests.test_hosted_jobs_postgres import DSN_VARIABLE, PostgresHarness

ROOT = Path(__file__).resolve().parents[1]
INSTANCES = 4


class PostgresAppHarness(sqlite_suite.JobAppMixin, PostgresHarness):

    def __init__(self, dsn, mode):
        self.instance_pools = []
        super().__init__(dsn, mode)
        self.start()

    def instance_source(self):
        """Return one instance's connections; pools are never shared."""
        if self.pool is None:
            return self.connect
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            self.dsn,
            kwargs={"options": self.options},
            min_size=1,
            max_size=4,
            open=True,
        )
        self.instance_pools.append(pool)
        return pool

    def instance_stores(self):
        source = self.instance_source()
        return (
            PostgresAuthorityStore(source, key_pepper=self.authority_pepper),
            PostgresHostedJobRepository(source, hosted_job_hmac(self.pepper)),
        )

    def new_authority(self):
        return PostgresAuthorityStore(
            self.connections, key_pepper=self.authority_pepper
        )

    def audit(self):
        return self.rows(
            "SELECT organization_id, api_key_id, action, outcome, "
            "resource_type, resource_id, details_json FROM audit_log "
            "ORDER BY created_at, id"
        )

    def close(self):
        for pool in self.instance_pools:
            pool.close()
        super().close()


def _dsn():
    dsn = os.environ.get(DSN_VARIABLE)
    if not dsn:
        pytest.skip(f"{DSN_VARIABLE} is not set")
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    return dsn


@pytest.fixture(params=["factory", "pool"])
def h(request):
    harness = PostgresAppHarness(_dsn(), request.param)
    yield harness
    harness.close()


@pytest.fixture
def pg():
    harness = PostgresAppHarness(_dsn(), "pool")
    yield harness
    harness.close()


_REUSED = {
    name: test
    for name, test in vars(sqlite_suite).items()
    if name.startswith("test_")
    and callable(test)
    and "h" in inspect.signature(test).parameters
}
globals().update(_REUSED)


def test_sqlite_app_suite_is_reused():
    assert len(_REUSED) >= 11
    assert "test_two_instances_share_one_database" in _REUSED
    assert "test_another_organization_cannot_see_or_cancel_a_job" in _REUSED
    assert issubclass(PostgresAuthorityStore, SeedableAuthorityStore)


def test_instance_a_submits_and_instance_b_reads_and_cancels(pg):
    instance_a, instance_b = pg.new_client(), pg.new_client()
    assert pg.instance_pools[-1] is not pg.instance_pools[-2]
    submitted = pg.post(client=instance_a, key="shared-operation")
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["id"]

    read = instance_b.get(f"/v1/jobs/{job_id}", headers=pg.bearer())
    assert read.status_code == 200 and read.json()["phase"] == "active"
    result = instance_b.get(f"/v1/jobs/{job_id}/result", headers=pg.bearer())
    assert result.json() == {"result": None}
    logs = instance_b.get(f"/v1/jobs/{job_id}/logs", headers=pg.bearer())
    assert logs.json() == {"logs": "runtime output\n"}
    cancelled = instance_b.post(
        f"/v1/jobs/{job_id}/cancel", headers=pg.bearer()
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["cancel_requested"] is True
    # A replay on the other instance is the same job, not a second dispatch.
    assert pg.post(client=instance_b, key="shared-operation").json()["id"] == (
        job_id
    )
    assert pg.backend.submits == 1
    other = instance_b.get(
        f"/v1/jobs/{job_id}", headers=pg.bearer(pg.other_key)
    )
    assert other.status_code == 404
    assert pg.rows("SELECT count(*) FROM hosted_jobs") == [(1,)]
    assert pg.rows(
        "SELECT cancel_requested FROM hosted_jobs WHERE id=%s", (job_id,)
    ) == [(1,)]
    actions = [row[2] for row in pg.audit()]
    assert actions.count("job.submit") == 2 and "job.cancel" in actions


def test_instances_racing_one_idempotency_key_dispatch_once(pg):
    clients = [pg.new_client() for _ in range(INSTANCES)]
    barrier = threading.Barrier(INSTANCES, timeout=10)

    def submit(client):
        barrier.wait()
        return pg.post(client=client, key="contended-operation")

    with ThreadPoolExecutor(max_workers=INSTANCES) as executor:
        responses = list(executor.map(submit, clients))
    # A loser may see the winner mid-dispatch, but never a second job.
    accepted = [r for r in responses if r.status_code == 202]
    assert accepted and {r.status_code for r in responses} <= {202, 409}
    assert len({r.json()["id"] for r in accepted}) == 1
    assert pg.backend.submits == 1
    assert pg.rows("SELECT count(*) FROM hosted_jobs") == [(1,)]


def test_readiness_fails_when_the_database_is_unreachable(pg):
    import psycopg
    from fastapi.testclient import TestClient

    from bespokelabs.sandbox.control_plane.job_app import create_job_app

    reachable = [True]

    def connect():
        if reachable[0]:
            return pg.connect()
        # A port nothing listens on: a real driver failure, not a stub.
        return psycopg.connect(
            "postgresql://unused@127.0.0.1:1/unused", connect_timeout=2
        )

    client = TestClient(
        create_job_app(
            authority=PostgresAuthorityStore(
                connect, key_pepper=pg.authority_pepper
            ),
            repository=PostgresHostedJobRepository(
                connect, hosted_job_hmac(pg.pepper)
            ),
            configuration=pg.configuration(),
        )
    )
    assert client.get("/readyz").json() == {"status": "ready"}
    reachable[0] = False
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert client.get("/healthz").status_code == 200
    reachable[0] = True
    assert client.get("/readyz").status_code == 200


def test_seeding_from_two_processes_creates_one_organization(pg):
    from bespokelabs.sandbox.control_plane.job_seed import seed_job_access

    barrier = threading.Barrier(INSTANCES, timeout=10)

    def ensure(_):
        authority = PostgresAuthorityStore(
            pg.connect, key_pepper=pg.authority_pepper
        )
        barrier.wait()
        return authority.ensure_organization("Seeded together")["id"]

    with ThreadPoolExecutor(max_workers=INSTANCES) as executor:
        identifiers = set(executor.map(ensure, range(INSTANCES)))
    assert len(identifiers) == 1
    assert pg.rows(
        "SELECT count(*) FROM organizations WHERE name=%s",
        ("Seeded together",),
    ) == [(1,)]
    seeded = seed_job_access(
        pg.authority, organization="Seeded together", key_name="ci"
    )
    assert {seeded.organization_id} == identifiers
    assert pg.authority.authenticate(seeded.issued.secret).api_key_id == (
        seeded.api_key_id
    )


def test_example_host_module_serves_on_postgres(pg, monkeypatch):
    pytest.importorskip("psycopg_pool")
    from fastapi.testclient import TestClient

    from bespokelabs.sandbox.control_plane.job_seed import seed_job_access

    separator = "&" if "?" in pg.dsn else "?"
    environment = {
        "JOBS_DATABASE_URL": (
            f"{pg.dsn}{separator}options={quote(pg.options)}"
        ),
        "JOBS_API_KEY_PEPPER": pg.authority_pepper,
        "JOBS_HMAC_SECRET_HEX": secrets.token_hex(32),
        "JOBS_BATCH_PROJECT": "example-project",
        "JOBS_BATCH_REGION": "us-central1",
        "JOBS_BATCH_ACCOUNT": "jobs-service@example-project.example",
        "JOBS_ALLOWED_ORGANIZATIONS": f"{pg.org}, ",
        "JOBS_ALLOWED_IMAGE_PREFIX": "registry.example/approved/",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    specification = importlib.util.spec_from_file_location(
        "jobs_service_host_example", ROOT / "examples/jobs_service_host.py"
    )
    host = importlib.util.module_from_spec(specification)
    monkeypatch.setitem(sys.modules, specification.name, host)
    specification.loader.exec_module(host)
    try:
        with TestClient(host.app) as client:
            assert client.get("/healthz").json() == {"status": "ok"}
            assert client.get("/readyz").json() == {"status": "ready"}
            seeded = seed_job_access(
                host.authority, organization="Example", key_name="ci"
            )
            headers = {"Authorization": f"Bearer {seeded.issued.secret}"}
            missing = client.get("/v1/jobs/hjob_missing", headers=headers)
            assert missing.status_code == 404
            # The seeded organization is not on the binding's allowlist.
            denied = client.post(
                "/v1/jobs",
                headers={**headers, "Idempotency-Key": "example"},
                json={"binding_id": "batch", "spec": {"command": "true"}},
            )
            assert denied.status_code == 403
            assert pg.rows("SELECT count(*) FROM hosted_jobs") == [(0,)]
        assert host.pool.closed

        allowed = Principal(pg.org, "key", frozenset({"jobs:submit"}))
        good = JobSpec("true", image="registry.example/approved/tool:1")
        assert host.admit(allowed, host.BINDING, good) is True
        stranger = Principal(pg.other_org, "key", frozenset({"*"}))
        for principal, spec in (
            (stranger, good),
            (allowed, JobSpec("true")),
            (allowed, JobSpec("true", image="registry.example/other/tool")),
            (allowed, JobSpec("true", image=good.image, cpu=64)),
            (allowed, JobSpec("true", image=good.image, memory_mb=65536)),
            (allowed, JobSpec("true", image=good.image, gpu="any")),
            (allowed, JobSpec("true", image=good.image, timeout_secs=86400)),
            (
                allowed,
                JobSpec(
                    "true", image=good.image, backend_options={"spot": True}
                ),
            ),
        ):
            assert host.admit(principal, host.BINDING, spec) is False
    finally:
        host.pool.close()
