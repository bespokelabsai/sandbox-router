# Durable runtime attachments

`control_plane.attachments` provides opt-in persistence and reconnection hooks,
wired into `ControlPlane` creation, execution, agents, network updates and an
explicit hosted-ID attachment endpoint. With no attachment configuration,
existing creation and restart behavior remains unchanged. Direct SDK session
state remains a separate interface. There is no automatic provider discovery.

## Trust boundary

An `AttachmentDescriptor` is a separate, strict document, not a serialized
`SandboxSessionState`. Schema version 1 permits exactly these fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Required integer `1`, not a boolean |
| `backend` | `modal`, `e2b`, `daytona`, or `tensorlake` |
| `binding_id` | Server-configured binding identity |
| `binding_version` | Positive integer server configuration version |
| `provider_resource_id` | Bounded opaque provider resource ID |
| `remote_workdir` | Optional bounded absolute remote directory; Daytona and Tensorlake only |
| `incarnation` | Optional bounded opaque application incarnation marker |

Optional fields may be omitted when decoding; canonical encoding includes them
as `null` when absent. IDs contain ASCII letters, digits, underscores and
hyphens, start with a letter or digit, and are at most 255 characters. Working
directories are at most 512 characters, with conservative path characters and
no traversal, empty intermediate components, shell syntax, or control characters.
Root `/` and a trailing slash are valid and preserved; no local path resolution
or normalization is performed. These are remote session directories, never local
filesystem paths.
Unknown fields, duplicate JSON keys, endpoints, credentials, arbitrary SDK
state, environment maps, SSH keys and host-path fields are rejected, not filtered.
Validation errors do not echo rejected keys or values.

`SDKAttachmentAdapter.describe` reads only the trusted runtime's public
`backend_name` and `provider_resource_id`. It does **not** call `session_state()`.
The creator supplies an optional remote working directory and incarnation from
trusted creation context, not from client-provided session state.

`ProviderBinding` belongs to server configuration. It contains the provider
credentials, a stable account-scope identifier, permitted organization IDs and
strict backend-specific settings. Credentials and settings are immutable copies
and excluded from its representation. They are never copied into attachment
rows. The ownership database also stores shared binding identity/version,
backend, account-scope label, a digest of nonsecret routing settings and allowed
organization IDs, and active/revoked state. It stores neither credentials nor
credential hashes, and does not persist raw routing settings. Do not construct bindings from request data or database
session-state payloads. Choose the binding when creating the original runtime
using the same provider account; a resource ID alone cannot prove its account.

`ProviderBinding.organization_ids` accepts only a nonempty list, tuple, set or
frozenset of bounded strict-string organization IDs. Bare strings, bytes,
generators, null and mixed-type containers are rejected before membership checks.
Routing URLs must be explicit HTTPS endpoints without userinfo, query strings,
fragments or whitespace; routing settings must contain no secrets.

`BindingRegistry` checks organization membership, backend, identity and version
on every resolution, including cache hits. `ControlPlane` automatically attaches
its registry to shared version authority in the **same SQLite ownership database**
when attachments are opted in. A revocation committed by one worker rejects
subsequent resolution by other workers, even with retained handles. It cannot
withdraw a handle already returned or interrupt an in-flight provider call.
Standalone registries without `attach_repository(repository)` are explicitly
process-local; they do not offer cross-worker or restart-safe revocation.

Shared authority retains revocation tombstones and the highest version across
restarts. Separate workers may start with the same active version and identical
nonsecret metadata; conflicting metadata at that version is rejected. Neither an
equal nor a lower version can reactivate a revoked binding: explicit configuration
with a higher version is required. Credentials remain solely in worker memory.
When credentials, account, membership or settings change, operators must bump the
version. Credential-only changes cannot be detected by persisted metadata because
no credential hash is stored. Existing descriptors deliberately fail closed until
explicitly republished with the new binding and verified lifecycle authority.

## Provider reuse

The default adapter constructs a binding-authenticated backend shim and calls
the existing `SandboxClient.resume(SandboxSessionState(...))` wrapper. Only
`sandbox_id` and, where supported, `workdir` are reconstructed. No raw SDK state
is deserialized. Optional SDK imports occur only on reconnect.

