"""Live resource sampling, scope isolation, persistence, and outage handling."""

import json
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bespokelabs.sandbox import Sandbox
from bespokelabs.sandbox import _resource_probe as probe
from bespokelabs.sandbox.control_plane.errors import (
    AuthorizationError,
    NotFoundError,
)
from bespokelabs.sandbox.control_plane.resource_metrics import (
    ResourceMonitor,
    normalize,
)
from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from bespokelabs.sandbox.exceptions import SandboxError
from bespokelabs.sandbox.types import SandboxConfig, SandboxResult

RAW = {
    "cpu_seconds": 10,
    "cpu_capacity": 2,
    "counter_id": "sandbox-a",
    "monotonic_seconds": 100,
    "memory_used_bytes": 1024,
    "memory_limit_bytes": 2048,
    "gpus": [
        {
            "id": "gpu-1",
            "name": "GPU",
            "utilization_percent": 75,
            "memory_used_bytes": 512,
            "memory_total_bytes": 1024,
        }
    ],
}


class ProbeTests(unittest.TestCase):

    def test_cgroup_quota_hierarchy_and_gpu_na(self):
        files = {
            "/proc/self/cgroup": "0::/sandbox/child",
            "/proc/self/mountinfo": "1 0 0:1 /sandbox /sys/fs/cgroup rw - cgroup2 cgroup rw",
            "/sys/fs/cgroup/child/cpu.stat": "usage_usec 5000000",
            "/sys/fs/cgroup/child/cpu.max": "200000 100000",
            "/sys/fs/cgroup/cpu.max": "50000 100000",
            "/sys/fs/cgroup/child/cpuset.cpus.effective": "0-3,6",
            "/sys/fs/cgroup/child/memory.current": "1024",
            "/sys/fs/cgroup/child/memory.max": "max",
            "/sys/fs/cgroup/memory.max": "2048",
        }
        with (
            patch.object(
                probe, "read", side_effect=lambda p: files.get(str(p), "")
            ),
            patch.object(
                probe.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout="gpu1, NVIDIA, 25, 512, 1024\ngpu2, MIG, N/A, 100, 1000",
                ),
            ) as run,
        ):
            result = probe.collect()
        self.assertEqual(result["cpu_seconds"], 5)
        self.assertEqual(result["cpu_capacity"], 0.5)
        self.assertEqual(result["memory_limit_bytes"], 2048)
        self.assertEqual(result["gpus"][0]["memory_used_bytes"], 512 * 1048576)
        self.assertIsNone(result["gpus"][1]["utilization_percent"])
        self.assertEqual(run.call_args.kwargs["timeout"], 3)

    def test_unavailable_does_not_read_host_usage(self):
        with (
            patch.object(probe, "read", return_value="") as read,
            patch.object(
                probe.subprocess, "run", side_effect=FileNotFoundError
            ),
        ):
            result = probe.collect()
        self.assertIsNone(result["cpu_seconds"])
        self.assertIsNone(result["memory_used_bytes"])
        self.assertEqual(result["gpus"], [])
        self.assertFalse(
            any(
                str(call.args[0]) in {"/proc/stat", "/proc/meminfo"}
                for call in read.call_args_list
            )
        )

    def test_sdk_probe_and_unsupported_host_backend(self):
        session = Mock()
        session.execute_command.return_value = SandboxResult(
            stdout=json.dumps(RAW)
        )
        runtime = Sandbox._from_session(
            "docker", session, SandboxConfig(backend="docker")
        )
        self.assertEqual(runtime.resource_usage(), RAW)
        self.assertEqual(session.execute_command.call_args.args[0], "python3")
        session.execute_command.return_value = SandboxResult(stdout="invalid")
        self.assertEqual(runtime.resource_usage(), {})
        local = Sandbox._from_session(
            "local", session, SandboxConfig(backend="local")
        )
        session.reset_mock()
        self.assertEqual(local.resource_usage(), {})
        session.execute_command.assert_not_called()

    def test_cpu_delta_zero_reset_and_malformed_metrics(self):
        self.assertIsNone(normalize(RAW)["cpu_percent"])
        current = {**RAW, "cpu_seconds": 25, "monotonic_seconds": 115}
        self.assertEqual(normalize(current, RAW)["cpu_percent"], 50)
        self.assertEqual(
            normalize({**current, "cpu_seconds": 10}, RAW)["cpu_percent"], 0
        )
        for override in (
            {"cpu_seconds": 1},
            {"counter_id": "new"},
            {"monotonic_seconds": 200},
            {"cpu_capacity": 4},
        ):
            self.assertIsNone(
                normalize({**current, **override}, RAW)["cpu_percent"]
            )
        for invalid in (True, -1, float("nan"), float("inf"), "12", 10**1000):
            row = normalize(
                {
                    "memory_used_bytes": invalid,
                    "gpus": [{"id": "x", "utilization_percent": invalid}],
                }
            )
            self.assertIsNone(row["memory_used_bytes"])
            self.assertIsNone(row["gpus"][0]["utilization_percent"])
            json.dumps(row, allow_nan=False)

    def test_resource_usage_after_destroy_never_calls_provider(self):
        for backend in ("docker", "local"):
            with self.subTest(backend=backend):
                session = Mock()
                runtime = Sandbox._from_session(
                    backend, session, SandboxConfig(backend=backend)
                )
                runtime.destroy()
                session.reset_mock()
                with self.assertRaises(SandboxError):
                    runtime.resource_usage()
                session.execute_command.assert_not_called()


class MetricsServiceTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "metrics.db"
        self.store = SQLiteStore(self.path, key_pepper="test")
        self.now = datetime(2026, 11, 8, tzinfo=UTC)
        self.runtime = Mock(provider_resource_id="fake")
        self.runtime.resource_usage.return_value = RAW
        self.service = ControlPlane(
            self.store,
            sandbox_factory=lambda *_args, **_kwargs: self.runtime,
            allowed_backends=["local"],
            now=lambda: self.now,
        )
        _, key = self.service.bootstrap_organization("One")
        self.key = key
        self.principal = self.store.authenticate(key.secret)
        _, other = self.service.bootstrap_organization("Other")
        self.other = self.store.authenticate(other.secret)
        self.sb = self.service.create_sandbox(self.principal, "local", {})

    def tearDown(self):
        self.service._resource_monitor.stop()
        self.temp.cleanup()

    def sample(self):
        return {**normalize(RAW), "collected_at": self.now.isoformat()}

    def test_tenant_permissions_history_and_freshness(self):
        self.store.record_resource_sample(
            self.principal.organization_id, self.sb.id, self.sample()
        )
        response = self.service.resource_usage(self.principal, self.sb.id)
        self.assertEqual(response["status"], "partial")
        self.assertEqual(len(response["samples"]), 1)
        with self.assertRaises(NotFoundError):
            self.service.resource_usage(self.other, self.sb.id)
        for scope in ("usage:read", "sandboxes:read"):
            key = self.service.issue_api_key(
                self.principal, name=scope, scopes=[scope]
            )
            with self.assertRaises(AuthorizationError):
                self.service.resource_usage(
                    self.store.authenticate(key.secret), self.sb.id
                )
        reader = self.store.authenticate(key.secret)
        self.assertNotIn(
            "resource_usage", self.service.sandbox_detail(reader, self.sb.id)
        )
        self.now += timedelta(seconds=46)
        self.assertEqual(
            self.service.resource_usage(self.principal, self.sb.id)["status"],
            "stale",
        )
        self.service._runtimes.clear()
        self.assertEqual(
            self.service.resource_usage(self.principal, self.sb.id)["status"],
            "unavailable",
        )
        self.assertEqual(
            SQLiteStore(self.path, key_pepper="test").resource_samples(
                self.principal.organization_id, self.sb.id
            ),
            response["samples"],
        )
        self.runtime.resource_usage.assert_not_called()

    def test_history_is_bounded_and_stops_with_sandbox(self):
        for _ in range(245):
            self.store.record_resource_sample(
                self.principal.organization_id, self.sb.id, self.sample()
            )
        self.assertEqual(
            len(
                self.store.resource_samples(
                    self.principal.organization_id, self.sb.id
                )
            ),
            240,
        )
        self.store.mark_lifecycle(self.sb.id, "destroyed", self.now.isoformat())
        self.store.record_resource_sample(
            self.principal.organization_id, self.sb.id, self.sample()
        )
        self.assertEqual(
            self.service.resource_usage(self.principal, self.sb.id)["status"],
            "stopped",
        )
        self.now += timedelta(hours=25)
        self.service._resource_targets()
        self.assertEqual(
            self.store.resource_samples(
                self.principal.organization_id, self.sb.id
            ),
            [],
        )

    def test_http_resources_requires_both_scopes_and_has_no_exec_side_effect(
        self,
    ):
        from fastapi.testclient import TestClient

        from bespokelabs.sandbox.control_plane.api import create_app

        self.store.record_resource_sample(
            self.principal.organization_id, self.sb.id, self.sample()
        )
        client = TestClient(create_app(self.service))
        path = f"/v1/sandboxes/{self.sb.id}/resources"
        self.assertEqual(client.get(path).status_code, 401)
        response = client.get(
            path, headers={"Authorization": f"Bearer {self.key.secret}"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["samples"][0]["memory_used_bytes"], 1024
        )
        self.runtime.resource_usage.assert_not_called()
        with self.store._connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM executions"
                ).fetchone()[0],
                0,
            )
        client.close()

    def test_background_sampling_persists_without_metering(self):
        monitor = self.service._resource_monitor
        monitor.tick()
        for _ in range(100):
            if self.store.resource_samples(
                self.principal.organization_id, self.sb.id
            ):
                break
            time.sleep(0.01)
        self.assertEqual(
            len(
                self.store.resource_samples(
                    self.principal.organization_id, self.sb.id
                )
            ),
            1,
        )
        self.runtime.resource_usage.assert_called_once()
        self.assertEqual(
            self.store.resource_samples(
                self.principal.organization_id, self.sb.id
            )[0]["memory_used_bytes"],
            1024,
        )
        with self.store._connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM executions"
                ).fetchone()[0],
                0,
            )


