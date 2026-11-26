"""Offline tests of strict runtime identity, tenant authority and reconnect races."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from unittest import mock

from bespokelabs.sandbox.control_plane.attachments import (
    AttachmentConflict,
    AttachmentCoordinator,
    AttachmentDescriptor,
    AttachmentError,
    AttachmentRepository,
    BindingRegistry,
    InvalidDescriptor,
    ProviderBinding,
    SDKAttachmentAdapter,
)
from bespokelabs.sandbox.control_plane.errors import AuthorizationError
from bespokelabs.sandbox.control_plane.models import Principal

_SECRET = "credential-sentinel-never-persist"


def binding(backend: str = "e2b", **kwargs: object) -> ProviderBinding:
    values = {
        "id": "binding-a",
        "version": 1,
        "backend": backend,
        "account_scope": "account-a",
        "organization_ids": frozenset({"org-a"}),
        "settings": {
            "modal": {"server_url": "https://modal.invalid"},
            "e2b": {},
            "daytona": {
                "api_url": "https://daytona.invalid/api",
                "target": "us",
            },
            "tensorlake": {
                "api_url": "https://tensorlake.invalid",
                "proxy_url": "https://proxy.invalid",
                "organization_id": "org-a",
                "project_id": "project-a",
                "namespace": "ns-a",
            },
        }.get(backend, {}),
        "credentials": (
            {"token_id": "token-id", "token_secret": _SECRET}
            if backend == "modal"
            else {"api_key": _SECRET}
        ),
    }
    values.update(kwargs)
    return ProviderBinding(**values)


def descriptor(backend: str = "e2b", **kwargs: object) -> AttachmentDescriptor:
    values = {
        "backend": backend,
        "binding_id": "binding-a",
        "binding_version": 1,
        "provider_resource_id": "resource-a",
    }
    values.update(kwargs)
    return AttachmentDescriptor(**values)


def runtime(backend: str = "e2b") -> mock.Mock:
    result = mock.Mock(backend_name=backend, provider_resource_id="resource-a")
    result.session_state.side_effect = AssertionError(
        "Must never read SDK state"
    )
    result.env_vars = {"PRIVATE": _SECRET}
    return result


class DescriptorTests(unittest.TestCase):

    def test_strict_round_trip(self) -> None:
        original = descriptor(
            "daytona", remote_workdir="/workspace/project", incarnation="v1"
        )
        self.assertEqual(
            AttachmentDescriptor.from_json(original.to_json()), original
        )
        self.assertNotIn(_SECRET, original.to_json())
        minimal = json.loads(descriptor().to_json())
        del minimal["remote_workdir"], minimal["incarnation"]
        self.assertEqual(AttachmentDescriptor.from_dict(minimal), descriptor())

    def test_rejects_arbitrary_client_state_without_echo(self) -> None:
        for key in (
            "env_vars",
            "ssh_keys",
            "host_path",
            "api_url",
            "endpoint",
            "credentials",
            "data",
            "session_state",
            "unknown",
        ):
            document = json.loads(descriptor().to_json())
            document[key] = _SECRET
            with (
                self.subTest(key=key),
                self.assertRaises(InvalidDescriptor) as caught,
            ):
                AttachmentDescriptor.from_json(json.dumps(document))
            self.assertNotIn(_SECRET, str(caught.exception))

    def test_malformed_identity_version_and_workdir(self) -> None:
        bad_values = [
            {"provider_resource_id": "https://example.invalid/resource"},
            {"provider_resource_id": "../../host"},
            {"provider_resource_id": "resource\n"},
            {"provider_resource_id": ""},
            {"provider_resource_id": "a" * 256},
            {"provider_resource_id": {"api_key": _SECRET}},
            {"binding_id": "account.example"},
            {"binding_version": True},
            {"binding_version": 0},
            {"schema_version": 2},
            {"schema_version": True},
            {"incarnation": "bad/path"},
            {"backend": []},
        ]
        for values in bad_values:
            with (
                self.subTest(values=values),
                self.assertRaises(InvalidDescriptor),
            ):
                descriptor(**values)
        for workdir in (
            "relative",
            "/a/../b",
            "/a/./b",
            "/a//b",
            "/a;echo",
            "/" + "a" * 500 + ";",
            "/a\x00",
            "/" + "a" * 512,
        ):
            with (
                self.subTest(workdir=workdir),
                self.assertRaises(InvalidDescriptor),
            ):
                descriptor("daytona", remote_workdir=workdir)
        with self.assertRaises(InvalidDescriptor):
            descriptor(remote_workdir="/workspace")

    def test_binding_membership_requires_typed_bounded_string_container(self):
        for members in (
            "org-a",
            b"org-a",
            None,
            (x for x in ["org-a"]),
            ["org-a", None],
            [""],
            ["a" * 256],
            [1],
        ):
            with (
                self.subTest(kind=type(members)),
                self.assertRaises(AttachmentError),
            ):
                binding(organization_ids=members)
        for members in (["org-a"], ("org-a",), {"org-a"}, frozenset({"org-a"})):
            self.assertEqual(
                binding(organization_ids=members).organization_ids,
                frozenset({"org-a"}),
            )

    def test_binding_requires_explicit_nonsecret_routing(self):
        for backend in ("modal", "daytona", "tensorlake"):
            with (
                self.subTest(backend=backend),
                self.assertRaises(AttachmentError),
            ):
                binding(backend, settings={})
        for url in (
            "https://user:password@example.invalid",
            "https://example.invalid/?key=x",
            "https://example.invalid/#secret",
            "http://example.invalid",
            "https://example.invalid:bad",
        ):
            with self.subTest(url=url), self.assertRaises(AttachmentError):
                binding("modal", settings={"server_url": url})
        for workdir in ("/", "/workspace/"):
            self.assertEqual(
                descriptor("daytona", remote_workdir=workdir).remote_workdir,
                workdir,
            )

    def test_unsupported_backends_fail_explicitly(self) -> None:
        for backend in (
            "local",
            "safehouse",
            "boxd",
            "runpod",
            "ray",
            "monty",
            "docker",
            "unknown",
        ):
            with (
                self.subTest(backend=backend),
                self.assertRaises(InvalidDescriptor),
            ):
                descriptor(backend)
            with self.assertRaises(AttachmentError):
                binding(backend)

    def test_malformed_documents_and_duplicate_keys(self) -> None:
        for raw in ("not json", "[]", "{}", "null", "x" * 4097):
            with (
                self.subTest(raw=raw[:10]),
                self.assertRaises(InvalidDescriptor),
            ):
                AttachmentDescriptor.from_json(raw)
        raw = (
            descriptor()
            .to_json()
            .replace(
                '"schema_version":1', '"schema_version":1,"schema_version":1'
            )
        )
        with self.assertRaises(InvalidDescriptor):
            AttachmentDescriptor.from_json(raw)

    def test_extract_only_explicit_public_properties(self) -> None:
        live = runtime()
        self.assertEqual(
            SDKAttachmentAdapter().describe(live, binding()), descriptor()
        )
        live.session_state.assert_not_called()
        self.assertNotIn(_SECRET, repr(binding()))
        with self.assertRaises(TypeError):
            binding().credentials["api_key"] = "replacement"


class RepositoryAndCoordinatorTests(unittest.TestCase):

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = Path(self.directory.name) / "state.sqlite"
        self.repo = AttachmentRepository(self.connect)
        self.repo.initialize()
        with self.repo.transaction() as conn:
            conn.execute(
                """CREATE TABLE sandboxes (
                organization_id TEXT, id TEXT, backend TEXT, status TEXT,
                version INTEGER, provider_resource_id TEXT,
                PRIMARY KEY(organization_id, id))"""
            )
            conn.executemany(
                "INSERT INTO sandboxes VALUES (?, ?, 'e2b', 'running', 1, 'resource-a')",
                [("org-a", "sandbox-a"), ("org-b", "sandbox-a")],
            )
        self.principal = Principal(
            "org-a",
            "key-a",
            frozenset({"sandboxes:execute", "sandboxes:create"}),
        )
        self.bindings = BindingRegistry((binding(),))
        self.adapter = mock.Mock(wraps=SDKAttachmentAdapter())
        self.adapter.reconnect.return_value = runtime()
        # This server-owned test adapter certifies synthetic creation provenance.
        self.adapter.verify_created.return_value = None
        self.coordinator = AttachmentCoordinator(
            self.repo, self.bindings, adapter=self.adapter
        )

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=3)

    def publish(self, **kwargs: object):
        return self.coordinator.publish_created(
            self.principal,
            "sandbox-a",
            runtime(),
            "binding-a",
            expected_generation=0,
            **kwargs,
        )

    def load(self, org: str = "org-a"):
        with self.repo.transaction() as conn:
            return self.repo.load(org, "sandbox-a", connection=conn)

    def test_publication_is_idempotent_and_persists_no_sdk_state(self) -> None:
        first = self.publish()
        again = self.coordinator.publish_created(
            self.principal,
            "sandbox-a",
            runtime(),
            "binding-a",
            expected_generation=first.generation,
        )
        self.assertEqual(first, again)
        with self.connect() as conn:
            dump = "\n".join(conn.iterdump())
        for forbidden in (
            _SECRET,
            "env_vars",
            "session_state",
            "ssh_keys",
            "api_url",
        ):
            self.assertNotIn(forbidden, dump)
        with self.assertRaises(AttachmentConflict):
            self.publish()

    def test_cache_is_never_authority(self) -> None:
        self.publish()
        first = self.coordinator.resolve(self.principal, "sandbox-a")
        self.assertIs(
            self.coordinator.resolve(self.principal, "sandbox-a"), first
        )
        self.adapter.reconnect.assert_called_once()
        with self.repo.transaction() as conn:
            conn.execute(
                "UPDATE sandboxes SET status='stopping', version=version+1"
            )
        with self.assertRaises(AttachmentConflict):
            self.coordinator.resolve(self.principal, "sandbox-a")
        first.destroy.assert_not_called()
        self.assertEqual(self.coordinator._cache, {})

    def test_authorization_and_tenant_ownership_precede_provider_access(
        self,
    ) -> None:
        self.publish()
        for principal in (
            replace(self.principal, scopes=frozenset()),
            replace(self.principal, organization_id="org-b"),
            replace(self.principal, organization_id="org-missing"),
        ):
            with (
                self.subTest(principal=principal),
                self.assertRaises((AttachmentError, AuthorizationError)),
            ):
                self.coordinator.resolve(principal, "sandbox-a")
        self.adapter.reconnect.assert_not_called()
        self.assertIsNone(self.load("org-b").descriptor)
        # Same sandbox ID in another organization gets a separately scoped record.
        other_binding = binding(
            id="binding-b", organization_ids=frozenset({"org-b"})
        )
        self.bindings.configure(other_binding)
        other = replace(self.principal, organization_id="org-b")
        self.coordinator.publish_created(
            other, "sandbox-a", runtime(), "binding-b", expected_generation=0
        )
        self.coordinator.resolve(other, "sandbox-a")
        self.assertEqual(self.load("org-b").descriptor.binding_id, "binding-b")
        self.assertEqual(self.load().descriptor.binding_id, "binding-a")

    def test_explicit_scope_and_no_authority_from_runtime(self) -> None:
        creator = replace(
            self.principal, scopes=frozenset({"sandboxes:create"})
        )
        with self.assertRaises(AuthorizationError):
            self.coordinator.publish_created(
                replace(creator, scopes=frozenset({"sandboxes:execute"})),
                "sandbox-a",
                runtime(),
                "binding-a",
                expected_generation=0,
            )
        self.coordinator.publish_created(
            creator,
            "sandbox-a",
            runtime(),
            "binding-a",
            expected_generation=0,
        )
        with self.repo.transaction() as conn:
            conn.execute(
                "UPDATE sandboxes SET provider_resource_id='resource-b'"
            )
        with self.assertRaises(AttachmentConflict):
            self.coordinator.resolve(self.principal, "sandbox-a")
        self.adapter.reconnect.assert_not_called()

    def test_binding_version_scope_account_and_revocation_are_rechecked(
        self,
    ) -> None:
        for changed in (
            binding(version=2),
            binding(version=2, account_scope="account-b"),
            binding(version=2, organization_ids=frozenset({"org-b"})),
        ):
            with self.subTest(changed=changed):
                self.bindings = BindingRegistry((binding(),))
                coordinator = AttachmentCoordinator(
                    self.repo, self.bindings, adapter=self.adapter
                )
                if self.load().descriptor is None:
                    self.publish()
                live = coordinator.resolve(self.principal, "sandbox-a")
                self.bindings.configure(changed)
                with self.assertRaises(AttachmentError):
                    coordinator.resolve(self.principal, "sandbox-a")
                live.destroy.assert_not_called()
        self.bindings = BindingRegistry((binding(),))
        coordinator = AttachmentCoordinator(
            self.repo, self.bindings, adapter=self.adapter
        )
        coordinator.resolve(self.principal, "sandbox-a")
        self.bindings.revoke("binding-a")
        with self.assertRaises(AttachmentError):
            coordinator.resolve(self.principal, "sandbox-a")
        with self.assertRaises(AttachmentError):
            BindingRegistry((binding(), binding()))

    def test_invalidation_is_generation_conditional_and_caller_transaction_owned(
        self,
    ) -> None:
        record = self.publish()
        with self.repo.transaction() as conn:
            self.assertFalse(
                self.repo.invalidate(
                    "org-a", "sandbox-a", expected_generation=0, connection=conn
                )
            )
            self.assertEqual(
                self.repo.load("org-a", "sandbox-a", connection=conn), record
            )
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.repo.transaction() as conn:
                conn.execute(
                    "UPDATE sandboxes SET status='stopping', version=2 WHERE organization_id='org-a'"
                )
                self.assertTrue(
                    self.repo.invalidate(
                        "org-a",
                        "sandbox-a",
                        expected_generation=record.generation,
                        connection=conn,
                    )
                )
                self.assertTrue(conn.in_transaction)
                raise RuntimeError("rollback")
        self.assertEqual(self.load(), record)
        with self.repo.transaction() as conn:
            conn.execute(
                "UPDATE sandboxes SET status='destroyed', version=2 WHERE organization_id='org-a'"
            )
            self.assertTrue(
                self.repo.invalidate("org-a", "sandbox-a", connection=conn)
            )
        tombstone = self.load()
        self.assertIsNone(tombstone.descriptor)
        self.assertEqual(tombstone.generation, record.generation + 1)
        with self.assertRaises(AttachmentConflict):
            self.repo.publish(
                "org-a",
                "sandbox-a",
                descriptor(),
                expected_generation=record.generation,
            )
        with self.connect() as conn:
            with self.assertRaises(AttachmentError):
                self.repo.invalidate("org-a", "sandbox-a", connection=conn)

    def test_initial_tombstone_blocks_late_creation(self) -> None:
        self.repo.invalidate("org-a", "sandbox-a", expected_generation=0)
        with self.assertRaises(AttachmentConflict):
            self.publish()

    def test_corrupt_document_does_not_block_invalidation(self) -> None:
        self.publish()
        with self.repo.transaction() as conn:
            conn.execute(
                "UPDATE cp_runtime_attachments_v1 SET descriptor_json='not-json'"
            )
            self.assertTrue(
                self.repo.invalidate("org-a", "sandbox-a", connection=conn)
            )
        self.assertIsNone(self.load().descriptor)
        for generation in (True, -1, "0"):
            with (
                self.subTest(generation=generation),
                self.assertRaises(AttachmentConflict),
            ):
                self.repo.publish(
                    "org-a",
                    "sandbox-a",
                    descriptor(),
                    expected_generation=generation,
                )
            with self.assertRaises(AttachmentConflict):
                self.repo.invalidate(
                    "org-a", "sandbox-a", expected_generation=generation
                )

    def test_deterministic_reconnect_termination_race(self) -> None:
        self.publish()
        entered, release = threading.Event(), threading.Event()
        discarded = runtime()
        errors = []

        def reconnect(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("race did not complete")
            return discarded

        def resolve():
            try:
                self.coordinator.resolve(self.principal, "sandbox-a")
            except Exception as exc:
                errors.append(exc)

        self.adapter.reconnect.side_effect = reconnect
        worker = threading.Thread(target=resolve)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            # This write must succeed while reconnect is blocked (no open transaction).
            with self.repo.transaction() as conn:
                conn.execute(
                    "UPDATE sandboxes SET status='stopping', version=2 WHERE organization_id='org-a'"
                )
                self.repo.invalidate("org-a", "sandbox-a", connection=conn)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], AttachmentConflict)
        self.assertEqual(self.coordinator._cache, {})
        discarded.destroy.assert_not_called()

    def test_attachment_and_record_version_changes_prevent_installation(
        self,
    ) -> None:
        self.publish()
        for change in (
            "generation",
            "record_version",
            "binding",
            "attachment_version",
        ):
            with self.subTest(change=change):
                self.bindings = BindingRegistry((binding(),))
                coordinator = AttachmentCoordinator(
                    self.repo, self.bindings, adapter=self.adapter
                )

                def reconnect(*args, change=change):
                    if change == "binding":
                        self.bindings.configure(binding(version=2))
                    else:
                        with self.repo.transaction() as conn:
                            if change == "generation":
                                self.repo.invalidate(
                                    "org-a", "sandbox-a", connection=conn
                                )
                            elif change == "attachment_version":
                                conn.execute(
                                    "UPDATE cp_runtime_attachments_v1 SET version=version+1"
                                )
                            else:
                                conn.execute(
                                    "UPDATE sandboxes SET version=version+1"
                                )
                    return runtime()

                self.adapter.reconnect.side_effect = reconnect
                with self.assertRaises(AttachmentError):
                    coordinator.resolve(self.principal, "sandbox-a")
                self.assertEqual(coordinator._cache, {})
                # Restore only test metadata for the next isolated race.
                with self.repo.transaction() as conn:
                    conn.execute(
                        "UPDATE cp_runtime_attachments_v1 SET descriptor_json=?",
                        (descriptor().to_json(),),
                    )

    def test_missing_binding_and_malformed_db_descriptor_do_not_reconnect(
        self,
    ) -> None:
        self.publish()
        self.bindings.revoke("binding-a")
        with self.assertRaises(AttachmentError):
            self.coordinator.resolve(self.principal, "sandbox-a")
        with self.repo.transaction() as conn:
            payload = json.loads(descriptor().to_json())
            payload["endpoint"] = _SECRET
            conn.execute(
                "UPDATE cp_runtime_attachments_v1 SET descriptor_json=?",
                (json.dumps(payload),),
            )
        with self.assertRaises(InvalidDescriptor) as caught:
            self.coordinator.resolve(self.principal, "sandbox-a")
        self.assertNotIn(_SECRET, str(caught.exception))
        self.adapter.reconnect.assert_not_called()

    def test_fresh_coordinator_uses_explicit_adapter_not_creation(self) -> None:
        self.publish()
        restarted = AttachmentCoordinator(
            AttachmentRepository(self.connect),
            self.bindings,
            adapter=self.adapter,
        )
        attached = restarted.resolve(self.principal, "sandbox-a")
        self.assertEqual(attached.provider_resource_id, "resource-a")
        self.adapter.reconnect.assert_called_once_with(descriptor(), binding())

    def test_shared_revocation_rejects_other_worker_cache_and_restart(self):
        self.bindings.attach_repository(self.repo)
        other = BindingRegistry((binding(),))
        other.attach_repository(AttachmentRepository(self.connect))
        self.publish()
        worker = AttachmentCoordinator(self.repo, other, adapter=self.adapter)
        live = worker.resolve(self.principal, "sandbox-a")
        self.bindings.revoke("binding-a")
        with self.assertRaises(AttachmentError):
            worker.resolve(self.principal, "sandbox-a")
        self.assertEqual(worker._cache, {})
        live.destroy.assert_not_called()
        for candidate in (binding(), binding(version=1)):
            with self.assertRaises(AttachmentError):
                BindingRegistry((candidate,)).attach_repository(self.repo)
        replacement = BindingRegistry((binding(version=2),))
        replacement.attach_repository(self.repo)
        with self.assertRaises(AttachmentError):
            other.resolve("org-a", descriptor())
        replacement.resolve("org-a", descriptor(binding_version=2))
        replacement.revoke("binding-a")
        for candidate in (binding(version=1), binding(version=2)):
            with self.assertRaises(AttachmentError):
                BindingRegistry((candidate,)).attach_repository(self.repo)
        BindingRegistry((binding(version=3),)).attach_repository(self.repo)
        with self.connect() as conn:
            dump = "\n".join(conn.iterdump())
        self.assertNotIn(_SECRET, dump)
        import hashlib

        self.assertNotIn(hashlib.sha256(_SECRET.encode()).hexdigest(), dump)

    def test_shared_revocation_during_reconnect_discards_without_destroy(self):
        self.bindings.attach_repository(self.repo)
        other = BindingRegistry((binding(),))
        other.attach_repository(self.repo)
        self.publish()
        live = runtime()

        def reconnect(*args):
            other.revoke("binding-a")
            return live

        self.adapter.reconnect.side_effect = reconnect
        with self.assertRaises(AttachmentError):
            self.coordinator.resolve(self.principal, "sandbox-a")
        self.assertEqual(self.coordinator._cache, {})
        live.destroy.assert_not_called()
        script = """