| Backend | Existing-resource operation | Binding credentials/settings |
| --- | --- | --- |
| Modal | Public `Client.from_credentials(token_id=..., token_secret=...)`, `Sandbox.from_id(client=...)` | Explicit tokens; binding and effective SDK `server_url` must both equal the reviewed SDK default |
| E2B | Stock hosted reconnect **disabled**, before SDK calls | `api_key`; descriptor support does not imply reconnect capability |
| Daytona | `Daytona(config).get(id, request_timeout=30)` | `api_key`; required `api_url`, `target` |
| Tensorlake | `SandboxClient(...).connect(id, proxy_url=...)`; read-only ID verification | `api_key`; required `api_url`, `proxy_url`, `organization_id`, `project_id`, `namespace` |

Credentials are explicitly passed, and account routing is constrained by
server-owned bindings, never selected from a descriptor or working-directory
`.env` file. Application adapter code does not write `os.environ`, SDK
configuration, or process cwd. This is not a guarantee that upstream SDK public
cleanup leaves ambient SDK state unchanged: the Modal floor has a process-wide
singleton side effect described below. Modal uses only the
public `Client.from_credentials` factory. Because this factory eagerly opens a
connection using effective SDK routing, both the binding's `server_url` and
`modal.config.config.get("server_url")` must equal the reviewed SDK default
**before** the factory receives explicit `token_id` and `token_secret`. Custom
environment or profile routing fails closed, with no factory/RPC call. Modal
0.60.15 has no exported default constant; its reviewed literal is
`https://api.modal.com`. Modal 1.6.4 exports `DEFAULT_SERVER_URL`, whose exact
value is `https://api.modal.com,https://api.modal2.com`. Unknown versions or
unavailable effective routing fail closed; there is no private client constructor
or configuration override fallback.

The hosted Modal connector owns only the explicit client returned by that public
factory. It retains that already-open client with the verified `ModalSession`;
it does not re-enter the client context. Lookup or session-ID verification
failure releases only that transport via public `Client.__exit__`, without
terminating the unverified provider resource. Wrapper construction or stock
adapter verification failure after lookup likewise releases only the owned
transport. A successful hosted session keeps the client open until an explicit
`destroy()` (including service shutdown) successfully calls SDK termination,
then consumes client ownership and invokes public context exit once. A client
exit failure cannot turn confirmed provider termination into an unknown outcome
or expose SDK exception text. On SDK termination failure, the hosted-owned path
instead raises a sanitized `SandboxError` with `outcome=UNKNOWN`, retaining the
session and client for an explicitly authorized retry; it must not report the
resource destroyed. Hosted strict mode is fixed at session construction, not
inferred from whether its ownership token has since been consumed. After a local
transport-only release, `destroy()` refuses with sanitized `outcome=UNKNOWN` and
makes no RPC; it cannot silently fall into the native exception-swallowing path.
Only a previously confirmed termination remains idempotently successful after
release. This does not add API retryability for discarded handles.
Native/direct `ModalSession(sandbox=...)` defaults to unowned,
retains its legacy best-effort destroy semantics, and never closes an implicit,
shared, or otherwise externally supplied SDK client. `RuntimeSession` and the
global SDK compatibility minimum are unchanged. That application-level behavior
preservation does not guarantee co-resident native SDK state remains unchanged
when a hosted floor client exits.

**Unresolved Modal floor limitations — no overall security clearance:** Modal
0.60.15's public context exit closes the transport channel, but its retained SDK
shutdown coroutine keeps the client object and its explicit credential tuple
reachable until SDK shutdown/process exit. Failed lookups, rejected handles and
successfully destroyed hosted sessions all retain these references at that floor;
calling the coroutine merely "inert" understates the credential-retention effect.
The floor's public close path also unconditionally resets the SDK's process-wide
ambient environment-client singleton, even when closing an explicit factory
client. Thus co-resident native/ambient SDK behavior cannot be claimed unchanged.
Using the async public exit on the same SDK loop does not eliminate either
upstream limitation.

Modal 1.6.4's public exit unregisters its own hook and releases its
connection/cancellation state. Offline contracts assert the reviewed lifetime
behavior, but channel closure alone is not proof of credential release or
process-wide isolation. Application code adds no private cleanup, direct singleton
writes, hook-unregister workaround, client pooling, or async-exit workaround; the
upstream public-exit singleton reset is not prevented. Both
reviewed hosted versions remain enabled: a proposed hosted-only restriction to
reviewed 1.6.4 is **pending user approval**, not implemented here. The native/global
0.60.15 dependency floor is unchanged. These limitations require an explicit
operator compatibility decision; neither tests nor prior scoped review imply
overall security clearance for this or a later head.

