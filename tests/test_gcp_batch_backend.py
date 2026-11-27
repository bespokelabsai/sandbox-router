"""Google Cloud Batch backend tests.

The Batch and Cloud Logging clients are faked; these tests never call Google
Cloud.  They build real ``batch_v1`` request objects, so they need the
``[gcp-batch]`` extra and skip without it.
"""

from __future__ import annotations

import datetime
import os
import unittest
from unittest import mock

from bespokelabs.sandbox import JobSpec, JobState, Sandbox, SandboxClient
from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxAuthenticationError,
    SandboxConfigurationError,
    SandboxConnectionError,
)

try:
    from google.api_core import exceptions as gexc
    from google.cloud import batch_v1

    _HAS_BATCH = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _HAS_BATCH = False

if _HAS_BATCH:
    from bespokelabs.sandbox.backends import gcp_batch
    from bespokelabs.sandbox.backends.gcp_batch import (
        MAX_PARALLELISM,
        MAX_TASK_COUNT,
        MAX_TASK_FAILURES,
        GcpBatchClient,
        capacity_reason,
        machine_type_for,
        parse_gpu,
    )

JOB = "projects/proj/locations/us-central1/jobs/job-1"
_ENV = {"GOOGLE_CLOUD_PROJECT": "proj", "GOOGLE_CLOUD_REGION": "us-central1"}


def _when(seconds: int) -> datetime.datetime:
    return datetime.datetime(
        2026, 10, 17, 12, 0, seconds, tzinfo=datetime.timezone.utc
    )


def _job(
    state: str,
    *,
    uid: str = "uid-1",
    events: tuple[str, ...] = (),
    labels: dict[str, str] | None = None,
    task_count: int = 0,
) -> object:
    return batch_v1.Job(
        name=JOB,
        uid=uid,
        labels=labels or {},
        status=batch_v1.JobStatus(
            state=batch_v1.JobStatus.State[state],
            status_events=[
                batch_v1.StatusEvent(description=text) for text in events
            ],
        ),
        task_groups=[
            batch_v1.TaskGroup(
                name=f"{JOB}/taskGroups/group0", task_count=task_count
            )
        ],
    )


def _indexed(index: int, state: str, *events: tuple) -> object:
    """Build a task of a multi-task job, with its final state and index."""
    task = _task(*events)
    task.name = f"{JOB}/taskGroups/group0/tasks/{index}"
    task.status.state = batch_v1.TaskStatus.State[state]
    return task


def _pager(pages: list[list], requests: list) -> object:
    """Build a real ListTasksPager over fake pages, recording each fetch."""
    from google.cloud.batch_v1.services.batch_service import pagers

    def response(number: int) -> object:
        more = number + 1 < len(pages)
        return batch_v1.ListTasksResponse(
            tasks=pages[number],
            next_page_token=f"page-{number + 1}" if more else "",
        )

    def fetch(request, **_):
        requests.append(request.page_token)
        return response(int(request.page_token.rsplit("-", 1)[-1]))

    return pagers.ListTasksPager(
        method=fetch,
        request=batch_v1.ListTasksRequest(parent=f"{JOB}/taskGroups/group0"),
        response=response(0),
    )


_FAILED_TRANSITION = (
    "Job state is set from SCHEDULED to FAILED for job "
    "projects/123456789012/locations/us-central1/jobs/job-1. "
)
_QUOTA_EVENT = (
    _FAILED_TRANSITION
    + "Job failed due to: Batch Error: code - CODE_GCE_QUOTA_EXCEEDED, "
    "description - error count is 4, latest message example: Instance "
    "'job-1-0a1b2c3d-group0-0-x7k2' creation failed: Quota 'CPUS' exceeded.  "
    "Limit: 24.0 in region us-central1."
)
_STOCKOUT_EVENT = (
    _FAILED_TRANSITION + "Job failed due to: Batch Error: code - "
    "CODE_GCE_ZONE_RESOURCE_POOL_EXHAUSTED, description - error count is 2, "
    "latest message example: Instance 'job-1-0a1b2c3d-group0-0-x7k2' creation "
    "failed: The zone 'projects/example-project/zones/us-central1-a' does not "
    "have enough resources available to fulfill the request.  Try a different "
    "zone, or try again later."
)
# (name, job events oldest first, expected reason)
_CAPACITY_CASES = [
    ("quota, Batch error code", (_QUOTA_EVENT,), "quota_exceeded"),
    (
        "quota, Compute wording only",
        (
            "Instance 'job-1-0a1b2c3d-group0-0-x7k2' creation failed: Quota "
            "'NVIDIA_L4_GPUS' exceeded.  Limit: 1.0 in region us-central1.",
        ),
        "quota_exceeded",
    ),
    (
        "quota, Compute error code",
        ("googleapi: Error 403: QUOTA_EXCEEDED - Quota exceeded",),
        "quota_exceeded",
    ),
    ("stock-out, Batch error code", (_STOCKOUT_EVENT,), "capacity_unavailable"),
    (
        "stock-out, Compute error code with details",
        (
            "Instance creation failed: ZONE_RESOURCE_POOL_EXHAUSTED_WITH_"
            "DETAILS: (resource type:compute) (state: STOCKOUT)",
        ),
        "capacity_unavailable",
    ),
    (
        "stock-out, Compute wording only",
        (
            "The zone 'projects/example-project/zones/us-central1-b' does "
            "not have enough resources available to fulfill the request.",
        ),
        "capacity_unavailable",
    ),
    (
        "the newest capacity event wins",
        (_QUOTA_EVENT, _STOCKOUT_EVENT),
        "capacity_unavailable",
    ),
    (
        "image pull failure stays generic",
        (
            _FAILED_TRANSITION
            + "Job failed due to task failure. Specifically, task with index "
            '0 failed due to the following task event: "Task state is '
            'updated from PENDING to FAILED"',
        ),
        "batch_failed",
    ),
    (
        "a permission error stays generic",
        ("Required 'compute.instances.create' permission is missing",),
        "batch_failed",
    ),
    (
        "lowercase prose about quotas stays generic",
        ("Check your quota exceeded alerts and capacity dashboards",),
        "batch_failed",
    ),
    (
        "an old quota wait does not explain a later failure",
        (
            _QUOTA_EVENT,
            "Job state is set from QUEUED to SCHEDULED",
            "Job state is set from SCHEDULED to RUNNING",
            "Job state is set from RUNNING to FAILED",
        ),
        "batch_failed",
    ),
    ("no events at all", (), "batch_failed"),
]


def _task(*events: tuple) -> object:
    status_events = []
    for state, second, exit_code, *text in events:
        event = batch_v1.StatusEvent(
            task_state=batch_v1.TaskStatus.State[state],
            event_time=_when(second),
            description=text[0] if text else "",
        )
        if exit_code is not None:
            event.task_execution = batch_v1.TaskExecution(exit_code=exit_code)
        status_events.append(event)
    return batch_v1.Task(
        name=f"{JOB}/taskGroups/group0/tasks/0",
        status=batch_v1.TaskStatus(status_events=status_events),
    )


