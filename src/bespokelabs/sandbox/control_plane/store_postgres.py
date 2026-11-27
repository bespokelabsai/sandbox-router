"""PostgreSQL authority store: organizations, API keys, and audit records.

This is the part of the control-plane store the hosted-job path needs. Key
digests use the same keyed scheme as the SQLite store, so a deployment keeps
its pepper when it changes databases. Sandbox, dashboard-session, attachment,
and billing tables are not provided here.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterable

from .errors import AuthenticationError, NotFoundError
from .models import IssuedAPIKey, Principal
from .postgres_support import ConnectionSource, apply_migrations, transaction
from .security import redact_payload
from .store import _id, _normalize_expiration, _now, api_key_digest

_JOURNAL = "authority_schema_migrations"
# Arbitrary fixed key for the advisory lock that serializes organization seeding.
_ORGANIZATION_LOCK = 0x6F72675F73656564

_MIGRATIONS = (
    (
        1,
        (
            """CREATE TABLE organizations (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE api_keys (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id),
                name TEXT NOT NULL,
                key_prefix TEXT NOT NULL,
                key_digest TEXT NOT NULL UNIQUE,
                scopes_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                expires_at TEXT,
                revoked_at TEXT
            )""",
            "CREATE INDEX api_keys_org_idx ON api_keys(organization_id)",
            # No web-session table exists here, so session_id is not a reference.
            """CREATE TABLE audit_log (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id),
                api_key_id TEXT REFERENCES api_keys(id),
                session_id TEXT,
                action TEXT NOT NULL,
                resource_type TEXT,
                resource_id TEXT,
                outcome TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            "CREATE INDEX audit_log_org_created_idx "
            "ON audit_log(organization_id, created_at DESC)",
        ),
    ),
)


class PostgresAuthorityStore:
    """Organization and API-key authority shared by every service instance."""

    def __init__(
        self, connections: ConnectionSource, *, key_pepper: str
    ) -> None:
        if not key_pepper:
            raise ValueError("key_pepper must not be empty")
        self._connections = connections
        self._pepper = key_pepper.encode()
        apply_migrations(connections, _JOURNAL, _MIGRATIONS)

    def create_organization(self, name: str) -> dict[str, str]:
        name = name.strip()
        if not name:
            raise ValueError("organization name must not be empty")
        organization_id = _id("org")
        created_at = _now()
        with transaction(self._connections) as cursor:
            cursor.execute(
                "INSERT INTO organizations(id, name, created_at) "
                "VALUES (%s, %s, %s)",
                (organization_id, name, created_at),
            )
        return {
            "id": organization_id,
            "name": name,
            "created_at": created_at,
        }

    def ensure_organization(self, name: str) -> dict[str, str]:
        """Return the oldest organization with this name, creating it if absent."""
        name = name.strip()
        if not name:
            raise ValueError("organization name must not be empty")
        with transaction(self._connections) as cursor:
            # Names are not unique, so serialize concurrent callers explicitly.
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)", (_ORGANIZATION_LOCK,)
            )
            cursor.execute(
                "SELECT id, name, created_at FROM organizations "
                "WHERE name = %s ORDER BY created_at, id LIMIT 1",
                (name,),
            )
            row = cursor.fetchone()
            if row is not None:
                return dict(row)
            organization = {
                "id": _id("org"),
                "name": name,
                "created_at": _now(),
            }
            cursor.execute(
                "INSERT INTO organizations(id, name, created_at) "
                "VALUES (%s, %s, %s)",
                tuple(organization.values()),
            )
        return organization

    def issue_api_key(
        self,
        organization_id: str,
        *,
        name: str,
        scopes: Iterable[str] = ("*",),
        expires_at: str | None = None,
    ) -> IssuedAPIKey:
        name = name.strip()
        if not name:
            raise ValueError("API-key name must not be empty")
        normalized_scopes = tuple(sorted(set(scopes)))
        if not normalized_scopes:
            raise ValueError("at least one API-key scope is required")
        secret = "bsk_live_" + secrets.token_urlsafe(32)
        prefix = secret[:17]
        key_id = _id("key")
        created_at = _now()
        expires_at = _normalize_expiration(expires_at)
        from psycopg import IntegrityError

        try:
            with transaction(self._connections) as cursor:
                cursor.execute(
                    """INSERT INTO api_keys(
                        id, organization_id, name, key_prefix, key_digest,
                        scopes_json, created_at, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        key_id,
                        organization_id,
                        name,
                        prefix,
                        api_key_digest(self._pepper, secret),
                        json.dumps(normalized_scopes),
                        created_at,
                        expires_at,
                    ),
                )
        except IntegrityError as exc:
            raise NotFoundError("organization not found") from exc
        return IssuedAPIKey(
            id=key_id,
            organization_id=organization_id,
            name=name,
            prefix=prefix,
            scopes=normalized_scopes,
            secret=secret,
            created_at=created_at,
        )

    def list_api_keys(self, organization_id: str) -> list[dict]:
        """List key metadata for one organization; secrets are never stored."""
        with transaction(self._connections) as cursor:
            cursor.execute(
                "SELECT id, name, key_prefix, scopes_json, created_at, "
                "last_used_at, expires_at, revoked_at FROM api_keys "
                "WHERE organization_id = %s ORDER BY created_at",
                (organization_id,),
            )
            rows = cursor.fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "prefix": row["key_prefix"],
                "scopes": json.loads(row["scopes_json"]),
                "created_at": row["created_at"],
                "last_used_at": row["last_used_at"],
                "expires_at": row["expires_at"],
                "revoked_at": row["revoked_at"],
            }
            for row in rows
        ]

    def authenticate(self, secret: str) -> Principal:
        if not secret.startswith("bsk_live_"):
            raise AuthenticationError("invalid API key")
        with transaction(self._connections) as cursor:
            cursor.execute(
                "SELECT id, organization_id, scopes_json, expires_at, "
                "revoked_at FROM api_keys WHERE key_digest = %s",
                (api_key_digest(self._pepper, secret),),
            )
            row = cursor.fetchone()
            if row is None or row["revoked_at"] is not None:
                raise AuthenticationError("invalid API key")
            if row["expires_at"] and row["expires_at"] <= _now():
                raise AuthenticationError("API key has expired")
            cursor.execute(
                "UPDATE api_keys SET last_used_at = %s WHERE id = %s",
                (_now(), row["id"]),
            )
        return Principal(
            organization_id=row["organization_id"],
            api_key_id=row["id"],
            scopes=frozenset(json.loads(row["scopes_json"])),
        )

    def record_audit(
        self,
        organization_id: str,
        *,
        action: str,
        outcome: str,
        created_at: str,
        api_key_id: str | None = None,
        session_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        details: dict | None = None,
    ) -> str:
        """Append a redacted audit record; no update API is exposed."""
        audit_id = _id("aud")
        safe_details = redact_payload(details or {})
        with transaction(self._connections) as cursor:
            cursor.execute(
                """INSERT INTO audit_log(
                   id, organization_id, api_key_id, session_id, action,
                   resource_type, resource_id, outcome, details_json,
                   created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    audit_id,
                    organization_id,
                    api_key_id,
                    session_id,
                    action,
                    resource_type,
                    resource_id,
                    outcome,
                    json.dumps(safe_details, sort_keys=True),
                    created_at,
                ),
            )
        return audit_id