The coordinator also releases the known stock Modal transport on later lifecycle,
CAS, binding-revocation, install, or commit rejection. Concurrent reconnect losers
and displaced cache entries release only their own transport; a winning cached
handle stays open. Final cache publication stays locked through repository commit
and rollback eviction, in DB → binding → cache acquisition order. A concurrent
resolver cannot acquire a provisional handle that a rejected commit will dispose.
The provider reconnect itself remains outside this synchronized section. Failed
ControlPlane installation also removes its outer local cache entry. Deterministic
rollback/commit-failure and concurrent-install tests cover these custody fences.

Owned stock Modal sessions additionally carry a single immutable cache-publication
custody claim, synchronized only by the session's local lock. A custom server
adapter may return the same wrapper to concurrent resolvers of the already-cached
exact-generation winner. It must not recycle a previously published wrapper into
a new generation, another coordinator/cache, or a publication after rollback or
forget/transfer; those cases fail closed with `AttachmentConflict`. Supply a fresh
owned wrapper with an exclusive factory client instead. This session-local claim
fences aliases of the same session; it cannot certify a custom adapter that manually
constructs different sessions sharing one external SDK client. That remains outside
the owned-adapter contract. A rejected alias cannot release another publication's
client. Disposal of a displaced entry uses its original claim, so delayed exit
cannot close a newly accepted alias winner: such republication is prohibited
atomically before installation. Failed claims/transactions cannot revive a
consumed wrapper. Generic unowned/custom runtimes keep their prior behavior and
transport cleanup remains a no-op for them.

The claim contains no credentials, runtime registry, provider ownership or delete
authority; it is stored only on the owned session and its existing cache entry.
There is no reference-counted client pool or cleanup cache. Coordinator rejection,
displacement, and forget disposal perform SDK exit outside DB/cache critical
sections; the session claim lock is never held through SDK exit. This custody-only
constraint does not prove provider identity or resource incarnation.

This is an exact-type local custody helper, not an arbitrary
runtime cleanup callback: it never calls an unverified handle's `destroy()` or
terminates a provider resource. The helpers do not call context exit on native/shared
unowned clients; the upstream floor singleton caveat above still applies.
When the service installs a handle, custody transfers from coordinator cache to
its existing runtime cache, without premature context exit. Discarded runtime
custody after explicit termination, shutdown, or watchdog cleanup releases the
owned transport once, including when termination fails. No cleanup cache or new
delete authority is introduced; API-level retryability is not promised.

Termination RPC failure still means cloud closure is unknown. Wire status remains
`failed`, but a durable internal accounting hold retains concurrent capacity and
billing through restarts until trusted closure evidence. Closing the local
transport does **not** clear this hold. See [provider reconciliation](PROVIDER_RECONCILIATION.md)
for migration, timestamp/conflict fencing, and the explicit no-refund protocol.
The former hosted Modal tests asserting discarded-but-open clients are now
strengthened to require one public exit, retained capacity, and no extra provider
termination. The stale-reader test's confirmed-failure setup uses explicit
terminal lifecycle evidence, not an RPC error; all its billing assertions remain.

Daytona requires both URL and target. Tensorlake requires an explicit proxy URL
and all account scope settings, with bounded HTTP timeouts and no reconnect
retries; explicit proxy routing avoids a pending-state wait. Modern Tensorlake may read the existing ID with GET during verification.
No provider is created, started or resumed, no command is run as a liveness probe,
and returned resource IDs are checked against the descriptor. Missing SDKs,
unreviewed SDK versions and reconnect failures produce a generic
`AttachmentError`, without SDK exception text or chained credentials.

The stock adapter gates capability before SDK calls to reviewed exact versions:
Modal 0.60.15 / 1.6.4, Daytona 0.207.0 / 0.232.0, and Tensorlake 0.3.12 / 0.5.165.
Other versions fail closed pending contract review; global package dependency
floors and direct SDK APIs are unchanged. Tensorlake 0.3.12 is the tested sandbox
API floor, not a replacement for the project's broader Tensorlake dependency.

Modern E2B `connect` performs a POST that can automatically resume paused
resources. A GET-before-connect check cannot prevent a pause in between those
calls. Therefore the stock hosted adapter disables E2B reconnect entirely,
including the tested older generation, rather than relying on that unsafe
workaround or ambient routing defaults. Offline contracts cover both
code-interpreter 1.0.0 (e2b 1.11.1) and 2.11.10 (e2b 2.64.0) failing closed.
Custom server-owned adapters must independently uphold read-only reconnect,
explicit routing and identity verification; a descriptor is not a capability.

