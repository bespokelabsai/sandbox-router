"""Reaper planner and executor guards, with no provider and no I/O."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from bespokelabs.sandbox.control_plane.reaper import (
    DEFAULT_MAX_DELETES,
    DEFAULT_MAX_UNREFERENCED_FRACTION,
    DEFAULT_MIN_AGE,
    DEFAULT_MIN_REFERENCES,
    Liveness,
    LivenessReport,
    ReapAction,
    ReapCandidate,
    ReapDecision,
    ReapLimits,
    ReapOutcomeStatus,
    ReapPlan,
    ReapReason,
    execute_reap_plan,
    plan_reap,
)
from bespokelabs.sandbox.exceptions import (
    ErrorOutcome,
    SandboxError,
    SandboxNotFoundError,
)
from bespokelabs.sandbox.types import SandboxListing, SandboxSessionState

UTC = timezone.utc
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
OWNER = {"owner": "team-a"}


def _candidate(
    resource_id: str = "r1",
    *,
    age: timedelta | None = timedelta(hours=5),
    state: str | None = "stopped",
    labels: dict[str, str] | None = None,
    labels_complete: bool = True,
) -> ReapCandidate:
    return ReapCandidate(
        resource_id=resource_id,
        created_at=None if age is None else NOW - age,
        state=state,
        labels=dict(OWNER) if labels is None else labels,
        labels_complete=labels_complete,
    )


def _unused(*ids: str) -> LivenessReport:
    return LivenessReport.from_facts(not_in_use=ids)


def _plan(candidates, liveness=None, **kwargs) -> ReapPlan:
    candidates = list(candidates)
    if liveness is None:
        liveness = _unused(*(c.resource_id for c in candidates))
    kwargs.setdefault("required_labels", OWNER)
    return plan_reap(candidates, liveness, now=NOW, **kwargs)


def _reasons(plan: ReapPlan) -> dict[str, ReapReason]:
    return {d.candidate.resource_id: d.reason for d in plan.decisions}


def _delete_ids(plan: ReapPlan) -> list[str]:
    return [candidate.resource_id for candidate in plan.deletes]


class _Recorder:

    def __init__(self, errors: dict[str, Exception] | None = None) -> None:
        self.calls: list[str] = []
        self.errors = errors or {}

    def __call__(self, candidate: ReapCandidate) -> None:
        self.calls.append(candidate.resource_id)
        if candidate.resource_id in self.errors:
            raise self.errors[candidate.resource_id]


class DefaultsTest(unittest.TestCase):

    def test_defaults_are_conservative(self) -> None:
        limits = ReapLimits()
        self.assertEqual(limits.min_age, DEFAULT_MIN_AGE)
        self.assertEqual(DEFAULT_MIN_AGE, timedelta(hours=1))
        self.assertEqual(limits.max_deletes, DEFAULT_MAX_DELETES)
        self.assertEqual(DEFAULT_MAX_DELETES, 25)
        self.assertEqual(limits.min_references, DEFAULT_MIN_REFERENCES)
        self.assertEqual(DEFAULT_MIN_REFERENCES, 1)
        self.assertEqual(
            limits.max_unreferenced_fraction, DEFAULT_MAX_UNREFERENCED_FRACTION
        )
        self.assertEqual(DEFAULT_MAX_UNREFERENCED_FRACTION, 0.5)

    def test_invalid_limits_are_rejected(self) -> None:
        for kwargs in (
            {"min_age": timedelta(seconds=-1)},
            {"min_age": 3600},
            {"max_deletes": -1},
            {"max_deletes": True},
            {"max_deletes": 1.5},
            {"min_references": -1},
            {"max_unreferenced_fraction": 1.5},
            {"max_unreferenced_fraction": -0.1},
            {"max_unreferenced_fraction": "half"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ReapLimits(**kwargs)


class LivenessReportTest(unittest.TestCase):

    def test_facts_distinguish_three_answers(self) -> None:
        report = LivenessReport.from_facts(in_use=["a"], not_in_use=["b"])
        self.assertIs(report.liveness("a"), Liveness.IN_USE)
        self.assertIs(report.liveness("b"), Liveness.NOT_IN_USE)
        self.assertIs(report.liveness("c"), Liveness.UNKNOWN)

    def test_contradiction_counts_as_in_use(self) -> None:
        report = LivenessReport.from_facts(in_use=["a"], not_in_use=["a"])
        self.assertIs(report.liveness("a"), Liveness.IN_USE)

    def test_references_treat_absence_as_unused(self) -> None:
        report = LivenessReport.from_references(["a"])
        self.assertIs(report.liveness("a"), Liveness.IN_USE)
        self.assertIs(report.liveness("b"), Liveness.NOT_IN_USE)

    def test_failed_report_knows_nothing(self) -> None:
        report = LivenessReport.failed("timeout")
        self.assertEqual(report.failure, "timeout")
        self.assertIs(report.liveness("a"), Liveness.UNKNOWN)
        self.assertIsNotNone(LivenessReport.failed("").failure)

    def test_a_bare_string_is_not_a_set_of_ids(self) -> None:
        with self.assertRaises(TypeError):
            LivenessReport.from_facts(not_in_use="abc")
        with self.assertRaises(TypeError):
            LivenessReport.from_references("abc")

    def test_blank_and_non_string_ids_are_dropped(self) -> None:
        report = LivenessReport.from_facts(not_in_use=["", "  ", None, "a"])
        self.assertEqual(report.not_in_use, frozenset({"a"}))


class PlanInputTest(unittest.TestCase):

    def test_ownership_proof_is_required(self) -> None:
        for kwargs in ({}, {"required_labels": {}}, {"required_labels": None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                plan_reap([_candidate()], _unused("r1"), now=NOW, **kwargs)

    def test_empty_label_key_or_value_is_rejected(self) -> None:
        for labels in ({"": "x"}, {"owner": ""}):
            with self.subTest(labels=labels), self.assertRaises(ValueError):
                _plan([_candidate()], required_labels=labels)

    def test_now_must_be_timezone_aware(self) -> None:
        with self.assertRaises(ValueError):
            plan_reap(
                [_candidate()],
                _unused("r1"),
                now=datetime(2026, 1, 10, 12, 0),
                required_labels=OWNER,
            )

    def test_liveness_must_be_a_report(self) -> None:
        with self.assertRaises(TypeError):
            plan_reap([_candidate()], {"r1"}, now=NOW, required_labels=OWNER)

    def test_string_collections_are_rejected(self) -> None:
        with self.assertRaises(TypeError):
            _plan([_candidate()], protected_ids="r1")
        with self.assertRaises(TypeError):
            _plan([_candidate()], deletable_states="stopped")

    def test_empty_listing_gives_an_empty_plan(self) -> None:
        plan = _plan([])
        self.assertEqual(plan.decisions, ())
        self.assertEqual(plan.deletes, ())
        self.assertIsNone(plan.refusal)


class PlanGuardTest(unittest.TestCase):

    def test_eligible_resource_is_deleted(self) -> None:
        plan = _plan([_candidate()])
        self.assertEqual(_delete_ids(plan), ["r1"])
        decision = plan.decisions[0]
        self.assertIs(decision.action, ReapAction.DELETE)
        self.assertIs(decision.reason, ReapReason.ELIGIBLE)
        self.assertEqual(plan.kept, ())
        self.assertEqual(plan.counts(), {"eligible": 1})

    def test_invalid_id_is_kept(self) -> None:
        for bad in ("", "   ", None, 7):
            with self.subTest(resource_id=bad):
                candidate = ReapCandidate(
                    resource_id=bad,
                    created_at=NOW - timedelta(days=1),
                    labels=dict(OWNER),
                )
                plan = plan_reap(
                    [candidate],
                    LivenessReport.from_facts(),
                    now=NOW,
                    required_labels=OWNER,
                )
                self.assertEqual(plan.deletes, ())
                self.assertIs(plan.decisions[0].reason, ReapReason.INVALID_ID)

    def test_duplicate_listing_keeps_every_copy(self) -> None:
        plan = _plan([_candidate("r1"), _candidate("r1"), _candidate("r2")])
        self.assertEqual(_delete_ids(plan), ["r2"])
        self.assertEqual(
            [d.reason for d in plan.decisions],
            [
                ReapReason.DUPLICATE_LISTING,
                ReapReason.DUPLICATE_LISTING,
                ReapReason.ELIGIBLE,
            ],
        )

    def test_protected_id_is_kept(self) -> None:
        plan = _plan([_candidate("r1"), _candidate("r2")], protected_ids=["r1"])
        self.assertEqual(_delete_ids(plan), ["r2"])
        self.assertIs(_reasons(plan)["r1"], ReapReason.PROTECTED)

    def test_missing_or_different_label_is_not_owned(self) -> None:
        plan = _plan(
            [
                _candidate("none", labels={}),
                _candidate("other", labels={"owner": "team-b"}),
                _candidate("partial", labels={"owner": "team-a"}),
                _candidate("mine", labels={"owner": "team-a", "env": "ci"}),
            ],
            required_labels={"owner": "team-a", "env": "ci"},
        )
        self.assertEqual(_delete_ids(plan), ["mine"])
        reasons = _reasons(plan)
        for resource_id in ("none", "other", "partial"):
            self.assertIs(reasons[resource_id], ReapReason.NOT_OWNED)

    def test_unreadable_labels_are_ownership_unknown(self) -> None:
        candidate = ReapCandidate(
            resource_id="r1", created_at=NOW - timedelta(days=1), labels=None
        )
        plan = _plan([candidate])
        self.assertIs(_reasons(plan)["r1"], ReapReason.OWNERSHIP_UNKNOWN)

    def test_predicate_must_return_true(self) -> None:
        for verdict in (False, None, 1, "yes"):
            with self.subTest(verdict=verdict):
                plan = _plan(
                    [_candidate()],
                    required_labels=None,
                    is_owned=lambda _c, v=verdict: v,
                )
                self.assertEqual(plan.deletes, ())
                self.assertIs(_reasons(plan)["r1"], ReapReason.NOT_OWNED)
        plan = _plan(
            [_candidate()], required_labels=None, is_owned=lambda _c: True
        )
        self.assertEqual(_delete_ids(plan), ["r1"])

    def test_predicate_that_raises_keeps_the_resource(self) -> None:
        def boom(_candidate: ReapCandidate) -> bool:
            raise RuntimeError("lookup failed")

        plan = _plan([_candidate()], is_owned=boom)
        self.assertEqual(plan.deletes, ())
        self.assertIs(_reasons(plan)["r1"], ReapReason.OWNERSHIP_UNKNOWN)

    def test_labels_and_predicate_must_both_pass(self) -> None:
        plan = _plan(
            [_candidate("r1", labels={"owner": "team-b"})],
            is_owned=lambda _c: True,
        )
        self.assertIs(_reasons(plan)["r1"], ReapReason.NOT_OWNED)

    def test_incomplete_labels_do_not_reach_a_predicate(self) -> None:
        calls: list[str] = []

        def owned(candidate: ReapCandidate) -> bool:
            calls.append(candidate.resource_id)
            return True

        plan = _plan([_candidate(labels_complete=False)], is_owned=owned)
        self.assertEqual(calls, [])
        self.assertIs(_reasons(plan)["r1"], ReapReason.OWNERSHIP_UNKNOWN)
        # Labels that were not read back in full prove nothing on their own.
        plan = _plan([_candidate(labels_complete=False)])
        self.assertEqual(_delete_ids(plan), [])
        self.assertIs(_reasons(plan)["r1"], ReapReason.OWNERSHIP_UNKNOWN)

    def test_state_is_not_checked_unless_asked(self) -> None:
        plan = _plan(
            [_candidate(state=None), _candidate("r2", state="running")]
        )
        self.assertEqual(sorted(_delete_ids(plan)), ["r1", "r2"])

    def test_state_allowlist(self) -> None:
        plan = _plan(
            [
                _candidate("ok", state=" Stopped "),
                _candidate("none", state=None),
                _candidate("blank", state=" "),
                _candidate("live", state="running"),
                _candidate("near", state="stopping"),
                _candidate("new", state="some_future_state"),
            ],
            deletable_states=["STOPPED"],
        )
        self.assertEqual(_delete_ids(plan), ["ok"])
        reasons = _reasons(plan)
        self.assertIs(reasons["none"], ReapReason.STATE_UNKNOWN)
        self.assertIs(reasons["blank"], ReapReason.STATE_UNKNOWN)
        for resource_id in ("live", "near", "new"):
            self.assertIs(reasons[resource_id], ReapReason.STATE_NOT_DELETABLE)

    def test_empty_state_allowlist_deletes_nothing(self) -> None:
        plan = _plan([_candidate()], deletable_states=[])
        self.assertEqual(plan.deletes, ())

    def test_unknown_age_is_kept(self) -> None:
        naive = ReapCandidate(
            resource_id="naive",
            created_at=datetime(2020, 1, 1),
            labels=dict(OWNER),
        )
        text = ReapCandidate(
            resource_id="text",
            created_at="2020-01-01T00:00:00+00:00",
            labels=dict(OWNER),
        )
        plan = _plan([_candidate("none", age=None), naive, text])
        self.assertEqual(plan.deletes, ())
        for reason in _reasons(plan).values():
            self.assertIs(reason, ReapReason.AGE_UNKNOWN)

    def test_min_age_boundary(self) -> None:
        plan = _plan(
            [
                _candidate("young", age=timedelta(minutes=59, seconds=59)),
                _candidate("exact", age=timedelta(hours=1)),
                _candidate("future", age=timedelta(minutes=-5)),
            ]
        )
        self.assertEqual(_delete_ids(plan), ["exact"])
        reasons = _reasons(plan)
        self.assertIs(reasons["young"], ReapReason.TOO_YOUNG)
        self.assertIs(reasons["future"], ReapReason.TOO_YOUNG)

    def test_future_creation_is_too_young_even_with_zero_min_age(self) -> None:
        plan = _plan(
            [
                _candidate("future", age=timedelta(seconds=-1)),
                _candidate("now", age=timedelta(0)),
            ],
            limits=ReapLimits(min_age=timedelta(0)),
        )
        self.assertEqual(_delete_ids(plan), ["now"])
        self.assertIs(_reasons(plan)["future"], ReapReason.TOO_YOUNG)

    def test_age_is_compared_across_time_zones(self) -> None:
        east = timezone(timedelta(hours=9))
        candidate = ReapCandidate(
            resource_id="r1",
            created_at=(NOW - timedelta(minutes=30)).astimezone(east),
            labels=dict(OWNER),
        )
        self.assertIs(_reasons(_plan([candidate]))["r1"], ReapReason.TOO_YOUNG)

    def test_in_use_is_kept(self) -> None:
        plan = _plan(
            [_candidate("r1"), _candidate("r2")],
            LivenessReport.from_facts(in_use=["r1"], not_in_use=["r1", "r2"]),
        )
        self.assertEqual(_delete_ids(plan), ["r2"])
        self.assertIs(_reasons(plan)["r1"], ReapReason.IN_USE)

    def test_unknown_liveness_means_keep(self) -> None:
        plan = _plan(
            [_candidate("r1"), _candidate("r2")],
            LivenessReport.from_facts(not_in_use=["r2"]),
        )
        self.assertEqual(_delete_ids(plan), ["r2"])
        self.assertIs(_reasons(plan)["r1"], ReapReason.LIVENESS_UNKNOWN)
        self.assertIsNone(plan.refusal)

    def test_empty_facts_delete_nothing(self) -> None:
        plan = _plan([_candidate("r1")], LivenessReport.from_facts())
        self.assertEqual(plan.deletes, ())
        self.assertIs(_reasons(plan)["r1"], ReapReason.LIVENESS_UNKNOWN)

    def test_earlier_guard_reasons_take_precedence(self) -> None:
        # Not owned, wrong state, too young and in use: ownership is reported.
        candidate = _candidate(
            age=timedelta(seconds=1), state="running", labels={}
        )
        plan = _plan(
            [candidate],
            LivenessReport.from_facts(in_use=["r1"]),
            deletable_states=["stopped"],
        )
        self.assertIs(_reasons(plan)["r1"], ReapReason.NOT_OWNED)

    def test_listing_order_is_kept_and_every_resource_is_reported(self) -> None:
        candidates = [
            _candidate("c"),
            _candidate("a", labels={}),
            _candidate("b", age=None),
        ]
        plan = _plan(candidates)
        self.assertEqual(
            [d.candidate.resource_id for d in plan.decisions], ["c", "a", "b"]
        )
        self.assertEqual(len(plan.kept), 2)
        self.assertEqual(
            plan.counts(), {"eligible": 1, "not_owned": 1, "age_unknown": 1}
        )

    def test_sandbox_listing_is_accepted(self) -> None:
        listing = SandboxListing(
            provider_resource_id="sb-1",
            labels=dict(OWNER),
            state="stopped",
            created_at=NOW - timedelta(days=1),
            session_state=SandboxSessionState(backend="x", data={}),
        )
        candidate = ReapCandidate.from_listing(listing)
        self.assertEqual(candidate.resource_id, "sb-1")
        self.assertEqual(candidate.labels, OWNER)
        self.assertEqual(candidate.state, "stopped")
        self.assertTrue(candidate.labels_complete)
        plan = plan_reap(
            [listing], _unused("sb-1"), now=NOW, required_labels=OWNER
        )
        self.assertEqual(_delete_ids(plan), ["sb-1"])

    def test_planning_is_repeatable(self) -> None:
        candidates = [_candidate("r1"), _candidate("r2", labels={})]
        self.assertEqual(_plan(candidates), _plan(candidates))


class PlanRefusalTest(unittest.TestCase):

    def test_failed_source_deletes_nothing(self) -> None:
        plan = _plan(
            [_candidate("r1"), _candidate("r2"), _candidate("r3", labels={})],
            LivenessReport.failed("database unreachable"),
        )
        self.assertEqual(plan.deletes, ())
        self.assertIs(plan.refusal, ReapReason.LIVENESS_SOURCE_FAILED)
        self.assertEqual(plan.refusal_detail, "database unreachable")
        reasons = _reasons(plan)
        self.assertIs(reasons["r1"], ReapReason.LIVENESS_SOURCE_FAILED)
        self.assertIs(reasons["r2"], ReapReason.LIVENESS_SOURCE_FAILED)
        # A resource kept by an earlier guard keeps its own reason.
        self.assertIs(reasons["r3"], ReapReason.NOT_OWNED)

    def test_failed_source_refuses_an_empty_listing_too(self) -> None:
        plan = _plan([], LivenessReport.failed())
        self.assertIs(plan.refusal, ReapReason.LIVENESS_SOURCE_FAILED)

    def test_empty_reference_set_deletes_nothing(self) -> None:
        plan = _plan(
            [_candidate("r1"), _candidate("r2")],
            LivenessReport.from_references([]),
        )
        self.assertEqual(plan.deletes, ())
        self.assertIs(plan.refusal, ReapReason.REFERENCE_SET_TOO_SMALL)
        for reason in _reasons(plan).values():
            self.assertIs(reason, ReapReason.REFERENCE_SET_TOO_SMALL)

    def test_blank_references_do_not_count(self) -> None:
        plan = _plan(
            [_candidate("r1")], LivenessReport.from_references(["", " "])
        )
        self.assertIs(plan.refusal, ReapReason.REFERENCE_SET_TOO_SMALL)

    def test_min_references_is_configurable(self) -> None:
        candidates = [_candidate(f"r{i}") for i in range(4)]
        report = LivenessReport.from_references(["r0", "r1", "r2"])
        plan = _plan(candidates, report, limits=ReapLimits(min_references=4))
        self.assertIs(plan.refusal, ReapReason.REFERENCE_SET_TOO_SMALL)
        plan = _plan(candidates, report, limits=ReapLimits(min_references=3))
        self.assertEqual(_delete_ids(plan), ["r3"])

    def test_reference_set_deletes_only_unreferenced(self) -> None:
        candidates = [_candidate(f"r{i}") for i in range(4)]
        plan = _plan(candidates, LivenessReport.from_references(["r0", "r1"]))
        # Exactly half unreferenced is at the ceiling, not above it.
        self.assertIsNone(plan.refusal)
        self.assertEqual(sorted(_delete_ids(plan)), ["r2", "r3"])
        self.assertIs(_reasons(plan)["r0"], ReapReason.IN_USE)

    def test_mostly_unreferenced_listing_deletes_nothing(self) -> None:
        candidates = [_candidate(f"r{i}") for i in range(4)]
        plan = _plan(candidates, LivenessReport.from_references(["r0"]))
        self.assertEqual(plan.deletes, ())
        self.assertIs(plan.refusal, ReapReason.UNREFERENCED_FRACTION_EXCEEDED)
        self.assertIn("3 of 4", plan.refusal_detail)
        reasons = _reasons(plan)
        self.assertIs(reasons["r0"], ReapReason.IN_USE)
        for resource_id in ("r1", "r2", "r3"):
            self.assertIs(
                reasons[resource_id], ReapReason.UNREFERENCED_FRACTION_EXCEEDED
            )

    def test_fraction_counts_young_resources_and_ignores_unowned(self) -> None:
        # Three of four owned resources are unreferenced. Two are too young to
        # delete, which must not hide the truncated-looking reference set.
        candidates = [
            _candidate("r0"),
            _candidate("r1"),
            _candidate("r2", age=timedelta(minutes=1)),
            _candidate("r3", age=timedelta(minutes=1)),
            _candidate("x1", labels={}),
            _candidate("x2", labels={}),
        ]
        plan = _plan(candidates, LivenessReport.from_references(["r0"]))
        self.assertIs(plan.refusal, ReapReason.UNREFERENCED_FRACTION_EXCEEDED)
        self.assertEqual(plan.deletes, ())
        # Unowned resources do not dilute the fraction either.
        plan = _plan(
            candidates + [_candidate(f"y{i}", labels={}) for i in range(20)],
            LivenessReport.from_references(["r0"]),
        )
        self.assertIs(plan.refusal, ReapReason.UNREFERENCED_FRACTION_EXCEEDED)

    def test_fraction_ceiling_is_configurable(self) -> None:
        candidates = [_candidate(f"r{i}") for i in range(4)]
        plan = _plan(
            candidates,
            LivenessReport.from_references(["r0"]),
            limits=ReapLimits(max_unreferenced_fraction=0.75),
        )
        self.assertEqual(sorted(_delete_ids(plan)), ["r1", "r2", "r3"])

    def test_fraction_guard_does_not_apply_to_per_resource_facts(self) -> None:
        candidates = [_candidate(f"r{i}") for i in range(4)]
        plan = _plan(candidates)
        self.assertIsNone(plan.refusal)
        self.assertEqual(len(plan.deletes), 4)


class PlanCapTest(unittest.TestCase):

    def test_cap_keeps_the_newest_eligible(self) -> None:
        candidates = [
            _candidate("new", age=timedelta(hours=2)),
            _candidate("old", age=timedelta(hours=9)),
            _candidate("mid", age=timedelta(hours=5)),
        ]
        plan = _plan(candidates, limits=ReapLimits(max_deletes=2))
        self.assertEqual(_delete_ids(plan), ["old", "mid"])
        self.assertIs(_reasons(plan)["new"], ReapReason.DELETE_CAP_REACHED)
        self.assertEqual(plan.max_deletes, 2)
        self.assertIsNone(plan.refusal)

    def test_equal_ages_break_ties_by_id(self) -> None:
        plan = _plan(
            [_candidate("b"), _candidate("a"), _candidate("c")],
            limits=ReapLimits(max_deletes=2),
        )
        self.assertEqual(_delete_ids(plan), ["a", "b"])

    def test_zero_cap_deletes_nothing(self) -> None:
        plan = _plan([_candidate()], limits=ReapLimits(max_deletes=0))
        self.assertEqual(plan.deletes, ())
        self.assertIs(_reasons(plan)["r1"], ReapReason.DELETE_CAP_REACHED)

    def test_default_cap(self) -> None:
        candidates = [
            _candidate(f"r{i:03d}", age=timedelta(hours=2, minutes=i))
            for i in range(DEFAULT_MAX_DELETES + 5)
        ]
        plan = _plan(candidates)
        self.assertEqual(len(plan.deletes), DEFAULT_MAX_DELETES)
        self.assertEqual(plan.counts()["delete_cap_reached"], 5)


class ExecuteTest(unittest.TestCase):

    def _three(self, **limits) -> ReapPlan:
        return _plan(
            [
                _candidate("a", age=timedelta(hours=9)),
                _candidate("b", age=timedelta(hours=8)),
                _candidate("c", age=timedelta(hours=7)),
                _candidate("kept", labels={}),
            ],
            limits=ReapLimits(**limits),
        )

    def test_dry_run_is_the_default(self) -> None:
        recorder = _Recorder()
        result = execute_reap_plan(self._three(), recorder)
        self.assertEqual(recorder.calls, [])
        self.assertFalse(result.applied)
        self.assertEqual(result.counts(), {"would_delete": 3})
        self.assertEqual(
            [o.candidate.resource_id for o in result.outcomes], ["a", "b", "c"]
        )
        self.assertEqual(
            [d.candidate.resource_id for d in result.kept], ["kept"]
        )
        self.assertIs(result.kept[0].reason, ReapReason.NOT_OWNED)

    def test_apply_deletes_oldest_first(self) -> None:
        recorder = _Recorder()
        result = execute_reap_plan(self._three(), recorder, apply=True)
        self.assertEqual(recorder.calls, ["a", "b", "c"])
        self.assertTrue(result.applied)
        self.assertEqual(result.counts(), {"deleted": 3})
        self.assertEqual(result.failed, ())

    def test_failure_is_recorded_and_the_run_continues(self) -> None:
        recorder = _Recorder({"b": RuntimeError("permission denied")})
        result = execute_reap_plan(self._three(), recorder, apply=True)
        self.assertEqual(recorder.calls, ["a", "b", "c"])
        self.assertEqual(result.counts(), {"deleted": 2, "failed": 1})
        (failure,) = result.failed
        self.assertEqual(failure.candidate.resource_id, "b")
        self.assertEqual(failure.error, "RuntimeError: permission denied")

    def test_every_delete_failing_still_returns_a_result(self) -> None:
        recorder = _Recorder({k: ValueError() for k in "abc"})
        result = execute_reap_plan(self._three(), recorder, apply=True)
        self.assertEqual(result.counts(), {"failed": 3})
        self.assertEqual(result.failed[0].error, "ValueError")

    def test_error_text_is_bounded(self) -> None:
        class Unprintable(Exception):

            def __str__(self) -> str:
                raise RuntimeError("no text")

        recorder = _Recorder(
            {"a": RuntimeError("x" * 5000), "b": Unprintable()}
        )
        result = execute_reap_plan(self._three(), recorder, apply=True)
        long, unprintable = result.failed
        self.assertEqual(len(long.error), 200)
        self.assertEqual(unprintable.error, "Unprintable")

    def test_already_gone_is_success(self) -> None:
        recorder = _Recorder({"a": SandboxNotFoundError("gone")})
        result = execute_reap_plan(self._three(), recorder, apply=True)
        self.assertEqual(result.counts(), {"already_gone": 1, "deleted": 2})
        self.assertEqual(result.failed, ())
        self.assertIs(result.outcomes[0].status, ReapOutcomeStatus.ALREADY_GONE)
        self.assertIsNone(result.outcomes[0].error)

    def test_not_found_with_unknown_outcome_is_a_failure(self) -> None:
        error = SandboxNotFoundError("maybe", outcome=ErrorOutcome.UNKNOWN)
        result = execute_reap_plan(
            self._three(), _Recorder({"a": error}), apply=True
        )
        self.assertEqual(result.failed[0].candidate.resource_id, "a")

    def test_other_sandbox_errors_are_failures(self) -> None:
        result = execute_reap_plan(
            self._three(), _Recorder({"a": SandboxError("nope")}), apply=True
        )
        self.assertEqual(len(result.failed), 1)

    def test_caller_can_classify_already_gone(self) -> None:
        recorder = _Recorder({"a": LookupError("404"), "b": LookupError("500")})
        result = execute_reap_plan(
            self._three(),
            recorder,
            apply=True,
            is_already_gone=lambda error: "404" in str(error),
        )
        self.assertEqual(
            [o.status.value for o in result.outcomes],
            ["already_gone", "failed", "deleted"],
        )

    def test_classifier_must_return_true_and_may_not_raise(self) -> None:
        def boom(_error: Exception) -> bool:
            raise RuntimeError("classifier broke")

        for classifier in (boom, lambda _e: "yes", lambda _e: None):
            with self.subTest(classifier=classifier):
                result = execute_reap_plan(
                    self._three(),
                    _Recorder({"a": LookupError("404")}),
                    apply=True,
                    is_already_gone=classifier,
                )
                self.assertEqual(result.counts(), {"failed": 1, "deleted": 2})

    def test_repeated_run_is_idempotent(self) -> None:
        plan = self._three()
        first = execute_reap_plan(plan, _Recorder(), apply=True)
        gone = _Recorder({k: SandboxNotFoundError("gone") for k in "abc"})
        second = execute_reap_plan(plan, gone, apply=True)
        self.assertEqual(first.counts(), {"deleted": 3})
        self.assertEqual(second.counts(), {"already_gone": 3})
        self.assertEqual(second.failed, ())

    def test_executor_cap_can_only_lower_the_plan_cap(self) -> None:
        recorder = _Recorder()
        result = execute_reap_plan(
            self._three(), recorder, apply=True, max_deletes=2
        )
        self.assertEqual(recorder.calls, ["a", "b"])
        self.assertEqual(result.counts(), {"deleted": 2, "skipped_cap": 1})
        self.assertIs(result.outcomes[2].status, ReapOutcomeStatus.SKIPPED_CAP)

        recorder = _Recorder()
        result = execute_reap_plan(
            self._three(max_deletes=1), recorder, apply=True, max_deletes=99
        )
        self.assertEqual(recorder.calls, ["a"])

    def test_failed_attempts_count_toward_the_cap(self) -> None:
        recorder = _Recorder({"a": RuntimeError("x"), "b": RuntimeError("y")})
        result = execute_reap_plan(
            self._three(), recorder, apply=True, max_deletes=2
        )
        self.assertEqual(recorder.calls, ["a", "b"])
        self.assertEqual(result.counts(), {"failed": 2, "skipped_cap": 1})

    def test_cap_applies_to_a_dry_run(self) -> None:
        result = execute_reap_plan(self._three(), _Recorder(), max_deletes=1)
        self.assertEqual(result.counts(), {"would_delete": 1, "skipped_cap": 2})

    def test_zero_or_negative_cap_deletes_nothing(self) -> None:
        for cap in (0, -3):
            with self.subTest(cap=cap):
                recorder = _Recorder()
                result = execute_reap_plan(
                    self._three(), recorder, apply=True, max_deletes=cap
                )
                self.assertEqual(recorder.calls, [])
                self.assertEqual(result.counts(), {"skipped_cap": 3})

    def test_hand_built_plan_cannot_exceed_its_own_cap(self) -> None:
        decisions = tuple(
            ReapDecision(
                _candidate(name), ReapAction.DELETE, ReapReason.ELIGIBLE
            )
            for name in "abc"
        )
        recorder = _Recorder()
        result = execute_reap_plan(
            ReapPlan(decisions=decisions, max_deletes=1), recorder, apply=True
        )
        self.assertEqual(recorder.calls, ["a"])
        self.assertEqual(result.counts(), {"deleted": 1, "skipped_cap": 2})

    def test_refused_plan_is_never_executed(self) -> None:
        refused = _plan([_candidate()], LivenessReport.failed("down"))
        recorder = _Recorder()
        result = execute_reap_plan(refused, recorder, apply=True)
        self.assertEqual(recorder.calls, [])
        self.assertEqual(result.outcomes, ())
        self.assertIs(result.refusal, ReapReason.LIVENESS_SOURCE_FAILED)
        self.assertEqual(len(result.kept), 1)

        # Even a hand-built plan that pairs deletions with a refusal.
        decisions = (
            ReapDecision(_candidate(), ReapAction.DELETE, ReapReason.ELIGIBLE),
        )
        forged = ReapPlan(
            decisions=decisions,
            max_deletes=5,
            refusal=ReapReason.LIVENESS_SOURCE_FAILED,
        )
        result = execute_reap_plan(forged, recorder, apply=True)
        self.assertEqual(recorder.calls, [])
        self.assertEqual(result.counts(), {"skipped_cap": 1})

    def test_invalid_arguments(self) -> None:
        with self.assertRaises(TypeError):
            execute_reap_plan([_candidate()], _Recorder())
        with self.assertRaises(ValueError):
            execute_reap_plan(self._three(), _Recorder(), max_deletes=True)

    def test_interrupt_is_not_swallowed(self) -> None:
        def interrupt(_candidate: ReapCandidate) -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            execute_reap_plan(self._three(), interrupt, apply=True)


if __name__ == "__main__":
    unittest.main()
