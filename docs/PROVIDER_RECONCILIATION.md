# Conservative provider reconciliation

Reconciliation is tenant-scoped and observation-only: it never deletes a
resource. An inventory can be incomplete. Omission, permission failures,
timeouts, malformed observations, and ambiguous duplicates retain the active
resource and local billing. Authoritative absence requires the optional direct
lookup capability, scoped to the requested tenant, backend, and resource.
Adapters must not translate permission failures or incomplete searches into
absence. Adapters without explicit scope fields remain responsible for tenant
filtering.

## Evidence and timestamps

Provider timestamps must contain a timezone offset and convert safely to UTC.
Naive timestamps, invalid dates or offsets, and UTC conversion overflows are
rejected. At most 30 seconds of future clock skew is tolerated, clamped to the
reconciliation request's start time. Larger future timestamps are rejected;
neither estimated billing nor the provider watermark may extend into the future.
The store applies the same validation when called directly.

Duplicates are compared only after timestamp validation and UTC normalization.
The latest valid evidence wins, except that a malformed scoped sibling retains
uncertainty for that resource. Equal-time contradictions in status, cost, or
currency are unknown regardless of listing order. An optional direct lookup can
resolve that uncertainty. Terminal evidence never wins a contradictory tie by
virtue of appearing last in a page. This tie rule also applies transactionally
across separate runs: persisted live evidence fences equal-time terminal evidence.
The reverse order cannot revive an already terminal resource; the contradiction
is retained for investigation and counted as both rejected and unknown, including
on replay. Neither order posts a cost adjustment for the contradictory snapshot.
Strictly newer terminal evidence can still finalize a resource retained as live.

Unknown attempts can be retained as diagnostic observations but never advance
the provider-evidence watermark. An older real provider snapshot can therefore
still be accepted after a newer unsuccessful local lookup. Missing lookups use
the request's start time, not its completion time; a concurrent newer positive
observation fences out the delayed missing result.

## Failed termination is not cloud closure

A termination RPC failure remains wire status `failed` (an operation error),
with an internal durable `resource_hold`. Neither `failed_at`, an error code,
nor a claimed cleanup message proves the cloud resource closed. Held resources
continue to count toward concurrent admission and accrue on the existing billing
ledger, including after restart. The hold grants no attachment, ownership,
cleanup, or deletion authority. Unknown allocation/publication failures are held
separately from confirmed no-allocation/deleted creation failures.

Only trusted, timestamp-validated terminal/absence evidence or a successful
existing termination path clears the hold. Clearing it, setting the actual
terminal boundary, and provider metering occur in one transaction. Live evidence
can extend a held window but cannot restore execution or clear the diagnostic
uncertainty count; conflicting equal-time,
malformed, future, stale, list-only missing, and unknown evidence cannot release
capacity. Late termination failures cannot overwrite confirmed terminal state or
reset a hold. The billing write-lock fence also covers readers that fetched an
uncertain failed row before its confirmed closure.

Migration 11 atomically backfills legacy failed rows with prior stop metadata,
explicit unknown-allocation metadata, or a known provider identity without retained
successful cleanup evidence, unless a durable `terminated_at` or `destroyed_at`
already establishes the end. `provider_observations` also contains rejected
conflict/stale diagnostics: neither its mere presence nor a timestamp equal to
`failed_at` proves a terminal transition applied. Migration never uses that table
to release a hold. Even an actually applied legacy terminal `failed` observation
can lack an end timestamp; where applied-vs-diagnostic evidence cannot be proved,
it is deliberately overheld until a fresh trusted scoped lookup/terminal snapshot.
Pruned evidence cannot weaken this policy. Historical failed creation
rows without stop/unknown-allocation markers retain their prior classification:
the migration cannot infer a lost provider identity or invent a second cleanup
protocol from an arbitrary error string. The offline migration fixture was captured
using the actual f845 base store, including both equal-time ordering cases,
retained lower/stale terminal diagnostics, and truly applied failures without end
proof. This correction to migration 11 runs on first upgrade from the pre-hold
schema; it does not retroactively rerun a version already stamped by earlier code.
Already posted charges/tails are not
silently refunded when delayed evidence arrives; an explicit authorized refund
protocol remains necessary.

## Billing and immutable observation identities

Provider cost is cumulative through its observation timestamp. A delayed total
is combined with the locally estimated tail beyond that timestamp, not with a
second copy of the already charged lifetime. Posted provider and customer
charges never decrease when a lower cumulative report arrives. The effective
charge is the maximum of the existing effective total and the new cumulative
report plus any uncovered accrued tail. Subsequent local accrual adds only new
time. Billing re-reads authoritative lifecycle state under its database write
lock, so a reader that fetched an active row before closure cannot extend elapsed
time, estimated tails, ledger intervals, or their timestamps beyond the terminal
boundary. A stale reader can still fill an unmetered portion of the closed window;
legitimate finalization and positive provider adjustments remain possible without
reopening elapsed time. This is intentionally **not a refund protocol**: legitimate
refunds need an explicit, separately authorized adjustment mechanism.

An observation identity is immutable for a resource's lifetime. Reusing it,
whether for exact replay or for a changed payload, does not update lifecycle,
watermark, or billing. Adapters must issue a new identity for a changed snapshot.
Uniqueness checks, evidence insertion, state changes, and billing writes share
one transaction, so an insertion failure cannot leave state ahead of its ledger.

## Terminal contradictions and health counts

Confirmed absence is terminal in this reconciliation path. Later listed positive
evidence is retained for investigation and counted as unapplied; it does not
blindly resurrect capacity or reopen billing. The terminal watermark and closed
billable window are preserved. No further direct lookups are made for terminal
records. Operators must investigate false absence, identity reuse, or resource
reappearance; automatic recovery and charging for a second lifetime are outside
this contract. Terminal listing tombstones remain non-authoritative.

Results retain the existing `updated`, `missing`, and `orphans` counts and add:

- `unknown`: tracked resources with uncertain evidence, including list-only omissions.
  A failed inventory request counts the active resources affected by that request.
- `rejected`: invalid or mis-scoped payloads, discarded stale/contradictory
  candidates, and observations not applied by the store. This includes exact
  replays, conflicting identity reuse, stale observations, and terminal evidence.
  These are diagnostic counts, not disjoint resource totals: one resource may
  contribute multiple rejected observations and also be unknown.

`status` is `partial_outage` when inventory retrieval fails or either diagnostic
count is nonzero, including harmless replay-only runs; otherwise it is `ok`.
Callers should inspect the counts rather than interpreting `updated == 0` as
successful reconciliation. These diagnostics preserve conservative failure visibility without an adapter SDK
dependency. Held failed resources remain eligible for reconciliation and unknown
counts, including held allocations without a recoverable provider identity.