Local, Safehouse, Boxd, Runpod, Ray, Monty and Docker attachment are unsupported
by this module, including when direct SDK resume supports them. There is no
implicit host-path or Docker daemon binding. Unsupported backends fail explicitly.

## Hosted service configuration

Attachment configuration is server-owned and opt-in per backend:

```python
control_plane = ControlPlane(
    store,
    attachment_bindings=tuple(server_bindings),
    attachment_adapters={"e2b": bound_adapter},
)
```

The adapter registry is explicit: the service does not install SDK adapters or
choose accounts automatically. A configured backend requires exactly one binding
whose `organization_ids` contains the authenticated organization. Account scope
is an operator-supplied provider-account identity, never derived from an
organization label. Multiple matching bindings or missing creation capability
fail safely rather than using ambient credentials.

Registered adapters implement `describe` and `reconnect` from
`SDKAttachmentAdapter`. For new creation they additionally implement
`create(binding, **config)`, returning the normal sandbox runtime. This method
must use the binding's explicit credentials/settings and that same provider
account, and must honor the requested network and sandbox configuration. They
must also implement `verify_created(runtime, binding)`, verifying server-recorded
creation provenance linking that runtime to that exact binding/account. Merely
comparing a caller's claimed binding ID with a resource ID is not evidence. This
verification runs immediately after creation, before cost methods, identity
extraction, lifecycle publication or retention for cleanup. The publication
coordinator also verifies provenance inside its lifecycle transaction; the stock
reconnect-only adapter refuses to certify creation. A server-owned adapter may
subclass `SDKAttachmentAdapter` to reuse its strict extraction and SDK reconnection
while supplying a bound creation implementation.
The stock SDK attachment adapter alone is reconnection-only; it does not supply
a universal bound creation implementation. The ordinary `sandbox_factory` is
used only for backends not opted into attachments.

Creation commits the running state, provider resource identity, creation attempt
and descriptor in one SQLite transaction. If provenance verification rejects a
returned handle, the service discards it without calling any cleanup method,
retaining it in a runtime cache, or persisting its claimed provider identity.
A bound creation exception's claimed identity likewise grants no authority
without successful provenance verification. Later shutdown, termination, lazy
attachment and reconciliation cannot acquire
cleanup authority from that rejected handle. The failed creation remains
`cleanup_status=unknown`, with retry blocked: discarding a handle does **not**
prove that allocation failed, resources disappeared, billing stopped, or capacity
was recovered. Estimates remain estimates; separately authorized provider cost
observations retain their existing authority. No reaper or guessed-ID cleanup is
introduced.

If provenance succeeds but descriptor publication fails, the transaction rolls
back and the failure retains the known owned provider identity and local handle
for existing cleanup, with an unknown cleanup outcome. Legacy non-attachment
creation-failure cleanup is unchanged. Neither failure is marked deleted or safe
to retry. A repeated creation key never allocates again.

`POST /v1/sandboxes/{sandbox_id}/attach` takes only the hosted ID in the path:
no body, query parameters, provider ID, descriptor or raw SDK session state. It
requires `sandboxes:execute`, uses normal API-key/browser-session authentication,
CSRF enforcement and mutation auditing (`sandbox.attach`), and returns the same
sandbox-record response shape as the existing GET endpoint. Unsupported binding,
adapter or SDK capability produces a sanitized unsupported-operation response;
stopped or concurrently changed authority produces a conflict. Rejected payloads
and provider exception strings are not echoed.

Execute, agent and network operations lazily reconnect only after authorization
and tenant ownership checks. Completed execution/agent idempotency responses are
returned before attachment and remain replayable after termination. Attachment
itself runs no commands, performs no allocation and adds no usage. New execution
keys use existing execution claims and metering; attachment adds no second
execution or accounting path.

The service's lifecycle lock serializes local resolution and termination. A
coordinator installation callback runs inside the final database authority
transaction, serializing the final metadata check with stopping writes. This is
not a provider-side execution lease or proof against provider resource ID reuse. Only `_runtimes` retains
service handles; coordinator caches are discarded after resolution. Resource
sampling validates attachment generation and binding without reconnecting.
Termination intent, watchdog claims, confirmed terminal provider observations,
and terminal lifecycle writes invalidate the descriptor in their own database
transaction. Shutdown retains its existing best-effort provider destruction
behavior, including for attached handles, and its lifecycle writes invalidate
descriptors rather than deleting generation tombstones. Do not call `close()`
when simulating a crash or expecting resources to remain available for restart.

