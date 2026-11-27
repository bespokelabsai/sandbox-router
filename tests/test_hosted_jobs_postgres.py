"""Hosted jobs on PostgreSQL: the SQLite expectations plus real concurrency.

Database tests need ``SANDBOX_TEST_POSTGRES_DSN`` and are skipped without it,
so the default run stays offline. Each test works in its own throwaway schema.
"""

from __future__ import annotations

import inspect
import json
import os
import secrets
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict
from unittest.mock import patch

import pytest

from bespokelabs.sandbox.control_plane.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
)
from bespokelabs.sandbox.control_plane.job_interfaces import (
    AuthorityStore,
    JobRepository,
)
from bespokelabs.sandbox.control_plane.job_models import (
    JobPhase,
    ObservationConfidence,
)
from bespokelabs.sandbox.control_plane.job_store import (
    HostedJobRepository,
    canonical,
    hosted_job_hmac,
)
from bespokelabs.sandbox.control_plane.job_store_postgres import (
    PostgresHostedJobRepository,
)
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from bespokelabs.sandbox.control_plane.store_postgres import (
    PostgresAuthorityStore,
)
from tests import test_hosted_jobs as sqlite_suite

DSN_VARIABLE = "SANDBOX_TEST_POSTGRES_DSN"
RACERS = 8


class PostgresHarness(sqlite_suite.Harness):
    """The SQLite suite's harness with both stores swapped for PostgreSQL."""

    def __init__(self, dsn, mode):
        import psycopg

        self.dsn = dsn
        self.schema = "hosted_jobs_test_" + secrets.token_hex(8)
        self.options = f"-c search_path={self.schema}"
        self.pool = None
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(f"CREATE SCHEMA {self.schema}")
        if mode == "pool":
            from psycopg_pool import ConnectionPool

            self.pool = ConnectionPool(
                dsn,
                kwargs={"options": self.options},
                min_size=1,
                max_size=RACERS,
                open=True,
            )
        self.connections = self.pool or self.connect
        self.authority_pepper = secrets.token_hex(32)

        def authority(_path, *, key_pepper):
            self.authority = PostgresAuthorityStore(
                self.connections, key_pepper=self.authority_pepper
            )
            return self.authority

        with patch.object(sqlite_suite, "SQLiteStore", authority):
            super().__init__(None)

    def connect(self):
        import psycopg

        return psycopg.connect(self.dsn, options=self.options)

    def repo(self):
        return PostgresHostedJobRepository(
            self.connections, hosted_job_hmac(self.pepper)
        )

    def rows(self, query, parameters=()):
        with self.connect() as connection:
            return connection.execute(query, parameters).fetchall()

    def close(self):
        import psycopg

        if self.pool is not None:
            self.pool.close()
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute(f"DROP SCHEMA {self.schema} CASCADE")


def _harness(mode):
    dsn = os.environ.get(DSN_VARIABLE)
    if not dsn:
        pytest.skip(f"{DSN_VARIABLE} is not set")
    # With a database configured, a missing driver is a failure, not a skip.
    return PostgresHarness(dsn, mode)


@pytest.fixture(params=["factory", "pool"])
def h(request):
    harness = _harness(request.param)
    yield harness
    harness.close()


@pytest.fixture
def pg():
    harness = _harness("factory")
    yield harness
    harness.close()


def _portable(test) -> bool:
    # Tests that open the SQLite file themselves assert storage details, not
    # repository behaviour; everything else must hold unchanged on PostgreSQL.
    source = inspect.getsource(test)
    return (
        "h" in inspect.signature(test).parameters
        and "sqlite3" not in source
        and "h.path" not in source
    )


_REUSED = {
    name: test
    for name, test in vars(sqlite_suite).items()
    if name.startswith("test_") and callable(test) and _portable(test)
}
globals().update(_REUSED)


def test_sqlite_behaviour_suite_is_reused():
    assert len(_REUSED) >= 30
    assert "test_simultaneous_first_submissions_reserve_once" in _REUSED
    assert "test_expired_lease_and_stale_row_cannot_write_or_redispatch" in (
        _REUSED
    )


