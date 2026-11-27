"""PostgreSQL hosted-job repository, safe for several service instances.

The caller supplies a connection factory or pool for the database that owns
the ``organizations`` table, and the same server-secret HMAC function every
instance uses. Transitions are the shared ones from the SQLite repository; only
the SQL differs. Connections are expected at the default READ COMMITTED level.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Callable

from .errors import ConflictError, NotFoundError
from .job_models import HostedJob, JobBinding
from .job_store import HostedJobRepositoryBase, canonical, check_column_names
from .postgres_support import (
    ConnectionSource,
    apply_migrations,
    sql_module,
    transaction,
)

_JOURNAL = "hosted_jobs_schema_migrations"

_MIGRATIONS = (
    (
        1,
        (
            """CREATE TABLE hosted_jobs (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id),
                operation TEXT NOT NULL CHECK(operation = 'submit'),
                key_digest TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                binding_json TEXT NOT NULL,
                backend TEXT NOT NULL,
                account TEXT NOT NULL,
                resource_name TEXT NOT NULL,
                ownership_marker TEXT NOT NULL,
                phase TEXT NOT NULL CHECK(phase IN
                    ('reserved','submitting','uncertain','active','terminal')),
                version BIGINT NOT NULL DEFAULT 1,
                uid TEXT,
                state TEXT,
                confidence TEXT NOT NULL DEFAULT 'unobserved',
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                terminal_result TEXT,
                error_code TEXT,
                lease_token TEXT,
                lease_until DOUBLE PRECISION,
                submit_deadline DOUBLE PRECISION,
                UNIQUE(organization_id, operation, key_digest),
                UNIQUE(backend, account, resource_name)
            )""",
        ),
    ),
)


class PostgresHostedJobRepository(HostedJobRepositoryBase):

    def __init__(
        self,
        connections: ConnectionSource,
        hmac_function: Callable[[bytes], str],
    ) -> None:
        self._connections = connections
        self._hmac = hmac_function
        apply_migrations(
            connections,
            _JOURNAL,
            _MIGRATIONS,
            requires_table="organizations",
            missing_message=(
                "Hosted jobs require the organization authority database"
            ),
        )

    def get(self, organization_id: str, job_id: str) -> HostedJob:
        with transaction(self._connections) as cursor:
            cursor.execute(
                "SELECT * FROM hosted_jobs WHERE organization_id=%s AND id=%s",
                (organization_id, job_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise NotFoundError("Hosted job not found")
            return self._decode(row)

    def reserve(
        self,
        organization_id: str,
        key: str,
        fingerprint: str,
        binding: JobBinding,
        resource_name: str,
        marker: str,
    ) -> HostedJob:
        key_digest = self.digest("submit-key", key)
        binding_json = canonical(asdict(binding))
        with transaction(self._connections) as cursor:
            cursor.execute(
                "SELECT 1 FROM organizations WHERE id=%s", (organization_id,)
            )
            if cursor.fetchone() is None:
                raise NotFoundError("Organization not found")
            # A concurrent reservation of this key blocks here until the other
            # transaction ends, then inserts nothing; the read below runs in a
            # new statement snapshot and so sees the winner's committed row.
            cursor.execute(
                """INSERT INTO hosted_jobs
                (id, organization_id, operation, key_digest, fingerprint,
                 binding_json, backend, account, resource_name,
                 ownership_marker, phase)
                VALUES (%s, %s, 'submit', %s, %s, %s, %s, %s, %s, %s, 'reserved')
                ON CONFLICT (organization_id, operation, key_digest)
                DO NOTHING""",
                (
                    "hjob_" + uuid.uuid4().hex,
                    organization_id,
                    key_digest,
                    fingerprint,
                    binding_json,
                    binding.backend,
                    binding.account,
                    resource_name,
                    marker,
                ),
            )
            cursor.execute(
                "SELECT * FROM hosted_jobs WHERE organization_id=%s "
                "AND operation='submit' AND key_digest=%s",
                (organization_id, key_digest),
            )
            row = cursor.fetchone()
            if row is None:
                # Only reachable above READ COMMITTED; never invent a job.
                raise ConflictError("Hosted-job reservation is not visible")
            if (
                row["fingerprint"] != fingerprint
                or row["binding_json"] != binding_json
            ):
                raise ConflictError("Idempotency key has a different request")
            return self._decode(row)

    def _update(self, record: HostedJob, changes: dict) -> bool:
        """Fence version, lease, UID and full binding in one atomic statement.

        A concurrent writer of the same row makes this statement wait and then
        re-check its predicate, so at most one of two racing updates applies.
        """
        check_column_names(changes)
        sql = sql_module()
        assignments = sql.SQL(", ").join(
            sql.SQL("{}=%s").format(sql.Identifier(key)) for key in changes
        )
        with transaction(self._connections) as cursor:
            cursor.execute(
                sql.SQL(
                    "UPDATE hosted_jobs SET {}, version=version+1 "
                    "WHERE id=%s AND organization_id=%s AND version=%s "
                    "AND uid IS NOT DISTINCT FROM %s "
                    "AND lease_token IS NOT DISTINCT FROM %s "
                    "AND binding_json=%s AND resource_name=%s "
                    "AND phase=%s AND phase != 'terminal'"
                ).format(assignments),
                (
                    *changes.values(),
                    record.id,
                    record.organization_id,
                    record.version,
                    record.uid,
                    record.lease_token,
                    canonical(asdict(record.binding)),
                    record.resource_name,
                    record.phase.value,
                ),
            )
            return cursor.rowcount == 1
