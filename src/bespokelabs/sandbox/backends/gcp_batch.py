"""Google Cloud Batch backend: native detached jobs, no interactive sandboxes.

A job is one Batch job whose tasks each run one runnable: a container, or
with ``runnable="script"`` a shell script on the VM itself.  It has one task
unless ``task_count`` asks for more.  The
job id is the job's resource name (``projects/<p>/locations/<region>/jobs/
<id>``), so status, logs and cancel need no configuration beyond credentials.

Configuration (``JobSpec.backend_options`` wins over the environment):

  project          GCP project; else ``GOOGLE_CLOUD_PROJECT``, else the
                   Application Default Credentials project.
  region           Batch location; else ``GOOGLE_CLOUD_REGION``, else
                   ``CLOUDSDK_COMPUTE_REGION``.
  regions          Candidate regions in preference order, used when
                   ``region`` is not given: the job goes to the first one
                   with ``region_quota`` free, else to the first candidate.
  region_quota     Compute regional quota the job needs free, by metric,
                   e.g. ``{"CPUS": 16, "SSD_TOTAL_GB": 600}``.  Requires
                   ``regions``.
  service_account  Email of the service account the VMs run as;
                   ``service_account_scopes`` (list) sets its OAuth scopes.
  network /        Network and subnetwork URLs or paths for the VMs;
  subnetwork       ``no_external_ip`` (bool) drops their external IP.
  machine_type     Overrides the machine type chosen for ``cpu`` /
                   ``memory_mb`` / ``gpu``.
  runnable         ``container`` (default) or ``script``.  A script job runs
                   ``JobSpec.command`` (a shell string) as a script on the
                   VM and takes no ``image``.
  secret_env_vars  Environment variables read from Secret Manager, as
                   ``{"NAME": "projects/<project>/secrets/<name>/versions/
                   <version>"}``.  The job carries the reference, never the
                   value; the VMs' service account needs access to it.
  boot_disk_image /   Boot disk image (e.g. ``batch-debian`` or
  boot_disk_type      ``projects/<p>/global/images/<image>``) and disk type
                   (e.g. ``pd-balanced``); ``JobSpec.disk_mb`` is its size.
  install_gpu_drivers  Whether Batch installs GPU drivers.  Defaults to
                   whether ``JobSpec.gpu`` is set; ``True`` without it needs
                   an accelerator-optimized ``machine_type``.
  block_project_ssh_keys  ``True`` keeps project-wide SSH keys off the VMs.
  instance_labels  Labels for the VMs only, on top of ``JobSpec.labels``
                   (which go on the job and the VMs).
  provisioning_model  ``STANDARD`` (default), ``SPOT``, ``PREEMPTIBLE`` or
                   ``FLEX_START``.
  allowed_locations   List such as ``["zones/us-central1-a"]``.
  max_retry_count  Task retries (default 0).
  task_count       Number of tasks (default 1, at most ``MAX_TASK_COUNT``).
                   Every task runs the same command; Batch gives each one
                   its index in ``BATCH_TASK_INDEX`` (0-based) and the total
                   in ``BATCH_TASK_COUNT``.
  parallelism      How many tasks may run at once (1..``task_count``, at
                   most ``MAX_PARALLELISM``); by default Batch runs as many
                   as it can.
  container_options   Extra ``docker run`` options for the container.
  gpu_driver_version  Passed to Batch with ``install_gpu_drivers``.
  job_id           Explicit Batch job id; else ``<job_id_prefix>-<random>``
                   (``job_id_prefix`` defaults to ``job``).

Credentials are Application Default Credentials (``gcloud auth
application-default login`` locally, the attached service account on GCP).
"""

from __future__ import annotations

import datetime
import math
import os
import re
import threading
import uuid
from typing import Any

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxAuthenticationError,
    SandboxConfigurationError,
    SandboxConnectionError,
    SandboxError,
    SandboxExecutionError,
    SandboxNotFoundError,
    SandboxTimeoutError,
)
from bespokelabs.sandbox.types import (
    JobSpec,
    JobState,
    JobStatus,
    SandboxConfig,
)

_INSTALL_HINT = (
    "Google Cloud Batch SDK not installed. Run: "
    "pip install 'bespokelabs-sandbox[gcp-batch]'"
)

_OPTION_KEYS = frozenset(
    {
        "project",
        "region",
        "regions",
        "region_quota",
        "service_account",
        "service_account_scopes",
        "network",
        "subnetwork",
        "no_external_ip",
        "machine_type",
        "provisioning_model",
        "allowed_locations",
        "max_retry_count",
        "task_count",
        "parallelism",
        "container_options",
        "gpu_driver_version",
        "runnable",
        "secret_env_vars",
        "boot_disk_image",
        "boot_disk_type",
        "install_gpu_drivers",
        "block_project_ssh_keys",
        "instance_labels",
        "job_id",
        "job_id_prefix",
    }
)

_JOB_NAME_RE = re.compile(
    r"projects/(?P<project>[^/]+)/locations/(?P<region>[^/]+)"
    r"/jobs/(?P<job>[^/]+)"
)
_JOB_ID_RE = re.compile(r"[a-z]([a-z0-9-]{0,61}[a-z0-9])?")
_LABEL_RE = re.compile(r"[^a-z0-9_-]")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SECRET_VERSION_RE = re.compile(
    r"projects/(?P<project>[A-Za-z0-9][A-Za-z0-9.:-]*)"
    r"/secrets/[A-Za-z0-9_-]{1,255}/versions/[A-Za-z0-9_-]{1,255}"
)
# Accelerator-optimized machine families: every such VM carries GPUs.
_GPU_MACHINE_TYPE_RE = re.compile(r"(a2|a3|a4|a4x|g2|g4)-[a-z0-9-]+")
_RUNNABLES = ("container", "script")

