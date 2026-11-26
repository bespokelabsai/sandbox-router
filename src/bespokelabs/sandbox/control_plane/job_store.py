"""SQLite hosted-job repository sharing the organization authority database.

The caller supplies fresh connections and a stable, server-secret HMAC
function. No file paths, credentials, commands, or environment values are
retained here. Dispatch authorization is an irreversible compare-and-swap.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from typing import Callable, Iterator

from .errors import ConflictError, NotFoundError
from .job_models import (
    HostedJob,
    JobBinding,
    JobPhase,
    ObservationConfidence,
)

_COLUMN_NAME = re.compile(r"[a-z_]+")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS hosted_jobs_schema_migrations (
        version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS hosted_jobs (
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
        version INTEGER NOT NULL DEFAULT 1,
        uid TEXT,
        state TEXT,
        confidence TEXT NOT NULL DEFAULT 'unobserved',
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        terminal_result TEXT,
        error_code TEXT,
        lease_token TEXT,
        lease_until REAL,
        submit_deadline REAL,
        UNIQUE(organization_id, operation, key_digest),
        UNIQUE(backend, account, resource_name)
    )""",
)


def canonical(value: object) -> str:
    """Strict deterministic JSON; non-finite numbers are not fingerprints."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class HostedJobRepository:

    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
        hmac_function: Callable[[bytes], str],
    ) -> None:
        self._connection_factory = connection_factory
        self._hmac = hmac_function
        with self._transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='organizations'"
            ).fetchone():
                raise ValueError(
                    "Hosted jobs require the organization authority database"
                )
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute(
                "INSERT OR IGNORE INTO hosted_jobs_schema_migrations VALUES (1, CURRENT_TIMESTAMP)"
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(hosted_jobs)")
            }
            if "submit_deadline" not in columns:
                connection.execute(
                    "ALTER TABLE hosted_jobs ADD COLUMN submit_deadline REAL"
                )
            connection.execute(
                "INSERT OR IGNORE INTO hosted_jobs_schema_migrations VALUES (2, CURRENT_TIMESTAMP)"
            )

    @contextmanager
    def _transaction(
        self, *, write: bool = True
    ) -> Iterator[sqlite3.Connection]:
        connection = self._connection_factory()
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def digest(self, domain: str, value: object) -> str:
        digest = self._hmac(
            canonical(["hosted-jobs-v1", domain, value]).encode()
        )
        if not isinstance(digest, str) or not re.fullmatch(
            r"[a-f0-9]{64}", digest
        ):
            raise ValueError("HMAC function must return a SHA-256 hex digest")
        return digest

    @staticmethod
    def _decode(row: sqlite3.Row) -> HostedJob:
        return HostedJob(
            id=row["id"],
            organization_id=row["organization_id"],
            binding=JobBinding(**json.loads(row["binding_json"])),
            resource_name=row["resource_name"],
            ownership_marker=row["ownership_marker"],
            phase=JobPhase(row["phase"]),
            version=row["version"],
            fingerprint=row["fingerprint"],
            uid=row["uid"],
            state=row["state"],
            confidence=ObservationConfidence(row["confidence"]),
            cancel_requested=bool(row["cancel_requested"]),
            terminal_result=json.loads(row["terminal_result"])
            if row["terminal_result"]
            else None,
            error_code=row["error_code"],
            lease_token=row["lease_token"],
            lease_until=row["lease_until"],
            submit_deadline=row["submit_deadline"],
        )

    def get(self, organization_id: str, job_id: str) -> HostedJob:
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT * FROM hosted_jobs WHERE organization_id=? AND id=?",
                (organization_id, job_id),
            ).fetchone()
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
        with self._transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM organizations WHERE id=?", (organization_id,)
            ).fetchone():
                raise NotFoundError("Organization not found")
            row = connection.execute(
                "SELECT * FROM hosted_jobs WHERE organization_id=? AND operation='submit' AND key_digest=?",
                (organization_id, key_digest),
            ).fetchone()
            if row is not None:
                if row["fingerprint"] != fingerprint or row[
                    "binding_json"
                ] != canonical(asdict(binding)):
                    raise ConflictError(
                        "Idempotency key has a different request"
                    )
                return self._decode(row)
            job_id = "hjob_" + uuid.uuid4().hex
            connection.execute(
                """INSERT INTO hosted_jobs
                (id, organization_id, operation, key_digest, fingerprint,
                 binding_json, backend, account, resource_name, ownership_marker, phase)
                VALUES (?, ?, 'submit', ?, ?, ?, ?, ?, ?, ?, 'reserved')""",
                (
                    job_id,
                    organization_id,
                    key_digest,
                    fingerprint,
                    canonical(asdict(binding)),
                    binding.backend,
                    binding.account,
                    resource_name,
                    marker,
                ),
            )
            row = connection.execute(
                "SELECT * FROM hosted_jobs WHERE id=?", (job_id,)
            ).fetchone()
            return self._decode(row)

    def _update(self, record: HostedJob, changes: dict) -> bool:
        """Every mutable write fences version, lease, UID and full binding.

        Terminal snapshots are write-once. Only this repository constructs
        column names, and each must be a plain lower-case identifier;
        values never become SQL.
        """
        for key in changes:
            if not _COLUMN_NAME.fullmatch(key):
                raise ValueError(f"not a hosted_jobs column name: {key!r}")
        with self._transaction() as connection:
            assignments = ", ".join(f"{key}=?" for key in changes)
            return (
                connection.execute(
                    f"UPDATE hosted_jobs SET {assignments}, version=version+1 "
                    "WHERE id=? AND organization_id=? AND version=? AND uid IS ? "
                    "AND lease_token IS ? AND binding_json=? AND resource_name=? "
                    "AND phase=? AND phase != 'terminal'",
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
                ).rowcount
                == 1
            )

    def start_dispatch(
        self, record: HostedJob, now: float, duration: float
    ) -> bool:
        if record.phase is not JobPhase.RESERVED or record.cancel_requested:
            return False
        return self._update(
            record,
            {
                "phase": JobPhase.SUBMITTING.value,
                "submit_deadline": now + duration,
            },
        )

    def request_cancel(self, organization_id: str, job_id: str) -> HostedJob:
        while True:
            record = self.get(organization_id, job_id)
            if record.cancel_requested or record.phase is JobPhase.TERMINAL:
                return record
            changes: dict = {"cancel_requested": 1}
            if record.phase is JobPhase.RESERVED:
                changes.update(
                    phase=JobPhase.TERMINAL.value,
                    state="cancelled",
                    confidence=ObservationConfidence.VERIFIED.value,
                    terminal_result=canonical(
                        {
                            "state": "cancelled",
                            "exit_code": None,
                            "reason": "cancelled_before_dispatch",
                        }
                    ),
                )
            if self._update(record, changes):
                return self.get(organization_id, job_id)

    def claim_observation(
        self, record: HostedJob, now: float, duration: float
    ) -> HostedJob | None:
        if record.phase in {JobPhase.RESERVED, JobPhase.TERMINAL}:
            return None
        if record.lease_until is not None and record.lease_until > now:
            return None
        token = uuid.uuid4().hex
        if not self._update(
            record, {"lease_token": token, "lease_until": now + duration}
        ):
            return None
        claimed = self.get(record.organization_id, record.id)
        return claimed if claimed.lease_token == token else None

    def dispatch_unconfirmed(self, organization_id: str, job_id: str) -> None:
        while True:
            record = self.get(organization_id, job_id)
            if record.phase is not JobPhase.SUBMITTING:
                return
            if self._update(
                record,
                {
                    "phase": JobPhase.UNCERTAIN.value,
                    "confidence": ObservationConfidence.UNAVAILABLE.value,
                    "error_code": "submit_unconfirmed",
                },
            ):
                return

    def complete_observation(
        self,
        record: HostedJob,
        *,
        now: float,
        uid: str | None = None,
        state: str | None = None,
        result: dict | None = None,
        confidence: ObservationConfidence = ObservationConfidence.VERIFIED,
        error_code: str | None = None,
    ) -> bool:
        if record.lease_until is None or record.lease_until <= now:
            return False
        if record.uid is not None and uid is not None and record.uid != uid:
            return False
        # A later provider read must not regress an observed running job.
        if record.state == "running" and state == "pending":
            state = "running"
        pending_submit = (
            record.phase is JobPhase.SUBMITTING
            and record.submit_deadline is not None
            and now < record.submit_deadline
            and confidence
            in {
                ObservationConfidence.MISSING,
                ObservationConfidence.UNAVAILABLE,
            }
        )
        return self._update(
            record,
            {
                "phase": (
                    JobPhase.SUBMITTING.value
                    if pending_submit
                    else JobPhase.TERMINAL.value
                    if result is not None
                    else JobPhase.ACTIVE.value
                    if confidence is ObservationConfidence.VERIFIED
                    else JobPhase.UNCERTAIN.value
                ),
                "uid": uid or record.uid,
                "state": state or record.state,
                "terminal_result": canonical(result)
                if result is not None
                else None,
                "confidence": record.confidence.value
                if pending_submit
                else confidence.value,
                "error_code": record.error_code
                if pending_submit
                else error_code,
                "lease_token": None,
                "lease_until": None,
            },
        )

    def release_observation(self, record: HostedJob) -> bool:
        return self._update(record, {"lease_token": None, "lease_until": None})