import sqlite3, sys
from tests.test_runtime_attachments import binding
from bespokelabs.sandbox.control_plane.attachments import (
    AttachmentError, AttachmentRepository, BindingRegistry,
)
repo = AttachmentRepository(lambda: sqlite3.connect(sys.argv[1]))
try:
    BindingRegistry((binding(),)).attach_repository(repo)
except AttachmentError:
    pass
else:
    raise AssertionError("A fresh worker reactivated a revoked binding")
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(self.db_path)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_shared_same_version_metadata_conflicts_and_rotation(self):
        original = binding("daytona")
        registry = BindingRegistry((original,))
        registry.attach_repository(self.repo)
        same = BindingRegistry((original,))
        same.attach_repository(self.repo)
        changes = (
            {"account_scope": "account-b"},
            {"organization_ids": frozenset({"org-b"})},
            {
                "settings": {
                    "api_url": "https://other.invalid/api",
                    "target": "us",
                }
            },
            {
                "settings": {
                    "api_url": "https://daytona.invalid/api",
                    "target": "eu",
                }
            },
        )
        for change in changes:
            with self.subTest(change=change):
                changed = replace(original, **change)
                with self.assertRaises(AttachmentError):
                    BindingRegistry((changed,)).attach_repository(self.repo)
        registry.configure(
            replace(original, version=2, account_scope="account-b")
        )
        with self.assertRaises(AttachmentError):
            same.resolve("org-a", descriptor("daytona"))
        with self.assertRaises(AttachmentError):
            BindingRegistry((original,)).attach_repository(self.repo)

    def test_caller_publication_rollback_preserves_connection_and_no_cache(
        self,
    ):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            created = self.publish(connection=conn)
            self.assertIsNotNone(created.descriptor)
            self.assertTrue(conn.in_transaction)
            self.assertEqual(self.coordinator._cache, {})
            conn.rollback()
            self.assertEqual(conn.execute("SELECT 1").fetchone(), (1,))
            self.assertIsNone(self.load().descriptor)
        finally:
            conn.close()

    def test_wrong_provider_identity_is_rejected_without_destruction(self):
        self.publish()
        wrong = runtime()
        wrong.provider_resource_id = "wrong-id"
        self.adapter.reconnect.return_value = wrong
        with self.assertRaises(AttachmentError):
            self.coordinator.resolve(self.principal, "sandbox-a")
        self.assertEqual(self.coordinator._cache, {})
        wrong.destroy.assert_not_called()
        self.assertEqual(self.load().descriptor, descriptor())

    def test_stock_adapter_cannot_certify_caller_binding_assertion(self):
        coordinator = AttachmentCoordinator(self.repo, self.bindings)
        with self.assertRaises(AttachmentError):
            coordinator.publish_created(
                self.principal,
                "sandbox-a",
                runtime(),
                "binding-a",
                expected_generation=0,
            )
        self.assertIsNone(self.load().descriptor)
        self.adapter.verify_created.side_effect = AttachmentError(
            "No creation evidence."
        )
        with self.assertRaises(AttachmentError):
            self.publish()

    def test_concurrent_publication_cas_has_exactly_one_winner(self):
        barrier = threading.Barrier(2)
        outcomes = []

        def publish(resource):
            barrier.wait(timeout=5)
            try:
                self.repo.publish(
                    "org-a",
                    "sandbox-a",
                    descriptor(provider_resource_id=resource),
                    expected_generation=0,
                )
                outcomes.append("published")
            except AttachmentConflict:
                outcomes.append("conflict")

        threads = [
            threading.Thread(target=publish, args=(resource,))
            for resource in ("resource-a", "resource-b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(outcomes, ["published", "conflict"])
        self.assertEqual(self.load().generation, 1)

    def test_deferred_stale_snapshot_is_sanitized_without_owning_transaction(
        self,
    ):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN")
            self.repo.load("org-a", "sandbox-a", connection=conn)
            self.repo.publish(
                "org-a", "sandbox-a", descriptor(), expected_generation=0
            )
            with self.assertRaises(AttachmentConflict) as caught:
                self.repo.publish(
                    "org-a",
                    "sandbox-a",
                    descriptor(provider_resource_id="resource-b"),
                    expected_generation=0,
                    connection=conn,
                )
            self.assertNotIn("locked", str(caught.exception))
            self.assertTrue(conn.in_transaction)
            conn.rollback()
            self.assertEqual(conn.execute("SELECT 1").fetchone(), (1,))
        finally:
            conn.close()
        self.assertEqual(self.load().descriptor, descriptor())

    def test_forget_after_committed_termination_is_nondestructive(self):
        self.publish()
        live = self.coordinator.resolve(self.principal, "sandbox-a")
        with self.repo.transaction() as conn:
            conn.execute("UPDATE sandboxes SET status='destroyed', version=2")
            self.repo.invalidate("org-a", "sandbox-a", connection=conn)
        self.coordinator.forget("org-a", "sandbox-a")
        self.assertEqual(self.coordinator._cache, {})
        live.destroy.assert_not_called()
        with self.assertRaises(AttachmentConflict):
            self.coordinator.resolve(self.principal, "sandbox-a")


class SDKAdapterTests(unittest.TestCase):

    def test_import_with_all_optional_sdks_blocked(self) -> None:
        script = """
import sys
for name in ('modal', 'e2b_code_interpreter', 'daytona', 'tensorlake'):
    sys.modules[name] = None
from bespokelabs.sandbox.control_plane.attachments import AttachmentCoordinator
assert AttachmentCoordinator is not None
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unreviewed_sdk_is_rejected_before_import_or_calls(self) -> None:
        with (
            mock.patch(
                "bespokelabs.sandbox.control_plane.attachments.sdk_version",
                return_value="999.0",
            ),
            mock.patch.dict(sys.modules, {"daytona": None}),
        ):
            with self.assertRaises(AttachmentError):
                SDKAttachmentAdapter().reconnect(
                    descriptor("daytona"), binding("daytona")
                )

    def test_e2b_reconnect_disabled_even_with_sdk_present(self) -> None:
        sdk = ModuleType("e2b_code_interpreter")
        sdk.Sandbox = mock.Mock()
        with mock.patch.dict(sys.modules, {"e2b_code_interpreter": sdk}):
            with self.assertRaises(AttachmentError):
                SDKAttachmentAdapter().reconnect(descriptor(), binding())
        sdk.Sandbox.connect.assert_not_called()
        sdk.Sandbox.create.assert_not_called()

    def test_missing_optional_sdk_and_provider_errors_are_safe(self) -> None:
        adapter = SDKAttachmentAdapter()
        with mock.patch.dict(sys.modules, {"e2b_code_interpreter": None}):
            with self.assertRaises(AttachmentError) as caught:
                adapter.reconnect(descriptor(), binding())
        self.assertIsNone(caught.exception.__context__)
        sdk = ModuleType("e2b_code_interpreter")
        sdk.Sandbox = mock.Mock()
        sdk.Sandbox.connect.side_effect = RuntimeError(_SECRET)
        with mock.patch.dict(sys.modules, {"e2b_code_interpreter": sdk}):
            with self.assertRaises(AttachmentError) as caught:
                adapter.reconnect(descriptor(), binding())
        self.assertNotIn(_SECRET, str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        with self.assertRaises(AttachmentError):
            adapter.reconnect(descriptor(), binding(version=2))


if __name__ == "__main__":
    unittest.main()
