"""Decide which listed provider resources are safe to delete, then delete them.

Reconciliation observes, the planner here decides, and the executor deletes.
``plan_reap`` is pure: it reads a listing, the caller's facts about what is
still in use and a clock, and returns one decision per resource with a
machine-readable reason. ``execute_reap_plan`` applies a plan through a
delete callable the caller supplies. Neither knows a provider, and nothing
here is wired into the control-plane watchdog.

Every guard fails closed: missing, malformed, contradictory or unknown data
keeps the resource. A leaked resource costs money until the next run; a
wrongly deleted one cannot be brought back.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from types import MappingProxyType

from bespokelabs.sandbox.exceptions import ErrorOutcome, SandboxNotFoundError
from bespokelabs.sandbox.types import SandboxListing

DEFAULT_MIN_AGE = timedelta(hours=1)
DEFAULT_MAX_DELETES = 25
DEFAULT_MIN_REFERENCES = 1
DEFAULT_MAX_UNREFERENCED_FRACTION = 0.5

_ERROR_TEXT_LIMIT = 200


class Liveness(str, Enum):
    """Whether something still uses a resource. Only ``NOT_IN_USE`` permits deletion."""

    IN_USE = "in_use"
    NOT_IN_USE = "not_in_use"
    UNKNOWN = "unknown"


class ReapAction(str, Enum):
    DELETE = "delete"
    KEEP = "keep"


class ReapReason(str, Enum):
    """Why a resource was kept, or ``ELIGIBLE`` when every guard passed."""

    ELIGIBLE = "eligible"
    INVALID_ID = "invalid_id"
    DUPLICATE_LISTING = "duplicate_listing"
    PROTECTED = "protected"
    NOT_OWNED = "not_owned"
    OWNERSHIP_UNKNOWN = "ownership_unknown"
    STATE_UNKNOWN = "state_unknown"
    STATE_NOT_DELETABLE = "state_not_deletable"
    AGE_UNKNOWN = "age_unknown"
    TOO_YOUNG = "too_young"
    IN_USE = "in_use"
    LIVENESS_UNKNOWN = "liveness_unknown"
    LIVENESS_SOURCE_FAILED = "liveness_source_failed"
    REFERENCE_SET_TOO_SMALL = "reference_set_too_small"
    UNREFERENCED_FRACTION_EXCEEDED = "unreferenced_fraction_exceeded"
    DELETE_CAP_REACHED = "delete_cap_reached"


class ReapOutcomeStatus(str, Enum):
    WOULD_DELETE = "would_delete"
    DELETED = "deleted"
    ALREADY_GONE = "already_gone"
    FAILED = "failed"
    SKIPPED_CAP = "skipped_cap"


_EMPTY_LABELS: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class ReapCandidate:
    """One listed provider resource: a sandbox, a snapshot, or anything alike.

    ``created_at`` must be timezone-aware to count as a known age.
    ``labels_complete`` is ``False`` when the listing could not read every
    label back; an ownership predicate is then not trusted with the resource.
    """

    resource_id: str
    created_at: datetime | None = None
    state: str | None = None
    labels: Mapping[str, str] = field(default_factory=lambda: _EMPTY_LABELS)
    labels_complete: bool = True

    # Its labels are a mapping, so a generated hash would raise on use.
    __hash__ = None  # type: ignore[assignment]

    @classmethod
    def from_listing(cls, listing: SandboxListing) -> ReapCandidate:
        """Build a candidate from a ``Sandbox.list_sandboxes`` entry."""
        return cls(
            resource_id=listing.provider_resource_id,
            created_at=listing.created_at,
            state=listing.state,
            labels=dict(listing.labels),
            labels_complete=listing.labels_complete,
        )


@dataclass(frozen=True)
class LivenessReport:
    """What the caller's source of truth says about which resources are in use.

    Build one with ``from_facts`` when the source answers per resource, with
    ``from_references`` when it is a complete set of the resources something
    still points at, and with ``failed`` when the source could not be read.
    A resource named as both in use and not in use counts as in use.
    """

    in_use: frozenset[str] = frozenset()
    not_in_use: frozenset[str] = frozenset()
    absent_means_unused: bool = False
    failure: str | None = None

    @classmethod
    def from_facts(
        cls, *, in_use: Iterable[str] = (), not_in_use: Iterable[str] = ()
    ) -> LivenessReport:
        """Build from per-resource answers. One named in neither set is unknown."""
        return cls(
            in_use=_id_set(in_use, "in_use"),
            not_in_use=_id_set(not_in_use, "not_in_use"),
        )

    @classmethod
    def from_references(cls, referenced: Iterable[str]) -> LivenessReport:
        """Build from a complete reference set: an omitted resource is unused.

        The caller must have read every page of the source. A truncated set
        makes live resources look unused, so ``plan_reap`` refuses to delete
        when the set is too small or too much of the listing is missing from it.
        """
        return cls(
            in_use=_id_set(referenced, "referenced"), absent_means_unused=True
        )

    @classmethod
    def failed(cls, reason: str = "liveness source failed") -> LivenessReport:
        """Report an unreadable source. A plan made from this deletes nothing."""
        return cls(failure=str(reason) or "liveness source failed")

    def liveness(self, resource_id: str) -> Liveness:
        if self.failure is not None:
            return Liveness.UNKNOWN
        if resource_id in self.in_use:
            return Liveness.IN_USE
        if self.absent_means_unused or resource_id in self.not_in_use:
            return Liveness.NOT_IN_USE
        return Liveness.UNKNOWN


@dataclass(frozen=True)
class ReapLimits:
    """Limits for one planning run. Each default is the conservative choice.

    ``min_age`` protects a resource that is still being created or was not yet
    recorded as in use. ``max_deletes`` bounds the damage of one wrong run.
    ``min_references`` and ``max_unreferenced_fraction`` apply only to a report
    built with ``LivenessReport.from_references``.
    """

    min_age: timedelta = DEFAULT_MIN_AGE
    max_deletes: int = DEFAULT_MAX_DELETES
    min_references: int = DEFAULT_MIN_REFERENCES
    max_unreferenced_fraction: float = DEFAULT_MAX_UNREFERENCED_FRACTION

    def __post_init__(self) -> None:
        if not isinstance(self.min_age, timedelta) or self.min_age < timedelta(
            0
        ):
            raise ValueError("min_age must be a non-negative timedelta")
        for name in ("max_deletes", "min_references"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must not be negative")
        fraction = self.max_unreferenced_fraction
        if (
            isinstance(fraction, bool)
            or not isinstance(fraction, (int, float))
            or not 0 <= fraction <= 1
        ):
            raise ValueError("max_unreferenced_fraction must be within 0..1")


@dataclass(frozen=True)
class ReapDecision:
    candidate: ReapCandidate
    action: ReapAction
    reason: ReapReason

    __hash__ = None  # type: ignore[assignment]


@dataclass(frozen=True)
class ReapPlan:
    """One decision per listed resource, in listing order.

    ``refusal`` is set when a whole-run guard turned every deletion into a
    keep; the affected decisions carry the same reason.
    """

    decisions: tuple[ReapDecision, ...]
    max_deletes: int
    refusal: ReapReason | None = None
    refusal_detail: str | None = None

    __hash__ = None  # type: ignore[assignment]

    @property
    def deletes(self) -> tuple[ReapCandidate, ...]:
        """Resources to delete, oldest first."""
        chosen = [
            decision.candidate
            for decision in self.decisions
            if decision.action is ReapAction.DELETE
        ]
        return tuple(sorted(chosen, key=_oldest_first))

    @property
    def kept(self) -> tuple[ReapDecision, ...]:
        return tuple(
            decision
            for decision in self.decisions
            if decision.action is ReapAction.KEEP
        )

    def counts(self) -> dict[str, int]:
        """Count resources per reason value."""
        return dict(
            Counter(decision.reason.value for decision in self.decisions)
        )


@dataclass(frozen=True)
class ReapOutcome:
    candidate: ReapCandidate
    status: ReapOutcomeStatus
    error: str | None = None

    __hash__ = None  # type: ignore[assignment]


@dataclass(frozen=True)
class ReapResult:
    """What ``execute_reap_plan`` did with each planned deletion, plus the keeps."""

    applied: bool
    outcomes: tuple[ReapOutcome, ...]
    kept: tuple[ReapDecision, ...]
    refusal: ReapReason | None = None

    __hash__ = None  # type: ignore[assignment]

    @property
    def failed(self) -> tuple[ReapOutcome, ...]:
        return tuple(
            outcome
            for outcome in self.outcomes
            if outcome.status is ReapOutcomeStatus.FAILED
        )

    def counts(self) -> dict[str, int]:
        """Count planned deletions per outcome status value."""
        return dict(Counter(outcome.status.value for outcome in self.outcomes))


def plan_reap(
    candidates: Iterable[ReapCandidate | SandboxListing],
    liveness: LivenessReport,
    *,
    now: datetime,
    required_labels: Mapping[str, str] | None = None,
    is_owned: Callable[[ReapCandidate], bool] | None = None,
    protected_ids: Iterable[str] = (),
    deletable_states: Iterable[str] | None = None,
    limits: ReapLimits | None = None,
) -> ReapPlan:
    """Decide, without any I/O, which listed resources are safe to delete.

    A resource is deleted only when all of these hold:

    * its id is a non-empty string listed exactly once and not in
      ``protected_ids``;
    * it is provably owned: it carries every one of ``required_labels`` and
      ``is_owned`` returns ``True`` for it (at least one of the two must be
      given; an ``is_owned`` that raises, or labels that could not be read
      back in full, keep the resource);
    * its state is one of ``deletable_states``, when that is given;
    * its ``created_at`` is timezone-aware and at least ``limits.min_age`` old;
    * ``liveness`` says it is not in use;
    * no whole-run guard refused, and it is among the ``limits.max_deletes``
      oldest eligible resources.

    A failed liveness source, or a reference set that is too small or leaves
    too much of the owned listing unreferenced, refuses the run: the plan then
    deletes nothing. ``now`` must be timezone-aware. A ``SandboxListing`` is
    accepted in place of a candidate.
    """
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    if not isinstance(liveness, LivenessReport):
        raise TypeError("liveness must be a LivenessReport")
    limits = limits if limits is not None else ReapLimits()
    wanted = _ownership_labels(required_labels)
    if not wanted and is_owned is None:
        raise ValueError(
            "plan_reap needs required_labels or is_owned to prove ownership"
        )
    protected = _id_set(protected_ids, "protected_ids")
    states = _state_set(deletable_states)

    listed = [
        ReapCandidate.from_listing(candidate)
        if isinstance(candidate, SandboxListing)
        else candidate
        for candidate in candidates
    ]
    seen = Counter(
        candidate.resource_id
        for candidate in listed
        if _valid_id(candidate.resource_id)
    )
    decided: list[tuple[ReapCandidate, ReapReason]] = []
    owned = 0
    unreferenced = 0
    for candidate in listed:
        reason = _identity_reason(candidate, seen, protected)
        if reason is None:
            reason = _ownership_reason(candidate, wanted, is_owned)
            if reason is None:
                owned += 1
                if (
                    liveness.liveness(candidate.resource_id)
                    is Liveness.NOT_IN_USE
                ):
                    unreferenced += 1
        if reason is None:
            reason = _state_reason(candidate, states)
        if reason is None:
            reason = _age_reason(candidate, now, limits.min_age)
        if reason is None:
            reason = _liveness_reason(candidate, liveness)
        decided.append((candidate, reason or ReapReason.ELIGIBLE))

    refusal, detail = _run_refusal(liveness, limits, owned, unreferenced)
    eligible = sorted(
        (
            candidate
            for candidate, reason in decided
            if reason is ReapReason.ELIGIBLE
        ),
        key=_oldest_first,
    )
    # Ids are unique among eligible candidates: a duplicate is never eligible.
    chosen = {
        candidate.resource_id for candidate in eligible[: limits.max_deletes]
    }
    decisions = []
    for candidate, reason in decided:
        if reason is not ReapReason.ELIGIBLE:
            decisions.append(ReapDecision(candidate, ReapAction.KEEP, reason))
        elif refusal is not None:
            decisions.append(ReapDecision(candidate, ReapAction.KEEP, refusal))
        elif candidate.resource_id in chosen:
            decisions.append(ReapDecision(candidate, ReapAction.DELETE, reason))
        else:
            decisions.append(
                ReapDecision(
                    candidate, ReapAction.KEEP, ReapReason.DELETE_CAP_REACHED
                )
            )
    return ReapPlan(
        decisions=tuple(decisions),
        max_deletes=limits.max_deletes,
        refusal=refusal,
        refusal_detail=detail,
    )


def execute_reap_plan(
    plan: ReapPlan,
    delete: Callable[[ReapCandidate], object],
    *,
    apply: bool = False,
    max_deletes: int | None = None,
    is_already_gone: Callable[[Exception], bool] | None = None,
) -> ReapResult:
    """Apply *plan* through *delete* and report what happened to each resource.

    Nothing is deleted unless ``apply`` is ``True``; a dry run reports
    ``would_delete`` and never calls *delete*. At most ``plan.max_deletes``
    deletions are attempted (``max_deletes`` can lower that, never raise it);
    failed attempts count, and the rest are reported as ``skipped_cap``.

    A *delete* that raises ``SandboxNotFoundError`` with a proven-failed
    outcome, or an exception ``is_already_gone`` accepts, means the resource
    is already gone and counts as success. Any other ``Exception`` is recorded
    as ``failed`` for that resource and the run continues, so one failure does
    not strand the rest. Only process-level interruptions such as
    ``KeyboardInterrupt`` propagate.
    """
    if not isinstance(plan, ReapPlan):
        raise TypeError("plan must be a ReapPlan")
    cap = plan.max_deletes
    if max_deletes is not None:
        if isinstance(max_deletes, bool) or not isinstance(max_deletes, int):
            raise ValueError("max_deletes must be an integer")
        cap = min(cap, max_deletes)
    cap = max(cap, 0)
    # A plan deserialized or built by hand could carry deletions next to a
    # refusal; a refused run never deletes.
    if plan.refusal is not None:
        cap = 0

    outcomes: list[ReapOutcome] = []
    for attempts, candidate in enumerate(plan.deletes):
        if attempts >= cap:
            outcomes.append(
                ReapOutcome(candidate, ReapOutcomeStatus.SKIPPED_CAP)
            )
        elif not apply:
            outcomes.append(
                ReapOutcome(candidate, ReapOutcomeStatus.WOULD_DELETE)
            )
        else:
            outcomes.append(_delete_one(candidate, delete, is_already_gone))
    return ReapResult(
        applied=bool(apply),
        outcomes=tuple(outcomes),
        kept=plan.kept,
        refusal=plan.refusal,
    )


def _delete_one(
    candidate: ReapCandidate,
    delete: Callable[[ReapCandidate], object],
    is_already_gone: Callable[[Exception], bool] | None,
) -> ReapOutcome:
    try:
        delete(candidate)
    except Exception as error:
        if _already_gone(error, is_already_gone):
            return ReapOutcome(candidate, ReapOutcomeStatus.ALREADY_GONE)
        return ReapOutcome(
            candidate, ReapOutcomeStatus.FAILED, error=_error_text(error)
        )
    return ReapOutcome(candidate, ReapOutcomeStatus.DELETED)


def _already_gone(
    error: Exception, is_already_gone: Callable[[Exception], bool] | None
) -> bool:
    # A not-found whose outcome is unknown does not prove the resource is gone.
    if (
        isinstance(error, SandboxNotFoundError)
        and error.outcome is ErrorOutcome.FAILED
    ):
        return True
    if is_already_gone is None:
        return False
    try:
        return is_already_gone(error) is True
    except Exception:
        return False


def _error_text(error: Exception) -> str:
    try:
        message = str(error)
    except Exception:
        message = ""
    text = type(error).__name__ + (f": {message}" if message else "")
    return text[:_ERROR_TEXT_LIMIT]


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _id_set(values: Iterable[str], name: str) -> frozenset[str]:
    # A bare string would be read as a set of single characters.
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be a collection of ids, not a string")
    return frozenset(value for value in values if _valid_id(value))


def _ownership_labels(labels: Mapping[str, str] | None) -> dict[str, str]:
    wanted = {str(key): str(value) for key, value in dict(labels or {}).items()}
    if any(not key or not value for key, value in wanted.items()):
        raise ValueError(
            "required_labels must not contain empty keys or values"
        )
    return wanted


def _state_set(states: Iterable[str] | None) -> frozenset[str] | None:
    if states is None:
        return None
    if isinstance(states, (str, bytes)):
        raise TypeError("deletable_states must be a collection of states")
    return frozenset(
        state.strip().lower() for state in states if isinstance(state, str)
    )


def _oldest_first(candidate: ReapCandidate) -> tuple[float, str]:
    created = candidate.created_at
    try:
        stamp = created.timestamp() if created is not None else float("inf")
    except (OverflowError, OSError, ValueError):
        stamp = float("inf")
    return (stamp, str(candidate.resource_id))


def _identity_reason(
    candidate: ReapCandidate, seen: Counter, protected: frozenset[str]
) -> ReapReason | None:
    resource_id = candidate.resource_id
    if not _valid_id(resource_id):
        return ReapReason.INVALID_ID
    # Two entries for one id cannot both be right about its age and state.
    if seen[resource_id] > 1:
        return ReapReason.DUPLICATE_LISTING
    if resource_id in protected:
        return ReapReason.PROTECTED
    return None


def _ownership_reason(
    candidate: ReapCandidate,
    wanted: Mapping[str, str],
    is_owned: Callable[[ReapCandidate], bool] | None,
) -> ReapReason | None:
    labels = candidate.labels
    if not isinstance(labels, Mapping):
        return ReapReason.OWNERSHIP_UNKNOWN
    if any(labels.get(key) != value for key, value in wanted.items()):
        return ReapReason.NOT_OWNED
    if is_owned is None:
        return None
    # A predicate may reason from the absence of a label, which a partial
    # label set cannot show.
    if candidate.labels_complete is not True:
        return ReapReason.OWNERSHIP_UNKNOWN
    try:
        verdict = is_owned(candidate)
    except Exception:
        return ReapReason.OWNERSHIP_UNKNOWN
    return None if verdict is True else ReapReason.NOT_OWNED


def _state_reason(
    candidate: ReapCandidate, states: frozenset[str] | None
) -> ReapReason | None:
    if states is None:
        return None
    state = candidate.state
    if not isinstance(state, str) or not state.strip():
        return ReapReason.STATE_UNKNOWN
    if state.strip().lower() not in states:
        return ReapReason.STATE_NOT_DELETABLE
    return None


def _age_reason(
    candidate: ReapCandidate, now: datetime, min_age: timedelta
) -> ReapReason | None:
    created = candidate.created_at
    # A naive time could be off by a whole UTC offset: not a known age.
    if not isinstance(created, datetime) or created.utcoffset() is None:
        return ReapReason.AGE_UNKNOWN
    try:
        age = now - created
    except OverflowError:
        return ReapReason.AGE_UNKNOWN
    # A creation time in the future is younger than any minimum, including zero.
    if age < timedelta(0) or age < min_age:
        return ReapReason.TOO_YOUNG
    return None


def _liveness_reason(
    candidate: ReapCandidate, liveness: LivenessReport
) -> ReapReason | None:
    if liveness.failure is not None:
        return ReapReason.LIVENESS_SOURCE_FAILED
    verdict = liveness.liveness(candidate.resource_id)
    if verdict is Liveness.IN_USE:
        return ReapReason.IN_USE
    if verdict is Liveness.NOT_IN_USE:
        return None
    return ReapReason.LIVENESS_UNKNOWN


def _run_refusal(
    liveness: LivenessReport, limits: ReapLimits, owned: int, unreferenced: int
) -> tuple[ReapReason | None, str | None]:
    """Whole-run guards. A refusal turns every deletion in the plan into a keep."""
    if liveness.failure is not None:
        return ReapReason.LIVENESS_SOURCE_FAILED, liveness.failure
    if not liveness.absent_means_unused:
        return None, None
    # Only a set that is too small is dangerous: an empty or truncated
    # reference set marks every listed resource as unused.
    if len(liveness.in_use) < limits.min_references:
        return (
            ReapReason.REFERENCE_SET_TOO_SMALL,
            f"reference set has {len(liveness.in_use)} entries, "
            f"minimum is {limits.min_references}",
        )
    if owned and unreferenced / owned > limits.max_unreferenced_fraction:
        return (
            ReapReason.UNREFERENCED_FRACTION_EXCEEDED,
            f"{unreferenced} of {owned} owned resources are unreferenced, "
            f"above the {limits.max_unreferenced_fraction:.0%} ceiling",
        )
    return None, None
