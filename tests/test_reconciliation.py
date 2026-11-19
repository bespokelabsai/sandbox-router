"""Deterministic lifecycle-metering and reconciliation tests."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Event
from unittest import mock

from bespokelabs.sandbox.control_plane import store as store_module
from bespokelabs.sandbox.control_plane.errors import PolicyDeniedError
from bespokelabs.sandbox.control_plane.reconciliation import (
    ProviderObservation,
    normalize_observation,
    provider_timestamp,
)
from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from bespokelabs.sandbox.exceptions import (
    ErrorCode,
    ErrorOutcome,
    SandboxError,
    SandboxNotFoundError,
    SandboxTimeoutError,
)
from bespokelabs.sandbox.types import SandboxResult

UTC = timezone.utc


class _Clock:

    def __init__(self, *values: datetime) -> None:
        self.values = list(values)
        self.last = values[-1]

    def __call__(self) -> datetime:
        if self.values:
            self.last = self.values.pop(0)
        return self.last


class _Runtime:
    backend_name = "daytona"

    def __init__(self, resource_id: str) -> None:
        self.provider_resource_id = resource_id

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        return SandboxResult(stdout=code)

    def execute_command(self, command: str, args=None) -> SandboxResult:
        return SandboxResult(stdout=command)

    def estimate_compute_cost(self, elapsed_secs: float) -> float:
        return 0

    def destroy(self) -> None:
        pass


class _Factory:

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def __call__(self, backend: str, **config: object) -> _Runtime:
        del config
        self.calls += 1
        if self.fail:
            raise SandboxTimeoutError(
                "opaque timeout",
                backend=backend,
                op="create",
                context={"cleanup_status": "deleted"},
                outcome=ErrorOutcome.FAILED,
            )
        return _Runtime(f"resource-{self.calls}")


class _Reconciler:

    def __init__(self) -> None:
        self.by_org: dict[str, list[ProviderObservation]] = {}
        self.failures = 0
        self.calls: list[str] = []

    def list_resources(self, organization_id: str) -> list[ProviderObservation]:
        self.calls.append(organization_id)
        if self.failures:
            self.failures -= 1
            raise ConnectionError("provider unavailable")
        return self.by_org.get(organization_id, [])


class _LookupReconciler(_Reconciler):

    def __init__(self) -> None:
        super().__init__()
        self.result = None
        self.lookup_calls: list[tuple[str, str]] = []

    def get_resource(self, organization_id: str, provider_resource_id: str):
        self.lookup_calls.append((organization_id, provider_resource_id))
        if isinstance(self.result, Exception):
            raise self.result
        if callable(self.result):
            return self.result()
        return self.result


class LifecycleReconciliationTest(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(
            Path(self.temp_dir.name) / "phase2.db", key_pepper="phase2"
        )
        self.base = datetime(2026, 1, 1, tzinfo=UTC)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _service(self, clock: _Clock, factory=None, reconciler=None):
        service = ControlPlane(
            self.store,
            sandbox_factory=factory or _Factory(),
            allowed_backends={"daytona"},
            provider_reconcilers={"daytona": reconciler or _Reconciler()},
            now=clock,
            customer_markup=Decimal("1.25"),
        )
        _, key = service.bootstrap_organization("Acme")
        return service, self.store.authenticate(key.secret)

    def _report_one_hour(self, service, principal, sandbox, reconciler, clock):
        clock.last = self.base + timedelta(hours=1)
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                f"one-hour:{sandbox.id}",
                sandbox.provider_resource_id,
                "running",
                clock.last.isoformat(),
                Decimal("1"),
            )
        ]
        self.assertEqual(service.reconcile(principal, "daytona")["updated"], 1)
        reconciler.by_org[principal.organization_id] = []

    def _assert_two_hour_window(
        self, service, principal, sandbox, expected_cost=Decimal("1.054")
    ):
        summary = service.cost_summary(principal)[0]
        self.assertEqual(Decimal(summary["runtime_seconds"]), Decimal("7200"))
        self.assertEqual(Decimal(summary["provider_cost_usd"]), expected_cost)
        with sqlite3.connect(self.store._path) as connection:
            rows = connection.execute(
                "SELECT created_at, billable_seconds_delta, provider_delta_usd "
                "FROM lifecycle_ledger_entries WHERE sandbox_id=?",
                (sandbox.id,),
            ).fetchall()
            updated_at = connection.execute(
                "SELECT updated_at FROM lifecycle_costs WHERE sandbox_id=?",
                (sandbox.id,),
            ).fetchone()[0]
        closed = self.base + timedelta(hours=2)
        self.assertEqual(datetime.fromisoformat(updated_at), closed)
        self.assertEqual(sum(Decimal(row[1]) for row in rows), Decimal("7200"))
        self.assertEqual(sum(Decimal(row[2]) for row in rows), expected_cost)
        for created, seconds, _ in rows:
            self.assertLessEqual(
                datetime.fromisoformat(created)
                + timedelta(seconds=float(seconds)),
                closed,
            )

    def test_inflight_missing_fences_stale_cost_reader_at_commit(self):
        clock = _Clock(self.base, self.base)
        reconciler = _LookupReconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        self._report_one_hour(service, principal, sandbox, reconciler, clock)
        lookup_started, finish_lookup = Event(), Event()
        reader_started, finish_reader = Event(), Event()
        original = service._record_lifecycle_estimate

        def lookup():
            lookup_started.set()
            self.assertTrue(finish_lookup.wait(10))
            return None

        def stale_reader(record, end_at, **kwargs):
            if end_at == (self.base + timedelta(hours=3)).isoformat():
                self.assertEqual(record.status, "running")
                reader_started.set()
                self.assertTrue(finish_reader.wait(10))
            return original(record, end_at, **kwargs)

        reconciler.result = lookup
        service._record_lifecycle_estimate = stale_reader
        clock.last = self.base + timedelta(hours=2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            missing = pool.submit(service.reconcile, principal, "daytona")
            try:
                self.assertTrue(lookup_started.wait(10))
                clock.last = self.base + timedelta(hours=3)
                reader = pool.submit(service.cost_summary, principal)
                self.assertTrue(reader_started.wait(10))
                finish_lookup.set()
                self.assertEqual(missing.result(timeout=10)["missing"], 1)
                finish_reader.set()
                reader.result(timeout=10)
            finally:
                finish_lookup.set()
                finish_reader.set()
                service._record_lifecycle_estimate = original
        record = service.get_sandbox(principal, sandbox.id)
        self.assertEqual(record.status, "failed")
        self.assertEqual(
            record.failed_at, (self.base + timedelta(hours=2)).isoformat()
        )
        self._assert_two_hour_window(service, principal, sandbox)

    def test_stale_reader_cannot_reopen_destroy_fail_or_expiry_window(self):
        for closure in ("destroy", "fail", "expiry"):
            with self.subTest(closure=closure):
                clock = _Clock(self.base, self.base)
                reconciler = _Reconciler()
                service, principal = self._service(clock, reconciler=reconciler)
                sandbox = service.create_sandbox(principal, "daytona", {})
                self._report_one_hour(
                    service, principal, sandbox, reconciler, clock
                )
                reader_started, finish_reader = Event(), Event()
                original = service._record_lifecycle_estimate
                stale_time = self.base + timedelta(hours=3)

                def stale_reader(
                    record,
                    end_at,
                    *,
                    stale_time=stale_time,
                    reader_started=reader_started,
                    finish_reader=finish_reader,
                    original=original,
                    **kwargs,
                ):
                    if end_at == stale_time.isoformat():
                        self.assertEqual(record.status, "running")
                        reader_started.set()
                        self.assertTrue(finish_reader.wait(10))
                    return original(record, end_at, **kwargs)

                service._record_lifecycle_estimate = stale_reader
                clock.last = stale_time
                with ThreadPoolExecutor(max_workers=1) as pool:
                    reader = pool.submit(service.cost_summary, principal)
                    try:
                        self.assertTrue(reader_started.wait(10))
                        clock.last = self.base + timedelta(hours=2)
                        if closure == "destroy":
                            service.destroy_sandbox(principal, sandbox.id)
                        elif closure == "expiry":
                            with sqlite3.connect(
                                self.store._path
                            ) as connection:
                                connection.execute(
                                    "UPDATE sandboxes SET expires_at=? WHERE id=?",
                                    (clock.last.isoformat(), sandbox.id),
                                )
                            self.assertEqual(
                                service.run_watchdog_once()[
                                    "expired_terminated"
                                ],
                                1,
                            )
                        else:
                            # A trusted terminal failure persists closure before
                            # final billing. RPC failure alone is NOT closure.
                            self.store.mark_lifecycle(
                                sandbox.id, "failed", clock.last.isoformat()
                            )
                        finish_reader.set()
                        reader.result(timeout=10)
                    finally:
                        finish_reader.set()
                        service._record_lifecycle_estimate = original
                terminal = service.get_sandbox(principal, sandbox.id)
                original(
                    terminal,
                    clock.last.isoformat(),
                    observation_id=f"final:{sandbox.id}",
                )
                self._assert_two_hour_window(service, principal, sandbox)
                before = self._observation_snapshot(sandbox.id)
                original(
                    terminal,
                    stale_time.isoformat(),
                    observation_id=f"stale:{sandbox.id}",
                )
                self.assertEqual(self._observation_snapshot(sandbox.id), before)
                original(
                    terminal,
                    stale_time.isoformat(),
                    observation_id=f"adjustment:{sandbox.id}",
                    provider_cost=Decimal("2"),
                )
                summary = service.cost_summary(principal)[0]
                self.assertEqual(
                    Decimal(summary["runtime_seconds"]), Decimal("7200")
                )
                self.assertEqual(
                    Decimal(summary["provider_cost_usd"]), Decimal("2")
                )
                self._assert_two_hour_window(
                    service, principal, sandbox, expected_cost=Decimal("2")
                )
                self.assertEqual(
                    service.get_sandbox(principal, sandbox.id).status,
                    terminal.status,
                )

    def test_termination_rpc_failure_holds_capacity_budget_and_restart(self):
        for cleanup in ("destroy", "close"):
            with self.subTest(cleanup=cleanup):
                clock = _Clock(self.base, self.base)
                reconciler = _LookupReconciler()
                reconciler.result = ConnectionError("unavailable")
                service, principal = self._service(clock, reconciler=reconciler)
                sandbox = service.create_sandbox(principal, "daytona", {})

                def fail():
                    raise SandboxError(
                        "opaque RPC failure",
                        backend="daytona",
                        op="destroy",
                        outcome=ErrorOutcome.UNKNOWN,
                    )

                service._runtimes[sandbox.id].destroy = fail
                clock.last = self.base + timedelta(seconds=60)
                if cleanup == "close":
                    service.close()
                else:
                    with self.assertRaises(SandboxError):
                        service.destroy_sandbox(principal, sandbox.id)
                record = service.get_sandbox(principal, sandbox.id)
                self.assertEqual(record.status, "failed")
                self.assertIsNone(record.terminated_at)
                # Errors/events are diagnostics, not the durable accounting bit.
                with self.store.connect() as connection:
                    connection.execute(
                        "UPDATE sandboxes SET latest_error_json=NULL WHERE id=?",
                        (sandbox.id,),
                    )
                restarted_store = SQLiteStore(
                    self.store._path, key_pepper="phase2"
                )
                restarted = ControlPlane(
                    restarted_store,
                    sandbox_factory=_Factory(),
                    allowed_backends={"daytona"},
                    now=clock,
                    provider_reconcilers={"daytona": reconciler},
                    resource_sampling_interval_secs=0,
                )
                clock.last = self.base + timedelta(hours=1)
                summary = restarted.policy_summary(principal)
                self.assertEqual(summary["usage"]["active_sandboxes"], 1)
                self.assertTrue(restarted_store.resource_is_held(sandbox.id))
                cost = restarted.cost_summary(principal)[0]
                self.assertEqual(
                    Decimal(cost["runtime_seconds"]), Decimal(3600)
                )
                self.assertGreater(
                    Decimal(cost["provider_cost_usd"]), Decimal(0)
                )
                self.assertEqual(
                    restarted.reconcile(principal, "daytona")["unknown"], 1
                )
                restarted.set_policy(
                    principal,
                    max_concurrent_sandboxes=1,
                    hourly_spend_limit_usd=None,
                    daily_spend_limit_usd=None,
                    allowed_backends=None,
                    allowed_gpu_types=None,
                    max_sandbox_lifetime_secs=None,
                )
                with self.assertRaises(PolicyDeniedError):
                    restarted.create_sandbox(principal, "daytona", {})
                restarted.set_policy(
                    principal,
                    max_concurrent_sandboxes=None,
                    hourly_spend_limit_usd=Decimal("0.00001"),
                    daily_spend_limit_usd=None,
                    allowed_backends=None,
                    allowed_gpu_types=None,
                    max_sandbox_lifetime_secs=None,
                )
                with self.assertRaises(PolicyDeniedError):
                    restarted.create_sandbox(principal, "daytona", {})

    def test_uncertain_stale_reader_and_late_failure_cannot_reopen_closure(
        self,
    ):
        clock = _Clock(self.base, self.base)
        reconciler = _LookupReconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        stopping = self.store.begin_termination(
            principal.organization_id,
            sandbox.id,
            timestamp=(self.base + timedelta(seconds=60)).isoformat(),
        )
        self.store.fail_termination(
            sandbox.id,
            timestamp=(self.base + timedelta(seconds=60)).isoformat(),
            error={},
            expected_version=stopping.version,
        )
        # A positive provider total extends the held lifetime but never makes
        # the failed handle executable again, nor releases its capacity.
        self._report_one_hour(service, principal, sandbox, reconciler, clock)
        self.assertTrue(self.store.resource_is_held(sandbox.id))
        self.assertEqual(
            service.get_sandbox(principal, sandbox.id).status, "failed"
        )
        reader_started, finish_reader = Event(), Event()
        original = service._record_lifecycle_estimate
        stale_time = self.base + timedelta(hours=3)

        def stale_reader(record, end_at, **kwargs):
            if end_at == stale_time.isoformat():
                self.assertEqual(record.status, "failed")
                reader_started.set()
                self.assertTrue(finish_reader.wait(10))
            return original(record, end_at, **kwargs)

        service._record_lifecycle_estimate = stale_reader
        clock.last = stale_time
        with ThreadPoolExecutor(max_workers=1) as pool:
            reader = pool.submit(service.cost_summary, principal)
            try:
                self.assertTrue(reader_started.wait(10))
                clock.last = self.base + timedelta(hours=2)
                self.assertEqual(
                    service.reconcile(principal, "daytona")["missing"], 1
                )
                finish_reader.set()
                reader.result(timeout=10)
            finally:
                finish_reader.set()
                service._record_lifecycle_estimate = original
        closed = service.get_sandbox(principal, sandbox.id)
        self.assertEqual(closed.failed_at, clock.last.isoformat())
        self.assertFalse(self.store.resource_is_held(sandbox.id))
        self.assertEqual(
            service.policy_summary(principal)["usage"]["active_sandboxes"], 0
        )
        self._assert_two_hour_window(service, principal, sandbox)
        snapshot = self._observation_snapshot(sandbox.id)
        for version in (None, stopping.version):
            self.store.fail_termination(
                sandbox.id,
                timestamp=stale_time.isoformat(),
                error={"late": True},
                expected_version=version,
            )
            self.assertEqual(service.get_sandbox(principal, sandbox.id), closed)
            self.assertFalse(self.store.resource_is_held(sandbox.id))
        clock.last = self.base + timedelta(hours=4)
        service.cost_summary(principal)
        self.assertEqual(self._observation_snapshot(sandbox.id), snapshot)
        original(
            closed,
            clock.last.isoformat(),
            observation_id="final-adjustment",
            provider_cost=Decimal(2),
        )
        self._assert_two_hour_window(service, principal, sandbox, Decimal(2))

    def test_uncertain_hold_requires_nonconflicting_trusted_terminal_evidence(
        self,
    ):
        clock = _Clock(self.base, self.base)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        self.store.fail_termination(
            sandbox.id, timestamp=self.base.isoformat(), error={}
        )
        clock.last = self.base + timedelta(hours=1)
        for status in ("running", "terminated", "unknown"):
            reconciler.by_org[principal.organization_id] = [
                ProviderObservation(
                    f"held:{status}",
                    sandbox.provider_resource_id,
                    status,
                    clock.last.isoformat(),
                )
            ]
            result = service.reconcile(principal, "daytona")
            self.assertEqual(result["unknown"], 1)
            self.assertEqual(
                service.policy_summary(principal)["usage"]["active_sandboxes"],
                1,
            )
            self.assertTrue(self.store.resource_is_held(sandbox.id))
            self.assertEqual(
                service.get_sandbox(principal, sandbox.id).status, "failed"
            )
        clock.last += timedelta(hours=1)
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "confirmed-held-terminal",
                sandbox.provider_resource_id,
                "terminated",
                clock.last.isoformat(),
            )
        ]
        self.assertEqual(service.reconcile(principal, "daytona")["updated"], 1)
        self.assertEqual(
            service.policy_summary(principal)["usage"]["active_sandboxes"], 0
        )
        self.assertFalse(self.store.resource_is_held(sandbox.id))
        self.assertEqual(
            service.get_sandbox(principal, sandbox.id).terminated_at,
            clock.last.isoformat(),
        )

    def test_migration_backfills_legacy_termination_holds_without_end_proof(
        self,
    ):
        clock = _Clock(self.base, self.base)
        service, principal = self._service(clock)
        ambiguous = service.create_sandbox(principal, "daytona", {})
        unmarked_allocation = service.create_sandbox(principal, "daytona", {})
        confirmed = service.create_sandbox(principal, "daytona", {})
        no_allocation, _ = self.store.claim_sandbox_creation(
            principal.organization_id,
            "daytona",
            {},
            timestamp=self.base.isoformat(),
        )
        self.store.complete_sandbox_creation(
            no_allocation.id,
            status="failed",
            cleanup_status="deleted",
            timestamp=self.base.isoformat(),
        )
        stopped = (self.base + timedelta(seconds=60)).isoformat()
        for sandbox in (ambiguous, confirmed):
            self.store.begin_termination(
                principal.organization_id, sandbox.id, timestamp=stopped
            )
            self.store.fail_termination(sandbox.id, timestamp=stopped, error={})
        observed = (self.base + timedelta(seconds=120)).isoformat()
        self.store.reconcile_provider_observation(
            principal.organization_id,
            confirmed.id,
            backend="daytona",
            observation=ProviderObservation(
                "legacy-terminal",
                confirmed.provider_resource_id,
                "failed",
                observed,
            ),
            customer_markup=Decimal(1),
            now=self.base + timedelta(seconds=120),
        )
        with self.store.connect() as connection:
            connection.execute("UPDATE sandboxes SET latest_error_json=NULL")
            connection.execute(
                "UPDATE sandboxes SET status='failed', failed_at=? WHERE id=?",
                (stopped, unmarked_allocation.id),
            )
            connection.execute("DELETE FROM schema_migrations WHERE version=11")
            connection.execute(
                "ALTER TABLE sandboxes DROP COLUMN resource_hold"
            )
        migrated = SQLiteStore(self.store._path, key_pepper="phase2")
        self.assertTrue(migrated.resource_is_held(ambiguous.id))
        self.assertTrue(migrated.resource_is_held(unmarked_allocation.id))
        # Old failed_at plus an observation does not prove that it applied.
        # Even genuinely applied legacy failure is ambiguous without an end.
        self.assertTrue(migrated.resource_is_held(confirmed.id))
        self.assertFalse(migrated.resource_is_held(no_allocation.id))
        self.assertEqual(
            migrated.policy_summary(principal.organization_id)["usage"][
                "active_sandboxes"
            ],
            3,
        )

    def test_real_base_legacy_diagnostics_never_prove_migration_closure(self):
        fixture = json.loads(
            (
                Path(__file__).parent
                / "fixtures"
                / "legacy_termination_diagnostics.json"
            ).read_text()
        )
        self.assertEqual(
            fixture["source_commit"], "f845e8b53cc9bd5101fc3751582e3ecdf3fe921b"
        )
        path = Path(self.temp_dir.name) / "real-base.db"
        # Build precisely the legacy schema, then restore rows captured using
        # actual f845 store methods. No Git/history dependency in the test lane.
        with mock.patch.object(
            store_module, "_MIGRATIONS", store_module._MIGRATIONS[:10]
        ):
            legacy = SQLiteStore(path, key_pepper="fixture")
        with legacy.connect() as connection:
            for table, rows in fixture["tables"].items():
                for row in rows:
                    columns = ", ".join(row)
                    placeholders = ", ".join("?" for _ in row)
                    connection.execute(
                        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                        tuple(row.values()),
                    )
        for reopen in range(2):
            migrated = SQLiteStore(path, key_pepper="fixture")
            for case in fixture["cases"]:
                with self.subTest(reopen=reopen, case=case["name"]):
                    self.assertEqual(
                        migrated.resource_is_held(case["sandbox_id"]),
                        case["hold"],
                    )
            self.assertEqual(
                migrated.policy_summary(fixture["organization_id"])["usage"][
                    "active_sandboxes"
                ],
                4,
            )
        key = migrated.issue_api_key(
            fixture["organization_id"], name="Owner", scopes=("*",)
        )
        principal = migrated.authenticate(key.secret)
        reconciler = _LookupReconciler()
        clock = _Clock(self.base + timedelta(hours=2))
        service = ControlPlane(
            migrated,
            allowed_backends={"modal"},
            provider_reconcilers={"modal": reconciler},
            now=clock,
            resource_sampling_interval_secs=0,
        )
        costs = service.cost_summary(principal)
        self.assertEqual(
            sum(Decimal(row["runtime_seconds"]) for row in costs),
            Decimal(4 * 7200),
        )
        # A fresh, scoped direct lookup now establishes absence, not the mere
        # presence/order of any historical diagnostic terminal observation.
        clock.last = self.base + timedelta(hours=3)
        self.assertEqual(service.reconcile(principal, "modal")["missing"], 4)
        self.assertEqual(
            service.policy_summary(principal)["usage"]["active_sandboxes"], 0
        )
        closed_costs = service.cost_summary(principal)
        clock.last += timedelta(hours=1)
        self.assertEqual(service.cost_summary(principal), closed_costs)

    def test_cross_run_equal_time_live_terminal_conflicts_are_unknown(self):
        for first, second in (
            ("running", "terminated"),
            ("terminated", "running"),
        ):
            for same_listing in (False, True):
                with self.subTest(first=first, same_listing=same_listing):
                    clock = _Clock(self.base, self.base)
                    reconciler = _Reconciler()
                    service, principal = self._service(
                        clock, reconciler=reconciler
                    )
                    sandbox = service.create_sandbox(principal, "daytona", {})
                    stamp = self.base + timedelta(minutes=5)
                    clock.last = stamp
                    a = ProviderObservation(
                        f"a:{sandbox.id}",
                        sandbox.provider_resource_id,
                        first,
                        stamp.isoformat(),
                    )
                    b = ProviderObservation(
                        f"b:{sandbox.id}",
                        sandbox.provider_resource_id,
                        second,
                        stamp.isoformat(),
                    )
                    reconciler.by_org[principal.organization_id] = [a]
                    self.assertEqual(
                        service.reconcile(principal, "daytona")["updated"], 1
                    )
                    before = self._observation_snapshot(sandbox.id)
                    reconciler.by_org[principal.organization_id] = (
                        [a, b] if same_listing else [b]
                    )
                    result = service.reconcile(principal, "daytona")
                    self.assertEqual(result["updated"], 0)
                    self.assertEqual(result["missing"], 0)
                    self.assertEqual(result["unknown"], 1)
                    self.assertGreaterEqual(result["rejected"], 1)
                    self.assertEqual(result["status"], "partial_outage")
                    after = self._observation_snapshot(sandbox.id)
                    # Diagnostic evidence may change; state and all cost tables
                    # must not. Replays cannot mutate even the diagnostic rows.
                    self.assertEqual(
                        after[:1] + after[2:], before[:1] + before[2:]
                    )
                    replay = service.reconcile(principal, "daytona")
                    self.assertEqual(replay["unknown"], 1)
                    self.assertEqual(
                        self._observation_snapshot(sandbox.id), after
                    )
                    clock.last += timedelta(hours=1)
                    reconciler.by_org[principal.organization_id] = [
                        ProviderObservation(
                            f"mixed:{sandbox.id}",
                            sandbox.provider_resource_id,
                            second,
                            (stamp - timedelta(minutes=1)).isoformat(),
                        ),
                        b,
                    ]
                    mixed = service.reconcile(principal, "daytona")
                    self.assertEqual(mixed["updated"], 0)
                    self.assertEqual(mixed["unknown"], 1)
                    self.assertGreaterEqual(mixed["rejected"], 2)
                    record = service.get_sandbox(principal, sandbox.id)
                    self.assertEqual(
                        record.status,
                        "running" if first == "running" else "destroyed",
                    )
                    self.assertEqual(
                        record.last_provider_observed_at, stamp.isoformat()
                    )
                    if first == "running":
                        self.assertEqual(
                            Decimal(
                                service.cost_summary(principal)[0][
                                    "runtime_seconds"
                                ]
                            ),
                            Decimal("3900"),
                        )
                        reconciler.by_org[principal.organization_id] = [
                            ProviderObservation(
                                f"newer:{sandbox.id}",
                                sandbox.provider_resource_id,
                                "terminated",
                                clock.last.isoformat(),
                                Decimal("2"),
                            )
                        ]
                        self.assertEqual(
                            service.reconcile(principal, "daytona")["updated"],
                            1,
                        )
                        self.assertEqual(
                            service.get_sandbox(principal, sandbox.id).status,
                            "destroyed",
                        )
                        self.assertEqual(
                            Decimal(
                                service.cost_summary(principal)[0][
                                    "provider_cost_usd"
                                ]
                            ),
                            Decimal("2"),
                        )

    def test_failed_provisioning_accrues_nonzero_lifecycle_cost(self) -> None:
        clock = _Clock(self.base, self.base + timedelta(minutes=10))
        service, principal = self._service(clock, factory=_Factory(fail=True))

        with self.assertRaises(SandboxTimeoutError):
            service.create_sandbox(principal, "daytona", {})

        record = service.list_sandboxes(principal)[0]
        summary = service.cost_summary(principal)[0]
        self.assertEqual(record.requested_at, self.base.isoformat())
        self.assertEqual(record.provisioning_at, self.base.isoformat())
        self.assertEqual(
            record.failed_at, (self.base + timedelta(minutes=10)).isoformat()
        )
        self.assertEqual(record.cost_state, "estimated")
        self.assertEqual(Decimal(record.hourly_rate_usd), Decimal("0.054"))
        self.assertEqual(record.currency, "USD")
        self.assertEqual(record.pricing_source, "2026-03-28")
        self.assertEqual(Decimal(summary["runtime_seconds"]), Decimal("600.0"))
        self.assertGreater(Decimal(summary["provider_cost_usd"]), 0)
        with sqlite3.connect(self.store._path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM lifecycle_ledger_entries"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_provider_cost_replaces_estimate_without_double_counting(
        self,
    ) -> None:
        observed = self.base + timedelta(hours=1)
        clock = _Clock(self.base, self.base, observed)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        service.cost_summary(principal)
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "obs-1",
                sandbox.provider_resource_id or "",
                "running",
                observed.isoformat(),
                Decimal("0.123456789012345678"),
            )
        ]

        first = service.reconcile(principal, "daytona")
        second = service.reconcile(principal, "daytona")
        summary = service.cost_summary(principal)[0]

        self.assertEqual(first["updated"], 1)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(second["rejected"], 1)
        self.assertEqual(
            Decimal(summary["provider_cost_usd"]),
            Decimal("0.123456789012345678"),
        )
        refreshed = service.get_sandbox(principal, sandbox.id)
        self.assertEqual(refreshed.cost_state, "reconciled")
        reconciliation = service.reconciliation_summary(principal)
        self.assertEqual(reconciliation["provider_reported"], 1)
        self.assertEqual(reconciliation["reconciled"], 1)
        self.assertEqual(refreshed.pricing_source, "provider")
        self.assertEqual(
            refreshed.last_provider_observed_at, observed.isoformat()
        )
        with sqlite3.connect(self.store._path) as connection:
            deltas = connection.execute(
                "SELECT provider_delta_usd FROM lifecycle_ledger_entries"
            ).fetchall()
        self.assertEqual(
            sum((Decimal(row[0]) for row in deltas), Decimal(0)),
            Decimal("0.123456789012345678"),
        )
        self.assertEqual(len(deltas), 2)

        later = observed + timedelta(hours=1)
        clock.last = later
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "obs-status-only",
                sandbox.provider_resource_id or "",
                "running",
                later.isoformat(),
            )
        ]
        service.reconcile(principal, "daytona")
        service.reconcile(principal, "daytona")
        preserved = service.cost_summary(principal)[0]
        refreshed = service.get_sandbox(principal, sandbox.id)
        # A cumulative provider cost covers only the observed window. A later
        # status-only observation must not make the still-live hour free.
        self.assertEqual(
            Decimal(preserved["provider_cost_usd"]),
            Decimal("0.123456789012345678") + Decimal("0.054"),
        )
        self.assertEqual(refreshed.cost_state, "reconciled")
        self.assertEqual(refreshed.pricing_source, "provider")
        with sqlite3.connect(self.store._path) as connection:
            after = connection.execute(
                "SELECT provider_delta_usd FROM lifecycle_ledger_entries"
            ).fetchall()
        self.assertEqual(
            sum((Decimal(row[0]) for row in after), Decimal(0)),
            Decimal("0.123456789012345678") + Decimal("0.054"),
        )
        self.assertEqual(len(after), 3)

    def test_partial_outage_retry_disappearance_orphan_and_tenant_safety(
        self,
    ) -> None:
        clock = _Clock(self.base, self.base, self.base + timedelta(minutes=5))
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        _, other_key = service.bootstrap_organization("Other")
        other = self.store.authenticate(other_key.secret)
        other_sandbox = service.create_sandbox(other, "daytona", {})

        reconciler.failures = 1
        self.assertEqual(
            service.reconcile(principal, "daytona")["status"], "partial_outage"
        )
        self.assertIsNone(
            service.get_sandbox(principal, sandbox.id).last_provider_observed_at
        )
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "orphan-1",
                "untracked-resource",
                "running",
                self.base.isoformat(),
            )
        ]
        result = service.reconcile(principal, "daytona")

        # Inventory omission is uncertainty, not a terminal lifecycle event.
        # In particular, a list-only adapter cannot release capacity or billing.
        self.assertEqual(result["missing"], 0)
        self.assertEqual(result["orphans"], 1)
        retained = service.get_sandbox(principal, sandbox.id)
        self.assertFalse(retained.provider_missing)
        self.assertEqual(retained.status, "running")
        self.assertIsNone(retained.failed_at)
        self.assertIsNone(retained.terminated_at)
        self.assertGreater(
            Decimal(service.cost_summary(principal)[0]["provider_cost_usd"]),
            0,
        )
        untouched = service.get_sandbox(other, other_sandbox.id)
        self.assertFalse(untouched.provider_missing)
        self.assertEqual(untouched.status, "running")
        self.assertEqual(reconciler.calls, [principal.organization_id] * 2)
        before = Decimal(
            service.cost_summary(principal)[0]["provider_cost_usd"]
        )
        clock.last += timedelta(minutes=5)
        after = Decimal(service.cost_summary(principal)[0]["provider_cost_usd"])
        self.assertGreater(after, before)
        with sqlite3.connect(self.store._path) as connection:
            active = connection.execute(
                """SELECT COUNT(*) FROM sandboxes WHERE organization_id=?
                   AND status IN ('creating', 'running', 'stopping')""",
                (principal.organization_id,),
            ).fetchone()[0]
        self.assertEqual(active, 1)

    def test_direct_absence_is_terminal_and_repeated_confirmation_is_free(self):
        for typed in (False, True):
            with self.subTest(typed=typed):
                clock = _Clock(self.base, self.base)
                reconciler = _LookupReconciler()
                service, principal = self._service(clock, reconciler=reconciler)
                sandbox = service.create_sandbox(principal, "daytona", {})
                if typed:
                    reconciler.result = SandboxNotFoundError(
                        "resource absent", backend="daytona", op="get"
                    )
                clock.last += timedelta(minutes=5)
                confirmed_at = clock.last.isoformat()
                first = service.reconcile(principal, "daytona")
                terminal = service.get_sandbox(principal, sandbox.id)
                before = service.cost_summary(principal)
                with sqlite3.connect(self.store._path) as connection:
                    count = connection.execute(
                        "SELECT COUNT(*) FROM lifecycle_ledger_entries WHERE sandbox_id=?",
                        (sandbox.id,),
                    ).fetchone()[0]
                clock.last += timedelta(hours=1)
                second = service.reconcile(principal, "daytona")
                self.assertEqual(first["missing"], 1)
                self.assertEqual(second["missing"], 0)
                self.assertEqual(terminal.status, "failed")
                self.assertTrue(terminal.provider_missing)
                self.assertEqual(terminal.failed_at, confirmed_at)
                self.assertEqual(
                    terminal.last_provider_observed_at, confirmed_at
                )
                self.assertEqual(service.cost_summary(principal), before)
                self.assertEqual(
                    reconciler.lookup_calls,
                    [(principal.organization_id, sandbox.provider_resource_id)],
                )
                with sqlite3.connect(self.store._path) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM lifecycle_ledger_entries WHERE sandbox_id=?",
                            (sandbox.id,),
                        ).fetchone()[0],
                        count,
                    )

    def test_lookup_can_find_a_resource_omitted_from_the_list(self):
        clock = _Clock(self.base, self.base)
        reconciler = _LookupReconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        clock.last += timedelta(minutes=5)
        reconciler.result = ProviderObservation(
            "direct-present",
            sandbox.provider_resource_id,
            "running",
            clock.last.isoformat(),
            Decimal("0.08"),
            organization_id=principal.organization_id,
            backend="daytona",
        )
        result = service.reconcile(principal, "daytona")
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["missing"], 0)
        self.assertFalse(
            service.get_sandbox(principal, sandbox.id).provider_missing
        )
        self.assertEqual(
            Decimal(service.cost_summary(principal)[0]["provider_cost_usd"]),
            Decimal("0.08"),
        )

    def test_unknown_and_lookup_errors_retain_capacity_and_billing(self):
        cases = [
            ConnectionError("temporary outage"),
            PermissionError("not authorized"),
            RuntimeError("404 not found"),
            SandboxError("not found", code=ErrorCode.NOT_FOUND),
            SandboxNotFoundError("wrong backend", backend="other"),
            SandboxNotFoundError("uncertain", outcome=ErrorOutcome.UNKNOWN),
            SandboxNotFoundError(
                "wrong tenant", context={"organization_id": "other"}
            ),
            SandboxNotFoundError(
                "wrong resource", context={"provider_resource_id": "other"}
            ),
            "unknown observation",
            "wrong resource",
            "wrong tenant",
            "wrong backend",
        ]
        for case in cases:
            with self.subTest(case=case):
                clock = _Clock(self.base, self.base)
                reconciler = _LookupReconciler()
                service, principal = self._service(clock, reconciler=reconciler)
                sandbox = service.create_sandbox(principal, "daytona", {})
                clock.last += timedelta(minutes=5)
                if isinstance(case, str):
                    reconciler.result = ProviderObservation(
                        "lookup",
                        "other"
                        if case == "wrong resource"
                        else sandbox.provider_resource_id,
                        "unknown"
                        if case == "unknown observation"
                        else "missing",
                        clock.last.isoformat(),
                        Decimal("999"),
                        organization_id="other"
                        if case == "wrong tenant"
                        else principal.organization_id,
                        backend="other"
                        if case == "wrong backend"
                        else "daytona",
                    )
                else:
                    reconciler.result = case
                result = service.reconcile(principal, "daytona")
                record = service.get_sandbox(principal, sandbox.id)
                self.assertEqual(result["missing"], 0)
                self.assertEqual(record.status, "running")
                self.assertFalse(record.provider_missing)
                self.assertIsNone(record.failed_at)
                self.assertEqual(record.cost_state, "estimated")
                before = Decimal(
                    service.cost_summary(principal)[0]["provider_cost_usd"]
                )
                self.assertLess(before, Decimal("999"))
                clock.last += timedelta(minutes=5)
                self.assertGreater(
                    Decimal(
                        service.cost_summary(principal)[0]["provider_cost_usd"]
                    ),
                    before,
                )

    def test_unknown_listing_and_listing_tombstone_are_not_authoritative(self):
        clock = _Clock(self.base, self.base)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        for status in ("unknown", "missing"):
            clock.last += timedelta(minutes=5)
            reconciler.by_org[principal.organization_id] = [
                ProviderObservation(
                    status,
                    sandbox.provider_resource_id,
                    status,
                    clock.last.isoformat(),
                    Decimal("999"),
                )
            ]
            self.assertEqual(
                service.reconcile(principal, "daytona")["missing"], 0
            )
            self.assertEqual(
                service.get_sandbox(principal, sandbox.id).status, "running"
            )
        self.assertEqual(
            service.get_sandbox(principal, sandbox.id).cost_state, "estimated"
        )
        with sqlite3.connect(self.store._path) as connection:
            statuses = connection.execute(
                "SELECT status, provider_cost_usd FROM provider_observations WHERE sandbox_id=?",
                (sandbox.id,),
            ).fetchall()
        self.assertEqual(statuses, [("unknown", None), ("unknown", None)])

    def test_explicit_listing_scope_and_store_identity_are_fenced(self):
        clock = _Clock(self.base, self.base)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        _, key = service.bootstrap_organization("Other")
        other = self.store.authenticate(key.secret)
        for organization_id, backend, resource_id in [
            (other.organization_id, "daytona", sandbox.provider_resource_id),
            (principal.organization_id, "other", sandbox.provider_resource_id),
            (principal.organization_id, "daytona", "other-resource"),
        ]:
            clock.last += timedelta(minutes=1)
            observation = ProviderObservation(
                "wrong-identity",
                resource_id,
                "terminated",
                clock.last.isoformat(),
                Decimal("999"),
                organization_id=organization_id,
                backend=backend,
            )
            reconciler.by_org[principal.organization_id] = [observation]
            self.assertEqual(
                service.reconcile(principal, "daytona")["updated"], 0
            )
            self.assertFalse(
                self.store.reconcile_provider_observation(
                    organization_id,
                    sandbox.id,
                    backend=backend,
                    observation=observation,
                    customer_markup=Decimal("1.25"),
                )
            )
        record = service.get_sandbox(principal, sandbox.id)
        self.assertEqual(record.status, "running")
        self.assertFalse(record.provider_missing)
        self.assertIsNone(record.terminated_at)

    def test_stale_and_same_time_absence_cannot_override_positive_or_cost(self):
        clock = _Clock(self.base, self.base)
        reconciler = _LookupReconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        clock.last += timedelta(minutes=10)
        positive_at = clock.last
        positive = ProviderObservation(
            "positive",
            sandbox.provider_resource_id,
            "running",
            positive_at.isoformat(),
            Decimal("0.2"),
        )
        reconciler.by_org[principal.organization_id] = [positive]
        service.reconcile(principal, "daytona")
        for offset in (-60, 0):
            reconciler.by_org[principal.organization_id] = []
            reconciler.result = ProviderObservation(
                f"old-missing:{offset}",
                sandbox.provider_resource_id,
                "missing",
                (positive_at + timedelta(seconds=offset)).isoformat(),
                Decimal("999"),
            )
            self.assertEqual(
                service.reconcile(principal, "daytona")["missing"], 0
            )
            self.assertEqual(
                service.get_sandbox(principal, sandbox.id).status, "running"
            )
        # Stale positive cost/status observations are fenced as well.
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "old-terminal",
                sandbox.provider_resource_id,
                "terminated",
                (positive_at - timedelta(minutes=1)).isoformat(),
                Decimal("999"),
            )
        ]
        self.assertEqual(service.reconcile(principal, "daytona")["updated"], 0)
        self.assertEqual(
            Decimal(service.cost_summary(principal)[0]["provider_cost_usd"]),
            Decimal("0.2"),
        )
        self.assertEqual(
            service.get_sandbox(
                principal, sandbox.id
            ).last_provider_observed_at,
            positive_at.isoformat(),
        )
        # Provider timestamps with offsets compare chronologically, not lexically.
        reconciler.result = ProviderObservation(
            "offset-missing",
            sandbox.provider_resource_id,
            "missing",
            "2025-12-31T19:09:59-05:00",
        )
        reconciler.by_org[principal.organization_id] = []
        self.assertEqual(service.reconcile(principal, "daytona")["missing"], 0)

    def test_delayed_none_cannot_override_a_concurrent_positive(self):
        clock = _Clock(self.base, self.base)
        reconciler = _LookupReconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        clock.last += timedelta(minutes=5)
        newer = clock.last + timedelta(seconds=1)
        # A second store models a different service process completing its
        # positive observation while this direct lookup is still in flight.
        other_store = SQLiteStore(self.store._path, key_pepper="test-pepper")

        def positive_during_lookup():
            other_store.reconcile_provider_observation(
                principal.organization_id,
                sandbox.id,
                backend="daytona",
                observation=ProviderObservation(
                    "concurrent-positive",
                    sandbox.provider_resource_id,
                    "running",
                    newer.isoformat(),
                    Decimal("0.1"),
                ),
                customer_markup=Decimal("1.25"),
            )
            return None

        reconciler.result = positive_during_lookup
        self.assertEqual(service.reconcile(principal, "daytona")["missing"], 0)
        record = service.get_sandbox(principal, sandbox.id)
        self.assertEqual(record.status, "running")
        self.assertEqual(record.last_provider_observed_at, newer.isoformat())
        self.assertFalse(record.provider_missing)

    def test_uncertainty_accrues_after_provider_cost_then_confirmation_stops(
        self,
    ):
        clock = _Clock(self.base, self.base)
        reconciler = _LookupReconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        clock.last += timedelta(hours=1)
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "provider-total",
                sandbox.provider_resource_id,
                "running",
                clock.last.isoformat(),
                Decimal("0.2"),
            )
        ]
        service.reconcile(principal, "daytona")
        reconciler.by_org[principal.organization_id] = []
        reconciler.result = TimeoutError("lookup unavailable")
        clock.last += timedelta(hours=1)
        service.reconcile(principal, "daytona")
        self.assertEqual(
            Decimal(service.cost_summary(principal)[0]["provider_cost_usd"]),
            Decimal("0.254"),
        )
        # A later cumulative provider report replaces baseline plus estimate.
        reconciler.result = ProviderObservation(
            "provider-correction",
            sandbox.provider_resource_id,
            "running",
            clock.last.isoformat(),
            Decimal("0.3"),
        )
        service.reconcile(principal, "daytona")
        reconciler.result = None
        clock.last += timedelta(hours=1)
        self.assertEqual(service.reconcile(principal, "daytona")["missing"], 1)
        final_cost = service.cost_summary(principal)
        self.assertEqual(
            Decimal(final_cost[0]["provider_cost_usd"]), Decimal("0.354")
        )
        clock.last += timedelta(hours=1)
        service.reconcile(principal, "daytona")
        self.assertEqual(service.cost_summary(principal), final_cost)
        with sqlite3.connect(self.store._path) as connection:
            ledger_total = sum(
                (
                    Decimal(row[0])
                    for row in connection.execute(
                        "SELECT provider_delta_usd FROM lifecycle_ledger_entries WHERE sandbox_id=?",
                        (sandbox.id,),
                    )
                ),
                Decimal(0),
            )
        self.assertEqual(ledger_total, Decimal("0.354"))

    def test_fresh_direct_missing_accepts_final_cumulative_provider_cost(self):
        clock = _Clock(self.base, self.base)
        reconciler = _LookupReconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        clock.last += timedelta(hours=1)
        service.cost_summary(principal)
        clock.last += timedelta(hours=1)
        reconciler.result = ProviderObservation(
            "final-cost",
            sandbox.provider_resource_id,
            "missing",
            clock.last.isoformat(),
            Decimal("0.07"),
            organization_id=principal.organization_id,
            backend="daytona",
        )
        self.assertEqual(service.reconcile(principal, "daytona")["missing"], 1)
        before = service.cost_summary(principal)
        self.assertEqual(
            Decimal(before[0]["provider_cost_usd"]), Decimal("0.07")
        )
        service.reconcile(principal, "daytona")
        self.assertEqual(service.cost_summary(principal), before)

    def test_partial_list_updates_present_resource_but_retains_omitted_one(
        self,
    ):
        clock = _Clock(self.base, self.base)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        present = service.create_sandbox(principal, "daytona", {})
        omitted = service.create_sandbox(principal, "daytona", {})
        clock.last += timedelta(hours=1)
        positive = ProviderObservation(
            "latest",
            present.provider_resource_id,
            "running",
            clock.last.isoformat(),
        )
        older = ProviderObservation(
            "older",
            present.provider_resource_id,
            "terminated",
            (clock.last - timedelta(minutes=1)).isoformat(),
        )
        # Pagination may return duplicates out of order; use the latest evidence.
        reconciler.by_org[principal.organization_id] = [positive, older]
        result = service.reconcile(principal, "daytona")
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["missing"], 0)
        self.assertEqual(
            service.get_sandbox(principal, present.id).status, "running"
        )
        self.assertEqual(
            service.get_sandbox(principal, omitted.id).status, "running"
        )
        self.assertEqual(
            Decimal(
                service.cost_summary(principal, group_by="backend")[0][
                    "provider_cost_usd"
                ]
            ),
            Decimal("0.108"),
        )

    def test_delayed_provider_total_preserves_locally_accrued_tail(self):
        clock = _Clock(self.base, self.base)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        clock.last += timedelta(hours=2)
        service.cost_summary(principal)
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "delayed-total",
                sandbox.provider_resource_id,
                "running",
                (self.base + timedelta(hours=1)).isoformat(),
                Decimal("0.2"),
            )
        ]
        service.reconcile(principal, "daytona")
        self.assertEqual(
            Decimal(service.cost_summary(principal)[0]["provider_cost_usd"]),
            Decimal("0.254"),
        )
        clock.last += timedelta(hours=1)
        reconciler.by_org[principal.organization_id] = []
        service.reconcile(principal, "daytona")
        self.assertEqual(
            Decimal(service.cost_summary(principal)[0]["provider_cost_usd"]),
            Decimal("0.308"),
        )
        # Retrying the historical provider total cannot erase or duplicate tail.
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "delayed-total",
                sandbox.provider_resource_id,
                "running",
                (self.base + timedelta(hours=1)).isoformat(),
                Decimal("0.2"),
            )
        ]
        service.reconcile(principal, "daytona")
        self.assertEqual(
            Decimal(service.cost_summary(principal)[0]["provider_cost_usd"]),
            Decimal("0.308"),
        )

    def test_observation_and_billing_roll_back_together(self):
        clock = _Clock(self.base, self.base)
        reconciler = _LookupReconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        clock.last += timedelta(minutes=5)
        original = self.store.record_lifecycle_cost

        def unavailable(**kwargs):
            raise RuntimeError("ledger write unavailable")

        self.store.record_lifecycle_cost = unavailable
        try:
            with self.assertRaisesRegex(
                RuntimeError, "ledger write unavailable"
            ):
                service.reconcile(principal, "daytona")
        finally:
            self.store.record_lifecycle_cost = original
        record = service.get_sandbox(principal, sandbox.id)
        self.assertEqual(record.status, "running")
        self.assertFalse(record.provider_missing)
        self.assertIsNone(record.last_provider_observed_at)
        with sqlite3.connect(self.store._path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM provider_observations WHERE sandbox_id=?",
                    (sandbox.id,),
                ).fetchone()[0],
                0,
            )

    def test_reconciliation_never_invokes_terminators(self):
        clock = _Clock(self.base, self.base)
        reconciler = _LookupReconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})

        def forbidden(*args, **kwargs):
            self.fail("reconciliation must not delete resources")

        service._provider_terminators["daytona"] = forbidden
        service._runtimes[sandbox.id].destroy = forbidden
        clock.last += timedelta(minutes=1)
        reconciler.result = None
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "orphan", "orphan-resource", "running", clock.last.isoformat()
            )
        ]
        result = service.reconcile(principal, "daytona")
        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["orphans"], 1)

    def test_reconciliation_rejects_a_backend_outside_the_allowlist(
        self,
    ) -> None:
        reconciler = _Reconciler()
        service = ControlPlane(
            self.store,
            allowed_backends={"local"},
            provider_reconcilers={"daytona": reconciler},
        )
        _, key = service.bootstrap_organization("Allowlist")
        principal = self.store.authenticate(key.secret)

        with self.assertRaisesRegex(ValueError, "backend is not enabled"):
            service.reconcile(principal, " DAYTONA ")

        self.assertEqual(reconciler.calls, [])

    def test_clock_reversal_clamps_billable_duration_to_zero(self) -> None:
        clock = _Clock(self.base, self.base, self.base - timedelta(seconds=1))
        service, principal = self._service(clock)
        service.create_sandbox(principal, "daytona", {})

        summary = service.cost_summary(principal)[0]

        self.assertEqual(Decimal(summary["runtime_seconds"]), Decimal(0))
        self.assertEqual(Decimal(summary["provider_cost_usd"]), Decimal(0))

    def test_destroy_records_stopping_terminated_and_full_billable_window(
        self,
    ) -> None:
        running = self.base + timedelta(seconds=2)
        stopping = self.base + timedelta(minutes=30)
        terminated = stopping + timedelta(seconds=3)
        clock = _Clock(self.base, running, stopping, terminated)
        service, principal = self._service(clock)
        sandbox = service.create_sandbox(principal, "daytona", {})

        record = service.destroy_sandbox(principal, sandbox.id)
        summary = service.cost_summary(principal)[0]

        self.assertEqual(record.running_at, running.isoformat())
        self.assertEqual(record.stopping_at, stopping.isoformat())
        self.assertEqual(record.terminated_at, terminated.isoformat())
        self.assertEqual(record.status, "destroyed")
        self.assertEqual(Decimal(summary["runtime_seconds"]), Decimal("1803.0"))

    def test_reconciliation_preserves_terminal_state_and_billable_window(
        self,
    ) -> None:
        terminated = self.base + timedelta(minutes=5)
        much_later = self.base + timedelta(days=2)
        clock = _Clock(self.base, self.base, terminated, terminated, much_later)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        terminal = service.destroy_sandbox(principal, sandbox.id)
        before = service.cost_summary(principal)[0]

        result = service.reconcile(principal, "daytona")
        after = service.cost_summary(principal)[0]
        preserved = service.get_sandbox(principal, sandbox.id)

        self.assertEqual(result["missing"], 0)
        self.assertEqual(preserved.status, "destroyed")
        self.assertFalse(preserved.provider_missing)
        self.assertEqual(preserved.terminated_at, terminal.terminated_at)
        self.assertEqual(after, before)

    def test_daily_and_bounded_summaries_are_stable_across_utc_midnight(
        self,
    ) -> None:
        started = datetime(2026, 1, 1, 23, 30, tzinfo=UTC)
        first_refresh = started + timedelta(hours=1)
        second_refresh = started + timedelta(hours=1, minutes=30)
        clock = _Clock(started, started, first_refresh, second_refresh)
        service, principal = self._service(clock)
        service.create_sandbox(principal, "daytona", {})

        service.cost_summary(principal, group_by="day")
        daily = service.cost_summary(principal, group_by="day")
        bounded = service.cost_summary(
            principal,
            start="2026-01-01T00:00:00+00:00",
            end="2026-01-02T00:00:00+00:00",
        )
        clipped = service.cost_summary(
            principal,
            start="2026-01-01T23:45:00Z",
            end="2026-01-02T00:15:00Z",
        )

        self.assertEqual(
            [item["key"] for item in daily],
            ["2026-01-01", "2026-01-02"],
        )
        self.assertEqual(
            Decimal(daily[0]["runtime_seconds"]), Decimal("1800.0")
        )
        self.assertEqual(
            Decimal(daily[1]["runtime_seconds"]), Decimal("3600.0")
        )
        self.assertEqual(
            Decimal(bounded[0]["runtime_seconds"]), Decimal("1800.0")
        )
        self.assertEqual(
            Decimal(bounded[0]["provider_cost_usd"]), Decimal("0.027000")
        )
        self.assertEqual(
            Decimal(clipped[0]["runtime_seconds"]), Decimal("1800.0")
        )
        self.assertEqual(
            Decimal(clipped[0]["provider_cost_usd"]), Decimal("0.027000")
        )

    def test_transitional_statuses_and_invalid_status_are_strict(self) -> None:
        clock = _Clock(self.base, self.base)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        resource_id = sandbox.provider_resource_id or ""
        self.store.update_sandbox_status(sandbox.id, "creating")
        provisioning = self.base + timedelta(seconds=30)
        clock.last = provisioning
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "provisioning",
                resource_id,
                "provisioning",
                provisioning.isoformat(),
            )
        ]
        service.reconcile(principal, "daytona")
        self.assertEqual(
            service.get_sandbox(principal, sandbox.id).status, "creating"
        )
        stopping = self.base + timedelta(minutes=1)
        clock.last = stopping
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "stop", resource_id, "stopping", stopping.isoformat()
            )
        ]
        service.reconcile(principal, "daytona")
        stopped = service.get_sandbox(principal, sandbox.id)
        self.assertEqual(stopped.status, "stopping")
        self.assertEqual(stopped.stopping_at, stopping.isoformat())

        terminated = self.base + timedelta(minutes=2)
        clock.last = terminated
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "terminated",
                resource_id,
                "terminated",
                terminated.isoformat(),
            )
        ]
        service.reconcile(principal, "daytona")
        terminal = service.get_sandbox(principal, sandbox.id)
        self.assertEqual(terminal.status, "destroyed")
        self.assertEqual(terminal.terminated_at, terminated.isoformat())

        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "bad", resource_id, "mystery", stopping.isoformat()
            )
        ]
        result = service.reconcile(principal, "daytona")
        self.assertEqual(result["status"], "partial_outage")
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["rejected"], 2)
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(result["missing"], 0)
        self.assertEqual(
            service.get_sandbox(principal, sandbox.id).status, "destroyed"
        )

    def test_equal_time_conflicting_duplicates_are_order_independent(self):
        for statuses in (("running", "terminated"), ("terminated", "running")):
            with self.subTest(statuses=statuses):
                clock = _Clock(self.base, self.base)
                reconciler = _Reconciler()
                service, principal = self._service(clock, reconciler=reconciler)
                sandbox = service.create_sandbox(principal, "daytona", {})
                clock.last += timedelta(minutes=5)
                reconciler.by_org[principal.organization_id] = [
                    ProviderObservation(
                        f"duplicate-{index}",
                        sandbox.provider_resource_id,
                        status,
                        (self.base + timedelta(minutes=1)).isoformat(),
                    )
                    for index, status in enumerate(statuses)
                ]
                result = service.reconcile(principal, "daytona")
                record = service.get_sandbox(principal, sandbox.id)
                self.assertEqual(result["updated"], 0)
                self.assertEqual(result["unknown"], 1)
                self.assertEqual(result["rejected"], 2)
                self.assertEqual(result["status"], "partial_outage")
                self.assertEqual(record.status, "running")
                self.assertIsNone(record.last_provider_observed_at)
                self.assertIsNone(record.terminated_at)
                self.assertEqual(
                    Decimal(
                        service.cost_summary(principal)[0]["runtime_seconds"]
                    ),
                    Decimal(300),
                )

    def test_malformed_single_and_duplicate_times_fail_closed(self):
        bad_times = (
            "not-a-time",
            "2026-01-01T00:01:00",
            "2026-02-30T00:00:00Z",
            "2026-01-01T00:01:00+25:00",
            "2026-01-01T00:01:00+00:60",
            "2026-01-01T00:01:00+00:00:60",
            "0001-01-01T00:00:00+01:00",
            "9999-12-31T23:59:59-01:00",
            None,
            123,
        )
        for bad_time in bad_times:
            for duplicate in (False, True):
                for reverse in (False, True):
                    with self.subTest(
                        time=bad_time, duplicate=duplicate, reverse=reverse
                    ):
                        clock = _Clock(self.base, self.base)
                        reconciler = _Reconciler()
                        service, principal = self._service(
                            clock, reconciler=reconciler
                        )
                        sandbox = service.create_sandbox(
                            principal, "daytona", {}
                        )
                        clock.last += timedelta(minutes=5)
                        observations = [
                            ProviderObservation(
                                "invalid-time",
                                sandbox.provider_resource_id,
                                "terminated",
                                bad_time,
                            )
                        ]
                        if duplicate:
                            observations.append(
                                ProviderObservation(
                                    "valid-sibling",
                                    sandbox.provider_resource_id,
                                    "terminated",
                                    (
                                        self.base + timedelta(minutes=1)
                                    ).isoformat(),
                                )
                            )
                        reconciler.by_org[principal.organization_id] = (
                            list(reversed(observations))
                            if reverse
                            else observations
                        )
                        result = service.reconcile(principal, "daytona")
                        record = service.get_sandbox(principal, sandbox.id)
                        self.assertEqual(result["status"], "partial_outage")
                        self.assertEqual(result["updated"], 0)
                        self.assertEqual(result["missing"], 0)
                        self.assertEqual(result["unknown"], 1)
                        self.assertEqual(result["rejected"], len(observations))
                        self.assertEqual(record.status, "running")
                        self.assertIsNone(record.last_provider_observed_at)
                        self.assertIsNone(record.terminated_at)
                        self.assertEqual(
                            Decimal(
                                service.cost_summary(principal)[0][
                                    "provider_cost_usd"
                                ]
                            ),
                            Decimal("0.0045"),
                        )
                        clock.last += timedelta(minutes=5)
                        self.assertEqual(
                            Decimal(
                                service.cost_summary(principal)[0][
                                    "provider_cost_usd"
                                ]
                            ),
                            Decimal("0.009"),
                        )

    def test_provider_timestamp_z_and_offsets_keep_strict_time_validation(self):
        expected = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
        for value in (
            "2026-01-01T00:01:00Z",
            "2026-01-01T00:01:00+00:00",
            "2025-12-31T19:01:00-05:00",
            "2026-01-01T05:31:00+05:30",
        ):
            with self.subTest(value=value):
                self.assertEqual(provider_timestamp(value), expected)
        for value in (
            "2026-01-01T00:01:00",
            "2026-01-01T00:01:00+00:60",
            "2026-01-01T00:01:00+00:00:60",
            "2026-02-30T00:01:00Z",
            "2026-01-01T00:Z01:00Z",
            "0001-01-01T00:00:00+01:00",
            "9999-12-31T23:59:59-01:00",
        ):
            with self.subTest(value=value):
                self.assertIsNone(provider_timestamp(value))
        for seconds, accepted in ((30, True), (31, False)):
            value = (expected + timedelta(seconds=seconds)).isoformat()
            for timestamp in (value, value[:-6] + "Z"):
                with self.subTest(timestamp=timestamp):
                    normalized = normalize_observation(
                        ProviderObservation(
                            "clock", "resource", "running", timestamp
                        ),
                        now=expected,
                    )
                    if accepted:
                        self.assertIsNotNone(normalized)
                        self.assertEqual(
                            normalized.observed_at, expected.isoformat()
                        )
                    else:
                        self.assertIsNone(normalized)

    def test_valid_offset_duplicates_are_normalized_before_selection(self):
        clock = _Clock(self.base, self.base)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        clock.last += timedelta(minutes=5)
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "offset",
                sandbox.provider_resource_id,
                "running",
                "2025-12-31T19:01:00-05:00",
            ),
            ProviderObservation(
                "utc",
                sandbox.provider_resource_id,
                "running",
                "2026-01-01T00:01:00Z",
            ),
        ]
        result = service.reconcile(principal, "daytona")
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["rejected"], 0)
        self.assertEqual(result["unknown"], 0)
        self.assertEqual(
            service.get_sandbox(
                principal, sandbox.id
            ).last_provider_observed_at,
            (self.base + timedelta(minutes=1)).isoformat(),
        )

    def test_cumulative_decreases_preserve_charges_without_double_counting(
        self,
    ):
        clock = _Clock(self.base, self.base)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})

        def report(identity, hours, total, expected):
            reconciler.by_org[principal.organization_id] = [
                ProviderObservation(
                    identity,
                    sandbox.provider_resource_id,
                    "running",
                    (self.base + timedelta(hours=hours)).isoformat(),
                    Decimal(total),
                )
            ]
            self.assertEqual(
                service.reconcile(principal, "daytona")["updated"], 1
            )
            summary = service.cost_summary(principal)[0]
            self.assertEqual(
                Decimal(summary["provider_cost_usd"]), Decimal(expected)
            )
            self.assertEqual(
                Decimal(summary["customer_cost_usd"]),
                Decimal(expected) * Decimal("1.25"),
            )

        clock.last += timedelta(hours=1)
        report("initial", 1, "1", "1")
        report("lower-same-time", 1, ".1", "1")
        clock.last += timedelta(hours=1)
        # Accrue a tail before the later decreasing cumulative report.
        self.assertEqual(
            Decimal(service.cost_summary(principal)[0]["provider_cost_usd"]),
            Decimal("1.054"),
        )
        report("lower-later-time", 2, ".1", "1.054")
        clock.last += timedelta(hours=1)
        report("higher-later-time", 3, "1.1", "1.1")
        clock.last += timedelta(hours=1)
        self.assertEqual(
            Decimal(service.cost_summary(principal)[0]["provider_cost_usd"]),
            Decimal("1.154"),
        )
        report("delayed-same-total", 3, "1.1", "1.154")
        with sqlite3.connect(self.store._path) as connection:
            deltas = [
                Decimal(row[0])
                for row in connection.execute(
                    "SELECT provider_delta_usd FROM lifecycle_ledger_entries WHERE sandbox_id=?",
                    (sandbox.id,),
                )
            ]
        self.assertTrue(all(delta >= 0 for delta in deltas))
        self.assertEqual(sum(deltas), Decimal("1.154"))

    def test_unknown_attempt_does_not_fence_older_real_provider_evidence(self):
        for lookup in (False, True):
            with self.subTest(lookup=lookup):
                clock = _Clock(self.base, self.base)
                reconciler = _LookupReconciler() if lookup else _Reconciler()
                service, principal = self._service(clock, reconciler=reconciler)
                sandbox = service.create_sandbox(principal, "daytona", {})
                clock.last += timedelta(minutes=10)
                if lookup:
                    reconciler.result = TimeoutError("temporary lookup failure")
                first = service.reconcile(principal, "daytona")
                self.assertEqual(first["unknown"], 1)
                self.assertEqual(first["status"], "partial_outage")
                self.assertIsNone(
                    service.get_sandbox(
                        principal, sandbox.id
                    ).last_provider_observed_at
                )
                clock.last += timedelta(minutes=1)
                actual_time = self.base + timedelta(minutes=9)
                reconciler.by_org[principal.organization_id] = [
                    ProviderObservation(
                        "delayed-terminal",
                        sandbox.provider_resource_id,
                        "terminated",
                        actual_time.isoformat(),
                    )
                ]
                result = service.reconcile(principal, "daytona")
                record = service.get_sandbox(principal, sandbox.id)
                self.assertEqual(result["updated"], 1)
                self.assertEqual(result["rejected"], 0)
                self.assertEqual(record.status, "destroyed")
                self.assertEqual(
                    record.last_provider_observed_at, actual_time.isoformat()
                )
                self.assertEqual(record.terminated_at, actual_time.isoformat())

    def test_future_observations_never_prebill_or_freeze_accrual(self):
        for seconds in (1, 30, 31, 3600):
            for lookup in (False, True):
                with self.subTest(seconds=seconds, lookup=lookup):
                    clock = _Clock(self.base, self.base)
                    reconciler = (
                        _LookupReconciler() if lookup else _Reconciler()
                    )
                    service, principal = self._service(
                        clock, reconciler=reconciler
                    )
                    sandbox = service.create_sandbox(principal, "daytona", {})
                    clock.last += timedelta(minutes=5)
                    observation = ProviderObservation(
                        "future",
                        sandbox.provider_resource_id,
                        "running" if seconds <= 30 else "terminated",
                        (clock.last + timedelta(seconds=seconds)).isoformat(),
                        Decimal("0.5") if seconds > 30 else None,
                    )
                    if lookup:
                        reconciler.result = observation
                    else:
                        reconciler.by_org[principal.organization_id] = [
                            observation
                        ]
                    result = service.reconcile(principal, "daytona")
                    record = service.get_sandbox(principal, sandbox.id)
                    self.assertEqual(result["updated"], int(seconds <= 30))
                    self.assertEqual(result["unknown"], int(seconds > 30))
                    self.assertEqual(result["rejected"], int(seconds > 30))
                    self.assertEqual(record.status, "running")
                    self.assertEqual(
                        record.last_provider_observed_at,
                        clock.last.isoformat() if seconds <= 30 else None,
                    )
                    self.assertEqual(
                        Decimal(
                            service.cost_summary(principal)[0][
                                "runtime_seconds"
                            ]
                        ),
                        Decimal(300),
                    )
                    self.assertEqual(
                        Decimal(
                            service.cost_summary(principal)[0][
                                "provider_cost_usd"
                            ]
                        ),
                        Decimal("0.0045"),
                    )
                    clock.last += timedelta(minutes=5)
                    reconciler.by_org[principal.organization_id] = []
                    if lookup:
                        reconciler.result = TimeoutError("unavailable")
                    service.reconcile(principal, "daytona")
                    self.assertEqual(
                        Decimal(
                            service.cost_summary(principal)[0][
                                "runtime_seconds"
                            ]
                        ),
                        Decimal(600),
                    )
                    self.assertEqual(
                        Decimal(
                            service.cost_summary(principal)[0][
                                "provider_cost_usd"
                            ]
                        ),
                        Decimal("0.009"),
                    )

    def test_malformed_lookup_times_are_counted_as_unknown(self):
        for observed_at in (
            "bad",
            "2026-01-01T00:01:00",
            "0001-01-01T00:00:00+01:00",
        ):
            with self.subTest(observed_at=observed_at):
                clock = _Clock(self.base, self.base)
                reconciler = _LookupReconciler()
                service, principal = self._service(clock, reconciler=reconciler)
                sandbox = service.create_sandbox(principal, "daytona", {})
                clock.last += timedelta(minutes=5)
                reconciler.result = ProviderObservation(
                    "invalid-lookup",
                    sandbox.provider_resource_id,
                    "missing",
                    observed_at,
                )
                result = service.reconcile(principal, "daytona")
                self.assertEqual(result["status"], "partial_outage")
                self.assertEqual(result["unknown"], 1)
                self.assertEqual(result["rejected"], 1)
                self.assertEqual(result["missing"], 0)
                record = service.get_sandbox(principal, sandbox.id)
                self.assertEqual(record.status, "running")
                self.assertIsNone(record.last_provider_observed_at)
                self.assertEqual(
                    Decimal(
                        service.cost_summary(principal)[0]["runtime_seconds"]
                    ),
                    Decimal(300),
                )

    def _observation_snapshot(self, sandbox_id):
        with sqlite3.connect(self.store._path) as connection:
            return [
                connection.execute(
                    f"SELECT * FROM {table} WHERE {column}=? ORDER BY rowid",
                    (sandbox_id,),
                ).fetchall()
                for table, column in (
                    ("sandboxes", "id"),
                    ("provider_observations", "sandbox_id"),
                    ("lifecycle_costs", "sandbox_id"),
                    ("lifecycle_observations", "sandbox_id"),
                    ("lifecycle_ledger_entries", "sandbox_id"),
                )
            ]

    def test_reused_identity_rejects_entire_payload_and_replay_is_noop(self):
        clock = _Clock(self.base, self.base)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        clock.last += timedelta(hours=1)
        original = ProviderObservation(
            "immutable",
            sandbox.provider_resource_id,
            "running",
            clock.last.isoformat(),
            Decimal("1"),
        )
        reconciler.by_org[principal.organization_id] = [original]
        self.assertEqual(service.reconcile(principal, "daytona")["updated"], 1)
        before = self._observation_snapshot(sandbox.id)
        replay = service.reconcile(principal, "daytona")
        self.assertEqual(replay["updated"], 0)
        self.assertEqual(replay["rejected"], 1)
        self.assertEqual(self._observation_snapshot(sandbox.id), before)
        for status, total in (("terminated", "2"), ("stopping", ".1")):
            reconciler.by_org[principal.organization_id] = [
                ProviderObservation(
                    original.observation_id,
                    sandbox.provider_resource_id,
                    status,
                    original.observed_at,
                    Decimal(total),
                )
            ]
            result = service.reconcile(principal, "daytona")
            self.assertEqual(result["updated"], 0)
            self.assertEqual(result["rejected"], 1)
            self.assertEqual(self._observation_snapshot(sandbox.id), before)
        clock.last += timedelta(hours=1)
        service.cost_summary(principal)
        before = self._observation_snapshot(sandbox.id)
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                original.observation_id,
                sandbox.provider_resource_id,
                "terminated",
                clock.last.isoformat(),
                Decimal("2"),
            )
        ]
        result = service.reconcile(principal, "daytona")
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(self._observation_snapshot(sandbox.id), before)
        # A corrected unique identity can still charge the newer cumulative total.
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "corrected-identity",
                sandbox.provider_resource_id,
                "running",
                clock.last.isoformat(),
                Decimal("2"),
            )
        ]
        self.assertEqual(service.reconcile(principal, "daytona")["updated"], 1)
        self.assertEqual(
            Decimal(service.cost_summary(principal)[0]["provider_cost_usd"]),
            Decimal("2"),
        )

    def test_positive_after_confirmed_absence_is_visible_but_not_revived(self):
        clock = _Clock(self.base, self.base)
        reconciler = _LookupReconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        clock.last += timedelta(minutes=10)
        self.assertEqual(service.reconcile(principal, "daytona")["missing"], 1)
        terminal = service.get_sandbox(principal, sandbox.id)
        cost = service.cost_summary(principal)
        clock.last += timedelta(minutes=10)
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "contradictory-positive",
                sandbox.provider_resource_id,
                "running",
                clock.last.isoformat(),
                Decimal("2"),
            )
        ]
        result = service.reconcile(principal, "daytona")
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(result["unknown"], 0)
        self.assertEqual(result["status"], "partial_outage")
        self.assertEqual(service.get_sandbox(principal, sandbox.id), terminal)
        self.assertEqual(service.cost_summary(principal), cost)
        self.assertEqual(len(reconciler.lookup_calls), 1)
        with sqlite3.connect(self.store._path) as connection:
            evidence = connection.execute(
                "SELECT status, provider_cost_usd FROM provider_observations WHERE sandbox_id=? ORDER BY observed_at",
                (sandbox.id,),
            ).fetchall()
        self.assertEqual(evidence, [("missing", None), ("running", "2")])
        before = self._observation_snapshot(sandbox.id)
        service.reconcile(principal, "daytona")
        self.assertEqual(self._observation_snapshot(sandbox.id), before)

    def test_list_only_omission_reports_incomplete_reconciliation(self):
        clock = _Clock(self.base, self.base)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        clock.last += timedelta(minutes=5)
        result = service.reconcile(principal, "daytona")
        self.assertEqual(
            result,
            {
                "backend": "daytona",
                "status": "partial_outage",
                "updated": 0,
                "missing": 0,
                "orphans": 0,
                "unknown": 1,
                "rejected": 0,
            },
        )
        record = service.get_sandbox(principal, sandbox.id)
        self.assertEqual(record.status, "running")
        self.assertFalse(record.provider_missing)
        self.assertIsNone(record.last_provider_observed_at)

    def test_stale_observations_are_counted_without_lifecycle_updates(self):
        clock = _Clock(self.base, self.base)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        clock.last += timedelta(minutes=5)
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "older-than-creation",
                sandbox.provider_resource_id,
                "terminated",
                (self.base - timedelta(seconds=1)).isoformat(),
            )
        ]
        result = service.reconcile(principal, "daytona")
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["unknown"], 0)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(result["status"], "partial_outage")
        record = service.get_sandbox(principal, sandbox.id)
        self.assertEqual(record.status, "running")
        self.assertIsNone(record.last_provider_observed_at)
        self.assertEqual(
            Decimal(service.cost_summary(principal)[0]["runtime_seconds"]),
            Decimal(300),
        )

    def test_invalid_payload_shapes_and_costs_are_counted(self):
        for lookup in (False, True):
            for field, value in (
                ("status", None),
                ("status", "unsupported"),
                ("observation_id", None),
                ("currency", None),
                ("provider_cost_usd", Decimal("NaN")),
                ("provider_cost_usd", Decimal("Infinity")),
                ("provider_cost_usd", Decimal("-1")),
                ("provider_cost_usd", "not-a-cost"),
                ("organization_id", []),
                ("payload", {"status": "missing"}),
            ):
                with self.subTest(lookup=lookup, field=field, value=value):
                    clock = _Clock(self.base, self.base)
                    reconciler = (
                        _LookupReconciler() if lookup else _Reconciler()
                    )
                    service, principal = self._service(
                        clock, reconciler=reconciler
                    )
                    sandbox = service.create_sandbox(principal, "daytona", {})
                    clock.last += timedelta(minutes=5)
                    fields = {
                        "observation_id": "invalid-payload",
                        "provider_resource_id": sandbox.provider_resource_id,
                        "status": "terminated",
                        "observed_at": clock.last.isoformat(),
                    }
                    if field == "payload":
                        observation = value
                    else:
                        fields[field] = value
                        observation = ProviderObservation(**fields)
                    if lookup:
                        reconciler.result = observation
                    else:
                        reconciler.by_org[principal.organization_id] = [
                            observation
                        ]
                    result = service.reconcile(principal, "daytona")
                    self.assertEqual(result["updated"], 0)
                    self.assertEqual(result["missing"], 0)
                    self.assertEqual(result["unknown"], 1)
                    self.assertEqual(result["rejected"], 1)
                    self.assertEqual(result["status"], "partial_outage")
                    record = service.get_sandbox(principal, sandbox.id)
                    self.assertEqual(record.status, "running")
                    self.assertIsNone(record.last_provider_observed_at)
                    self.assertEqual(
                        Decimal(
                            service.cost_summary(principal)[0][
                                "runtime_seconds"
                            ]
                        ),
                        Decimal(300),
                    )

    def test_lower_later_total_cannot_reduce_charge_without_prior_refresh(self):
        clock = _Clock(self.base, self.base)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        for hours, total in ((1, "1"), (2, ".1")):
            clock.last = self.base + timedelta(hours=hours)
            reconciler.by_org[principal.organization_id] = [
                ProviderObservation(
                    f"total-{hours}",
                    sandbox.provider_resource_id,
                    "running",
                    clock.last.isoformat(),
                    Decimal(total),
                )
            ]
            self.assertEqual(
                service.reconcile(principal, "daytona")["updated"], 1
            )
            self.assertEqual(
                Decimal(
                    service.cost_summary(principal)[0]["provider_cost_usd"]
                ),
                Decimal("1"),
            )
        clock.last += timedelta(hours=1)
        self.assertEqual(
            Decimal(service.cost_summary(principal)[0]["provider_cost_usd"]),
            Decimal("1.054"),
        )

    def test_inventory_failure_counts_affected_active_resources(self):
        clock = _Clock(self.base, self.base)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        service.create_sandbox(principal, "daytona", {})
        service.create_sandbox(principal, "daytona", {})
        clock.last += timedelta(minutes=5)
        reconciler.failures = 1
        result = service.reconcile(principal, "daytona")
        self.assertEqual(
            result,
            {
                "backend": "daytona",
                "status": "partial_outage",
                "updated": 0,
                "missing": 0,
                "orphans": 0,
                "unknown": 2,
                "rejected": 0,
            },
        )
        self.assertTrue(
            all(
                record.last_provider_observed_at is None
                for record in self.store.list_sandboxes(
                    principal.organization_id
                )
            )
        )

    def test_evidence_insertion_failure_rolls_back_lifecycle(self):
        from bespokelabs.sandbox.control_plane.errors import ConflictError

        clock = _Clock(self.base, self.base)
        reconciler = _LookupReconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        clock.last += timedelta(minutes=5)
        before = self._observation_snapshot(sandbox.id)
        original = self.store.record_provider_observation
        self.store.record_provider_observation = lambda *args, **kwargs: False
        try:
            with self.assertRaisesRegex(
                ConflictError, "observation identity conflict"
            ):
                service.reconcile(principal, "daytona")
        finally:
            self.store.record_provider_observation = original
        self.assertEqual(self._observation_snapshot(sandbox.id), before)


if __name__ == "__main__":
    unittest.main()
