"""Strict durable runtime handles and tenant-authorized, lazy reconnection.

This module is opt-in: importing it does not import optional provider SDKs.
Descriptors are not SDK session-state payloads and are never accepted as such.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from importlib.metadata import version as sdk_version
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from bespokelabs.sandbox.control_plane.errors import AuthorizationError
from bespokelabs.sandbox.control_plane.models import Principal
from bespokelabs.sandbox.types import SandboxSessionState

_SUPPORTED = frozenset({"modal", "e2b", "daytona", "tensorlake"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,254}\Z")
_WORKDIR = re.compile(r"/[A-Za-z0-9_./-]*\Z")
_FIELDS = frozenset(
    {
        "schema_version",
        "backend",
        "binding_id",
        "binding_version",
        "provider_resource_id",
        "remote_workdir",
        "incarnation",
    }
)


class AttachmentError(RuntimeError):
    """Safe public attachment failure; messages never include input values."""


class InvalidDescriptor(AttachmentError):
    """Descriptor is not a supported, strictly allowlisted document."""


class AttachmentConflict(AttachmentError):
    """Lifecycle or attachment changed; caller must reload durable authority."""


def _identifier(value: object) -> bool:
    return type(value) is str and _IDENTIFIER.fullmatch(value) is not None


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


@dataclass(frozen=True)
class AttachmentDescriptor:
    """Versioned public identity only, with no credential or endpoint fields."""

    backend: str
    binding_id: str
    binding_version: int
    provider_resource_id: str
    remote_workdir: str | None = None
    incarnation: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.backend) is not str or self.backend not in _SUPPORTED:
            raise InvalidDescriptor(
                "Backend does not support durable attachment."
            )
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise InvalidDescriptor("Unsupported attachment schema version.")
        if not all(
            _identifier(value)
            for value in (self.binding_id, self.provider_resource_id)
        ) or not _positive(self.binding_version):
            raise InvalidDescriptor("Invalid attachment identity.")
        if self.incarnation is not None and not _identifier(self.incarnation):
            raise InvalidDescriptor("Invalid attachment incarnation.")
        workdir = self.remote_workdir
        if workdir is not None:
            if (
                type(workdir) is not str
                or len(workdir) > 512
                or not _WORKDIR.fullmatch(workdir)
                or any(part in {".", ".."} for part in workdir.split("/"))
                or "//" in workdir
                or self.backend not in {"daytona", "tensorlake"}
            ):
                raise InvalidDescriptor("Invalid remote working directory.")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: object) -> AttachmentDescriptor:
        required = _FIELDS - {"remote_workdir", "incarnation"}
        if (
            type(value) is not dict
            or not required <= set(value)
            or not set(value) <= _FIELDS
        ):
            raise InvalidDescriptor("Invalid attachment fields.")
        return cls(**value)

    @classmethod
    def from_json(cls, value: str) -> AttachmentDescriptor:
        def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise InvalidDescriptor("Duplicate attachment fields.")
                result[key] = item
            return result

        if type(value) is not str or len(value) > 4096:
            raise InvalidDescriptor("Invalid attachment document.")
        try:
            parsed = json.loads(value, object_pairs_hook=unique_pairs)
        except (ValueError, RecursionError):
            raise InvalidDescriptor("Invalid attachment document.") from None
        return cls.from_dict(parsed)


@dataclass(frozen=True)
class ProviderBinding:
    """Server configuration, never populated from a request or a DB record.

    ``account_scope`` identifies the configured provider account. Operators
    must bump ``version`` whenever credentials, account, or settings change.
    Organization membership is checked on every use, including cache hits.
    """

    id: str
    version: int
    backend: str
    account_scope: str
    organization_ids: frozenset[str] | set[str] | tuple[str, ...] | list[str]
    credentials: Mapping[str, str] = field(repr=False)
    settings: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if (
            not _identifier(self.id)
            or not _positive(self.version)
            or type(self.backend) is not str
            or self.backend not in _SUPPORTED
            or not _identifier(self.account_scope)
            or type(self.organization_ids) not in (list, tuple, frozenset, set)
            or not self.organization_ids
            or not all(_identifier(org) for org in self.organization_ids)
        ):
            raise AttachmentError("Invalid provider binding.")
        credential_keys = (
            {"token_id", "token_secret"}
            if self.backend == "modal"
            else {"api_key"}
        )
        setting_keys = {
            "modal": {"server_url"},
            "e2b": set(),
            "daytona": {"api_url", "target"},
            "tensorlake": {
                "api_url",
                "proxy_url",
                "organization_id",
                "project_id",
                "namespace",
            },
        }[self.backend]
        if (
            not isinstance(self.credentials, Mapping)
            or not isinstance(self.settings, Mapping)
            or set(self.credentials) != credential_keys
            or set(self.settings) != setting_keys
            or not all(
                type(value) is str and value
                for value in (
                    *self.credentials.values(),
                    *self.settings.values(),
                )
            )
        ):
            raise AttachmentError("Invalid provider binding configuration.")
        for key, value in self.settings.items():
            if key.endswith("_url"):
                try:
                    url = urlsplit(value)
                    valid = (
                        url.scheme == "https"
                        and bool(url.hostname)
                        and url.username is None
                        and url.password is None
                        and not url.query
                        and not url.fragment
                        and not any(char.isspace() for char in value)
                        and url.port != 0
                    )
                except ValueError:
                    valid = False
                if not valid:
                    raise AttachmentError("Invalid binding routing endpoint.")
            elif not _identifier(value):
                raise AttachmentError("Invalid binding routing scope.")
        object.__setattr__(
            self, "organization_ids", frozenset(self.organization_ids)
        )
        object.__setattr__(
            self, "credentials", MappingProxyType(dict(self.credentials))
        )
        object.__setattr__(
            self, "settings", MappingProxyType(dict(self.settings))
        )


class BindingRegistry:
    """Memory-only secrets with optional shared, nonsecret version authority.

    Without ``attach_repository`` revocation is explicitly process-local.
    Hosted services always attach their ownership repository before use.
    """

    def __init__(self, bindings: tuple[ProviderBinding, ...] = ()) -> None:
        self._lock = threading.RLock()
        self._bindings: dict[str, ProviderBinding] = {}
        self._versions: dict[str, int] = {}
        self._repository: AttachmentRepository | None = None
        for binding in bindings:
            self.configure(binding)

    @staticmethod
    def _metadata(binding: ProviderBinding) -> tuple:
        # Deliberately exclude credentials, including hashes of credentials.
        metadata = json.dumps(
            {
                "settings": dict(binding.settings),
                "organization_ids": sorted(binding.organization_ids),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            binding.version,
            binding.backend,
            binding.account_scope,
            hashlib.sha256(metadata.encode()).hexdigest(),
        )

    @staticmethod
    def _persist(conn: sqlite3.Connection, binding: ProviderBinding) -> None:
        metadata = BindingRegistry._metadata(binding)
        row = conn.execute(
            "SELECT version, backend, account_scope, metadata_digest, state "
            "FROM cp_provider_bindings_v1 WHERE id=?",
            (binding.id,),
        ).fetchone()
        if row is not None and (
            row[0] > binding.version
            or (
                row[0] == binding.version
                and tuple(row) != (*metadata, "active")
            )
        ):
            raise AttachmentError("Binding authority requires a newer version.")
        conn.execute(
            "INSERT INTO cp_provider_bindings_v1 VALUES (?, ?, ?, ?, ?, 'active') "
            "ON CONFLICT(id) DO UPDATE SET version=excluded.version, "
            "backend=excluded.backend, account_scope=excluded.account_scope, "
            "metadata_digest=excluded.metadata_digest, state='active'",
            (binding.id, *metadata),
        )

    def attach_repository(self, repository: AttachmentRepository) -> None:
        with repository.transaction() as conn, self._lock:
            if self._repository is not None:
                raise AttachmentError("Binding authority is already attached.")
            for binding in self._bindings.values():
                self._persist(conn, binding)
        self._repository = repository

    @contextmanager
    def _transaction(self, connection=None):
        if self._repository is None:
            yield None
        else:
            with self._repository.transaction(connection) as conn:
                yield conn

    def configure(self, binding: ProviderBinding) -> None:
        with self._transaction() as conn, self._lock:
            if binding.version <= self._versions.get(binding.id, 0):
                raise AttachmentError(
                    "Binding updates require a newer version."
                )
            if conn is not None:
                self._persist(conn, binding)
            self._bindings[binding.id] = binding
            self._versions[binding.id] = binding.version

    def revoke(self, binding_id: str) -> None:
        with self._transaction() as conn, self._lock:
            if conn is not None:
                conn.execute(
                    "UPDATE cp_provider_bindings_v1 SET state='revoked' WHERE id=?",
                    (binding_id,),
                )
            self._bindings.pop(binding_id, None)

    def resolve(
        self,
        organization_id: str,
        descriptor: AttachmentDescriptor,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> ProviderBinding:
        with self._transaction(connection) as conn, self._lock:
            binding = self._bindings.get(descriptor.binding_id)
            if (
                binding is None
                or binding.version != descriptor.binding_version
                or binding.backend != descriptor.backend
                or organization_id not in binding.organization_ids
            ):
                raise AttachmentError(
                    "Provider binding is unavailable for this scope."
                )
            if conn is not None:
                row = conn.execute(
                    "SELECT version, backend, account_scope, metadata_digest, state "
                    "FROM cp_provider_bindings_v1 WHERE id=?",
                    (binding.id,),
                ).fetchone()
                if row is None or tuple(row) != (
                    *self._metadata(binding),
                    "active",
                ):
                    raise AttachmentError(
                        "Provider binding is unavailable for this scope."
                    )
            return binding


def _owned_modal_session(runtime: Any) -> Any:
    # Never duck-type a cleanup/claim callback on an unverified runtime.
    from bespokelabs.sandbox.backends.modal import ModalSession
    from bespokelabs.sandbox.sandbox import Sandbox

    session = runtime._session if type(runtime) is Sandbox else runtime
    return session if type(session) is ModalSession else None


def _claim_owned_transport(runtime: Any, custody: object) -> bool:
    from bespokelabs.sandbox.backends.modal import ModalSession

    session = _owned_modal_session(runtime)
    return session is None or ModalSession._claim_attachment_custody(
        session, custody
    )


def release_owned_transport(
    runtime: Any, *, custody: object | None = None, unclaimed_only: bool = False
) -> None:
    """Dispose known owned transport, with atomic cache-custody fencing.

    A rejected alias cannot dispose another publication's client. Service/direct
    owners retain explicit no-argument disposal; no provider authority is added.
    """
    from bespokelabs.sandbox.backends.modal import ModalSession

    session = _owned_modal_session(runtime)
    if session is not None:
        ModalSession._release_owned_client(
            session, custody=custody, unclaimed_only=unclaimed_only
        )


class _BoundResumeBackend:
    """SDK session constructors using explicit binding credentials only."""

    def __init__(self, binding: ProviderBinding) -> None:
        self.binding = binding
        self._owned_modal_session: Any = None

    def release_owned_transport(self) -> None:
        """Discard a rejected reconnect's transport without deleting anything."""
        if self._owned_modal_session is not None:
            self._owned_modal_session._release_owned_client()
            self._owned_modal_session = None

    def resume(self, data: dict) -> Any:
        binding = self.binding
        resource_id = data["sandbox_id"]
        # Reviewed SDK contracts only. In particular modern E2B connect resumes
        # paused resources via POST; even GET-before-connect is racy. Disable
        # stock E2B hosted reconnect rather than changing direct SDK behavior.
        supported = {
            "modal": {"0.60.15", "1.6.4"},
            "daytona": {"0.207.0", "0.232.0"},
            "tensorlake": {"0.3.12", "0.5.165"},
        }
        if (
            binding.backend not in supported
            or sdk_version(binding.backend) not in supported[binding.backend]
        ):
            raise AttachmentError(
                "Provider SDK lacks reviewed read-only attachment."
            )
        if binding.backend == "modal":
            import modal
            from modal.config import config

            from bespokelabs.sandbox.backends.modal import ModalSession

            if sdk_version("modal") == "0.60.15":
                # This reviewed floor has no exported DEFAULT_SERVER_URL.
                # Its SDK default is fixed; never inspect private config tables.
                default_server_url = "https://api.modal.com"
            else:
                from modal.config import DEFAULT_SERVER_URL

                default_server_url = DEFAULT_SERVER_URL
            # The public factory eagerly opens and reads effective routing,
            # including env/profile overrides. Refuse custom routing BEFORE it
            # receives credentials; do not mutate config, env, or cwd to bind it.
            if (
                type(default_server_url) is not str
                or binding.settings["server_url"] != default_server_url
                or config.get("server_url") != default_server_url
            ):
                raise AttachmentError("Modal routing is not the SDK default.")
            client = modal.Client.from_credentials(
                token_id=binding.credentials["token_id"],
                token_secret=binding.credentials["token_secret"],
            )
            try:
                sandbox = modal.Sandbox.from_id(resource_id, client=client)
                session = ModalSession(sandbox=sandbox, owned_client=client)
                if session.session_state()["sandbox_id"] != resource_id:
                    raise AttachmentError(
                        "Provider returned an unexpected resource."
                    )
                # Transfer only after verification. If the outer wrapper or
                # adapter rejects it, release transport only, never destroy().
                self._owned_modal_session = session
                return session
            except Exception as exc:
                # Only this factory-created transport is ours to release. Never
                # destroy an unverified sandbox, or reopen the eager client.
                try:
                    client.__exit__(type(exc), exc, exc.__traceback__)
                except Exception:
                    # Preserve the lookup failure; reconnect sanitizes it.
                    pass
                raise
        if binding.backend == "daytona":
            from daytona import Daytona, DaytonaConfig

            from bespokelabs.sandbox.backends.daytona import DaytonaSession

            client = Daytona(
                DaytonaConfig(
                    api_key=binding.credentials["api_key"],
                    **dict(binding.settings),
                )
            )
            return DaytonaSession(
                client=client,
                sandbox=client.get(resource_id, request_timeout=30),
                workdir=data.get("workdir"),
            )
        if binding.backend == "tensorlake":
            from tensorlake.sandbox import SandboxClient as TensorlakeClient

            from bespokelabs.sandbox.backends.tensorlake import (
                TensorlakeSession,
            )

            settings = dict(binding.settings)
            proxy_url = settings.pop("proxy_url")
            timeout = (
                {"request_timeout": 30}
                if sdk_version("tensorlake") == "0.5.165"
                else {}
            )
            client = TensorlakeClient(
                api_key=binding.credentials["api_key"],
                max_retries=0,
                retry_backoff_sec=0,
                **settings,
                **timeout,
            )
            return TensorlakeSession(
                client=client,
                sandbox=client.connect(
                    resource_id, proxy_url=proxy_url, **timeout
                ),
                workdir=data.get("workdir", "/tmp"),
            )
        raise AttachmentError("Backend does not support durable attachment.")