## Repository setup

Inject a **connection factory**, not a database filename or a store's private
attributes. `SQLiteStore.connect()` is the supported connection boundary, and
`store.attachment_repository` is initialized alongside the store schema, even
when service attachment is disabled. Each factory invocation returns a fresh
caller-disposable SQLite connection to the same database as authoritative sandbox
records.

```python
from bespokelabs.sandbox.control_plane.attachments import (
    AttachmentCoordinator,
    AttachmentRepository,
    BindingRegistry,
)

attachments = AttachmentRepository(connection_factory)
attachments.initialize()
bindings = BindingRegistry(server_bindings)
bindings.attach_repository(attachments)  # Shared revocation; no credential persistence.
coordinator = AttachmentCoordinator(attachments, bindings)
```

Attachment initialization creates the namespaced `cp_runtime_attachments_v1`
and `cp_provider_bindings_v1` tables in that same database; it does not create a
separate ownership store. The default authority loader
selects tenant-scoped `sandboxes` rows with `organization_id`, `id`, `backend`,
`status`, `version`, and `provider_resource_id`. A custom `load_authority`
callback must read equivalent authoritative fields **using the provided
connection**, return `SandboxAuthority`, and never trust a runtime cache.

Rows are keyed by organization and sandbox. A never-written row has generation
and version zero. Publication advances both counters, except an identical
publication at the expected generation is a no-op. Invalidation advances both
and retains a descriptor-free tombstone, even for a previously absent row.
`expected_generation` provides compare-and-swap protection against stale writers.
Keep tombstones while delayed operations might still reference the sandbox ID.

Owned operations use short `BEGIN IMMEDIATE` transactions, commit/rollback and
close their connections. With `connection=conn`, the caller must already hold a
transaction. The repository acquires SQLite's write reservation with a no-op
write **before** reading CAS authority; `BEGIN IMMEDIATE` is recommended. A stale
deferred WAL snapshot or a busy writer produces a sanitized
`AttachmentConflict`, not raw SQLite diagnostic text. The caller must roll back
and retry its whole transaction from fresh authority; the repository does not
retry a stale snapshot. It never starts, commits, rolls back, or closes that
caller-owned transaction. Owned transactions do roll back on failure. All
lifecycle writers must use the same database and consistent transactions. These
SQLite guarantees are not provider-side fencing.

## Exact integration hooks

The hosted service uses these hooks; other lifecycle integrations must preserve
the same transaction and authorization boundaries.

### 1. Publish after successful creation

After recording the tenant-owned sandbox as `running`, with its actual backend
and provider resource ID, call:

```python
record = coordinator.publish_created(
    principal,
    sandbox_id,
    trusted_runtime,
    binding_id,
    expected_generation=0,  # Or the explicitly loaded current generation.
    remote_workdir=trusted_remote_workdir,
    scope="sandboxes:create",
    connection=conn,       # Same transaction as the running lifecycle write.
)
```

The default publication scope is `sandboxes:create`. The hook checks the
principal, authoritative tenant/lifecycle/backend/resource identity, binding
membership and server-owned adapter creation provenance before publication. It never caches a handle
from a transaction that could roll back. A retried publication must use the
current generation; an identical publication then returns the existing record.
A stale generation must not be silently retried after termination or replacement.

### 2. Resolve before execution

For an authenticated principal, use:

```python
runtime = coordinator.resolve(principal, sandbox_id, install=install_local_handle)
```

The default required scope is `sandboxes:execute`; a trusted internal caller may
explicitly provide another required scope. Do not accept that scope from the
request. The caller remains responsible for authenticating the principal with
the normal API-key authentication path. Every call reads tenant-owned durable
authority before considering its cache. A cached runtime is never authority.

A cache miss reconnects **outside** the database transaction. Before installation,
the coordinator reads again inside a fresh write transaction and checks the
running lifecycle, backend/resource identity, sandbox record version, attachment
generation/version/document, and current binding object/version/scope. A race
with stopping, destruction, replacement or binding rotation rejects the result.
A discarded handle is never destroyed: that would delete the shared provider
resource. The optional `install(runtime, attachment_record, binding)` callback
runs under the final authority transaction, including coordinator cache hits.
It must perform local bookkeeping only: no provider I/O, nested transactions or
commits. A later call may retry from fresh authority; there is no auto-create.

