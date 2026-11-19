"""Provider reconciliation contracts independent of any provider SDK."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol

from bespokelabs.sandbox.exceptions import ErrorOutcome, SandboxNotFoundError

# datetime.fromisoformat normalizes out-of-range offset minutes/seconds on
# some Python versions. Require real ISO offset components before parsing.
_PROVIDER_OFFSET = re.compile(
    r"(?:Z|[+-](?:[01]\d|2[0-3])(?::?[0-5]\d)?(?::?[0-5]\d(?:[.,]\d+)?)?)$"
)

PROVIDER_STATUSES = frozenset(
    {"provisioning", "running", "stopping", "terminated", "failed", "unknown"}
)


@dataclass(frozen=True)
class ProviderObservation:
    """A tenant-filtered resource observation, not an inventory completeness claim.

    ``unknown`` never changes lifecycle state or supplies authoritative cost.
    ``missing`` is accepted only from a direct lookup, never from a listing.
    Optional scope fields are checked when supplied; legacy adapters remain
    responsible for filtering by the organization passed to them. Costs are
    cumulative through ``observed_at``, which must be timezone-aware.
    """

    observation_id: str
    provider_resource_id: str
    status: str
    observed_at: str
    provider_cost_usd: Decimal | None = None
    currency: str = "USD"
    organization_id: str | None = None
    backend: str | None = None

    def matches(self, organization_id: str, backend: str) -> bool:
        """Check explicit scope without breaking tenant-filtered legacy adapters."""
        return self.organization_id in {
            None,
            organization_id,
        } and self.backend in {
            None,
            backend,
        }


def provider_timestamp(value: str) -> datetime | None:
    """Parse untrusted provider time once, requiring an offset and safe UTC."""
    try:
        if not _PROVIDER_OFFSET.search(value):
            return None
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def normalize_observation(
    observation: object, *, now: datetime
) -> ProviderObservation | None:
    """Fail closed at the adapter boundary; never bill or fence in the future.

    Allow at most 30 seconds of provider clock skew, clamped to the request's
    start time. Larger skew, naive time, invalid offsets and UTC overflows are
    rejected. Callers treat rejection as uncertainty and retain local accrual.
    """
    if not isinstance(observation, ProviderObservation):
        return None
    if (
        not isinstance(observation.status, str)
        or observation.status.lower().strip()
        not in PROVIDER_STATUSES | {"missing"}
        or not isinstance(observation.observation_id, str)
        or not observation.observation_id
        or not isinstance(observation.provider_resource_id, str)
        or not observation.provider_resource_id
        or not isinstance(observation.currency, str)
        or not observation.currency
        or any(
            value is not None and not isinstance(value, str)
            for value in (observation.organization_id, observation.backend)
        )
    ):
        return None
    now = now.astimezone(timezone.utc)
    observed = provider_timestamp(observation.observed_at)
    if observed is None or observed - now > timedelta(seconds=30):
        return None
    cost = observation.provider_cost_usd
    try:
        if cost is not None:
            cost = Decimal(cost)
            if not cost.is_finite() or cost < 0:
                return None
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None
    status = observation.status.lower().strip()
    return replace(
        observation,
        status=status,
        observed_at=min(observed, now).isoformat(),
        provider_cost_usd=cost if status != "unknown" else None,
    )


class ProviderReconciler(Protocol):
    """List resources after applying the provider's tenant label/filter.

    Lists may be partial. Omission is never evidence of resource disappearance.
    """

    def list_resources(self, organization_id: str) -> list[ProviderObservation]:
        ...


class ProviderResourceLookup(Protocol):
    """Optional capability alongside ``ProviderReconciler`` (no SDK coupling).

    The configured backend and both arguments identify exactly one resource.
    ``None`` or ``SandboxNotFoundError`` MUST mean authoritative absence for
    that identity, not a permission error, incomplete search, or outage. Use an
    ``unknown`` observation or another exception when existence is uncertain.
    A returned observation must refer to the requested resource. Adapters may
    return ``missing`` with a provider observation timestamp for explicit absence.
    No reconciliation capability may delete resources.
    """

    def get_resource(
        self, organization_id: str, provider_resource_id: str
    ) -> ProviderObservation | None:
        ...


def lookup_resource(
    reconciler: ProviderReconciler,
    *,
    organization_id: str,
    backend: str,
    provider_resource_id: str,
    observed_at: str,
) -> object:
    """Normalize optional lookup failures; payload validation stays at the boundary.

    Synthetic timestamps are taken BEFORE the request, so a slow missing
    response cannot outrank a positive observation made while it was in flight.
    """
    status = "unknown"
    lookup = getattr(reconciler, "get_resource", None)
    if callable(lookup):
        try:
            result = lookup(organization_id, provider_resource_id)
            if result is None:
                status = "missing"
            else:
                # Let the caller validate and count malformed or mis-scoped
                # payloads, rather than hiding them inside synthetic unknown.
                return result
        except SandboxNotFoundError as error:
            # Explicit contradictory scope must not confirm this resource missing.
            context = error.context
            if (
                error.outcome is ErrorOutcome.FAILED
                and error.backend in {None, backend}
                and context.get("organization_id", organization_id)
                == organization_id
                and context.get("provider_resource_id", provider_resource_id)
                == provider_resource_id
            ):
                status = "missing"
        except Exception:
            pass
    return ProviderObservation(
        observation_id=f"lookup:{provider_resource_id}:{observed_at}:{status}",
        provider_resource_id=provider_resource_id,
        status=status,
        observed_at=observed_at,
        organization_id=organization_id,
        backend=backend,
    )