class SDKAttachmentAdapter:
    """Allowlisted extraction and SDK-backed reuse, never creation or probing."""

    def verify_created(self, runtime: Any, binding: ProviderBinding) -> None:
        """Creation adapters must verify server-recorded runtime/account provenance.

        A caller-supplied runtime ID and binding ID are not such evidence.
        Verification must use local server evidence only: no provider I/O or
        nested transaction, since publication holds the lifecycle transaction.
        The reconnection-only stock adapter cannot certify creation.
        """
        raise AttachmentError("Bound creation provenance is unavailable.")

    def describe(
        self,
        runtime: Any,
        binding: ProviderBinding,
        *,
        remote_workdir: str | None = None,
        incarnation: str | None = None,
    ) -> AttachmentDescriptor:
        # Only these public properties are read. Never call session_state().
        if runtime.backend_name != binding.backend:
            raise AttachmentError("Runtime backend does not match its binding.")
        return AttachmentDescriptor(
            backend=binding.backend,
            binding_id=binding.id,
            binding_version=binding.version,
            provider_resource_id=runtime.provider_resource_id,
            remote_workdir=remote_workdir,
            incarnation=incarnation,
        )

    def reconnect(
        self, descriptor: AttachmentDescriptor, binding: ProviderBinding
    ) -> Any:
        if (
            descriptor.backend != binding.backend
            or descriptor.binding_id != binding.id
            or descriptor.binding_version != binding.version
        ):
            raise AttachmentError("Attachment does not match its binding.")
        # SandboxClient.resume supplies the normal Sandbox wrapper, without
        # constructing an ambient-auth backend client or running any setup.
        from bespokelabs.sandbox.sandbox import SandboxClient

        state_data = {"sandbox_id": descriptor.provider_resource_id}
        if descriptor.remote_workdir is not None:
            state_data["workdir"] = descriptor.remote_workdir
        client = object.__new__(SandboxClient)
        client._backend_name = descriptor.backend
        backend = _BoundResumeBackend(binding)
        client._backend_client = backend
        try:
            runtime = client.resume(
                SandboxSessionState(descriptor.backend, state_data)
            )
            if (
                runtime.backend_name != descriptor.backend
                or runtime.provider_resource_id
                != descriptor.provider_resource_id
            ):
                raise AttachmentError(
                    "Provider returned an unexpected resource."
                )
            return runtime
        except Exception:
            # Wrapper construction/identity verification may fail after resume.
            # Only the stock Modal factory transport is ours; no provider delete.
            backend.release_owned_transport()
        # Raise outside the handler to avoid retaining SDK text in chaining.
        raise AttachmentError(
            "Provider SDK unavailable or runtime reconnect failed."
        )