#: Most tasks one job may ask for.  Batch itself allows more; every status
#: read lists the job's tasks, so the bound keeps that read small.
MAX_TASK_COUNT = 10_000
#: Most tasks one job may run at once (Batch's own per-job limit).
MAX_PARALLELISM = 5_000
#: Most failed tasks described in ``JobStatus.details["tasks"]["failures"]``.
MAX_TASK_FAILURES = 20
_TASK_PAGE_SIZE = 1000
_TASK_INDEX_RE = re.compile(r".*/tasks/(?P<index>[0-9]{1,9})")

# Batch job states -> JobState.  STATE_UNSPECIFIED is a job Batch has not
# classified yet, not an unknown outcome, so it reads as pending.  A job
# being cancelled or deleted can still have VMs running, so neither is
# terminal yet (see _IN_PROGRESS_REASONS).
_STATES = {
    "STATE_UNSPECIFIED": JobState.PENDING,
    "QUEUED": JobState.PENDING,
    "SCHEDULED": JobState.PENDING,
    "RUNNING": JobState.RUNNING,
    "SUCCEEDED": JobState.SUCCEEDED,
    "FAILED": JobState.FAILED,
    "CANCELLATION_IN_PROGRESS": JobState.RUNNING,
    "CANCELLED": JobState.CANCELLED,
    "DELETION_IN_PROGRESS": JobState.RUNNING,
}
_IN_PROGRESS_REASONS = {
    "CANCELLATION_IN_PROGRESS": "cancelling",
    "DELETION_IN_PROGRESS": "deleting",
}

# Exit codes Batch reserves for its own failures
# (https://cloud.google.com/batch/docs/troubleshooting#reserved-exit-codes).
_RESERVED_EXIT_CODES = {
    50001: "vm_preempted",
    50002: "vm_lost",
    50003: "vm_rebooted",
    50004: "vm_unresponsive",
    50005: "timeout",
    50006: "vm_recreated",
}

# Why a job failed before any task exited, when Batch names a capacity cause.
# Quota wording is the documented status-event text ("Quota <NAME> exceeded.
# Limit: ..." and "... due to inadequate quotas"); the code tokens are the
# ones Batch and Compute Engine put in an event's text
# (``CODE_GCE_QUOTA_EXCEEDED``, ``CODE_GCE_ZONE_RESOURCE_POOL_EXHAUSTED``,
# ``ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS``).  The text itself is never
# returned.
_CAPACITY_REASONS = (
    (
        "quota_exceeded",
        re.compile(
            r"QUOTA_EXCEEDED|Quota '?[\w.-]{1,128}'? exceeded"
            r"|inadequate quotas"
        ),
    ),
    (
        "capacity_unavailable",
        re.compile(
            r"RESOURCE_POOL_EXHAUSTED|STOCKOUT"
            r"|does not have enough resources available"
        ),
    ),
)
# A job that reached RUNNING survived whatever capacity event came before it.
_REACHED_RUNNING = re.compile(r"\bto RUNNING\b")

# Friendly GPU names (Modal-style, as JobSpec.gpu uses elsewhere) -> Compute
# Engine accelerator types.  A raw ``nvidia-*`` type is accepted as is.
_GPU_ALIASES = {
    "T4": "nvidia-tesla-t4",
    "P4": "nvidia-tesla-p4",
    "P100": "nvidia-tesla-p100",
    "V100": "nvidia-tesla-v100",
    "L4": "nvidia-l4",
    "A100": "nvidia-tesla-a100",
    "A100-40GB": "nvidia-tesla-a100",
    "A100-80GB": "nvidia-a100-80gb",
    "H100": "nvidia-h100-80gb",
    "H200": "nvidia-h200-141gb",
    "B200": "nvidia-b200",
}

# Accelerator-optimized machine types: (machine type, GPUs, vCPUs, GB).
# The machine type fixes the GPU count, so the smallest one with the
# requested count and enough CPU and memory is chosen.
_GPU_MACHINES: dict[str, list[tuple[str, int, int, int]]] = {
    "nvidia-l4": [
        ("g2-standard-4", 1, 4, 16),
        ("g2-standard-8", 1, 8, 32),
        ("g2-standard-12", 1, 12, 48),
        ("g2-standard-16", 1, 16, 64),
        ("g2-standard-32", 1, 32, 128),
        ("g2-standard-24", 2, 24, 96),
        ("g2-standard-48", 4, 48, 192),
        ("g2-standard-96", 8, 96, 384),
    ],
    "nvidia-tesla-a100": [
        ("a2-highgpu-1g", 1, 12, 85),
        ("a2-highgpu-2g", 2, 24, 170),
        ("a2-highgpu-4g", 4, 48, 340),
        ("a2-highgpu-8g", 8, 96, 680),
        ("a2-megagpu-16g", 16, 96, 1360),
    ],
    "nvidia-a100-80gb": [
        ("a2-ultragpu-1g", 1, 12, 170),
        ("a2-ultragpu-2g", 2, 24, 340),
        ("a2-ultragpu-4g", 4, 48, 680),
        ("a2-ultragpu-8g", 8, 96, 1360),
    ],
    "nvidia-h100-80gb": [
        ("a3-highgpu-1g", 1, 26, 234),
        ("a3-highgpu-2g", 2, 52, 468),
        ("a3-highgpu-4g", 4, 104, 936),
        ("a3-highgpu-8g", 8, 208, 1872),
    ],
    "nvidia-h200-141gb": [("a3-ultragpu-8g", 8, 224, 2952)],
    "nvidia-b200": [("a4-highgpu-8g", 8, 224, 3968)],
}

# Older GPUs attach to N1 VMs, sized here from cpu / memory_mb.
_N1_GPUS = frozenset(
    {
        "nvidia-tesla-t4",
        "nvidia-tesla-p4",
        "nvidia-tesla-p100",
        "nvidia-tesla-v100",
    }
)
_N1_SHAPES = [
    (family, vcpus, gb_per_vcpu)
    for family, gb_per_vcpu in (("n1-standard", 3.75), ("n1-highmem", 6.5))
    for vcpus in (1, 2, 4, 8, 16, 32, 64, 96)
    if not (family == "n1-highmem" and vcpus == 1)
]


def _n1_price(vcpus: int, gb_per_vcpu: float) -> float:
    """Relative on-demand price of an N1 shape (us-central1 vCPU/GB rates)."""
    return vcpus * (0.031611 + gb_per_vcpu * 0.004237)


