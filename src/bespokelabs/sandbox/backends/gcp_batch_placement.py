"""Quota-aware region choice for Google Cloud Batch jobs.

A project's Compute quotas are per region, so a job submitted to a full
region queues while another region sits idle.  Given candidate regions in
preference order and the quota a job needs, this picks the first region
with enough free quota.

The choice (:func:`select_region`) is pure and separate from the quota
read (:func:`probe_region_quotas`), so it can be tested and reused without
Google Cloud.

Requirements are a mapping of Compute regional quota metric to the amount
that must be free, for example ``{"CPUS": 16, "SSD_TOTAL_GB": 600}`` or
``{"PREEMPTIBLE_NVIDIA_A100_GPUS": 1}``.  Metric names are the ones
``gcloud compute regions describe`` prints.

Free quota is a snapshot: it does not reserve anything, and it says nothing
about Spot capacity.  A region can pass this check and still queue the job.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from bespokelabs.sandbox.exceptions import BackendNotInstalledError

logger = logging.getLogger(__name__)

_INSTALL_HINT = (
    "Region selection needs the Compute SDK. Run: "
    "pip install 'bespokelabs-sandbox[gcp-batch]'"
)


@dataclass(frozen=True)
class RegionQuota:
    """Free quota per metric in one region.

    ``available`` is ``None`` when the region could not be read; a metric
    missing from the mapping is one the region does not report.
    """

    region: str
    available: Mapping[str, float] | None = field(default_factory=dict)


@dataclass(frozen=True)
class RegionChoice:
    region: str
    # False when no region had enough free quota and the first candidate
    # was returned so the job queues there instead of failing.
    has_capacity: bool


def select_region(
    quotas: Sequence[RegionQuota],
    requirements: Mapping[str, float],
    *,
    fallback: str | None = None,
) -> RegionChoice:
    """First region in ``quotas`` order with every required metric free.

    A region that could not be read, or that does not report a required
    metric, never qualifies.  When none qualifies the result is ``fallback``
    (default: the first candidate) with ``has_capacity`` false.
    """
    if not quotas:
        raise ValueError("select_region needs at least one candidate region")
    for quota in quotas:
        available = quota.available
        if available is None:
            continue
        if all(
            metric in available and available[metric] >= needed
            for metric, needed in requirements.items()
        ):
            return RegionChoice(region=quota.region, has_capacity=True)
    return RegionChoice(region=fallback or quotas[0].region, has_capacity=False)


def _regions_client() -> Any:
    try:
        from google.cloud import compute_v1  # type: ignore[import-untyped]
    except ImportError as exc:
        raise BackendNotInstalledError(_INSTALL_HINT) from exc
    return compute_v1.RegionsClient()


def _read_region(
    client_factory: Callable[[], Any], project: str, region: str
) -> RegionQuota:
    try:
        # One client per read: the reads run on separate threads.
        response = client_factory().get(project=project, region=region)
    except Exception as exc:  # noqa: BLE001 - one unreadable region must not fail the choice
        logger.warning(
            "could not read Compute quota for region %s: %s",
            region,
            type(exc).__name__,
        )
        return RegionQuota(region=region, available=None)
    available = {
        str(quota.metric): float(quota.limit or 0) - float(quota.usage or 0)
        for quota in (getattr(response, "quotas", None) or [])
    }
    return RegionQuota(region=region, available=available)


def probe_region_quotas(
    project: str,
    regions: Sequence[str],
    *,
    client_factory: Callable[[], Any] | None = None,
) -> list[RegionQuota]:
    """Read each region's free Compute quota in parallel, in ``regions`` order.

    A region whose read fails comes back with ``available=None`` instead of
    raising, so one bad region cannot block a submit.
    """
    if not regions:
        return []
    factory = client_factory or _regions_client
    with ThreadPoolExecutor(max_workers=min(len(regions), 8)) as pool:
        return list(
            pool.map(
                lambda region: _read_region(factory, project, region), regions
            )
        )


def choose_region(
    project: str,
    regions: Sequence[str],
    requirements: Mapping[str, float],
    *,
    fallback: str | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> RegionChoice:
    """Probe ``regions`` and pick the first with ``requirements`` free."""
    quotas = probe_region_quotas(
        project, regions, client_factory=client_factory
    )
    return select_region(quotas, requirements, fallback=fallback)