@dataclass(frozen=True)
class AttachmentRecord:
    organization_id: str
    sandbox_id: str
    generation: int = 0
    version: int = 0
    descriptor: AttachmentDescriptor | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cp_runtime_attachments_v1 (
    organization_id TEXT NOT NULL,
    sandbox_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation > 0),
    version INTEGER NOT NULL CHECK(version > 0),
    descriptor_json TEXT,
    PRIMARY KEY (organization_id, sandbox_id)
);
"""


class AttachmentRepository:
    """SQLite repository with injected connections and durable CAS tombstones.

    A caller-supplied connection remains caller-owned: no begin, commit,
    rollback or close. The caller must hold a write transaction when mutating.
    """

    def __init__(
        self, connection_factory: Callable[[], sqlite3.Connection]
    ) -> None:
        self.connection_factory = connection_factory

    def initialize(self) -> None:
        with self.transaction() as connection:
            connection.execute(_SCHEMA)
            connection.execute(
                """CREATE TABLE IF NOT EXISTS cp_provider_bindings_v1 (
                id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                backend TEXT NOT NULL, account_scope TEXT NOT NULL,
                metadata_digest TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('active', 'revoked'))
            )"""
            )

    @contextmanager
    def transaction(
        self, connection: sqlite3.Connection | None = None
    ) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            if not connection.in_transaction:
                raise AttachmentError(
                    "Caller connection requires an active transaction."
                )
            try:
                # Acquire SQLite's write reservation before reading CAS authority.
                # Deferred stale snapshots fail here rather than overwriting rows.
                connection.execute(
                    "UPDATE cp_runtime_attachments_v1 SET version=version WHERE 0"
                )
                yield connection
            except sqlite3.OperationalError:
                raise AttachmentConflict(
                    "Attachment transaction must be retried."
                ) from None
            return
        owned = self.connection_factory()
        try:
            owned.execute("BEGIN IMMEDIATE")
            yield owned
            owned.commit()
        except sqlite3.OperationalError:
            owned.rollback()
            raise AttachmentConflict(
                "Attachment transaction must be retried."
            ) from None
        except BaseException:
            owned.rollback()
            raise
        finally:
            owned.close()

    def load(
        self,
        organization_id: str,
        sandbox_id: str,
        *,
        connection: sqlite3.Connection,
    ) -> AttachmentRecord:
        row = connection.execute(
            """SELECT generation, version, descriptor_json
               FROM cp_runtime_attachments_v1
               WHERE organization_id=? AND sandbox_id=?""",
            (organization_id, sandbox_id),
        ).fetchone()
        if row is None:
            return AttachmentRecord(organization_id, sandbox_id)
        return AttachmentRecord(
            organization_id,
            sandbox_id,
            row[0],
            row[1],
            AttachmentDescriptor.from_json(row[2])
            if row[2] is not None
            else None,
        )

    def publish(
        self,
        organization_id: str,
        sandbox_id: str,
        descriptor: AttachmentDescriptor,
        *,
        expected_generation: int,
        connection: sqlite3.Connection | None = None,
    ) -> AttachmentRecord:
        # Round-trip validation also rejects accidental descriptor subclasses.
        if type(descriptor) is not AttachmentDescriptor:
            raise InvalidDescriptor("Invalid attachment descriptor.")
        if type(expected_generation) is not int or expected_generation < 0:
            raise AttachmentConflict("Invalid expected attachment generation.")
        encoded = descriptor.to_json()
        AttachmentDescriptor.from_json(encoded)
        with self.transaction(connection) as conn:
            current = self.load(organization_id, sandbox_id, connection=conn)
            if current.generation != expected_generation:
                raise AttachmentConflict("Attachment generation changed.")
            if current.descriptor == descriptor:
                return current
            generation, version = current.generation + 1, current.version + 1
            conn.execute(
                """INSERT INTO cp_runtime_attachments_v1 VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(organization_id, sandbox_id) DO UPDATE SET
                   generation=excluded.generation, version=excluded.version,
                   descriptor_json=excluded.descriptor_json""",
                (organization_id, sandbox_id, generation, version, encoded),
            )
            return AttachmentRecord(
                organization_id, sandbox_id, generation, version, descriptor
            )

    def invalidate(
        self,
        organization_id: str,
        sandbox_id: str,
        *,
        expected_generation: int | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        """Tombstone in the lifecycle transaction, optionally generation-CAS."""
        if expected_generation is not None and (
            type(expected_generation) is not int or expected_generation < 0
        ):
            raise AttachmentConflict("Invalid expected attachment generation.")
        with self.transaction(connection) as conn:
            # Invalidation must work even if a descriptor was corrupted. Read
            # only CAS metadata, never decode a soon-to-be-discarded document.
            row = conn.execute(
                """SELECT generation, version FROM cp_runtime_attachments_v1
                   WHERE organization_id=? AND sandbox_id=?""",
                (organization_id, sandbox_id),
            ).fetchone()
            generation, version = (row[0], row[1]) if row else (0, 0)
            if (
                expected_generation is not None
                and generation != expected_generation
            ):
                return False
            conn.execute(
                """INSERT INTO cp_runtime_attachments_v1 VALUES (?, ?, ?, ?, NULL)
                   ON CONFLICT(organization_id, sandbox_id) DO UPDATE SET
                   generation=excluded.generation, version=excluded.version,
                   descriptor_json=NULL""",
                (
                    organization_id,
                    sandbox_id,
                    generation + 1,
                    version + 1,
                ),
            )
            return True


@dataclass(frozen=True)
class SandboxAuthority:
    """Minimal tenant-owned lifecycle snapshot loaded in the same transaction."""

    organization_id: str
    sandbox_id: str
    backend: str
    status: str
    version: int
    provider_resource_id: str | None


def load_sandbox_authority(
    connection: sqlite3.Connection, organization_id: str, sandbox_id: str
) -> SandboxAuthority:
    """Default loader for the control plane's authoritative sandbox table."""
    row = connection.execute(
        """SELECT organization_id, id, backend, status, version, provider_resource_id
           FROM sandboxes WHERE organization_id=? AND id=?""",
        (organization_id, sandbox_id),
    ).fetchone()
    if row is None:
        raise AttachmentError("Sandbox is unavailable for this scope.")
    return SandboxAuthority(*row)


