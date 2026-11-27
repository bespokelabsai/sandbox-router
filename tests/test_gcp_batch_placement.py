"""Region choice for Cloud Batch jobs.

The Compute client is faked; these tests never call Google Cloud and need no
extra installed.
"""

from __future__ import annotations

import types
import unittest

from bespokelabs.sandbox.backends.gcp_batch_placement import (
    RegionQuota,
    choose_region,
    probe_region_quotas,
    select_region,
)

NEED = {"CPUS": 16, "SSD_TOTAL_GB": 600}


def _quota(metric: str, limit: float, usage: float) -> object:
    return types.SimpleNamespace(metric=metric, limit=limit, usage=usage)


class _Regions:
    """Fake ``compute_v1.RegionsClient`` keyed by region name."""

    def __init__(self, by_region: dict[str, object]) -> None:
        self.by_region = by_region
        self.calls: list[tuple[str, str]] = []

    def get(self, *, project: str, region: str) -> object:
        self.calls.append((project, region))
        value = self.by_region[region]
        if isinstance(value, Exception):
            raise value
        return types.SimpleNamespace(quotas=value)


class SelectRegionTests(unittest.TestCase):

    def test_first_region_with_every_metric_free_wins(self) -> None:
        choice = select_region(
            [
                RegionQuota("a", {"CPUS": 8, "SSD_TOTAL_GB": 9000}),
                RegionQuota("b", {"CPUS": 16, "SSD_TOTAL_GB": 600}),
                RegionQuota("c", {"CPUS": 999, "SSD_TOTAL_GB": 9000}),
            ],
            NEED,
        )
        self.assertEqual((choice.region, choice.has_capacity), ("b", True))

    def test_unreadable_region_and_missing_metric_never_qualify(self) -> None:
        choice = select_region(
            [
                RegionQuota("a", None),
                RegionQuota("b", {"CPUS": 999}),
                RegionQuota("c", {"CPUS": 16, "SSD_TOTAL_GB": 600}),
            ],
            NEED,
        )
        self.assertEqual(choice.region, "c")

    def test_no_capacity_falls_back_to_first_candidate(self) -> None:
        quotas = [RegionQuota("a", {"CPUS": 1}), RegionQuota("b", None)]
        choice = select_region(quotas, NEED)
        self.assertEqual((choice.region, choice.has_capacity), ("a", False))
        choice = select_region(quotas, NEED, fallback="z")
        self.assertEqual((choice.region, choice.has_capacity), ("z", False))

    def test_no_requirements_takes_the_first_readable_region(self) -> None:
        choice = select_region(
            [RegionQuota("a", None), RegionQuota("b", {})], {}
        )
        self.assertEqual((choice.region, choice.has_capacity), ("b", True))

    def test_no_candidates_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            select_region([], NEED)


class ProbeTests(unittest.TestCase):

    def test_free_quota_is_limit_minus_usage_in_candidate_order(self) -> None:
        client = _Regions(
            {
                "a": [_quota("CPUS", 100, 90), _quota("SSD_TOTAL_GB", 1000, 0)],
                "b": [_quota("CPUS", 100, 0)],
            }
        )
        quotas = probe_region_quotas(
            "proj", ["a", "b"], client_factory=lambda: client
        )
        self.assertEqual([q.region for q in quotas], ["a", "b"])
        self.assertEqual(
            quotas[0].available, {"CPUS": 10.0, "SSD_TOTAL_GB": 1000.0}
        )
        self.assertEqual(quotas[1].available, {"CPUS": 100.0})
        self.assertEqual(sorted(client.calls), [("proj", "a"), ("proj", "b")])

    def test_a_failed_read_is_none_not_an_error(self) -> None:
        client = _Regions({"a": RuntimeError("403"), "b": []})
        quotas = probe_region_quotas(
            "proj", ["a", "b"], client_factory=lambda: client
        )
        self.assertIsNone(quotas[0].available)
        self.assertEqual(quotas[1].available, {})

    def test_no_regions_reads_nothing(self) -> None:
        self.assertEqual(
            probe_region_quotas(
                "proj", [], client_factory=lambda: self.fail("read")
            ),
            [],
        )

    def test_choose_region_skips_the_full_and_the_unreadable(self) -> None:
        client = _Regions(
            {
                "a": [_quota("CPUS", 100, 99), _quota("SSD_TOTAL_GB", 1000, 0)],
                "b": RuntimeError("boom"),
                "c": [_quota("CPUS", 100, 0), _quota("SSD_TOTAL_GB", 1000, 0)],
            }
        )
        choice = choose_region(
            "proj", ["a", "b", "c"], NEED, client_factory=lambda: client
        )
        self.assertEqual((choice.region, choice.has_capacity), ("c", True))


if __name__ == "__main__":
    unittest.main()