Execution/termination ordering beyond handle resolution still belongs to the
service's execution leases or lifecycle locks. A runtime already returned before
a termination commits cannot retroactively be withdrawn. Do not hold a database
transaction open while calling provider commands.

### 3. Invalidate atomically with termination

In the **same transaction** that transitions authoritative lifecycle to
`stopping` or a terminal status:

```python
attachments.invalidate(
    organization_id,
    sandbox_id,
    connection=conn,
)
```

Unconditional invalidation is appropriate for a serialized lifecycle transition.
For an operation tied to a known generation, supply
`expected_generation=generation` and inspect the returned boolean; `False` means
nothing was changed. Invalidation reads only counter metadata, so even a
malformed descriptor cannot block a lifecycle tombstone.

After the transaction commits, optionally discard the local cache immediately:

```python
coordinator.forget(organization_id, sandbox_id)
```

`forget` does not call a provider. It is only a cache optimization: other
processes also reject stale cached entries on their next durable-authority read.
Keep provider destruction in the service's existing termination path.
`ControlPlane.close` also destroys attached runtimes as before, invalidating
descriptors through its stopping and terminal writes while retaining tombstones.

## Verification and limitations

`tests/test_runtime_attachments.py` uses temporary SQLite databases, synthetic
records, fake SDK modules and deterministic thread synchronization. It exercises
reconnection after a fresh coordinator, no-create SDK calls, two-organization
isolation, scope checks, binding/version changes, malformed descriptors,
credential exclusion, transaction ownership/rollback, conditional invalidation,
and reconnect-versus-termination races. Tests require no provider extras and make
no live provider calls.

`tests/test_hosted_attachments.py` adds service/HTTP tests with fake runtimes:
two-store and fresh-interpreter reuse, exactly-once execution accounting,
completed-response replay after stop, lazy agents/network changes, rollback of
creation publication and termination invalidation, cross-store stop barriers,
binding/version/SDK failures, audit/CSRF enforcement, shutdown and watchdog
cleanup, and reconciliation tombstones. No real provider creation or destruction
is performed.

Attachment does not convert old raw state, refresh credentials, recover expired
resources or restart stopped providers. Not every backend or SDK version supports
attachment, and configured creation adapters remain operator responsibilities.
Descriptor incarnation is an application marker, not independently verified provider proof:
providers must supply stable resource IDs; ID reuse cannot be detected without
additional provider-specific incarnation support. Activation for providers that
can reuse IDs remains blocked until that verification exists. There is no
automatic adoption or destruction of a resource based on an ID or incarnation
marker. Discarded or wrong-ID reconnect handles are never destroyed.

`tests/test_sdk_attachment_contract.py` exercises real installed SDK signatures
and explicit routing with autospecced SDK call boundaries, hostile ambient
routing placeholders, and socket guards. Its separate eight-job Python 3.12 CI
matrix installs each provider's reviewed floor/current versions independently
(including incompatible Daytona/E2B-floor HTTPX ranges). The targeted SDK must
import and cannot silently skip in CI. These are offline compatibility contracts,
not live account-access or provider lifecycle verification.

### Follow-up negative controls

Offline negative controls against pre-follow-up `c2d64d8` reproduce three distinct
failures: the real-f845 legacy migration fixture releases all four ambiguous
resources (capacity 0 instead of 4); the event-gated coordinator commit-failure
race closes the client already returned successfully to consumer B; and the real
Modal-floor SDK release-then-destroy contract silently succeeds instead of raising
`UNKNOWN`. The corrected tests pass without provider network access.

The coordinator race uses fake owned transports and actual SQLite rollback,
including an injected `KeyboardInterrupt` exception (not an OS signal). It tests
local transaction/cache custody, not a provider lease or a new ControlPlane
close/destroy concurrency claim. Real SDK lifecycle contracts separately exercise
public client exit and stub RPCs before eager client opening. A further negative
control against `7603dc9` reproduces the three-generation delayed-disposal race
when a custom adapter reuses a stock owned wrapper; the stock fresh-wrapper path
alone does not trigger it. New tests require explicit alias rejection, preserve
concurrent same-object/exact-generation winners, fence parallel cross-coordinator
claims, and verify failure/displacement exit counts without provider termination.

Modal 0.60.15's client/credential retention and ambient-singleton reset remain
unresolved public-API limitations, not an "inert hook" waiver or disabled floor.
The proposed hosted-only version restriction awaits user approval. This new head
requires fresh scoped review; earlier review or approval does not transfer.
