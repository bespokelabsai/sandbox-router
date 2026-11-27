# Reaper planner

[Back to provider reconciliation](PROVIDER_RECONCILIATION.md)

`bespokelabs.sandbox.control_plane.reaper` is a small library for cleaning up
leaked provider resources (sandboxes, snapshots, or anything else a provider
can list). It splits the work into three steps with separate owners:

| Step | Owner | Does |
|---|---|---|
| Observe | [Reconciliation](PROVIDER_RECONCILIATION.md), `Sandbox.list_sandboxes` | Lists what the provider has. Never deletes. |
| Decide | `plan_reap` | Pure function: one keep/delete decision per resource. No I/O. |
| Delete | `execute_reap_plan` | Applies a plan through a delete callable you supply. |

It is a library only. The control-plane watchdog does not call it, and it
changes no existing cleanup behaviour. It contains no provider code: you bring
the listing, the facts about what is still in use, and the delete call.

```python
from datetime import datetime, timezone

from bespokelabs.sandbox.control_plane.reaper import (
    LivenessReport,
    execute_reap_plan,
    plan_reap,
)

labels = {"owner": "my-service"}
listings = client.list_sandboxes(labels)  # raises rather than return a partial list

try:
    liveness = LivenessReport.from_facts(
        in_use=ids_still_running(), not_in_use=ids_known_finished()
    )
except Exception as error:
    liveness = LivenessReport.failed(type(error).__name__)

plan = plan_reap(
    listings,
    liveness,
    now=datetime.now(timezone.utc),
    required_labels=labels,
)
print(plan.counts())  # e.g. {"eligible": 2, "too_young": 5, "in_use": 9}

result = execute_reap_plan(plan, delete_one)  # dry run
result = execute_reap_plan(plan, delete_one, apply=True)
```

## The rule: unknown means keep

A leaked resource costs money until the next run. A wrongly deleted one cannot
be brought back. So every guard fails closed: data that is missing, malformed,
contradictory or unknown keeps the resource, and each kept resource carries a
machine-readable reason saying which guard kept it.

## Telling the planner what is in use

`LivenessReport` carries the caller's facts. It has three constructors:

- `LivenessReport.from_facts(in_use=..., not_in_use=...)` when the source
  answers per resource. A resource named in neither set is **unknown** and is
  kept. A resource named in both counts as in use.
- `LivenessReport.from_references(referenced)` when the source is a complete
  set of the resources something still points at. A resource the set omits is
  treated as not in use, so the set must cover every page of the source. Two
  extra guards (below) refuse the run when the set looks truncated.
- `LivenessReport.failed(reason)` when the source could not be read. The plan
  then deletes nothing.

Do not turn a failed or partial read into an empty `from_facts` or
`from_references` report. Report the failure.

## Guards in `plan_reap`

Guards run in this order; the first one that fails gives the reason.

| Guard | Keeps the resource when | Reason | Default |
|---|---|---|---|
| Identity | the id is not a non-empty string | `invalid_id` | always on |
| Duplicates | the id is listed more than once | `duplicate_listing` | always on |
| Protected ids | the id is in `protected_ids` | `protected` | none protected |
| Ownership | a required label is missing or different, or `is_owned` does not return `True` | `not_owned` | one of `required_labels` / `is_owned` is required |
| Ownership | `is_owned` raises, the labels are unreadable, or the listing has `labels_complete=False` | `ownership_unknown` | always on |
| State | `deletable_states` is given and the state is missing | `state_unknown` | no state check |
| State | `deletable_states` is given and the state is not in it | `state_not_deletable` | no state check |
| Age | `created_at` is missing or has no timezone | `age_unknown` | always on |
| Minimum age | the resource is younger than `min_age`, or created in the future | `too_young` | 1 hour |
| Liveness | the report says in use | `in_use` | always on |
| Liveness | the report does not mention the resource | `liveness_unknown` | always on |
| Delete cap | more than `max_deletes` resources are eligible; the oldest are deleted | `delete_cap_reached` | 25 per run |

`plan_reap` raises `ValueError` when neither `required_labels` nor `is_owned`
is given: it never plans against resources nobody has proven are theirs.
`deletable_states` is an allowlist matched case-insensitively, so a state the
provider adds later is kept by default.

The minimum age matters even when liveness is known. A resource is often
created before it is recorded as in use, so a new one can look unused for a
short time.

### Whole-run refusals

Some conditions mean the facts cannot be trusted at all. The plan then deletes
nothing: `plan.refusal` names the reason, `plan.refusal_detail` explains it,
and every resource that would have been deleted is kept with that reason.

| Refusal | When | Default |
|---|---|---|
| `liveness_source_failed` | the report was built with `LivenessReport.failed` | always on |
| `reference_set_too_small` | a `from_references` set has fewer than `min_references` entries | 1 |
| `unreferenced_fraction_exceeded` | more than `max_unreferenced_fraction` of the owned listing is missing from a `from_references` set | 0.5 |

An empty reference set marks every listed resource as unused, and a mostly
unreferenced listing is what a truncated reference set looks like. The fraction
counts every owned resource, including ones too young to delete, so young
resources cannot hide a truncated set. Raise `max_unreferenced_fraction` (up to
`1.0`) only when a mostly leaked listing is the expected case.

The limits live on `ReapLimits(min_age=..., max_deletes=...,
min_references=..., max_unreferenced_fraction=...)`.

## Guards in `execute_reap_plan`

- **Dry run by default.** Without `apply=True` the delete callable is never
  called and each planned deletion is reported as `would_delete`.
- **Cap.** At most `plan.max_deletes` deletions are attempted. The
  `max_deletes` argument can lower that but not raise it. Failed attempts
  count toward the cap; the rest are reported as `skipped_cap`.
- **Refused plans are not executed**, even if a hand-built plan carries
  deletions next to a refusal.
- **Already gone is success.** A delete that raises `SandboxNotFoundError`
  with a proven-failed outcome, or an exception that `is_already_gone`
  accepts, is reported as `already_gone`. Running the same plan twice is safe.
- **One failure does not strand the rest.** Any other `Exception` is recorded
  as `failed` for that resource, with the exception type and a bounded
  message, and the run continues. The executor returns a `ReapResult` with one
  outcome per planned deletion plus every kept decision. Only process-level
  interruptions such as `KeyboardInterrupt` propagate.

## What the library does not do

- It does not list resources, read your liveness source, or call a provider.
- It does not check that a plan is still fresh. Plan and execute in the same
  run; a stored plan describes the past.
- It does not record anything in the control-plane store or affect billing.
  Deleting a tracked sandbox this way still needs reconciliation to observe
  its absence.