@unittest.skipUnless(_HAS_BATCH, "google-cloud-batch not installed")
class GcpBatchTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self.service = mock.MagicMock()
        self.backend = GcpBatchClient()
        self.backend._client = self.service
        patcher = mock.patch.dict(
            "bespokelabs.sandbox.backends.BACKENDS",
            {"gcp_batch": mock.Mock(return_value=self.backend)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, _ENV)
        env.start()
        self.addCleanup(env.stop)
        self.client = SandboxClient("gcp_batch")


class BuildJobTests(GcpBatchTestCase):

    def test_maps_the_spec_onto_a_batch_job(self) -> None:
        job = self.backend.build_job(
            JobSpec(
                command=["python", "-m", "train", "--epochs", "3"],
                image="us-docker.pkg.dev/proj/repo/train:1",
                env_vars={"MODE": "fast"},
                cpu=4,
                memory_mb=16384,
                disk_mb=100 * 1024,
                timeout_secs=7200,
                allow_internet=False,
                labels={"Team": "Evals", "run/id": "R_1"},
                workdir="/work",
                backend_options={
                    "service_account": "runner@proj.iam.gserviceaccount.com",
                    "network": "global/networks/default",
                    "subnetwork": "regions/us-central1/subnetworks/default",
                    "no_external_ip": True,
                    "provisioning_model": "spot",
                    "allowed_locations": ["zones/us-central1-a"],
                    "max_retry_count": 2,
                },
            )
        )
        task = job.task_groups[0].task_spec
        self.assertEqual(job.task_groups[0].task_count, 1)
        container = task.runnables[0].container
        self.assertEqual(
            container.image_uri, "us-docker.pkg.dev/proj/repo/train:1"
        )
        self.assertEqual(container.entrypoint, "python")
        self.assertEqual(
            list(container.commands), ["-m", "train", "--epochs", "3"]
        )
        self.assertTrue(container.block_external_network)
        self.assertEqual(container.options, "--workdir=/work")
        self.assertEqual(dict(task.environment.variables), {"MODE": "fast"})
        self.assertEqual(task.compute_resource.cpu_milli, 4000)
        self.assertEqual(task.compute_resource.memory_mib, 16384)
        self.assertEqual(task.max_run_duration.total_seconds(), 7200)
        self.assertEqual(task.max_retry_count, 2)

        self.assertEqual(dict(job.labels), {"team": "evals", "run_id": "r_1"})
        allocation = job.allocation_policy
        self.assertEqual(dict(allocation.labels), dict(job.labels))
        self.assertEqual(
            allocation.service_account.email,
            "runner@proj.iam.gserviceaccount.com",
        )
        nic = allocation.network.network_interfaces[0]
        self.assertEqual(nic.network, "global/networks/default")
        self.assertTrue(nic.no_external_ip_address)
        self.assertEqual(
            list(allocation.location.allowed_locations),
            ["zones/us-central1-a"],
        )
        policy = allocation.instances[0].policy
        self.assertEqual(policy.boot_disk.size_gb, 100)
        self.assertEqual(
            policy.provisioning_model,
            batch_v1.AllocationPolicy.ProvisioningModel.SPOT,
        )
        self.assertFalse(allocation.instances[0].install_gpu_drivers)
        self.assertEqual(
            job.logs_policy.destination,
            batch_v1.LogsPolicy.Destination.CLOUD_LOGGING,
        )

    def test_full_worker_job_shape(self) -> None:
        """A GPU worker job on a private network with its own identity."""
        image = "us-central1-docker.pkg.dev/proj/repo/worker@sha256:" + "a" * 64
        job = self.backend.build_job(
            JobSpec(
                command=[
                    "python",
                    "-m",
                    "worker.main",
                    "--input",
                    "gs://bucket/runs/r1/in",
                    "--output",
                    "gs://bucket/runs/r1/out",
                ],
                image=image,
                env_vars={"RUN_ID": "r1", "API_URL": "https://example.com"},
                cpu=8,
                memory_mb=32768,
                disk_mb=200 * 1024,
                gpu="L4",
                timeout_secs=6 * 3600,
                labels={"app": "worker", "run_id": "r1"},
                backend_options={
                    "project": "proj",
                    "region": "us-central1",
                    "service_account": "runner@proj.iam.gserviceaccount.com",
                    "service_account_scopes": [
                        "https://www.googleapis.com/auth/cloud-platform"
                    ],
                    "network": "projects/proj/global/networks/workers",
                    "subnetwork": (
                        "projects/proj/regions/us-central1/subnetworks/workers"
                    ),
                    "no_external_ip": True,
                },
            )
        )
        task = job.task_groups[0].task_spec
        container = task.runnables[0].container
        self.assertEqual(container.image_uri, image)
        self.assertEqual(container.entrypoint, "python")
        self.assertEqual(container.commands[-1], "gs://bucket/runs/r1/out")
        self.assertEqual(
            dict(task.environment.variables),
            {"RUN_ID": "r1", "API_URL": "https://example.com"},
        )
        self.assertEqual(
            (task.compute_resource.cpu_milli, task.compute_resource.memory_mib),
            (8000, 32768),
        )
        self.assertEqual(task.max_run_duration.total_seconds(), 6 * 3600)
        self.assertEqual(task.max_retry_count, 0)
        allocation = job.allocation_policy
        instance = allocation.instances[0]
        self.assertTrue(instance.install_gpu_drivers)
        self.assertEqual(instance.policy.machine_type, "g2-standard-12")
        self.assertEqual(instance.policy.boot_disk.size_gb, 200)
        self.assertEqual(
            allocation.service_account.email,
            "runner@proj.iam.gserviceaccount.com",
        )
        self.assertEqual(
            list(allocation.service_account.scopes),
            ["https://www.googleapis.com/auth/cloud-platform"],
        )
        nic = allocation.network.network_interfaces[0]
        self.assertEqual(nic.network, "projects/proj/global/networks/workers")
        self.assertEqual(
            nic.subnetwork,
            "projects/proj/regions/us-central1/subnetworks/workers",
        )
        self.assertTrue(nic.no_external_ip_address)
        self.assertEqual(dict(job.labels), {"app": "worker", "run_id": "r1"})

    def test_rejects_labels_that_collide_once_coerced(self) -> None:
        with self.assertRaises(SandboxConfigurationError) as ctx:
            self.backend.build_job(
                JobSpec(
                    command="x",
                    image="i",
                    labels={"Team": "a", "team": "b"},
                )
            )
        self.assertIn("'team'", str(ctx.exception))

    def test_scopes_need_a_service_account(self) -> None:
        with self.assertRaises(SandboxConfigurationError):
            self.backend.build_job(
                JobSpec(
                    command="x",
                    image="i",
                    backend_options={"service_account_scopes": ["s"]},
                )
            )

    def test_shell_commands_run_under_sh(self) -> None:
        job = self.backend.build_job(
            JobSpec(command="echo hi && exit 3", image="img")
        )
        container = job.task_groups[0].task_spec.runnables[0].container
        self.assertEqual(container.entrypoint, "/bin/sh")
        self.assertEqual(list(container.commands), ["-c", "echo hi && exit 3"])

    def test_gpu_picks_drivers_and_machine(self) -> None:
        # Accelerator-optimized machine types carry their GPUs: the machine
        # type alone sizes them, as in Batch's own GPU examples.
        job = self.backend.build_job(
            JobSpec(command="nvidia-smi", image="img", gpu="L4", cpu=6)
        )
        instance = job.allocation_policy.instances[0]
        self.assertTrue(instance.install_gpu_drivers)
        self.assertEqual(instance.policy.machine_type, "g2-standard-8")
        self.assertEqual(list(instance.policy.accelerators), [])

        job = self.backend.build_job(
            JobSpec(
                command="x",
                image="img",
                gpu="A100-80GB",
                cpu=8,
                memory_mb=32 * 1024,
            )
        )
        instance = job.allocation_policy.instances[0]
        self.assertTrue(instance.install_gpu_drivers)
        self.assertEqual(instance.policy.machine_type, "a2-ultragpu-1g")
        self.assertEqual(list(instance.policy.accelerators), [])

        job = self.backend.build_job(
            JobSpec(
                command="x",
                image="img",
                gpu="H100:8",
                backend_options={"machine_type": "a3-megagpu-8g"},
            )
        )
        policy = job.allocation_policy.instances[0].policy
        self.assertEqual(policy.machine_type, "a3-megagpu-8g")
        self.assertEqual(list(policy.accelerators), [])

    def test_n1_gpus_attach_as_accelerators(self) -> None:
        job = self.backend.build_job(
            JobSpec(command="x", image="img", gpu="T4:2", cpu=4)
        )
        instance = job.allocation_policy.instances[0]
        self.assertTrue(instance.install_gpu_drivers)
        accel = instance.policy.accelerators[0]
        self.assertEqual((accel.type_, accel.count), ("nvidia-tesla-t4", 2))
        self.assertTrue(instance.policy.machine_type.startswith("n1-"))

    def test_a_driver_version_lists_the_gpus(self) -> None:
        job = self.backend.build_job(
            JobSpec(
                command="x",
                image="img",
                gpu="A100-80GB",
                backend_options={"gpu_driver_version": "LATEST"},
            )
        )
        policy = job.allocation_policy.instances[0].policy
        self.assertEqual(policy.machine_type, "a2-ultragpu-1g")
        accel = policy.accelerators[0]
        self.assertEqual(
            (accel.type_, accel.count, accel.driver_version),
            ("nvidia-a100-80gb", 1, "LATEST"),
        )

    def test_rejections(self) -> None:
        cases = [
            (JobSpec(command="x"), SandboxConfigurationError),
            (
                JobSpec(command="x", image="i", backend_options={"nope": 1}),
                SandboxConfigurationError,
            ),
            (
                JobSpec(command="x", image="i", gpu="L4:3"),
                SandboxConfigurationError,
            ),
            (
                JobSpec(
                    command="x",
                    image="i",
                    backend_options={"provisioning_model": "free"},
                ),
                SandboxConfigurationError,
            ),
        ]
        for spec, error in cases:
            with self.subTest(spec=spec):
                with self.assertRaises(error):
                    self.backend.build_job(spec)

    def test_task_count_and_parallelism(self) -> None:
        group = self.backend.build_job(
            JobSpec(
                command="x",
                image="i",
                backend_options={"task_count": 8, "parallelism": 3},
            )
        ).task_groups[0]
        self.assertEqual((group.task_count, group.parallelism), (8, 3))
        # Without parallelism the field stays unset: Batch picks its default.
        group = self.backend.build_job(
            JobSpec(command="x", image="i", backend_options={"task_count": 8})
        ).task_groups[0]
        self.assertEqual((group.task_count, group.parallelism), (8, 0))
        group = self.backend.build_job(
            JobSpec(
                command="x",
                image="i",
                backend_options={
                    "task_count": MAX_TASK_COUNT,
                    "parallelism": MAX_PARALLELISM,
                },
            )
        ).task_groups[0]
        self.assertEqual(
            (group.task_count, group.parallelism),
            (MAX_TASK_COUNT, MAX_PARALLELISM),
        )
        # The task index is Batch's own BATCH_TASK_INDEX, not a new variable.
        task = group.task_spec
        self.assertNotIn("environment", task)
        self.assertEqual(len(group.task_environments), 0)

    def test_task_count_and_parallelism_rejections(self) -> None:
        for options in (
            {"task_count": 0},
            {"task_count": -1},
            {"task_count": True},
            {"task_count": "3"},
            {"task_count": 1.5},
            {"task_count": MAX_TASK_COUNT + 1},
            {"parallelism": 0},
            {"parallelism": 2},
            {"parallelism": False},
            {"task_count": 4, "parallelism": 5},
            {"task_count": 4, "parallelism": "2"},
            {"task_count": MAX_TASK_COUNT, "parallelism": MAX_PARALLELISM + 1},
        ):
            with self.subTest(options=options):
                with self.assertRaises(SandboxConfigurationError):
                    self.backend.build_job(
                        JobSpec(command="x", image="i", backend_options=options)
                    )

    def test_absent_new_options_leave_the_job_unchanged(self) -> None:
        job = self.backend.build_job(
            JobSpec(command="x", image="i", labels={"team": "a"})
        )
        self.assertEqual(job.task_groups[0].task_count, 1)
        self.assertEqual(job.task_groups[0].parallelism, 0)
        task = job.task_groups[0].task_spec
        self.assertEqual(task.runnables[0].container.image_uri, "i")
        self.assertFalse(task.runnables[0].script.text)
        self.assertNotIn("environment", task)
        instances = job.allocation_policy.instances[0]
        self.assertFalse(instances.install_gpu_drivers)
        self.assertFalse(instances.block_project_ssh_keys)
        self.assertNotIn("boot_disk", instances.policy)
        self.assertEqual(dict(job.allocation_policy.labels), {"team": "a"})
        self.assertEqual(dict(job.labels), {"team": "a"})

    def test_secret_env_vars_reference_secret_manager(self) -> None:
        version = "projects/example-project/secrets/api-key/versions/latest"
        job = self.backend.build_job(
            JobSpec(
                command="x",
                image="i",
                env_vars={"MODE": "fast"},
                backend_options={"secret_env_vars": {"API_KEY": version}},
            )
        )
        environment = job.task_groups[0].task_spec.environment
        self.assertEqual(dict(environment.variables), {"MODE": "fast"})
        self.assertEqual(
            dict(environment.secret_variables), {"API_KEY": version}
        )
        only = (
            self.backend.build_job(
                JobSpec(
                    command="x",
                    image="i",
                    backend_options={
                        "secret_env_vars": {
                            "API_KEY": "projects/123/secrets/api-key/versions/7"
                        }
                    },
                )
            )
            .task_groups[0]
            .task_spec.environment
        )
        self.assertEqual(dict(only.variables), {})
        self.assertEqual(
            dict(only.secret_variables),
            {"API_KEY": "projects/123/secrets/api-key/versions/7"},
        )

    def test_secret_env_var_rejections_do_not_echo_the_reference(self) -> None:
        version = "projects/example-project/secrets/api-key/versions/1"
        cases = [
            ({"secret_env_vars": ["API_KEY"]}, {}),
            ({"secret_env_vars": {"1BAD": version}}, {}),
            ({"secret_env_vars": {"API_KEY": version}}, {"API_KEY": "x"}),
            ({"secret_env_vars": {"API_KEY": 7}}, {}),
            ({"secret_env_vars": {"API_KEY": "pasted-secret-value"}}, {}),
            (
                {
                    "secret_env_vars": {
                        "API_KEY": "projects/example-project/secrets/api-key"
                    }
                },
                {},
            ),
            (
                {
                    "secret_env_vars": {
                        "API_KEY": "projects/p/locations/l/secrets/s/versions/1"
                    }
                },
                {},
            ),
            ({"secret_env_vars": {"API_KEY": version + "/extra"}}, {}),
        ]
        for options, env_vars in cases:
            with self.subTest(options=options):
                with self.assertRaises(SandboxConfigurationError) as ctx:
                    self.backend.build_job(
                        JobSpec(
                            command="x",
                            image="i",
                            env_vars=env_vars,
                            backend_options=options,
                        )
                    )
                self.assertNotIn("pasted-secret-value", str(ctx.exception))

    def test_script_runs_the_command_on_the_vm(self) -> None:
        text = "#!/bin/bash\nset -euo pipefail\necho hello\n"
        job = self.backend.build_job(
            JobSpec(
                command=text,
                env_vars={"MODE": "fast"},
                backend_options={"runnable": "script"},
            )
        )
        task = job.task_groups[0].task_spec
        self.assertEqual(len(task.runnables), 1)
        self.assertEqual(task.runnables[0].script.text, text)
        self.assertNotIn("container", task.runnables[0])
        self.assertEqual(dict(task.environment.variables), {"MODE": "fast"})
        explicit = self.backend.build_job(
            JobSpec(
                command="x",
                image="i",
                backend_options={"runnable": "container"},
            )
        )
        runnable = explicit.task_groups[0].task_spec.runnables[0]
        self.assertEqual(runnable.container.image_uri, "i")

    def test_script_rejections(self) -> None:
        script = {"runnable": "script"}
        cases = [
            JobSpec(command="x", image="i", backend_options=script),
            JobSpec(command=["echo", "x"], backend_options=script),
            JobSpec(command="x", workdir="/work", backend_options=script),
            JobSpec(
                command="x",
                backend_options={**script, "container_options": "--init"},
            ),
            JobSpec(command="x", allow_internet=False, backend_options=script),
            JobSpec(command="x", image="i", backend_options={"runnable": "vm"}),
            JobSpec(command="x", backend_options={"runnable": "container"}),
        ]
        for spec in cases:
            with self.subTest(spec=spec):
                with self.assertRaises(SandboxConfigurationError):
                    self.backend.build_job(spec)

    def test_boot_disk_image_and_type(self) -> None:
        image = "projects/example-project/global/images/worker-image"
        job = self.backend.build_job(
            JobSpec(
                command="x",
                image="i",
                disk_mb=200 * 1024,
                backend_options={
                    "boot_disk_image": image,
                    "boot_disk_type": "pd-balanced",
                },
            )
        )
        disk = job.allocation_policy.instances[0].policy.boot_disk
        self.assertEqual(
            (disk.size_gb, disk.image, disk.type_),
            (200, image, "pd-balanced"),
        )
        unsized = self.backend.build_job(
            JobSpec(
                command="x",
                image="i",
                backend_options={"boot_disk_type": "pd-ssd"},
            )
        )
        disk = unsized.allocation_policy.instances[0].policy.boot_disk
        self.assertEqual(
            (disk.size_gb, disk.image, disk.type_), (0, "", "pd-ssd")
        )
        for key in ("boot_disk_image", "boot_disk_type"):
            for value in ("", 7, "pd balanced", "x" * 1025):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(SandboxConfigurationError):
                        self.backend.build_job(
                            JobSpec(
                                command="x",
                                image="i",
                                backend_options={key: value},
                            )
                        )

    def test_gpu_drivers_for_an_explicit_gpu_machine_type(self) -> None:
        job = self.backend.build_job(
            JobSpec(
                command="x",
                image="i",
                backend_options={
                    "machine_type": "g2-standard-8",
                    "install_gpu_drivers": True,
                },
            )
        )
        instances = job.allocation_policy.instances[0]
        self.assertTrue(instances.install_gpu_drivers)
        self.assertEqual(instances.policy.machine_type, "g2-standard-8")
        self.assertEqual(list(instances.policy.accelerators), [])
        # Without the option an explicit machine type installs nothing.
        plain = self.backend.build_job(
            JobSpec(
                command="x",
                image="i",
                backend_options={"machine_type": "g2-standard-8"},
            )
        )
        self.assertFalse(
            plain.allocation_policy.instances[0].install_gpu_drivers
        )
        # An image that ships its own drivers can turn the install off.
        preinstalled = self.backend.build_job(
            JobSpec(
                command="x",
                image="i",
                gpu="L4",
                backend_options={"install_gpu_drivers": False},
            )
        )
        self.assertFalse(
            preinstalled.allocation_policy.instances[0].install_gpu_drivers
        )
        for options in (
            {"install_gpu_drivers": True},
            {"install_gpu_drivers": True, "machine_type": "n2-standard-8"},
            {"install_gpu_drivers": "yes", "machine_type": "g2-standard-8"},
            {"install_gpu_drivers": 1, "machine_type": "g2-standard-8"},
        ):
            with self.subTest(options=options):
                with self.assertRaises(SandboxConfigurationError):
                    self.backend.build_job(
                        JobSpec(command="x", image="i", backend_options=options)
                    )

    def test_block_project_ssh_keys(self) -> None:
        job = self.backend.build_job(
            JobSpec(
                command="x",
                image="i",
                backend_options={"block_project_ssh_keys": True},
            )
        )
        self.assertTrue(
            job.allocation_policy.instances[0].block_project_ssh_keys
        )
        off = self.backend.build_job(
            JobSpec(
                command="x",
                image="i",
                backend_options={"block_project_ssh_keys": False},
            )
        )
        self.assertFalse(
            off.allocation_policy.instances[0].block_project_ssh_keys
        )
        with self.assertRaises(SandboxConfigurationError):
            self.backend.build_job(
                JobSpec(
                    command="x",
                    image="i",
                    backend_options={"block_project_ssh_keys": "true"},
                )
            )

    def test_instance_labels_go_on_the_vms_only(self) -> None:
        job = self.backend.build_job(
            JobSpec(
                command="x",
                image="i",
                labels={"team": "a"},
                backend_options={"instance_labels": {"Pool": "GPU Workers"}},
            )
        )
        self.assertEqual(dict(job.labels), {"team": "a"})
        self.assertEqual(
            dict(job.allocation_policy.labels),
            {"team": "a", "pool": "gpu_workers"},
        )
        only = self.backend.build_job(
            JobSpec(
                command="x",
                image="i",
                backend_options={"instance_labels": {"pool": "gpu"}},
            )
        )
        self.assertEqual(dict(only.labels), {})
        self.assertEqual(dict(only.allocation_policy.labels), {"pool": "gpu"})
        for labels, extra in (
            ({"team": "a"}, {"Team": "b"}),
            ({}, {"Pool": "a", "pool": "b"}),
            ({}, {"pool": 1}),
            ({}, ["pool"]),
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(SandboxConfigurationError):
                    self.backend.build_job(
                        JobSpec(
                            command="x",
                            image="i",
                            labels=labels,
                            backend_options={"instance_labels": extra},
                        )
                    )

    def test_interactive_sandboxes_are_not_supported(self) -> None:
        with self.assertRaises(FeatureNotSupportedError):
            Sandbox("gcp_batch")


class GpuSelectionTests(unittest.TestCase):

    @unittest.skipUnless(_HAS_BATCH, "google-cloud-batch not installed")
    def test_parse_gpu(self) -> None:
        self.assertEqual(parse_gpu("A100-80GB:2"), ("nvidia-a100-80gb", 2))
        self.assertEqual(parse_gpu("t4"), ("nvidia-tesla-t4", 1))
        self.assertEqual(parse_gpu("nvidia-l4:4"), ("nvidia-l4", 4))
        for bad in ("A10G", "L4:0", "L4:x"):
            with self.subTest(gpu=bad):
                with self.assertRaises(SandboxConfigurationError):
                    parse_gpu(bad)

    @unittest.skipUnless(_HAS_BATCH, "google-cloud-batch not installed")
    def test_machine_type_for(self) -> None:
        self.assertEqual(
            machine_type_for("nvidia-l4", 1, 1, 1024), "g2-standard-4"
        )
        # 16 GB nominal is not 16 GiB usable.
        self.assertEqual(
            machine_type_for("nvidia-l4", 1, 1, 16384), "g2-standard-8"
        )
        self.assertEqual(
            machine_type_for("nvidia-l4", 4, 1, 1024), "g2-standard-48"
        )
        self.assertEqual(
            machine_type_for("nvidia-tesla-t4", 1, 3, 1024), "n1-standard-4"
        )
        self.assertEqual(
            machine_type_for("nvidia-tesla-t4", 1, 2, 11 * 1024),
            "n1-highmem-2",
        )
        self.assertEqual(
            machine_type_for("nvidia-h100-80gb", 8, 1, 1024), "a3-highgpu-8g"
        )
        with self.assertRaises(SandboxConfigurationError):
            machine_type_for("nvidia-l4", 1, 64, 1024)
        with self.assertRaises(SandboxConfigurationError):
            machine_type_for("nvidia-rtx-pro-6000", 1, 1, 1024)


class SubmitTests(GcpBatchTestCase):

    def test_submit_returns_the_resource_name(self) -> None:
        self.service.create_job.return_value = batch_v1.Job(name=JOB)
        job_id = self.client.submit_job(
            JobSpec(
                command="true",
                image="img",
                backend_options={"job_id": "job-1"},
            )
        )
        self.assertEqual(job_id, JOB)
        request = self.service.create_job.call_args.kwargs["request"]
        self.assertEqual(request.parent, "projects/proj/locations/us-central1")
        self.assertEqual(request.job_id, "job-1")
        self.assertTrue(request.request_id)

    def test_generated_ids_and_explicit_location(self) -> None:
        self.service.create_job.return_value = batch_v1.Job()
        job_id = self.client.submit_job(
            JobSpec(
                command="true",
                image="img",
                backend_options={
                    "project": "other",
                    "region": "europe-west4",
                    "job_id_prefix": "myapp",
                },
            )
        )
        self.assertRegex(
            job_id,
            r"^projects/other/locations/europe-west4/jobs/myapp-[0-9a-f]+$",
        )

    def test_region_is_chosen_from_candidates_by_free_quota(self) -> None:
        from bespokelabs.sandbox.backends import gcp_batch_placement

        self.service.create_job.return_value = batch_v1.Job()
        choice = gcp_batch_placement.RegionChoice("us-east1", True)
        with mock.patch.object(
            gcp_batch_placement, "choose_region", return_value=choice
        ) as choose:
            job_id = self.client.submit_job(
                JobSpec(
                    command="true",
                    image="img",
                    backend_options={
                        "regions": ["us-central1", "us-east1"],
                        "region_quota": {"CPUS": 16},
                    },
                )
            )
        choose.assert_called_once_with(
            "proj", ["us-central1", "us-east1"], {"CPUS": 16}
        )
        self.assertIn("/locations/us-east1/jobs/", job_id)

    def test_region_candidates_without_quota_or_with_explicit_region(
        self,
    ) -> None:
        from bespokelabs.sandbox.backends import gcp_batch_placement

        self.service.create_job.return_value = batch_v1.Job()
        with mock.patch.object(gcp_batch_placement, "choose_region") as choose:
            first = self.client.submit_job(
                JobSpec(
                    command="true",
                    image="img",
                    backend_options={"regions": ["us-west1", "us-east1"]},
                )
            )
            explicit = self.client.submit_job(
                JobSpec(
                    command="true",
                    image="img",
                    backend_options={
                        "region": "europe-west4",
                        "regions": ["us-west1"],
                        "region_quota": {"CPUS": 16},
                    },
                )
            )
        choose.assert_not_called()
        self.assertIn("/locations/us-west1/jobs/", first)
        self.assertIn("/locations/europe-west4/jobs/", explicit)

    def test_invalid_region_candidates_are_rejected(self) -> None:
        for options in (
            {"regions": []},
            {"regions": "us-central1"},
            {"regions": ["us-central1", ""]},
            {"region_quota": {"CPUS": 16}},
            {"regions": ["us-central1"], "region_quota": {"CPUS": "many"}},
            {"regions": ["us-central1"], "region_quota": ["CPUS"]},
        ):
            with self.subTest(options=options):
                with self.assertRaises(SandboxConfigurationError):
                    self.client.submit_job(
                        JobSpec(
                            command="true", image="img", backend_options=options
                        )
                    )
        self.service.create_job.assert_not_called()

    def test_configuration_errors(self) -> None:
        with self.assertRaises(SandboxConfigurationError):
            self.client.submit_job(
                JobSpec(
                    command="true",
                    image="img",
                    backend_options={"job_id": "Bad_ID"},
                )
            )
        with mock.patch.dict(os.environ, {"GOOGLE_CLOUD_REGION": ""}):
            with self.assertRaises(SandboxConfigurationError):
                self.client.submit_job(JobSpec(command="true", image="img"))
        self.service.create_job.assert_not_called()

    def test_provider_errors_are_classified(self) -> None:
        self.service.create_job.side_effect = gexc.PermissionDenied("no")
        with self.assertRaises(SandboxAuthenticationError):
            self.client.submit_job(JobSpec(command="true", image="img"))
        self.service.create_job.side_effect = gexc.ServiceUnavailable("503")
        with self.assertRaises(SandboxConnectionError):
            self.client.submit_job(JobSpec(command="true", image="img"))


class StatusTests(GcpBatchTestCase):

    def test_pending_does_not_list_tasks(self) -> None:
        self.service.get_job.return_value = _job("QUEUED")
        status = self.client.job_status(JOB)
        self.assertEqual(status.state, JobState.PENDING)
        self.assertEqual(status.details["batch_state"], "QUEUED")
        self.service.list_tasks.assert_not_called()

    def test_details_carry_the_jobs_labels(self) -> None:
        """README: a caller checks a job's labels before it cancels it."""
        self.service.get_job.return_value = _job(
            "QUEUED", labels={"app": "demo", "env": "testing"}
        )
        status = self.client.job_status(JOB)
        self.assertEqual(
            status.details["labels"],
            {"app": "demo", "env": "testing"},
        )
        self.service.get_job.return_value = _job("QUEUED")
        self.assertEqual(self.client.job_status(JOB).details["labels"], {})

    def test_message_is_absent_without_event_text(self) -> None:
        """README: details["message"] exists only when Batch gave a text."""
        self.service.get_job.return_value = _job("RUNNING")
        self.service.list_tasks.return_value = iter(
            [_task(("RUNNING", 5, None))]
        )
        status = self.client.job_status(JOB)
        self.assertNotIn("message", status.details)

    def test_running(self) -> None:
        self.service.get_job.return_value = _job("RUNNING")
        self.service.list_tasks.return_value = iter(
            [_task(("ASSIGNED", 1, None), ("RUNNING", 5, None))]
        )
        status = self.client.job_status(JOB)
        self.assertEqual(status.state, JobState.RUNNING)
        self.assertEqual(status.started_at, _when(5))
        self.assertIsNone(status.finished_at)

    def test_succeeded(self) -> None:
        self.service.get_job.return_value = _job("SUCCEEDED")
        self.service.list_tasks.return_value = iter(
            [_task(("RUNNING", 5, None), ("SUCCEEDED", 9, 0))]
        )
        status = self.client.job_status(JOB)
        self.assertEqual((status.state, status.exit_code), ("succeeded", 0))
        self.assertEqual(status.finished_at, _when(9))
        parent = self.service.list_tasks.call_args.kwargs["request"].parent
        self.assertEqual(parent, f"{JOB}/taskGroups/group0")

    def test_failed_and_timed_out(self) -> None:
        for code, reason in ((3, "exit_code"), (50005, "timeout")):
            with self.subTest(code=code):
                self.service.get_job.return_value = _job("FAILED")
                self.service.list_tasks.return_value = iter(
                    [_task(("RUNNING", 5, None), ("FAILED", 9, code))]
                )
                status = self.client.job_status(JOB)
                self.assertEqual(
                    (status.state, status.exit_code, status.reason),
                    (JobState.FAILED, code, reason),
                )

    def test_failure_without_an_exit_code(self) -> None:
        cause = (
            "Job state is set from SCHEDULED to FAILED: the service account "
            "cannot pull us-central1-docker.pkg.dev/proj/repo/worker"
        )
        self.service.get_job.return_value = _job("FAILED", events=(cause,))
        self.service.list_tasks.return_value = iter(
            [_task(("FAILED", 9, None, "Task state is updated to FAILED"))]
        )
        status = self.client.job_status(JOB)
        self.assertEqual(
            (status.state, status.exit_code, status.reason),
            (JobState.FAILED, None, "batch_failed"),
        )
        self.assertEqual(status.details["message"], cause)

    def test_failed_task_explains_its_exit(self) -> None:
        text = "Task state is updated from RUNNING to FAILED with exit code 2."
        self.service.get_job.return_value = _job(
            "FAILED", events=("Job failed due to task failure.",)
        )
        self.service.list_tasks.return_value = iter(
            [_task(("RUNNING", 5, None, "started"), ("FAILED", 9, 2, text))]
        )
        status = self.client.job_status(JOB)
        self.assertEqual((status.exit_code, status.reason), (2, "exit_code"))
        self.assertEqual(status.details["message"], text)

    def test_capacity_failures_have_their_own_reasons(self) -> None:
        for name, events, reason in _CAPACITY_CASES:
            with self.subTest(name):
                self.service.get_job.return_value = _job(
                    "FAILED", events=events
                )
                self.service.list_tasks.return_value = iter(())
                status = self.client.job_status(JOB)
                self.assertEqual(
                    (status.state, status.exit_code, status.reason),
                    (JobState.FAILED, None, reason),
                )
                # The provider's text stays in details, never in the reason.
                self.assertEqual(
                    status.details.get("message"), (events or [None])[-1]
                )

    def test_capacity_reason_reads_the_tasks_events_too(self) -> None:
        self.service.get_job.return_value = _job(
            "FAILED", events=("Job state is set from SCHEDULED to FAILED",)
        )
        self.service.list_tasks.return_value = iter(
            [_task(("FAILED", 9, None, _STOCKOUT_EVENT))]
        )
        status = self.client.job_status(JOB)
        self.assertEqual(status.reason, "capacity_unavailable")

    def test_an_exit_code_outranks_a_capacity_event(self) -> None:
        self.service.get_job.return_value = _job(
            "FAILED", events=(_QUOTA_EVENT,)
        )
        self.service.list_tasks.return_value = iter(
            [_task(("RUNNING", 5, None), ("FAILED", 9, 3))]
        )
        status = self.client.job_status(JOB)
        self.assertEqual((status.exit_code, status.reason), (3, "exit_code"))

    def test_capacity_reasons_are_only_for_failed_jobs(self) -> None:
        self.service.get_job.return_value = _job(
            "QUEUED", events=(_QUOTA_EVENT,)
        )
        self.assertIsNone(self.client.job_status(JOB).reason)
        self.assertIsNone(capacity_reason(None))
        self.assertIsNone(
            capacity_reason([batch_v1.StatusEvent(type_="STATUS_CHANGED")])
        )

    def test_capacity_reason_matches_the_documented_quota_wording(
        self,
    ) -> None:
        for text in (
            "VM group0 creation failed: Quota CPUS exceeded. Limit: 24.0 in "
            "region us-central1",
            "Quota checking process decides to delay scheduling for the job "
            "uid-1 due to inadequate quotas [Quota: CPUS, limit: 24, usage: "
            "24, wanted: 8.]",
        ):
            with self.subTest(text[:30]):
                event = batch_v1.StatusEvent(description=text)
                self.assertEqual(capacity_reason([event]), "quota_exceeded")

    def test_capacity_event_before_running_is_not_the_cause(self) -> None:
        events = [
            batch_v1.StatusEvent(description=_STOCKOUT_EVENT),
            batch_v1.StatusEvent(
                description="Job state is set from SCHEDULED to RUNNING"
            ),
            batch_v1.StatusEvent(
                description="Job state is set from RUNNING to FAILED"
            ),
        ]
        self.assertIsNone(capacity_reason(events))
        # The same stock-out after the last run attempt is the cause.
        self.assertEqual(
            capacity_reason([events[1], events[0]]), "capacity_unavailable"
        )

    def test_capacity_reason_orders_events_by_time(self) -> None:
        import datetime

        def at(second: int, text: str) -> object:
            return batch_v1.StatusEvent(
                description=text,
                event_time=datetime.datetime(
                    2025, 1, 1, 0, 0, second, tzinfo=datetime.timezone.utc
                ),
            )

        # Listed newest first: the quota event is older than the move to
        # RUNNING, so it does not explain the failure.
        events = [
            at(30, "Job state is set from RUNNING to FAILED"),
            at(20, "Job state is set from SCHEDULED to RUNNING"),
            at(10, _QUOTA_EVENT),
        ]
        self.assertIsNone(capacity_reason(events))

    def test_single_task_status_has_no_task_summary(self) -> None:
        for task_count in (0, 1):
            with self.subTest(task_count=task_count):
                self.service.get_job.return_value = _job(
                    "SUCCEEDED", task_count=task_count
                )
                self.service.list_tasks.return_value = iter(
                    [_task(("RUNNING", 5, None), ("SUCCEEDED", 9, 0))]
                )
                status = self.client.job_status(JOB)
                self.assertNotIn("tasks", status.details)
                request = self.service.list_tasks.call_args.kwargs["request"]
                self.assertEqual(request.page_size, 1)

    def test_reattach_from_a_new_process(self) -> None:
        """A restarted worker holds only the job id: no env, no old client."""
        service = mock.MagicMock()
        service.get_job.return_value = _job("SUCCEEDED")
        service.list_tasks.return_value = iter([_task(("SUCCEEDED", 9, 0))])
        fresh = GcpBatchClient()
        fresh._client = service
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.dict(
                "bespokelabs.sandbox.backends.BACKENDS",
                {"gcp_batch": mock.Mock(return_value=fresh)},
            ),
        ):
            client = SandboxClient("gcp_batch")
            status = client.job_status(JOB)
        self.assertEqual((status.state, status.exit_code), ("succeeded", 0))
        service.get_job.assert_called_once_with(name=JOB)

    def test_missing_job_is_unknown(self) -> None:
        self.service.get_job.side_effect = gexc.NotFound("gone")
        status = self.client.job_status(JOB)
        self.assertEqual(
            (status.state, status.reason), (JobState.UNKNOWN, "job_not_found")
        )

    def test_wait_job(self) -> None:
        self.service.get_job.side_effect = [
            _job("SCHEDULED"),
            gexc.ServiceUnavailable("blip"),
            _job("SUCCEEDED"),
        ]
        self.service.list_tasks.return_value = iter(
            [_task(("SUCCEEDED", 9, 0))]
        )
        with mock.patch("bespokelabs.sandbox._jobs.time.sleep"):
            status = self.client.wait_job(JOB, timeout_secs=60, poll_secs=1)
        self.assertEqual(status.state, JobState.SUCCEEDED)

    def test_rejects_non_batch_ids(self) -> None:
        with self.assertRaises(SandboxConfigurationError):
            self.client.job_status("local:abc")


class MultiTaskStatusTests(GcpBatchTestCase):

    def test_pending_does_not_list_tasks(self) -> None:
        self.service.get_job.return_value = _job("QUEUED", task_count=4)
        status = self.client.job_status(JOB)
        self.assertNotIn("tasks", status.details)
        self.service.list_tasks.assert_not_called()

    def test_all_tasks_succeeded(self) -> None:
        self.service.get_job.return_value = _job("SUCCEEDED", task_count=3)
        self.service.list_tasks.return_value = iter(
            [
                _indexed(
                    i,
                    "SUCCEEDED",
                    ("RUNNING", 5 + i, None),
                    ("SUCCEEDED", 20 + i, None),
                )
                for i in range(3)
            ]
        )
        status = self.client.job_status(JOB)
        self.assertEqual(
            (status.state, status.exit_code, status.reason),
            (JobState.SUCCEEDED, 0, None),
        )
        self.assertEqual(
            status.details["tasks"],
            {
                "count": 3,
                "succeeded": 3,
                "failed": 0,
                "other": 0,
                "failures": [],
                "truncated": False,
            },
        )
        # First task to start, last task to finish.
        self.assertEqual(status.started_at, _when(5))
        self.assertEqual(status.finished_at, _when(22))

    def test_mixed_outcomes(self) -> None:
        self.service.get_job.return_value = _job(
            "FAILED",
            task_count=6,
            events=("Job failed due to task failure.",),
        )
        text = "Task state is updated from RUNNING to FAILED with exit code 7."
        self.service.list_tasks.return_value = iter(
            [
                # Listed out of order: failures are reported by index.
                _indexed(
                    4, "FAILED", ("RUNNING", 4, None), ("FAILED", 30, 50001)
                ),
                _indexed(
                    0, "SUCCEEDED", ("RUNNING", 2, None), ("SUCCEEDED", 9, 0)
                ),
                _indexed(
                    1, "FAILED", ("RUNNING", 3, None), ("FAILED", 12, 7, text)
                ),
                _indexed(2, "FAILED", ("FAILED", 13, None, _STOCKOUT_EVENT)),
                _indexed(3, "UNEXECUTED"),
                _indexed(5, "FAILED", ("FAILED", 14, None, "Task failed")),
            ]
        )
        status = self.client.job_status(JOB)
        self.assertEqual(status.state, JobState.FAILED)
        # The job's own exit code and reason are its first failed task's.
        self.assertEqual((status.exit_code, status.reason), (7, "exit_code"))
        self.assertEqual(status.details["message"], text)
        self.assertEqual(
            status.details["tasks"],
            {
                "count": 6,
                "succeeded": 1,
                "failed": 4,
                "other": 1,
                "failures": [
                    {"index": 1, "exit_code": 7, "reason": "exit_code"},
                    {
                        "index": 2,
                        "exit_code": None,
                        "reason": "capacity_unavailable",
                    },
                    {"index": 4, "exit_code": 50001, "reason": "vm_preempted"},
                    {"index": 5, "exit_code": None, "reason": "batch_failed"},
                ],
                "truncated": False,
            },
        )
        # No provider text in the summary.
        self.assertNotIn("zone", repr(status.details["tasks"]))
        self.assertEqual(status.started_at, _when(2))
        self.assertEqual(status.finished_at, _when(30))
        request = self.service.list_tasks.call_args.kwargs["request"]
        self.assertEqual(request.parent, f"{JOB}/taskGroups/group0")
        self.assertGreater(request.page_size, 1)

    def test_running_counts_unfinished_tasks_as_other(self) -> None:
        self.service.get_job.return_value = _job("RUNNING", task_count=5)
        self.service.list_tasks.return_value = iter(
            [
                _indexed(
                    0, "SUCCEEDED", ("RUNNING", 2, None), ("SUCCEEDED", 9, 0)
                ),
                _indexed(1, "FAILED", ("RUNNING", 3, None), ("FAILED", 8, 1)),
                _indexed(2, "RUNNING", ("RUNNING", 4, None)),
                _indexed(3, "PENDING"),
            ]
        )
        status = self.client.job_status(JOB)
        # A task failure is not the job's outcome while the job still runs.
        self.assertEqual(
            (status.state, status.exit_code, status.reason),
            (JobState.RUNNING, None, None),
        )
        tasks = status.details["tasks"]
        # The fifth task is not listed yet; it still counts.
        self.assertEqual(
            (
                tasks["count"],
                tasks["succeeded"],
                tasks["failed"],
                tasks["other"],
            ),
            (5, 1, 1, 3),
        )
        self.assertEqual(
            tasks["failures"],
            [{"index": 1, "exit_code": 1, "reason": "exit_code"}],
        )
        self.assertEqual(status.started_at, _when(2))
        self.assertIsNone(status.finished_at)

    def test_job_failure_without_task_failures(self) -> None:
        self.service.get_job.return_value = _job(
            "FAILED", task_count=3, events=(_QUOTA_EVENT,)
        )
        self.service.list_tasks.return_value = iter(
            [_indexed(i, "PENDING") for i in range(3)]
        )
        status = self.client.job_status(JOB)
        self.assertEqual(
            (status.exit_code, status.reason), (None, "quota_exceeded")
        )
        self.assertEqual(
            status.details["tasks"],
            {
                "count": 3,
                "succeeded": 0,
                "failed": 0,
                "other": 3,
                "failures": [],
                "truncated": False,
            },
        )

    def test_listing_follows_every_page(self) -> None:
        pages = [
            [_indexed(0, "SUCCEEDED"), _indexed(1, "FAILED", ("FAILED", 5, 2))],
            [],
            [_indexed(2, "SUCCEEDED"), _indexed(3, "SUCCEEDED")],
            [_indexed(4, "FAILED", ("FAILED", 9, 3))],
        ]
        requests: list[str] = []
        self.service.get_job.return_value = _job("FAILED", task_count=5)
        self.service.list_tasks.return_value = _pager(pages, requests)
        tasks = self.client.job_status(JOB).details["tasks"]
        self.assertEqual(requests, ["page-1", "page-2", "page-3"])
        self.assertEqual(
            (
                tasks["count"],
                tasks["succeeded"],
                tasks["failed"],
                tasks["other"],
            ),
            (5, 3, 2, 0),
        )
        self.assertEqual([f["index"] for f in tasks["failures"]], [1, 4])

    def test_failures_are_bounded_and_flagged(self) -> None:
        failed = MAX_TASK_FAILURES + 5
        self.service.get_job.return_value = _job(
            "FAILED", task_count=failed + 1
        )
        self.service.list_tasks.return_value = iter(
            [_indexed(0, "SUCCEEDED")]
            + [
                _indexed(i, "FAILED", ("FAILED", 9, 1))
                for i in range(failed, 0, -1)
            ]
        )
        tasks = self.client.job_status(JOB).details["tasks"]
        self.assertEqual((tasks["failed"], tasks["succeeded"]), (failed, 1))
        self.assertTrue(tasks["truncated"])
        # The lowest indexes are the ones kept.
        self.assertEqual(
            [f["index"] for f in tasks["failures"]],
            list(range(1, MAX_TASK_FAILURES + 1)),
        )
        # Exactly the bound is complete, not truncated.
        self.service.get_job.return_value = _job(
            "FAILED", task_count=MAX_TASK_FAILURES
        )
        self.service.list_tasks.return_value = iter(
            [
                _indexed(i, "FAILED", ("FAILED", 9, 1))
                for i in range(MAX_TASK_FAILURES)
            ]
        )
        tasks = self.client.job_status(JOB).details["tasks"]
        self.assertEqual(len(tasks["failures"]), MAX_TASK_FAILURES)
        self.assertFalse(tasks["truncated"])

    def test_listing_stops_at_the_task_bound(self) -> None:
        """A job made elsewhere with more tasks than this client allows."""
        requests: list[str] = []
        pages = [[_indexed(i, "SUCCEEDED")] for i in range(6)]
        self.service.get_job.return_value = _job("RUNNING", task_count=6)
        self.service.list_tasks.return_value = _pager(pages, requests)
        with mock.patch.object(gcp_batch, "MAX_TASK_COUNT", 3):
            tasks = self.client.job_status(JOB).details["tasks"]
        self.assertEqual((tasks["succeeded"], tasks["other"]), (3, 3))
        self.assertTrue(tasks["truncated"])
        self.assertLess(len(requests), 5)

    def test_task_listing_errors(self) -> None:
        self.service.get_job.return_value = _job("RUNNING", task_count=2)
        self.service.list_tasks.side_effect = gexc.NotFound("gone")
        tasks = self.client.job_status(JOB).details["tasks"]
        self.assertEqual((tasks["count"], tasks["other"]), (2, 2))
        self.service.list_tasks.side_effect = gexc.ServiceUnavailable("down")
        with self.assertRaises(SandboxConnectionError):
            self.client.job_status(JOB)


class CancelTests(GcpBatchTestCase):

    def test_cancel_uses_cancel_job_and_keeps_the_record(self) -> None:
        self.service.get_job.return_value = _job("RUNNING")
        self.service.list_tasks.return_value = iter([_task()])
        status = self.client.cancel_job(JOB)
        # Batch cancels asynchronously: not terminal yet, and the same answer
        # job_status gives while the VMs stop.
        self.assertEqual(
            (status.state, status.reason), (JobState.RUNNING, "cancelling")
        )
        self.assertFalse(status.done)
        self.service.cancel_job.assert_called_once_with(name=JOB)
        self.service.delete_job.assert_not_called()

        self.service.get_job.return_value = _job("CANCELLATION_IN_PROGRESS")
        self.service.list_tasks.return_value = iter([_task()])
        during = self.client.job_status(JOB)
        self.assertEqual(
            (during.state, during.reason), (JobState.RUNNING, "cancelling")
        )
        # The record survives, so a later read says cancelled, not gone.
        self.service.get_job.return_value = _job("CANCELLED")
        self.service.list_tasks.return_value = iter([_task()])
        later = self.client.job_status(JOB)
        self.assertEqual(
            (later.state, later.reason), (JobState.CANCELLED, "cancelled")
        )
        self.assertTrue(later.done)

    def test_wait_after_cancel_ends_cancelled(self) -> None:
        self.service.get_job.side_effect = [
            _job("CANCELLATION_IN_PROGRESS"),
            _job("CANCELLED"),
        ]
        self.service.list_tasks.side_effect = lambda **_: iter([_task()])
        with mock.patch("bespokelabs.sandbox._jobs.time.sleep"):
            status = self.client.wait_job(JOB, timeout_secs=60, poll_secs=1)
        self.assertEqual(status.state, JobState.CANCELLED)

    def test_cancel_in_progress_is_not_requested_twice(self) -> None:
        for batch_state, reason in (
            ("CANCELLATION_IN_PROGRESS", "cancelling"),
            ("DELETION_IN_PROGRESS", "deleting"),
        ):
            with self.subTest(state=batch_state):
                self.service.get_job.return_value = _job(batch_state)
                self.service.list_tasks.return_value = iter([_task()])
                status = self.client.cancel_job(JOB)
                self.assertEqual(
                    (status.state, status.reason), (JobState.RUNNING, reason)
                )
        self.service.cancel_job.assert_not_called()
        self.service.delete_job.assert_not_called()

    def test_falls_back_to_delete_without_cancel_job_in_the_sdk(self) -> None:
        service = mock.MagicMock(spec=["get_job", "list_tasks", "delete_job"])
        service.get_job.return_value = _job("QUEUED")
        self.backend._client = service
        status = self.client.cancel_job(JOB)
        self.assertEqual(
            (status.state, status.reason), (JobState.RUNNING, "deleting")
        )
        service.delete_job.assert_called_once_with(name=JOB)
        # The same answer job_status gives while Batch deletes the job.
        service.get_job.return_value = _job("DELETION_IN_PROGRESS")
        service.list_tasks.return_value = iter([_task()])
        during = self.client.job_status(JOB)
        self.assertEqual(
            (during.state, during.reason), (status.state, status.reason)
        )

    def test_falls_back_to_delete_when_the_service_lacks_cancel(self) -> None:
        self.service.get_job.return_value = _job("QUEUED")
        self.service.cancel_job.side_effect = gexc.MethodNotImplemented("501")
        status = self.client.cancel_job(JOB)
        self.assertEqual(status.reason, "deleting")
        self.service.delete_job.assert_called_once_with(name=JOB)

    def test_cancel_errors_are_classified_not_swallowed(self) -> None:
        self.service.get_job.return_value = _job("QUEUED")
        self.service.cancel_job.side_effect = gexc.PermissionDenied("no")
        with self.assertRaises(SandboxAuthenticationError):
            self.client.cancel_job(JOB)
        self.service.delete_job.assert_not_called()

    def test_cancel_leaves_a_finished_job(self) -> None:
        self.service.get_job.return_value = _job("SUCCEEDED")
        self.service.list_tasks.return_value = iter([_task()])
        self.assertEqual(self.client.cancel_job(JOB).state, "succeeded")
        self.service.cancel_job.assert_not_called()
        self.service.delete_job.assert_not_called()

    def test_cancel_a_missing_job(self) -> None:
        self.service.get_job.side_effect = gexc.NotFound("gone")
        self.assertEqual(self.client.cancel_job(JOB).reason, "job_not_found")

    def test_job_vanishing_during_cancel(self) -> None:
        self.service.get_job.return_value = _job("QUEUED")
        self.service.cancel_job.side_effect = gexc.NotFound("gone")
        self.assertEqual(self.client.cancel_job(JOB).reason, "job_not_found")


class LogsTests(GcpBatchTestCase):

    def test_reads_cloud_logging_by_job_uid(self) -> None:
        self.service.get_job.return_value = _job("RUNNING", uid="u-42")
        logging_client = mock.MagicMock()
        logging_client.list_entries.return_value = [
            mock.Mock(payload="third"),
            mock.Mock(payload="second\n"),
        ]
        self.backend._logging_clients["proj"] = logging_client

        self.assertEqual(self.client.job_logs(JOB, tail=2), "second\nthird\n")
        kwargs = logging_client.list_entries.call_args.kwargs
        self.assertIn('labels.job_uid="u-42"', kwargs["filter_"])
        self.assertIn("projects/proj/logs/batch_task_logs", kwargs["filter_"])
        self.assertEqual(kwargs["max_results"], 2)
        self.assertEqual(kwargs["order_by"], "timestamp desc")

        logging_client.list_entries.return_value = [mock.Mock(payload="a")]
        self.assertEqual(self.client.job_logs(JOB), "a\n")
        self.assertIsNone(
            logging_client.list_entries.call_args.kwargs["order_by"]
        )


class InstallTests(unittest.TestCase):

    def test_missing_sdk_names_the_extra(self) -> None:
        with mock.patch.dict("sys.modules", {"google.cloud": None}):
            from bespokelabs.sandbox.backends.gcp_batch import (
                GcpBatchClient as Client,
            )

            with self.assertRaises(BackendNotInstalledError) as ctx:
                Client()
        self.assertIn("gcp-batch", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
