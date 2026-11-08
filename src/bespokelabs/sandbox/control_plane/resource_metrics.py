"""Bounded, best-effort resource sampling independent of lifecycle supervision."""

from __future__ import annotations

import math
import threading
import time

STALE_SECONDS = 45


def number(value, maximum=None):
    """Validate untrusted provider telemetry before persistence or charting."""
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value) or value < 0:
            return None
        if maximum is not None and value > maximum:
            return None
        return value
    except OverflowError:
        return None


def normalize(raw, previous=None):
    """Convert cumulative CPU counters into percent of effective CPU capacity."""
    raw = raw if isinstance(raw, dict) else {}
    cpu = None
    capacity = number(raw.get("cpu_capacity"), 65536)
    elapsed = None
    if (
        previous
        and raw.get("counter_id")
        and raw.get("counter_id") == previous.get("counter_id")
        and capacity == previous.get("cpu_capacity")
    ):
        current = number(raw.get("cpu_seconds"))
        before = number(previous.get("cpu_seconds"))
        timestamp = number(raw.get("monotonic_seconds"))
        prior_time = number(previous.get("monotonic_seconds"))
        if None not in (current, before, timestamp, prior_time) and capacity:
            elapsed = timestamp - prior_time
            if 0 < elapsed <= 60 and current >= before:
                cpu = min(100, (current - before) / elapsed / capacity * 100)
    gpus = []
    seen = set()
    raw_gpus = raw.get("gpus", [])
    for gpu in raw_gpus[:16] if isinstance(raw_gpus, list) else []:
        if not isinstance(gpu, dict) or not isinstance(gpu.get("id"), str):
            continue
        identity = gpu["id"][:100]
        if identity in seen:
            continue
        seen.add(identity)
        gpus.append(
            {
                "id": identity,
                "name": str(gpu.get("name", "GPU"))[:100],
                "utilization_percent": number(
                    gpu.get("utilization_percent"), 100
                ),
                "memory_used_bytes": number(
                    gpu.get("memory_used_bytes"), 2**60
                ),
                "memory_total_bytes": number(
                    gpu.get("memory_total_bytes"), 2**60
                ),
            }
        )
    memory = number(raw.get("memory_used_bytes"), 2**60)
    available = (
        cpu is not None
        or memory is not None
        or any(
            g["utilization_percent"] is not None
            or g["memory_used_bytes"] is not None
            for g in gpus
        )
    )
    return {
        "cpu_percent": cpu,
        "cpu_capacity_cores": capacity,
        "cpu_window_seconds": elapsed if cpu is not None else None,
        "memory_used_bytes": memory,
        "memory_limit_bytes": number(raw.get("memory_limit_bytes"), 2**60),
        "gpus": gpus,
        "status": "available"
        if cpu is not None and memory is not None
        else "partial"
        if available
        else "unavailable",
        "message": "CPU needs two consecutive samples; missing counters or devices are unavailable."
        if available
        else "Resource counters are unavailable for this runtime or image.",
    }


class ResourceMonitor:
    """Limit probes to four daemon workers, never overlapping one sandbox.

    Slow calls become unavailable after ten seconds. A timed-out worker retains
    its slot until it returns, preventing unbounded threads or overlapping execs.
    Neither HTTP reads nor the lifecycle watchdog wait on these provider calls.
    """

    def __init__(self, targets, save, now, *, interval=15):
        self.targets, self.save, self.now = targets, save, now
        self.interval = interval
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread = None
        self._active = {}
        self._last = {}
        self._previous = {}
        self._targets = []
        self._targets_checked_at = None

    def start(self):
        if self.interval <= 0:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._targets_checked_at = None
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="sandbox-resource-monitor"
            )
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def tick(self):
        """Schedule due probes fairly and record timeouts without waiting."""
        with self._lock:
            if self._stop.is_set():
                return
            current = time.monotonic()
            # Discover runtimes at sampling cadence, while checking timeouts
            # and filling free worker slots on every one-second tick.
            if (
                self._targets_checked_at is None
                or current - self._targets_checked_at >= self.interval
            ):
                self._targets = self.targets()
                self._targets_checked_at = current
            targets = self._targets
            ids = {record.id for record, _ in targets}
            self._last = {k: v for k, v in self._last.items() if k in ids}
            self._previous = {
                k: v for k, v in self._previous.items() if k in ids
            }
            for sandbox_id, job in list(self._active.items()):
                if current - job["started"] >= 10 and not job["expired"]:
                    job["expired"] = True
                    self._previous.pop(sandbox_id, None)
                    sample = normalize({})
                    sample["message"] = (
                        "Resource probe timed out; waiting for the provider call to finish."
                    )
                    self._save(job["record"], sample)
            for record, runtime in sorted(
                targets, key=lambda pair: self._last.get(pair[0].id, 0)
            ):
                if len(self._active) >= 4:
                    break
                if (
                    record.id in self._active
                    or current - self._last.get(record.id, -self.interval)
                    < self.interval
                ):
                    continue
                job = {"started": current, "expired": False, "record": record}
                self._last[record.id] = current
                self._active[record.id] = job
                threading.Thread(
                    target=self._probe,
                    args=(record, runtime, job),
                    daemon=True,
                    name="sandbox-resource-probe",
                ).start()

    def _save(self, record, sample):
        sample["collected_at"] = self.now().isoformat()
        self.save(record.organization_id, record.id, sample)

    def _probe(self, record, runtime, job):
        try:
            collector = getattr(runtime, "resource_usage", None)
            raw = collector() if callable(collector) else {}
            raw = raw if isinstance(raw, dict) else {}
        except Exception:
            raw = {}
        try:
            with self._lock:
                if self._stop.is_set() or job["expired"]:
                    return
                if time.monotonic() - job["started"] >= 10:
                    raw = {}
                sample = normalize(raw, self._previous.get(record.id))
                self._previous[record.id] = raw
                self._save(record, sample)
        except Exception:
            # A failed telemetry write must still release this worker slot.
            pass
        finally:
            with self._lock:
                self._active.pop(record.id, None)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                # Telemetry outages must not interrupt lifecycle or requests.
                pass
            self._stop.wait(1)