# The VM keeps part of its memory for the OS and the Batch agent.
_USABLE_MEMORY = 0.9


def parse_gpu(gpu: str) -> tuple[str, int]:
    """Parse ``"<type>[:<count>]"`` into a Compute Engine accelerator type."""
    name, _, count_text = gpu.strip().partition(":")
    try:
        count = int(count_text) if count_text else 1
    except ValueError:
        count = 0
    if count < 1:
        raise SandboxConfigurationError(
            f"Invalid GPU request {gpu!r}: the count must be a positive integer",
            backend="gcp_batch",
        )
    key = name.rstrip("!").upper()
    if key in _GPU_ALIASES:
        return _GPU_ALIASES[key], count
    if name.lower().startswith("nvidia-"):
        return name.lower(), count
    raise SandboxConfigurationError(
        f"Unknown GPU type {name!r}; use one of "
        f"{', '.join(sorted(_GPU_ALIASES))} or a Compute Engine accelerator "
        "type such as 'nvidia-l4'",
        backend="gcp_batch",
    )


def machine_type_for(
    accelerator: str, count: int, cpu: float, memory_mb: int
) -> str:
    """Pick the smallest machine type that carries the GPUs and fits the task."""

    def fits(vcpus: int, gb: float) -> bool:
        return vcpus >= cpu and gb * 1024 * _USABLE_MEMORY >= memory_mb

    if accelerator in _N1_GPUS:
        for family, vcpus, gb_per_vcpu in sorted(
            _N1_SHAPES, key=lambda shape: _n1_price(shape[1], shape[2])
        ):
            if fits(vcpus, vcpus * gb_per_vcpu):
                return f"{family}-{vcpus}"
    else:
        machines = _GPU_MACHINES.get(accelerator)
        if machines is None:
            raise SandboxConfigurationError(
                f"No machine type is known for {accelerator!r}; pass "
                "backend_options['machine_type']",
                backend="gcp_batch",
            )
        candidates = [m for m in machines if m[1] == count]
        if not candidates:
            counts = sorted({m[1] for m in machines})
            raise SandboxConfigurationError(
                f"{accelerator} comes in counts of {counts}, not {count}",
                backend="gcp_batch",
            )
        for machine, _, vcpus, gb in candidates:
            if fits(vcpus, gb):
                return machine
    raise SandboxConfigurationError(
        f"No machine type with {count}x {accelerator} fits cpu={cpu}, "
        f"memory_mb={memory_mb}; pass backend_options['machine_type']",
        backend="gcp_batch",
    )


def _label(value: str) -> str:
    """Coerce a string into a GCP label key/value (lowercase, <= 63 chars)."""
    return _LABEL_RE.sub("_", value.lower())[:63]