def test_both_implementations_satisfy_the_storage_protocols(tmp_path):
    store = SQLiteStore(tmp_path / "authority.sqlite", key_pepper="pepper")
    assert isinstance(store, AuthorityStore)
    assert isinstance(
        HostedJobRepository(store.connect, hosted_job_hmac(b"k" * 32)),
        JobRepository,
    )
    assert issubclass(PostgresAuthorityStore, AuthorityStore)
    assert issubclass(PostgresHostedJobRepository, JobRepository)


def test_postgres_modules_import_without_the_driver():
    script = (
        "import sys\n"
        "sys.modules['psycopg'] = None\n"
        "from bespokelabs.sandbox.control_plane.job_store_postgres import "
        "PostgresHostedJobRepository\n"
        "from bespokelabs.sandbox.control_plane.store_postgres import "
        "PostgresAuthorityStore\n"
        "try:\n"
        "    PostgresAuthorityStore(lambda: None, key_pepper='pepper')\n"
        "except ImportError as exc:\n"
        "    assert 'postgres' in str(exc), exc\n"
        "else:\n"
        "    raise SystemExit('expected ImportError')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_job_routes_require_exactly_one_storage_source(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI

    from bespokelabs.sandbox.control_plane.job_api import (
        HostedJobConfiguration,
        install_job_routes,
    )

    store = SQLiteStore(tmp_path / "authority.sqlite", key_pepper="pepper")
    configuration = HostedJobConfiguration(
        bindings=(),
        submit_bindings={},
        hmac_secret=b"k" * 32,
        admit=lambda *_: False,
    )
    repository = HostedJobRepository(store.connect, hosted_job_hmac(b"k" * 32))
    for chosen_store, chosen_repository in ((None, None), (store, repository)):
        with pytest.raises(ValueError, match="Exactly one"):
            install_job_routes(
                FastAPI(),
                chosen_store,
                configuration,
                lambda *_: None,
                repository=chosen_repository,
            )
    install_job_routes(
        FastAPI(), None, configuration, lambda *_: None, repository=repository
    )


def test_migrations_are_journaled_idempotent_and_need_the_authority(pg):
    journal = "SELECT version FROM hosted_jobs_schema_migrations"
    assert pg.rows(journal) == [(1,)]
    assert pg.rows("SELECT version FROM authority_schema_migrations") == [(1,)]
    applied = pg.rows("SELECT * FROM hosted_jobs_schema_migrations")
    record = pg.submit()
    pg.repo()
    PostgresAuthorityStore(pg.connections, key_pepper=pg.authority_pepper)
    assert pg.rows("SELECT * FROM hosted_jobs_schema_migrations") == applied
    assert pg.repository.get(pg.org, record.id).id == record.id

    import psycopg

    with psycopg.connect(pg.dsn, autocommit=True) as connection:
        connection.execute(f"CREATE SCHEMA {pg.schema}_empty")
    try:
        with pytest.raises(ValueError, match="organization authority"):
            PostgresHostedJobRepository(
                lambda: psycopg.connect(
                    pg.dsn, options=f"-c search_path={pg.schema}_empty"
                ),
                hosted_job_hmac(pg.pepper),
            )
    finally:
        with psycopg.connect(pg.dsn, autocommit=True) as connection:
            connection.execute(f"DROP SCHEMA {pg.schema}_empty CASCADE")


def test_instances_starting_together_migrate_once(pg):
    import psycopg

    schema = pg.schema + "_start"
    options = f"-c search_path={schema}"
    with psycopg.connect(pg.dsn, autocommit=True) as connection:
        connection.execute(f"CREATE SCHEMA {schema}")
    barrier = threading.Barrier(RACERS, timeout=10)

    def start():
        def connect():
            return psycopg.connect(pg.dsn, options=options)

        barrier.wait()
        PostgresAuthorityStore(connect, key_pepper="pepper")
        PostgresHostedJobRepository(connect, hosted_job_hmac(pg.pepper))

    try:
        with ThreadPoolExecutor(max_workers=RACERS) as executor:
            for future in [executor.submit(start) for _ in range(RACERS)]:
                future.result(30)
        with psycopg.connect(pg.dsn, options=options) as connection:
            for journal in (
                "authority_schema_migrations",
                "hosted_jobs_schema_migrations",
            ):
                assert connection.execute(
                    f"SELECT version FROM {journal}"
                ).fetchall() == [(1,)]
    finally:
        with psycopg.connect(pg.dsn, autocommit=True) as connection:
            connection.execute(f"DROP SCHEMA {schema} CASCADE")


def test_authority_keys_use_the_sqlite_digest_scheme(pg, tmp_path):
    sqlite_store = SQLiteStore(
        tmp_path / "authority.sqlite", key_pepper=pg.authority_pepper
    )
    issued = pg.authority.issue_api_key(
        pg.org, name=" Example ", scopes=("jobs:read", "jobs:submit")
    )
    assert issued.name == "Example" and issued.prefix == issued.secret[:17]
    (stored,) = pg.rows(
        "SELECT key_digest FROM api_keys WHERE id=%s", (issued.id,)
    )
    assert stored[0] == sqlite_store._digest(issued.secret)
    assert issued.secret not in json.dumps(pg.rows("SELECT * FROM api_keys"))

    principal = pg.authority.authenticate(issued.secret)
    assert principal.organization_id == pg.org
    assert principal.api_key_id == issued.id
    assert principal.scopes == frozenset({"jobs:read", "jobs:submit"})
    assert pg.rows("SELECT last_used_at FROM api_keys")[0][0]

    other_pepper = PostgresAuthorityStore(pg.connections, key_pepper="another")
    for store, secret in (
        (pg.authority, "not-a-key"),
        (pg.authority, "bsk_live_" + "x" * 43),
        (other_pepper, issued.secret),
    ):
        with pytest.raises(AuthenticationError):
            store.authenticate(secret)
    expired = pg.authority.issue_api_key(
        pg.org, name="Expired", expires_at="2000-01-01T00:00:00+00:00"
    )
    with pytest.raises(AuthenticationError, match="expired"):
        pg.authority.authenticate(expired.secret)
    with pg.connect() as connection:
        connection.execute(
            "UPDATE api_keys SET revoked_at='2000-01-01' WHERE id=%s",
            (issued.id,),
        )
    with pytest.raises(AuthenticationError):
        pg.authority.authenticate(issued.secret)

    with pytest.raises(NotFoundError):
        pg.authority.issue_api_key("org_missing", name="Orphan")
    for call in (
        lambda: pg.authority.create_organization("  "),
        lambda: pg.authority.issue_api_key(pg.org, name=" "),
        lambda: pg.authority.issue_api_key(pg.org, name="None", scopes=()),
        lambda: PostgresAuthorityStore(pg.connections, key_pepper=""),
    ):
        with pytest.raises(ValueError):
            call()


def test_audit_records_are_redacted_and_session_is_not_a_reference(pg):
    audit_id = pg.authority.record_audit(
        pg.org,
        action="jobs.submit",
        outcome="accepted",
        created_at="2024-01-01T00:00:00+00:00",
        session_id="session-from-another-system",
        resource_type="hosted_job",
        resource_id="hjob_example",
        details={"api_key": "bsk_live_example", "note": "kept"},
    )
    ((session_id, details),) = pg.rows(
        "SELECT session_id, details_json FROM audit_log WHERE id=%s",
        (audit_id,),
    )
    assert session_id == "session-from-another-system"
    assert json.loads(details) == {"api_key": "[REDACTED]", "note": "kept"}


def test_racing_reservations_of_one_key_yield_one_job(pg):
    barrier = threading.Barrier(RACERS, timeout=10)
    repositories = [pg.repo() for _ in range(RACERS)]
    fingerprint = "f" * 64

    def reserve(index):
        barrier.wait()
        return repositories[index].reserve(
            pg.org,
            "contended-key",
            fingerprint,
            pg.binding,
            f"resource-{index}",
            f"marker-{index}",
        )

    with ThreadPoolExecutor(max_workers=RACERS) as executor:
        records = list(executor.map(reserve, range(RACERS)))
    assert len({record.id for record in records}) == 1
    assert len(set(records)) == 1
    assert records[0].phase is JobPhase.RESERVED
    assert pg.rows("SELECT count(*) FROM hosted_jobs") == [(1,)]
    # The losers get the SQLite answers: a replay, or a conflict if different.
    assert (
        pg.repository.reserve(
            pg.org, "contended-key", fingerprint, pg.binding, "late", "late"
        )
        == records[0]
    )
    with pytest.raises(ConflictError):
        pg.repository.reserve(
            pg.org, "contended-key", "e" * 64, pg.binding, "late", "late"
        )


def test_reservation_waits_for_an_uncommitted_holder_of_the_key(pg):
    key_digest = pg.repository.digest("submit-key", "held-key")
    fingerprint = "f" * 64
    holder = pg.connect()
    try:
        holder.execute(
            """INSERT INTO hosted_jobs
            (id, organization_id, operation, key_digest, fingerprint,
             binding_json, backend, account, resource_name, ownership_marker,
             phase)
            VALUES ('hjob_holder', %s, 'submit', %s, %s, %s, %s, %s,
                    'resource-holder', 'marker-holder', 'reserved')""",
            (
                pg.org,
                key_digest,
                fingerprint,
                canonical(asdict(pg.binding)),
                pg.binding.backend,
                pg.binding.account,
            ),
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            same = executor.submit(
                pg.repo().reserve,
                pg.org,
                "held-key",
                fingerprint,
                pg.binding,
                "resource-waiter",
                "marker-waiter",
            )
            different = executor.submit(
                pg.repo().reserve,
                pg.org,
                "held-key",
                "e" * 64,
                pg.binding,
                "resource-other",
                "marker-other",
            )
            with pytest.raises(FutureTimeout):
                same.result(0.5)
            assert not different.done()
            holder.commit()
            record = same.result(10)
            with pytest.raises(ConflictError):
                different.result(10)
    finally:
        holder.close()
    assert record.id == "hjob_holder"
    assert record.resource_name == "resource-holder"
    assert pg.rows("SELECT count(*) FROM hosted_jobs") == [(1,)]


def test_racing_claims_grant_one_lease(pg):
    record = pg.submit()
    for round_number in range(5):
        pg.now += 100
        snapshot = pg.repository.get(pg.org, record.id)
        barrier = threading.Barrier(RACERS, timeout=10)
        repositories = [pg.repo() for _ in range(RACERS)]

        def claim(repository, snapshot=snapshot, barrier=barrier):
            barrier.wait()
            return repository.claim_observation(snapshot, pg.now, 10)

        with ThreadPoolExecutor(max_workers=RACERS) as executor:
            claims = list(executor.map(claim, repositories))
        winners = [claim for claim in claims if claim is not None]
        assert len(winners) == 1, round_number
        current = pg.repository.get(pg.org, record.id)
        assert current.lease_token == winners[0].lease_token
        assert current.version == snapshot.version + 1
        # A live lease excludes every later claimant until it expires.
        assert pg.repo().claim_observation(current, pg.now, 10) is None


def test_racing_cancel_and_observation_update_apply_exactly_one(pg):
    for round_number in range(12):
        record = pg.submit(key=f"cancel-race-{round_number}")
        claimed = pg.repository.claim_observation(record, pg.now, 10)
        assert claimed
        barrier = threading.Barrier(2, timeout=10)
        canceller, observer = pg.repo(), pg.repo()

        def cancel(barrier=barrier, repository=canceller, record=record):
            barrier.wait()
            return repository.request_cancel(pg.org, record.id)

        def complete(barrier=barrier, repository=observer, claimed=claimed):
            barrier.wait()
            return repository.complete_observation(
                claimed,
                now=pg.now,
                uid=claimed.uid,
                state="succeeded",
                result={"state": "succeeded"},
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            cancelling = executor.submit(cancel)
            completing = executor.submit(complete)
            returned, applied = cancelling.result(10), completing.result(10)
        final = pg.repository.get(pg.org, record.id)
        assert returned == final
        assert final.version == claimed.version + 1
        if applied:
            # The terminal snapshot won; a late intent must not rewrite it.
            assert final.phase is JobPhase.TERMINAL
            assert final.terminal_result == {"state": "succeeded"}
            assert not final.cancel_requested and final.lease_token is None
        else:
            # The intent won; the observer's stale snapshot wrote nothing.
            assert final.phase is JobPhase.ACTIVE
            assert final.cancel_requested and final.terminal_result is None
            assert final.lease_token == claimed.lease_token


def test_fenced_update_rechecks_after_waiting_on_a_locked_row(pg):
    record = pg.submit()
    claimed = pg.repository.claim_observation(record, pg.now, 10)
    assert claimed
    locker = pg.connect()
    try:
        locker.execute(
            "UPDATE hosted_jobs SET cancel_requested=1, version=version+1 "
            "WHERE id=%s",
            (record.id,),
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            stale = executor.submit(
                pg.repo().complete_observation,
                claimed,
                now=pg.now,
                uid=claimed.uid,
                state="succeeded",
                result={"state": "succeeded"},
            )
            with pytest.raises(FutureTimeout):
                stale.result(0.5)
            locker.commit()
            assert stale.result(10) is False
    finally:
        locker.close()
    final = pg.repository.get(pg.org, record.id)
    assert final.cancel_requested and final.phase is JobPhase.ACTIVE
    assert final.confidence is ObservationConfidence.VERIFIED
    assert final.terminal_result is None


def test_http_routes_on_two_instances_dispatch_once(pg):
    pytest.importorskip("httpx")
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from bespokelabs.sandbox.control_plane.job_api import (
        HostedJobConfiguration,
        install_job_routes,
    )

    key = pg.authority.issue_api_key(pg.org, name="HTTP", scopes=("*",))
    outsider = pg.authority.issue_api_key(
        pg.other_org, name="Other", scopes=("jobs:read",)
    )
    configuration = HostedJobConfiguration(
        bindings=(pg.configured(),),
        submit_bindings={pg.binding.id: pg.binding.version},
        hmac_secret=pg.pepper,
        admit=lambda principal, binding, spec: True,
    )

    def instance():
        authority = PostgresAuthorityStore(
            pg.connections, key_pepper=pg.authority_pepper
        )

        def authenticate(authorization, scope):
            try:
                principal = authority.authenticate(
                    (authorization or "").removeprefix("Bearer ")
                )
            except AuthenticationError:
                raise fastapi.HTTPException(401, "Unauthorized") from None
            if not principal.allows(scope):
                raise fastapi.HTTPException(403, "Forbidden")
            return principal

        app = fastapi.FastAPI()
        install_job_routes(
            app, None, configuration, authenticate, repository=pg.repo()
        )
        return TestClient(app)

    clients = [instance() for _ in range(4)]
    headers = {
        "Authorization": f"Bearer {key.secret}",
        "Idempotency-Key": "http-operation",
    }
    body = {"binding_id": pg.binding.id, "spec": {"command": "printf hello"}}
    barrier = threading.Barrier(len(clients), timeout=10)

    def post(client):
        barrier.wait()
        return client.post("/v1/jobs", headers=headers, json=body)

    with ThreadPoolExecutor(max_workers=len(clients)) as executor:
        responses = list(executor.map(post, clients))
    assert [r.status_code for r in responses] == [202] * 4, responses[0].text
    assert len({r.json()["id"] for r in responses}) == 1
    assert pg.backend.submits == 1
    job_id = responses[0].json()["id"]

    first, second = clients[0], clients[1]
    assert first.get(f"/v1/jobs/{job_id}", headers=headers).json()["phase"] in {
        "active",
        "submitting",
    }
    cancelled = second.post(f"/v1/jobs/{job_id}/cancel", headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["cancel_requested"]
    changed = {**body, "spec": {"command": "different"}}
    assert (
        second.post("/v1/jobs", headers=headers, json=changed).status_code
        == 409
    )
    other = {"Authorization": f"Bearer {outsider.secret}"}
    assert first.get(f"/v1/jobs/{job_id}", headers=other).status_code == 404
    assert first.get(f"/v1/jobs/{job_id}", headers={}).status_code == 401
    assert pg.backend.submits == 1