class MonitorTests(unittest.TestCase):

    def test_target_discovery_uses_sampling_cadence(self):
        for interval in (5, 15):
            with self.subTest(interval=interval):
                targets = Mock(return_value=[])
                monitor = ResourceMonitor(
                    targets,
                    Mock(),
                    lambda: datetime.now(UTC),
                    interval=interval,
                )
                with patch(
                    "bespokelabs.sandbox.control_plane.resource_metrics.time.monotonic"
                ) as clock:
                    for tick in range(interval):
                        clock.return_value = tick
                        monitor.tick()
                    targets.assert_called_once()
                    clock.return_value = interval
                    monitor.tick()
                    self.assertEqual(targets.call_count, 2)

    def test_timeout_has_bounded_workers_and_discards_late_results(self):
        release = threading.Event()
        entered = threading.Event()

        def collect():
            entered.set()
            release.wait(2)
            return RAW

        runtime = SimpleNamespace(resource_usage=collect)
        targets = [
            (SimpleNamespace(id=str(i), organization_id="org"), runtime)
            for i in range(6)
        ]
        saved = []
        discover = Mock(return_value=targets)
        monitor = ResourceMonitor(
            discover,
            lambda *args: saved.append(args),
            lambda: datetime.now(UTC),
        )
        try:
            monitor.tick()
            self.assertTrue(entered.wait(1))
            self.assertEqual(len(monitor._active), 4)
            with monitor._lock:
                for job in monitor._active.values():
                    job["started"] -= 11
            monitor.tick()
            self.assertEqual(len(saved), 4)
            discover.assert_called_once()
            self.assertTrue(
                all(item[2]["status"] == "unavailable" for item in saved)
            )
            monitor.tick()
            self.assertEqual(len(saved), 4)
        finally:
            monitor.stop()
            release.set()
        for _ in range(100):
            if not monitor._active:
                break
            time.sleep(0.01)
        self.assertEqual(len(saved), 4)
        self.assertFalse(monitor._active)
