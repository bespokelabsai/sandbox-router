"""Read Linux cgroup v2 and visible NVIDIA device counters inside a sandbox.

This file is sent verbatim to python3 in the sandbox; keep it stdlib-only.
Never fall back to host-wide /proc/stat or /proc/meminfo measurements.
"""

from __future__ import annotations

import csv
import json
import math
import os
import subprocess
import time
from pathlib import Path


def number(value):
    """Return finite nonnegative telemetry, or None for unsupported values."""
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def read(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, ValueError):
        return ""


def cgroup_directory():
    """Resolve this process's unified group against its visible mount root."""
    group = next(
        (
            line[3:]
            for line in read("/proc/self/cgroup").splitlines()
            if line.startswith("0::")
        ),
        None,
    )
    if group is None or ".." in Path(group).parts:
        return None, None
    for line in read("/proc/self/mountinfo").splitlines():
        before, sep, after = line.partition(" - ")
        fields = before.split()
        if not sep or not after.startswith("cgroup2 ") or len(fields) < 5:
            continue
        root, mount = fields[3:5]
        try:
            relative = Path(group).relative_to(root)
        except ValueError:
            continue
        return Path(mount) / relative, Path(mount)
    return None, None


def collect():
    """Collect counters without changing the sandbox or installing packages."""
    data = {
        "cpu_seconds": None,
        "cpu_capacity": None,
        "memory_used_bytes": None,
        "memory_limit_bytes": None,
        "gpus": [],
        "gpu_status": "unavailable",
    }
    directory, mount = cgroup_directory()
    if directory is not None:
        stats = dict(
            line.split()
            for line in read(directory / "cpu.stat").splitlines()
            if len(line.split()) == 2
        )
        microseconds = number(stats.get("usage_usec"))
        data["cpu_seconds"] = (
            microseconds / 1e6 if microseconds is not None else None
        )
        data["memory_used_bytes"] = number(read(directory / "memory.current"))
        capacities, limits = [], []
        current = directory
        while True:
            quota = read(current / "cpu.max").split()
            if (
                len(quota) == 2
                and number(quota[0]) is not None
                and number(quota[1])
            ):
                capacities.append(float(quota[0]) / float(quota[1]))
            limit = number(read(current / "memory.max"))
            if limit:
                limits.append(limit)
            if current == mount:
                break
            current = current.parent
        cpus = read(directory / "cpuset.cpus.effective")
        if cpus:
            try:
                count = sum(
                    int(part.split("-")[-1]) - int(part.split("-")[0]) + 1
                    for part in cpus.split(",")
                )
                if count > 0:
                    capacities.append(count)
            except ValueError:
                pass
        data["cpu_capacity"] = min(capacities) if capacities else None
        data["memory_limit_bytes"] = min(limits) if limits else None
        data["counter_id"] = (
            f"{directory}:{read('/proc/sys/kernel/random/boot_id')}"
        )
    data["monotonic_seconds"] = time.monotonic()
    # nvidia-smi reports visible device-wide activity, not process attribution.
    # N/A (including unsupported MIG utilization) stays null, never zero.
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=uuid,name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
        if result.returncode == 0:
            for row in list(csv.reader(result.stdout.splitlines()))[:16]:
                if len(row) != 5:
                    continue
                gpu_id, name, utilization, used, total = (
                    part.strip() for part in row
                )
                data["gpus"].append(
                    {
                        "id": gpu_id[:100],
                        "name": name[:100],
                        "utilization_percent": number(utilization),
                        "memory_used_bytes": number(used) * 1048576
                        if number(used) is not None
                        else None,
                        "memory_total_bytes": number(total) * 1048576
                        if number(total) is not None
                        else None,
                    }
                )
            data["gpu_status"] = "available" if data["gpus"] else "not_detected"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return data


if __name__ == "__main__":
    print(json.dumps(collect(), allow_nan=False))