def _labels(labels: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    sources: dict[str, str] = {}
    for key, value in labels.items():
        clean_key = _label(key)
        if not clean_key or not clean_key[0].isalpha():
            clean_key = f"l_{clean_key}"[:63]
        if clean_key in sources:
            raise SandboxConfigurationError(
                f"Labels {sources[clean_key]!r} and {key!r} both become the "
                f"GCP label key {clean_key!r}",
                backend="gcp_batch",
                op="submit_job",
            )
        sources[clean_key] = key
        out[clean_key] = _label(value)
    return out


def _invalid(message: str) -> SandboxConfigurationError:
    return SandboxConfigurationError(
        message, backend="gcp_batch", op="submit_job"
    )


def secret_env_vars(spec: JobSpec) -> dict[str, str]:
    """Return the validated ``secret_env_vars`` option (name -> version)."""
    secrets = spec.backend_options.get("secret_env_vars")
    if secrets is None:
        return {}
    if not isinstance(secrets, dict):
        raise _invalid(
            "backend_options['secret_env_vars'] must map variable names to "
            "Secret Manager secret versions"
        )
    for name, resource in secrets.items():
        if not isinstance(name, str) or not _ENV_NAME_RE.fullmatch(name):
            raise _invalid(
                f"secret_env_vars name {name!r} is not a valid variable name"
            )
        if name in spec.env_vars:
            raise _invalid(f"{name!r} is in both env_vars and secret_env_vars")
        # The reference is not echoed: a malformed one may be a pasted value.
        if not isinstance(resource, str) or not _SECRET_VERSION_RE.fullmatch(
            resource
        ):
            raise _invalid(
                f"secret_env_vars[{name!r}] must look like "
                "'projects/<project>/secrets/<name>/versions/<version>'"
            )
    return dict(secrets)


def secret_project(resource: str) -> str:
    """Return the project of a validated secret version resource name."""
    match = _SECRET_VERSION_RE.fullmatch(resource)
    if match is None:
        raise _invalid("Not a Secret Manager secret version resource name")
    return match["project"]


def instance_labels(spec: JobSpec) -> dict[str, str]:
    """Return the coerced VM-only labels, which may not repeat a job label."""
    extra = spec.backend_options.get("instance_labels")
    if extra is None:
        return {}
    if not isinstance(extra, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in extra.items()
    ):
        raise _invalid(
            "backend_options['instance_labels'] must map label names to "
            "strings"
        )
    labels = _labels(extra)
    repeated = sorted(set(labels) & set(_labels(spec.labels)))
    if repeated:
        raise _invalid(
            f"instance_labels repeats the job label {repeated[0]!r}; "
            "JobSpec.labels already go on the VMs"
        )
    return labels


def _flag(options: dict, key: str) -> bool | None:
    value = options.get(key)
    if value is not None and not isinstance(value, bool):
        raise _invalid(f"backend_options[{key!r}] must be a boolean")
    return value


def _count(options: dict, key: str, maximum: int) -> int | None:
    value = options.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _invalid(f"backend_options[{key!r}] must be a positive integer")
    if value > maximum:
        raise _invalid(f"backend_options[{key!r}] can be at most {maximum}")
    return value


def task_counts(spec: JobSpec) -> tuple[int, int | None]:
    """Return the validated ``(task_count, parallelism)`` options.

    ``parallelism`` is ``None`` when it is left to Batch.
    """
    options = spec.backend_options
    task_count = _count(options, "task_count", MAX_TASK_COUNT) or 1
    parallelism = _count(options, "parallelism", MAX_PARALLELISM)
    if parallelism is not None and parallelism > task_count:
        raise _invalid(
            "backend_options['parallelism'] cannot exceed task_count "
            f"({task_count})"
        )
    return task_count, parallelism


def _event_time(event: Any) -> float | None:
    stamp = getattr(event, "event_time", None)
    try:
        return stamp.timestamp() if stamp is not None else None
    except (AttributeError, OverflowError, ValueError):
        return None


def capacity_reason(events: Any) -> str | None:
    """Classify status events as a quota or a capacity failure, if either.

    Returns ``"quota_exceeded"``, ``"capacity_unavailable"`` or ``None``.
    Events are read in time order when every one carries a time (the API does
    not promise an order).  Anything up to the last move to RUNNING is
    ignored: a job that waited on capacity, then ran and failed for another
    cause, is not a capacity failure.  Of what remains the newest match wins.
    """
    ordered = [
        event
        for event in (events or [])
        if isinstance(getattr(event, "description", None), str)
    ]
    times = [_event_time(event) for event in ordered]
    if ordered and all(stamp is not None for stamp in times):
        ordered = [
            event
            for _, event in sorted(
                zip(times, ordered, strict=True), key=lambda pair: pair[0]
            )
        ]
    for index in range(len(ordered) - 1, -1, -1):
        if _REACHED_RUNNING.search(ordered[index].description):
            ordered = ordered[index + 1 :]
            break
    for event in reversed(ordered):
        for reason, pattern in _CAPACITY_REASONS:
            if pattern.search(event.description):
                return reason
    return None


def validate_options(spec: JobSpec) -> None:
    """Check the options that can be judged without the SDK or the network.

    Covers ``runnable``, ``secret_env_vars``, ``boot_disk_image``,
    ``boot_disk_type``, ``install_gpu_drivers``, ``block_project_ssh_keys``,
    ``instance_labels``, ``task_count`` and ``parallelism``; ``build_job``
    checks the rest.
    """
    options = spec.backend_options
    task_counts(spec)
    runnable = options.get("runnable", "container")
    if runnable not in _RUNNABLES:
        raise _invalid(
            "backend_options['runnable'] must be 'container' or 'script'"
        )
    if runnable == "script":
        if spec.image:
            raise _invalid(
                "A script job runs on the VM and takes no JobSpec.image; "
                "give either an image or runnable='script'"
            )
        if not isinstance(spec.command, str):
            raise _invalid(
                "A script job needs JobSpec.command as the script text "
                "(a string), not an argv list"
            )
        if spec.workdir or options.get("container_options"):
            raise _invalid(
                "workdir and container_options apply to a container, not "
                "to a script job"
            )
        if not spec.allow_internet:
            raise _invalid(
                "allow_internet=False blocks a container's network; a "
                "script job is restricted through the VM's network instead"
            )
    secret_env_vars(spec)
    for key in ("boot_disk_image", "boot_disk_type"):
        value = options.get(key)
        if value is not None and (
            not isinstance(value, str)
            or not value
            or len(value) > 1024
            or re.search(r"\s", value)
        ):
            raise _invalid(
                f"backend_options[{key!r}] must be a non-empty string "
                "without whitespace"
            )
    _flag(options, "block_project_ssh_keys")
    if _flag(options, "install_gpu_drivers") and not spec.gpu:
        machine_type = options.get("machine_type")
        if not isinstance(
            machine_type, str
        ) or not _GPU_MACHINE_TYPE_RE.fullmatch(machine_type):
            raise _invalid(
                "install_gpu_drivers needs JobSpec.gpu or an "
                "accelerator-optimized machine_type (A2, A3, A4, G2, G4)"
            )
    instance_labels(spec)


def _classify(exc: Exception, op: str) -> SandboxError:
    """Translate google-api-core errors into the public error taxonomy."""
    if isinstance(exc, SandboxError):
        return exc
    name = type(exc).__name__
    common = {"backend": "gcp_batch", "op": op}
    if name in {"PermissionDenied", "Unauthenticated", "Forbidden"}:
        return SandboxAuthenticationError(
            f"Google Cloud Batch {op} was not authorized: {exc}", **common
        )
    if name in {"InvalidArgument", "BadRequest", "FailedPrecondition"}:
        return SandboxConfigurationError(
            f"Google Cloud Batch rejected {op}: {exc}", **common
        )
    if name in {"DeadlineExceeded", "GatewayTimeout"} or isinstance(
        exc, TimeoutError
    ):
        return SandboxTimeoutError(
            f"Google Cloud Batch {op} timed out: {exc}", **common
        )
    if name in {
        "ServiceUnavailable",
        "InternalServerError",
        "BadGateway",
        "TooManyRequests",
        "ResourceExhausted",
        "Aborted",
        "RetryError",
    } or isinstance(exc, ConnectionError):
        return SandboxConnectionError(
            f"Google Cloud Batch {op} failed: {exc}", **common
        )
    return SandboxExecutionError(
        f"Google Cloud Batch {op} failed: {exc}", **common
    )


def _is_not_found(exc: Exception) -> bool:
    return type(exc).__name__ == "NotFound" or getattr(exc, "code", None) == 404


def _is_unimplemented(exc: Exception) -> bool:
    return (
        type(exc).__name__ in {"MethodNotImplemented", "NotImplemented"}
        or getattr(exc, "code", None) == 501
    )


def _as_datetime(value: Any) -> datetime.datetime | None:
    """Normalize a proto timestamp (DatetimeWithNanoseconds) to aware UTC."""
    if not isinstance(value, datetime.datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    if value.timestamp() <= 0:
        return None
    return value


def _enum_name(value: Any) -> str:
    return getattr(value, "name", None) or str(value)


class GcpBatchClient:
    """Job-only backend on Google Cloud Batch.

    ``create()`` / ``resume()`` raise FeatureNotSupportedError: Batch runs
    jobs, not interactive sandboxes.  Use ``SandboxClient("gcp_batch")``'s
    ``submit_job`` / ``job_status`` / ``wait_job`` / ``job_logs`` /
    ``cancel_job`` (Batch CancelJob; delete only as a fallback).
    """

    #: Interactive sandboxes are not available on this backend.
    supports_sessions = False

    def __init__(self) -> None:
        try:
            from google.cloud import batch_v1  # type: ignore[import-untyped]
        except ImportError as exc:
            raise BackendNotInstalledError(_INSTALL_HINT) from exc
        self._batch = batch_v1
        self._client: Any = None
        self._logging_clients: dict[str, Any] = {}
        self._lock = threading.Lock()

    # -- Interactive API (unsupported) -----------------------------------

    def create(self, config: SandboxConfig) -> Any:
        del config
        raise FeatureNotSupportedError(
            "gcp_batch runs detached jobs only; use "
            "SandboxClient('gcp_batch').submit_job(JobSpec(...))",
            backend="gcp_batch",
            op="create",
        )

    def resume(self, data: dict) -> Any:
        del data
        raise FeatureNotSupportedError(
            "gcp_batch runs detached jobs only; there is no sandbox to resume",
            backend="gcp_batch",
            op="resume",
        )

    # -- Clients ---------------------------------------------------------

    def _service(self) -> Any:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    try:
                        self._client = self._batch.BatchServiceClient()
                    except Exception as exc:
                        raise _classify(exc, "connect") from exc
        return self._client

    def _logging(self, project: str) -> Any:
        client = self._logging_clients.get(project)
        if client is None:
            try:
                from google.cloud import (
                    logging_v2,  # type: ignore[import-untyped]
                )
            except ImportError as exc:
                raise BackendNotInstalledError(_INSTALL_HINT) from exc
            with self._lock:
                client = self._logging_clients.get(project)
                if client is None:
                    client = logging_v2.Client(project=project)
                    self._logging_clients[project] = client
        return client

    # -- Submit ----------------------------------------------------------

    def build_job(self, spec: JobSpec) -> Any:
        """Translate a JobSpec into a ``batch_v1.Job`` (no network I/O)."""
        batch = self._batch
        options = spec.backend_options
        unknown = sorted(set(options) - _OPTION_KEYS)
        if unknown:
            raise SandboxConfigurationError(
                f"Unknown gcp_batch backend_options: {', '.join(unknown)}",
                backend="gcp_batch",
                op="submit_job",
            )
        validate_options(spec)
        script = options.get("runnable") == "script"
        if not spec.image and not script:
            raise SandboxConfigurationError(
                "gcp_batch jobs need JobSpec.image (a container image URI, "
                "e.g. in Artifact Registry), or backend_options['runnable']"
                "='script' to run the command as a script on the VM",
                backend="gcp_batch",
                op="submit_job",
            )

        if script:
            runnable = batch.Runnable(
                script=batch.Runnable.Script(text=spec.command)
            )
        else:
            runnable = batch.Runnable(container=self._container(spec))

        task = batch.TaskSpec(
            runnables=[runnable],
            compute_resource=batch.ComputeResource(
                cpu_milli=int(math.ceil(spec.cpu * 1000)),
                memory_mib=int(spec.memory_mb),
            ),
            max_run_duration=datetime.timedelta(seconds=int(spec.timeout_secs)),
            max_retry_count=int(options.get("max_retry_count", 0)),
        )
        secrets = secret_env_vars(spec)
        if spec.env_vars or secrets:
            task.environment = batch.Environment(variables=dict(spec.env_vars))
            if secrets:
                task.environment.secret_variables = secrets

        policy = batch.AllocationPolicy.InstancePolicy()
        install_drivers = False
        machine_type = options.get("machine_type")
        if spec.gpu:
            accelerator, count = parse_gpu(spec.gpu)
            # An accelerator-optimized machine type (A2, A3, A4, G2) carries
            # its GPUs, and Batch's own examples size it by machine type
            # alone; only an N1 VM has GPUs attached as accelerators.  The
            # driver version is an accelerator's field, so naming one lists
            # the GPUs either way.
            if accelerator in _N1_GPUS or options.get("gpu_driver_version"):
                accel = batch.AllocationPolicy.Accelerator(
                    type_=accelerator, count=count
                )
                if options.get("gpu_driver_version"):
                    accel.driver_version = str(options["gpu_driver_version"])
                policy.accelerators = [accel]
            install_drivers = True
            machine_type = machine_type or machine_type_for(
                accelerator, count, spec.cpu, spec.memory_mb
            )
        if options.get("install_gpu_drivers") is not None:
            install_drivers = options["install_gpu_drivers"]
        if machine_type:
            policy.machine_type = str(machine_type)
        disk: dict[str, Any] = {}
        if spec.disk_mb:
            disk["size_gb"] = int(math.ceil(spec.disk_mb / 1024))
        if options.get("boot_disk_image"):
            disk["image"] = options["boot_disk_image"]
        if options.get("boot_disk_type"):
            disk["type_"] = options["boot_disk_type"]
        if disk:
            policy.boot_disk = batch.AllocationPolicy.Disk(**disk)
        if options.get("provisioning_model"):
            model = str(options["provisioning_model"]).upper()
            try:
                policy.provisioning_model = (
                    batch.AllocationPolicy.ProvisioningModel[model]
                )
            except KeyError as exc:
                raise SandboxConfigurationError(
                    f"Unknown provisioning_model {model!r}",
                    backend="gcp_batch",
                    op="submit_job",
                ) from exc

        instances = batch.AllocationPolicy.InstancePolicyOrTemplate(
            policy=policy, install_gpu_drivers=install_drivers
        )
        if options.get("block_project_ssh_keys"):
            if "block_project_ssh_keys" not in type(instances).meta.fields:
                raise _invalid(
                    "block_project_ssh_keys needs a newer google-cloud-batch"
                )
            instances.block_project_ssh_keys = True
        allocation = batch.AllocationPolicy(instances=[instances])
        labels = _labels(spec.labels)
        vm_labels = {**labels, **instance_labels(spec)}
        if vm_labels:
            # On the VMs too, so their cost is attributed like the job's.
            allocation.labels = vm_labels
        if options.get("service_account"):
            allocation.service_account = batch.ServiceAccount(
                email=str(options["service_account"]),
                scopes=list(options.get("service_account_scopes") or []),
            )
        elif options.get("service_account_scopes"):
            raise SandboxConfigurationError(
                "service_account_scopes needs service_account",
                backend="gcp_batch",
                op="submit_job",
            )
        if options.get("allowed_locations"):
            allocation.location = batch.AllocationPolicy.LocationPolicy(
                allowed_locations=list(options["allowed_locations"])
            )
        if (
            options.get("network")
            or options.get("subnetwork")
            or options.get("no_external_ip")
        ):
            interface = batch.AllocationPolicy.NetworkInterface(
                no_external_ip_address=bool(options.get("no_external_ip"))
            )
            if options.get("network"):
                interface.network = str(options["network"])
            if options.get("subnetwork"):
                interface.subnetwork = str(options["subnetwork"])
            allocation.network = batch.AllocationPolicy.NetworkPolicy(
                network_interfaces=[interface]
            )

        task_count, parallelism = task_counts(spec)
        group = batch.TaskGroup(task_spec=task, task_count=task_count)
        if parallelism is not None:
            group.parallelism = parallelism
        return batch.Job(
            task_groups=[group],
            allocation_policy=allocation,
            labels=labels,
            logs_policy=batch.LogsPolicy(
                destination=batch.LogsPolicy.Destination.CLOUD_LOGGING
            ),
        )

    def _container(self, spec: JobSpec) -> Any:
        batch = self._batch
        options = spec.backend_options
        if isinstance(spec.command, list):
            entrypoint, commands = spec.command[0], spec.command[1:]
        else:
            entrypoint, commands = "/bin/sh", ["-c", spec.command]
        container = batch.Runnable.Container(
            image_uri=spec.image,
            entrypoint=entrypoint,
            commands=commands,
            block_external_network=not spec.allow_internet,
        )
        container_options = []
        if spec.workdir:
            if re.search(r"\s", spec.workdir):
                raise SandboxConfigurationError(
                    "gcp_batch workdir cannot contain whitespace",
                    backend="gcp_batch",
                    op="submit_job",
                )
            container_options.append(f"--workdir={spec.workdir}")
        if options.get("container_options"):
            container_options.append(str(options["container_options"]))
        if container_options:
            container.options = " ".join(container_options)
        return container

    def submit_job(self, spec: JobSpec) -> str:
        options = spec.backend_options
        job = self.build_job(spec)
        project = self._project(options)
        region = self._region(options, project)
        job_id = options.get("job_id") or (
            f"{options.get('job_id_prefix', 'job')}-{uuid.uuid4().hex[:16]}"
        )
        if not _JOB_ID_RE.fullmatch(str(job_id)):
            raise SandboxConfigurationError(
                f"Invalid Batch job id {job_id!r}: use lowercase letters, "
                "digits and hyphens, starting with a letter (max 63 chars)",
                backend="gcp_batch",
                op="submit_job",
            )
        parent = f"projects/{project}/locations/{region}"
        request = self._batch.CreateJobRequest(
            parent=parent,
            job_id=str(job_id),
            job=job,
            # Makes the SDK's own retries of this create idempotent.
            request_id=str(uuid.uuid4()),
        )
        try:
            created = self._service().create_job(request=request)
        except Exception as exc:
            raise _classify(exc, "submit_job") from exc
        return getattr(created, "name", None) or f"{parent}/jobs/{job_id}"

    # -- Reattach --------------------------------------------------------

    @staticmethod
    def _parse(job_id: str) -> dict[str, str]:
        match = _JOB_NAME_RE.fullmatch(str(job_id))
        if match is None:
            raise SandboxConfigurationError(
                f"Not a Cloud Batch job name: {job_id!r}",
                backend="gcp_batch",
                op="decode_job_id",
            )
        return match.groupdict()

    def _get(self, job_id: str, op: str) -> Any | None:
        self._parse(job_id)
        try:
            return self._service().get_job(name=job_id)
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise _classify(exc, op) from exc

    @staticmethod
    def _group(job: Any) -> tuple[str, int]:
        """Return the job's task group name and how many tasks it has."""
        groups = list(getattr(job, "task_groups", None) or [])
        name = getattr(groups[0], "name", "") if groups else ""
        count = getattr(groups[0], "task_count", 0) if groups else 0
        return name or f"{job.name}/taskGroups/group0", max(int(count or 0), 1)

    def _task(self, job: Any) -> Any | None:
        """Return the job's single task, for its exit code and timings."""
        parent, _ = self._group(job)
        try:
            pager = self._service().list_tasks(
                request=self._batch.ListTasksRequest(parent=parent, page_size=1)
            )
            for task in pager:
                return task
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise _classify(exc, "job_status") from exc
        return None

    def _tasks(self, job: Any) -> tuple[list[Any], bool]:
        """List a multi-task job's tasks, following every page.

        Returns the tasks and whether the listing stopped at
        ``MAX_TASK_COUNT`` with more to come.
        """
        parent, _ = self._group(job)
        tasks: list[Any] = []
        try:
            # Iterating the pager requests each next page as it is needed.
            pager = self._service().list_tasks(
                request=self._batch.ListTasksRequest(
                    parent=parent, page_size=_TASK_PAGE_SIZE
                )
            )
            for task in pager:
                if len(tasks) >= MAX_TASK_COUNT:
                    return tasks, True
                tasks.append(task)
        except Exception as exc:
            if _is_not_found(exc):
                return [], False
            raise _classify(exc, "job_status") from exc
        return tasks, False

    @staticmethod
    def _task_events(task: Any) -> dict[str, Any]:
        """Read a task's events: its last text, exit code and timings."""
        message = None
        exit_code = None
        started_at = finished_at = None
        last_state = ""
        for event in list(
            getattr(getattr(task, "status", None), "status_events", None) or []
        ):
            task_state = _enum_name(getattr(event, "task_state", ""))
            when = _as_datetime(getattr(event, "event_time", None))
            message = getattr(event, "description", None) or message
            last_state = task_state or last_state
            if task_state == "RUNNING" and started_at is None:
                started_at = when
            if task_state in {"SUCCEEDED", "FAILED"}:
                finished_at = when
            # Proto3 reads an unset message as zeros; only a present
            # task_execution carries a real exit code.
            if task_state in {"SUCCEEDED", "FAILED"} and (
                "task_execution" in event
            ):
                exit_code = int(event.task_execution.exit_code)
        return {
            "message": message,
            "exit_code": exit_code,
            "started_at": started_at,
            "finished_at": finished_at,
            "last_state": last_state,
        }

    def _summarize(
        self, job: Any, count: int, job_events: list[Any]
    ) -> dict[str, Any]:
        """Aggregate a multi-task job's tasks into counts and failures."""
        tasks, cut = self._tasks(job)
        succeeded = 0
        failures: list[dict[str, Any]] = []
        started: list[datetime.datetime] = []
        finished: list[datetime.datetime] = []
        for position, task in enumerate(tasks):
            events = self._task_events(task)
            if events["started_at"]:
                started.append(events["started_at"])
            if events["finished_at"]:
                finished.append(events["finished_at"])
            status = getattr(task, "status", None)
            task_state = _enum_name(getattr(status, "state", ""))
            if task_state in {"", "STATE_UNSPECIFIED"}:
                task_state = events["last_state"]
            if task_state == "SUCCEEDED":
                succeeded += 1
            if task_state != "FAILED":
                continue
            match = _TASK_INDEX_RE.fullmatch(str(getattr(task, "name", "")))
            exit_code = events["exit_code"]
            if exit_code is None:
                reason = (
                    capacity_reason(getattr(status, "status_events", None))
                    or capacity_reason(job_events)
                    or "batch_failed"
                )
            else:
                reason = _RESERVED_EXIT_CODES.get(exit_code, "exit_code")
            failures.append(
                {
                    "index": int(match["index"]) if match else position,
                    "exit_code": exit_code,
                    "reason": reason,
                    "message": events["message"],
                }
            )
        failures.sort(key=lambda failure: failure["index"])
        total = max(count, len(tasks))
        first = failures[0] if failures else None
        return {
            "first_failure": first,
            "started_at": min(started) if started else None,
            "finished_at": max(finished) if finished else None,
            "tasks": {
                "count": total,
                "succeeded": succeeded,
                "failed": len(failures),
                # Pending, running, never run, or not listed.
                "other": total - succeeded - len(failures),
                "failures": [
                    {
                        key: failure[key]
                        for key in ("index", "exit_code", "reason")
                    }
                    for failure in failures[:MAX_TASK_FAILURES]
                ],
                "truncated": cut or len(failures) > MAX_TASK_FAILURES,
            },
        }

    def _gone(self, job_id: str) -> JobStatus:
        return JobStatus(
            job_id=job_id,
            state=JobState.UNKNOWN,
            reason="job_not_found",
            details={"backend": "gcp_batch"},
        )

    def _status(self, job_id: str, job: Any) -> JobStatus:
        batch_state = _enum_name(job.status.state)
        state = _STATES.get(batch_state, JobState.PENDING)
        details: dict[str, Any] = {
            "backend": "gcp_batch",
            "batch_state": batch_state,
            "uid": getattr(job, "uid", None),
            # The job's own labels, as Batch stored them, so a caller can check
            # a job is one it started before it cancels it.
            "labels": dict(getattr(job, "labels", None) or {}),
        }
        events = list(getattr(job.status, "status_events", None) or [])
        if events:
            details["last_event"] = getattr(events[-1], "description", None)
        task_message = None
        exit_code = None
        started_at = finished_at = None
        task_reason = None
        _, task_count = self._group(job)
        if state is JobState.PENDING:
            pass
        elif task_count > 1:
            summary = self._summarize(job, task_count, events)
            details["tasks"] = summary["tasks"]
            started_at = summary["started_at"]
            if state in {
                JobState.SUCCEEDED,
                JobState.FAILED,
                JobState.CANCELLED,
            }:
                finished_at = summary["finished_at"]
            # The job's exit code and reason are its lowest-index failed
            # task's; the other failures are in details["tasks"].
            first = summary["first_failure"]
            if state is JobState.FAILED and first is not None:
                exit_code = first["exit_code"]
                task_message = first["message"]
                task_reason = first["reason"]
        else:
            task = self._task(job)
            task_events = self._task_events(task)
            task_message = task_events["message"]
            exit_code = task_events["exit_code"]
            started_at = task_events["started_at"]
            finished_at = task_events["finished_at"]
            if exit_code is None:
                task_reason = capacity_reason(
                    getattr(
                        getattr(task, "status", None), "status_events", None
                    )
                )
        # The provider's own words for what happened last.  A task that ran
        # explains its own exit; a job that failed with no task exit (image
        # pull, quota, permissions) is explained by the job's last event.
        if state is JobState.FAILED and exit_code is None:
            message = details.get("last_event") or task_message
        else:
            message = task_message or details.get("last_event")
        if message:
            details["message"] = message
        reason = _IN_PROGRESS_REASONS.get(batch_state)
        if state is JobState.FAILED:
            if exit_code is not None:
                reason = _RESERVED_EXIT_CODES.get(exit_code, "exit_code")
            else:
                # No task exited: say so, or name the capacity cause when
                # the job's or the task's newest events carry one.
                reason = (
                    capacity_reason(events) or task_reason or "batch_failed"
                )
        elif state is JobState.CANCELLED:
            reason = "cancelled"
        if state is JobState.SUCCEEDED and exit_code is None:
            exit_code = 0
        return JobStatus(
            job_id=job_id,
            state=state,
            exit_code=exit_code,
            started_at=started_at,
            finished_at=finished_at,
            reason=reason,
            details=details,
        )

    def job_status(self, job_id: str) -> JobStatus:
        job = self._get(job_id, "job_status")
        if job is None:
            return self._gone(job_id)
        return self._status(job_id, job)

    def job_logs(self, job_id: str, tail: int | None = None) -> str:
        """Read the task's stdout/stderr from Cloud Logging.

        Batch writes them to the ``batch_task_logs`` log, labelled with the
        job's uid.  Entries can lag the task by a few seconds.
        """
        parts = self._parse(job_id)
        job = self._get(job_id, "job_logs")
        if job is None:
            raise SandboxNotFoundError(
                f"Cloud Batch job {job_id} no longer exists",
                backend="gcp_batch",
                op="job_logs",
            )
        project = parts["project"]
        log_filter = (
            f'logName="projects/{project}/logs/batch_task_logs" '
            f'AND labels.job_uid="{job.uid}"'
        )
        if tail == 0:
            return ""
        try:
            from google.cloud.logging_v2 import (
                DESCENDING,  # type: ignore[import-untyped]
            )

            entries = self._logging(project).list_entries(
                resource_names=[f"projects/{project}"],
                filter_=log_filter,
                order_by=DESCENDING if tail else None,
                max_results=tail,
            )
            lines = [self._entry_text(entry) for entry in entries]
        except SandboxError:
            raise
        except Exception as exc:
            raise _classify(exc, "job_logs") from exc
        if tail:
            lines.reverse()
        return "".join(
            line if line.endswith("\n") else line + "\n" for line in lines
        )

    @staticmethod
    def _entry_text(entry: Any) -> str:
        payload = getattr(entry, "payload", None)
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict):
            return str(payload.get("message", payload))
        return str(payload or "")

    def cancel_job(self, job_id: str) -> JobStatus:
        """Cancel a pending or running job; a finished job is left as is.

        Uses Batch's CancelJob, which stops the job's VMs and keeps its
        record.  Batch cancels asynchronously, so the returned status is
        ``running`` with ``reason="cancelling"`` -- consistent with
        ``job_status`` -- until Batch settles the job as CANCELLED; call
        ``wait_job`` to see that.  When the installed SDK or the service
        lacks CancelJob, the job is deleted instead (also stopping its VMs)
        and the reason is ``"deleting"`` -- again what ``job_status`` says
        while Batch deletes it; a deleted job then reads as ``unknown`` /
        ``job_not_found``.
        """
        job = self._get(job_id, "cancel_job")
        if job is None:
            return self._gone(job_id)
        status = self._status(job_id, job)
        if status.done or status.reason in _IN_PROGRESS_REASONS.values():
            return status
        how = self._cancel_or_delete(job_id)
        if how is None:
            return self._gone(job_id)
        return JobStatus(
            job_id=job_id,
            state=JobState.RUNNING,
            started_at=status.started_at,
            # The reason job_status gives for the state the request starts.
            reason=_IN_PROGRESS_REASONS[
                "CANCELLATION_IN_PROGRESS"
                if how == "cancel"
                else "DELETION_IN_PROGRESS"
            ],
            details=status.details,
        )

    def _cancel_or_delete(self, job_id: str) -> str | None:
        """Stop the job: ``"cancel"``, ``"delete"``, or ``None`` if it is gone."""
        service = self._service()
        cancel = getattr(service, "cancel_job", None)
        if callable(cancel) and hasattr(self._batch, "CancelJobRequest"):
            try:
                cancel(name=job_id)
                return "cancel"
            except Exception as exc:
                if _is_not_found(exc):
                    return None
                if not _is_unimplemented(exc):
                    raise _classify(exc, "cancel_job") from exc
        try:
            service.delete_job(name=job_id)
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise _classify(exc, "cancel_job") from exc
        return "delete"

    # -- Configuration ---------------------------------------------------

    @staticmethod
    def _project(options: dict) -> str:
        project = options.get("project") or os.environ.get(
            "GOOGLE_CLOUD_PROJECT"
        )
        if not project:
            try:
                import google.auth  # type: ignore[import-untyped]

                _, project = google.auth.default()
            except Exception:
                project = None
        if not project:
            raise SandboxConfigurationError(
                "No GCP project: set GOOGLE_CLOUD_PROJECT or "
                "backend_options['project']",
                backend="gcp_batch",
                op="submit_job",
            )
        return str(project)

    @staticmethod
    def _region(options: dict, project: str) -> str:
        region = options.get("region")
        if region:
            return str(region)
        candidates = options.get("regions")
        requirements = options.get("region_quota")
        if candidates is None:
            if requirements is not None:
                raise SandboxConfigurationError(
                    "backend_options['region_quota'] needs "
                    "backend_options['regions'] to choose from",
                    backend="gcp_batch",
                    op="submit_job",
                )
        else:
            if (
                isinstance(candidates, str)
                or not candidates
                or not all(isinstance(r, str) and r for r in candidates)
            ):
                raise SandboxConfigurationError(
                    "backend_options['regions'] must be a non-empty list of "
                    "region names",
                    backend="gcp_batch",
                    op="submit_job",
                )
            if not requirements:
                return str(candidates[0])
            if not isinstance(requirements, dict) or not all(
                isinstance(metric, str)
                and isinstance(amount, (int, float))
                and not isinstance(amount, bool)
                for metric, amount in requirements.items()
            ):
                raise SandboxConfigurationError(
                    "backend_options['region_quota'] must map Compute quota "
                    "metric names to numbers",
                    backend="gcp_batch",
                    op="submit_job",
                )
            from bespokelabs.sandbox.backends import gcp_batch_placement

            return gcp_batch_placement.choose_region(
                project, list(candidates), requirements
            ).region
        region = os.environ.get("GOOGLE_CLOUD_REGION") or os.environ.get(
            "CLOUDSDK_COMPUTE_REGION"
        )
        if not region:
            raise SandboxConfigurationError(
                "No Cloud Batch region: set GOOGLE_CLOUD_REGION or "
                "backend_options['region']",
                backend="gcp_batch",
                op="submit_job",
            )
        return str(region)
