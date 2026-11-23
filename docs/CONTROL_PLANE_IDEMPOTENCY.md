# Hosted execution idempotency

`POST /v1/sandboxes/{id}/execute` accepts an `Idempotency-Key` header for
code and command requests. The hosted service uses the corresponding
`request_id` argument. This does not change direct SDK execution or response
formats. Without a key, each call gets a new request ID and is independent.

## Request identity and replay

Keys are scoped to the authenticated organization and sandbox. The same key
can be used independently in another sandbox or organization. Authentication,
execution scope, and sandbox ownership are checked before delivering a cached
result; a cache hit does not bypass authorization.

New code/command executions persist a keyed HMAC-SHA-256 digest of canonical,
sorted-key JSON containing an `execute` operation discriminator and the `code`,
`language`, `command`, and `args` fields, including their defaults. Raw code and arguments are not stored
in the fingerprint column or separate request payload columns. The digest uses
the store's configured key pepper: keep that pepper stable across restarts and
service instances. Rotating it changes request verification, so even a matching
payload for a retained key will conflict. This is not a confidentiality
guarantee or encryption. Existing result storage still contains stdout and
stderr, which may themselves contain sensitive data.

Use the same payload when retrying. Changing code, language, command, argument
values or order, or switching between code and command conflicts. Fields are
compared conservatively, even if unused by the chosen execution mode; omitted
arguments (`null`) and an explicit empty argument list are distinct. Agent
execution uses its existing fingerprint format and shares the scoped key
namespace: an agent request cannot reuse a code/command execution key, or vice
versa.

A matching completed execution returns its persisted response, including the
original request ID and usage, without invoking a runtime, creating a provider
resource, estimating execution cost, or adding usage/ledger entries. This also
works with a freshly opened store after a control-plane restart and after the
sandbox is terminated. A new key still requires a running, attached runtime.

## Reservation and uncertain outcomes

The durable unique key on `(organization_id, sandbox_id, request_id)` reserves
an execution before the provider call. A cache miss is not permission to run:
the reservation must also succeed. Concurrent control-plane instances using
the same SQLite database cannot both execute or charge the same key. A retry
that races with completion either receives the saved result or a conflict; it
does not wait for completion.

A running, interrupted, or failed execution without a saved completed result
returns `ConflictError` (`409` over HTTP), never an automatic rerun. In
particular, a process crash after an external side effect or accounting but
before response persistence leaves an ambiguous reservation. This is
at-most-once dispatch per retained key, not an exactly-once guarantee for
external side effects or durable response delivery. Inspect the sandbox and
verify that the prior run has stopped before deliberately submitting a new
key; doing so may repeat external work.

## Upgrade compatibility

Historical execution rows with a `NULL` request fingerprint fail closed for
code/command retries, including rows with saved completed responses. The
service cannot verify their original payload, so it returns `409` over HTTP
explaining that the prior payload cannot be verified. It neither replays nor
reruns these rows, and does not invent or backfill a fingerprint. Existing
agent requests retain their fingerprint comparison and agent-specific conflict
behavior.

This behavior uses the existing execution fingerprint column and unique key;
it requires no schema version change or historical data rewrite. Guarantees
depend on retaining execution records in the same durable database, not on
process-local locks. There is no provider-side recovery, cleanup, reconciliation,
or automatic retry enabled by this behavior.