@dataclass(frozen=True)
class _CachedRuntime:
    record: AttachmentRecord
    authority: SandboxAuthority
    binding: ProviderBinding
    runtime: Any = field(repr=False)
    custody: object = field(default_factory=object, repr=False)


class AttachmentCoordinator:
    """Small creation/execute/termination hooks; no reaper or shutdown behavior.

    Every cache lookup first reads durable tenant authority. SDK reconnect is
    outside a transaction; installation then rechecks lifecycle, authority
    version, attachment generation/version and current server binding.
    """

    def __init__(
        self,
        repository: AttachmentRepository,
        bindings: BindingRegistry,
        *,
        adapter: SDKAttachmentAdapter | None = None,
        load_authority: Callable[
            [sqlite3.Connection, str, str], SandboxAuthority
        ] = load_sandbox_authority,
    ) -> None:
        self.repository = repository
        self.bindings = bindings
        self.adapter = adapter or SDKAttachmentAdapter()
        self.load_authority = load_authority
        self._cache: dict[tuple[str, str], _CachedRuntime] = {}
        self._cache_lock = threading.RLock()

    @staticmethod
    def _require(principal: Principal, scope: str) -> None:
        if not scope or not principal.allows(scope):
            raise AuthorizationError("Runtime attachment scope is required.")

    def _authority(
        self, conn: sqlite3.Connection, organization_id: str, sandbox_id: str
    ) -> SandboxAuthority:
        authority = self.load_authority(conn, organization_id, sandbox_id)
        if (
            authority.organization_id != organization_id
            or authority.sandbox_id != sandbox_id
            or authority.status != "running"
            or authority.backend not in _SUPPORTED
            or not _positive(authority.version)
        ):
            raise AttachmentConflict("Sandbox is not attachable.")
        return authority

    @staticmethod
    def _match(
        authority: SandboxAuthority, descriptor: AttachmentDescriptor
    ) -> None:
        if (
            authority.backend != descriptor.backend
            or authority.provider_resource_id != descriptor.provider_resource_id
        ):
            raise AttachmentConflict(
                "Attachment does not match sandbox authority."
            )

    def publish_created(
        self,
        principal: Principal,
        sandbox_id: str,
        runtime: Any,
        binding_id: str,
        *,
        expected_generation: int,
        remote_workdir: str | None = None,
        incarnation: str | None = None,
        scope: str = "sandboxes:create",
        connection: sqlite3.Connection | None = None,
    ) -> AttachmentRecord:
        """Publish a trusted created runtime after its running lifecycle write.

        Does not cache the handle: caller transactions can still roll back.
        The server-owned adapter must verify creation's binding provenance.
        """
        self._require(principal, scope)
        with (
            self.repository.transaction(connection) as conn,
            self.bindings._lock,
        ):
            authority = self._authority(
                conn, principal.organization_id, sandbox_id
            )
            binding = self.bindings._bindings.get(binding_id)
            if (
                binding is None
                or principal.organization_id not in binding.organization_ids
            ):
                raise AttachmentError(
                    "Provider binding is unavailable for this scope."
                )
            self.adapter.verify_created(runtime, binding)
            descriptor = self.adapter.describe(
                runtime,
                binding,
                remote_workdir=remote_workdir,
                incarnation=incarnation,
            )
            self.bindings.resolve(
                principal.organization_id, descriptor, connection=conn
            )
            self._match(authority, descriptor)
            return self.repository.publish(
                principal.organization_id,
                sandbox_id,
                descriptor,
                expected_generation=expected_generation,
                connection=conn,
            )

    def resolve(
        self,
        principal: Principal,
        sandbox_id: str,
        *,
        scope: str = "sandboxes:execute",
        install: (
            Callable[[Any, AttachmentRecord, ProviderBinding], None] | None
        ) = None,
    ) -> Any:
        """Resolve and optionally install under the final authority transaction.

        The callback must perform local bookkeeping only, without provider I/O
        or database commits. This closes the resolve-to-install termination gap.
        """
        self._require(principal, scope)
        key = (principal.organization_id, sandbox_id)
        runtime = None
        custody = None
        retained = False
        displaced = None
        protected = None
        final_cache_locked = False
        with self._cache_lock:
            initial = self._cache.get(key)
        try:
            with self.repository.transaction() as conn, self.bindings._lock:
                authority = self._authority(conn, *key)
                record = self.repository.load(*key, connection=conn)
                if record.descriptor is None:
                    raise AttachmentError("Sandbox has no runtime attachment.")
                self._match(authority, record.descriptor)
                binding = self.bindings.resolve(
                    key[0], record.descriptor, connection=conn
                )
                with self._cache_lock:
                    cached = self._cache.get(key)
                    if (
                        cached is not None
                        and cached.record == record
                        and cached.authority == authority
                        and cached.binding is binding
                    ):
                        protected = cached
                        if install is not None:
                            install(cached.runtime, record, binding)
                        return cached.runtime
            runtime = self.adapter.reconnect(record.descriptor, binding)
            if (
                runtime.backend_name != record.descriptor.backend
                or runtime.provider_resource_id
                != record.descriptor.provider_resource_id
            ):
                raise AttachmentError(
                    "Provider returned an unexpected resource."
                )
            with self.repository.transaction() as conn, self.bindings._lock:
                current_authority = self._authority(conn, *key)
                current_record = self.repository.load(*key, connection=conn)
                current_binding = self.bindings.resolve(
                    key[0], record.descriptor, connection=conn
                )
                if (
                    current_authority != authority
                    or current_record != record
                    or current_binding is not binding
                ):
                    # Never destroy a discarded handle: it names a shared resource.
                    raise AttachmentConflict(
                        "Sandbox changed during runtime reconnect."
                    )
                # Lock order stays DB -> binding -> cache. Keep cache custody
                # locked THROUGH commit and any rollback eviction below: a
                # published entry is provisional until the transaction exits.
                # Reconnect (provider I/O) has already completed outside locks.
                self._cache_lock.acquire()
                final_cache_locked = True
                cached = self._cache.get(key)
                if (
                    cached is not None
                    and cached.record == record
                    and cached.authority == authority
                    and cached.binding is binding
                ):
                    protected = cached
                    if install is not None:
                        install(cached.runtime, record, binding)
                    return cached.runtime
                candidate = _CachedRuntime(record, authority, binding, runtime)
                if not _claim_owned_transport(runtime, candidate.custody):
                    raise AttachmentConflict(
                        "Owned transport cannot be republished or shared across caches."
                    )
                custody = candidate.custody
                if install is not None:
                    install(runtime, record, binding)
                self._cache[key] = candidate
                retained = True
                if cached is not None and cached.runtime is not runtime:
                    displaced = cached
            return runtime
        except BaseException:
            # Rollback includes cancellation/interrupts as well as SQL errors.
            # Never evict a concurrently published winner when this reconnect
            # loses its final authority check (or install/commit fails).
            with self._cache_lock:
                cached = self._cache.get(key)
                if cached is not None and (
                    (cached is initial and cached is not protected)
                    or (retained and cached.runtime is runtime)
                ):
                    self._cache.pop(key)
                    if cached.runtime is not runtime or custody is None:
                        displaced = cached
            retained = False
            raise
        finally:
            if final_cache_locked:
                # Rollback cleanup has removed provisional custody before any
                # other reader can acquire it. Dispose transport outside locks.
                self._cache_lock.release()
            if runtime is not None and not retained:
                # Atomic session claim, not an unlocked cache-presence check:
                # same-generation/cross-cache aliases cannot close a winner.
                release_owned_transport(
                    runtime, custody=custody, unclaimed_only=custody is None
                )
            if displaced is not None:
                release_owned_transport(
                    displaced.runtime, custody=displaced.custody
                )

    def forget(
        self,
        organization_id: str,
        sandbox_id: str,
        *,
        release_transport: bool = True,
    ) -> None:
        """Discard local custody, or transfer it to the existing destroy path."""
        with self._cache_lock:
            cached = self._cache.pop((organization_id, sandbox_id), None)
        if cached is not None and release_transport:
            release_owned_transport(cached.runtime, custody=cached.custody)
