# Durable hosted native jobs

This module provides tenant-scoped persistence and orchestration for native
provider jobs. An **explicitly opt-in HTTP plugin** is available through
`create_app(..., hosted_job_configuration=...)`; default apps expose no job
routes, import no job plugin or provider SDK, and activate no providers. The
existing direct `SandboxJobBackend` API is unchanged: `submit_job(spec)` still
returns `str`, and `JobSpec` defaults retain their existing meanings.

To run these routes on their own as a stateless service with several instances,
see [Deploying the jobs-only service](JOBS_SERVICE_DEPLOYMENT.md).

For the opt-in, provider-free `RemoteJobClient` over these existing HTTP routes,
see [Remote hosted-job SDK](REMOTE_JOBS.md). It leaves native job methods and
existing remote sandbox clients unchanged and does not deploy or activate a host.

The supplied adapter supports Google Cloud Batch through the public
`GcpBatchClient` submission/status methods and explicitly supplied public
Google SDK clients for CancelJob and bounded Cloud Logging reads. This is not
a claim that every sandbox provider supports native jobs, nor a claim of
exactly-once execution.

## Configuration boundary

Construct the repository with a factory returning a **new SQLite connection**
to the same durable database that owns the `organizations` table. Connections
are committed or rolled back and closed by the repository. Do not use a separate
job database, a process-local in-memory database, or a copied organization list.
The repository enables foreign keys and owns only `hosted_jobs` and the
namespaced `hosted_jobs_schema_migrations` journal. It does not modify the
existing store's schema version sequence or access its private path fields.

Supply a stable, secret-keyed SHA-256 HMAC function accepting `bytes` and
returning 64 lowercase hexadecimal characters. All processes sharing the
database must use the same HMAC key and immutable binding registry. Key rotation
requires retaining the ability to recognize existing idempotency reservations;
changing the HMAC function silently would break that property.

`ConfiguredJobBinding` is server-only configuration. It contains:

- An immutable `JobBinding(id, version, backend, account, project, region)`.
- An explicit organization allowlist.
- A lazy adapter factory that closes over server-managed credentials.

The binding's account is a non-secret identity, **not** a token or credential
file. Never repoint an existing binding version to another account, project,
region, or credential principal. Retain old versions while their jobs need
observation or cancellation. The factory must authenticate as the configured
account; the Batch job status does not prove which credential principal made
an RPC. A task's runtime service account is not the orchestration credential
binding.

Adapter factories, pure `validate(spec, binding)`, and resource-name reservation
must not perform network I/O or construct provider clients. Provider SDK imports
and connections remain lazy. **An explicit bound `client_factory` is required**;
there is no default factory or ambient ADC fallback in the hosted adapter.
For cancellation also supply `cancel_client_factory`, returning the official
`batch_v1.BatchServiceClient` under the same credential/account policy. Missing
CancelJob support fails closed, without invoking the direct client's
potentially delete-capable cancellation wrapper. For logs also supply
`logging_client_factory`, returning the official public
`google.cloud.logging_v2.services.logging_service_v2.LoggingServiceV2Client`.
This is the GAPIC client, not the high-level `logging_v2.Client`: its public
`list_log_entries` method exposes individual response pages and continuation
tokens. Missing logging configuration fails closed for positive log tails;
submission, observation, cancellation, and zero-tail reads do not need it.
The adapter never delegates to native `job_logs`, discovers credentials from
private client fields, or creates an ambient-credential Logging client.
The operator must bind **all three factories** to the configured credential
principal: comparing the account string or observing ownership labels is not
credential-identity verification.
Credentials must never be accepted in a request body or stored in a job row.

Factories own their returned clients' lifetime. Have them return reused,
server-owned clients (one per immutable binding), not construct clients on every
call; close transports and logging clients at host shutdown, after draining
in-flight calls. The adapter neither owns nor closes supplied clients. It uses
no private native-client fields to discover SDK handles or credentials.

Illustrative integration factory (all arguments supplied by the host):

```python
from bespokelabs.sandbox.control_plane.job_models import ConfiguredJobBinding
from bespokelabs.sandbox.control_plane.job_adapters.gcp_batch import GcpBatchHostedAdapter
from bespokelabs.sandbox.control_plane.job_service import HostedJobService
from bespokelabs.sandbox.control_plane.job_store import HostedJobRepository


def make_hosted_jobs(connection_factory, hmac_function, binding,
                     allowed_organizations, native_client_factory,
                     cancel_client_factory, logging_client_factory):
    repository = HostedJobRepository(connection_factory, hmac_function)
    configured = ConfiguredJobBinding(
        binding=binding,
        organizations=frozenset(allowed_organizations),
        adapter_factory=lambda: GcpBatchHostedAdapter(
            binding, client_factory=native_client_factory,
            cancel_client_factory=cancel_client_factory,
            logging_client_factory=logging_client_factory,
        ),
    )
    return HostedJobService(repository, [configured])
```

The core remains independent of HTTP and optional server dependencies.
`HostedJob` must not be serialized directly. The HTTP plugin uses an explicit
allowlisted DTO, never a dataclass dump, and creates its own repository using
`control_plane.store.connect` (the public caller-owned fresh-connection boundary)
against the existing organization authority. It takes no alternate database
path and never accesses the store's private connector.

## PostgreSQL store

SQLite remains the default and is unchanged. To run the hosted-job path on
PostgreSQL, install the `postgres` extra (`psycopg` 3 with its binary and pool
packages) and construct both stores on the same database. The driver is
imported only when a store is constructed.

```python
from psycopg_pool import ConnectionPool

from bespokelabs.sandbox.control_plane.job_store import hosted_job_hmac
from bespokelabs.sandbox.control_plane.job_store_postgres import (
    PostgresHostedJobRepository,
)
from bespokelabs.sandbox.control_plane.store_postgres import (
    PostgresAuthorityStore,
)


def make_postgres_stores(dsn, key_pepper, stable_server_hmac_secret):
    pool = ConnectionPool(dsn, open=True)
    authority = PostgresAuthorityStore(pool, key_pepper=key_pepper)
    repository = PostgresHostedJobRepository(
        pool, hosted_job_hmac(stable_server_hmac_secret)
    )
    return pool, authority, repository
```

Both classes take either a pool exposing `connection()` (such as
`psycopg_pool.ConnectionPool`) or a zero-argument factory returning a new
`psycopg` connection, which is closed after each operation. The caller owns
the DSN, credentials, TLS settings, `search_path`, and the pool's lifetime;
nothing is read from the environment and no module-level state is kept. Use the
default READ COMMITTED isolation level. Construct the authority store first:
the job repository refuses a database without an `organizations` table.

`PostgresAuthorityStore` covers organizations, API keys, `authenticate`, and
`record_audit` only (the `AuthorityStore` protocol in `job_interfaces.py`). API
keys are stored under the same keyed digest as the SQLite store. It has no
sandbox, dashboard-session, attachment, or billing tables, so it cannot back
the full sandbox control plane or `create_app`. The audit `session_id` column
is a plain nullable value, not a reference.

`HostedJobService` accepts any `JobRepository`. For HTTP, use
`create_job_app(authority=..., repository=..., configuration=...)` from
`job_app.py`, which serves only the job routes and health endpoints on either
pair of stores; see
[Deploying the jobs-only service](JOBS_SERVICE_DEPLOYMENT.md). It is built on
`install_job_routes(app, None, configuration, authenticate,
repository=repository)`, which you can also call directly with your own
`authenticate` callable. The repository's HMAC function must be
`hosted_job_hmac(configuration.hmac_secret)`.

Both authority stores also provide `ensure_organization` and `list_api_keys`
(the `SeedableAuthorityStore` protocol), which the operator seeding helper in
`job_seed.py` uses in place of an HTTP administration route.

Migrations run when each store is constructed. Each store records applied
versions in its own journal (`authority_schema_migrations` and
`hosted_jobs_schema_migrations`); every version is applied with its journal row
in one transaction under a transaction-scoped advisory lock, so re-running is a
no-op and instances that start together apply each version once. The database
role needs permission to create tables in the target schema on first start.

With several service instances on one PostgreSQL database:

- **Safe:** the hosted-job path. Reservation of an idempotency key is a single
  `INSERT ... ON CONFLICT DO NOTHING` followed by a re-read, so concurrent
  submissions of one key get one job and the same replay-or-conflict answer.
  Dispatch authorization, observation leases, cancellation intent, and terminal
  snapshots are single-statement compare-and-swap updates fenced on version,
  lease, UID, binding, and phase. API-key authentication and audit appends are
  ordinary shared rows. All instances must use the same HMAC secret, key pepper,
  and binding registry, and reasonably synchronized clocks for lease expiry.
- **Not made safe by this:** the sandbox control plane. Its in-memory runtime
  registry and supervisor remain single-process, and its sandbox, session,
  attachment, and billing state still lives only in the SQLite store. Run one
  such process per SQLite database as before.

## Opt-in HTTP host configuration

Install the existing `server` extra for FastAPI. Importing the SDK or hosted-job
core does not require FastAPI. No CLI flag, environment-variable activation,
automatic provider choice, or default credential discovery is added.

```python
from bespokelabs.sandbox.control_plane.api import create_app
from bespokelabs.sandbox.control_plane.job_api import HostedJobConfiguration


def make_http_app(control_plane, configured_bindings, submission_versions,
                  stable_server_hmac_secret, server_admission_policy):
    return create_app(
        control_plane,
        hosted_job_configuration=HostedJobConfiguration(
            bindings=tuple(configured_bindings),
            submit_bindings=dict(submission_versions),
            hmac_secret=stable_server_hmac_secret,
            admit=server_admission_policy,
        ),
    )
```

All arguments above are **trusted host configuration**, not request fields.
Use a durable, secret-managed HMAC key of at least 32 bytes, identical across
restarts/workers; there is no ephemeral default. The secret is never persisted
in the job database. `submit_bindings` explicitly selects one submission version
per opaque binding ID, e.g. `{"batch": 2}`. Retain old immutable versions in
`bindings` while old jobs need provider-backed observation, logs, or cancellation.
No latest-version inference is performed. Binding registries must be actual
list/tuple containers, tenant allowlists actual list/tuple/set/frozenset
containers (snapshotted to frozensets), not strings, mappings, or iterators.
Empty registries/maps/tenant containers grant nothing, never global access.

The mandatory `admit(principal, binding, spec)` callback must return exactly
`True`; false/non-boolean results and exceptions deny with 403 before any
reservation or provider RPC. It receives a copy and cannot rewrite the submitted
request. The host must enforce allowed images, runtime service accounts,
resources, deadlines, network policy, provider options, concurrency, rate and
existing spend/quota controls here. In particular the generic body size and
resource ceilings are **not budget admission**. Do not supply an always-true
callback in a deployment. No competing quota mechanism or billing authority is
created by this plugin. Admission runs again on replays and can deny a replay
without resetting its prior durable dispatch authorization. The callback must be
a **pure, replay-safe policy evaluation**: it must not place external quota holds,
debit balances, or perform other side effects, including on replayed requests.
Evaluate existing authority without mutating it; repeated evaluation must not
consume quota or spend again.

The registered routes are:

| Route | Required scope | Response |
| --- | --- | --- |
| `POST /v1/jobs` | `jobs:submit` | 202 job DTO |
| `GET /v1/jobs/{id}` | `jobs:read` | 200 job DTO |
| `GET /v1/jobs/{id}/result` | `jobs:result` | 200 `{"result": null}` or terminal result |
| `GET /v1/jobs/{id}/logs?tail=1000` | `jobs:logs` | 200 `{"logs": "..."}` |
| `POST /v1/jobs/{id}/cancel` | `jobs:cancel` | 200 job DTO; durable intent, not proof of completion |

There is deliberately **no list route**. Submission requires a printable,
nonempty `Idempotency-Key` header (at most 512 characters) and a JSON body:

```json
{"binding_id": "batch", "spec": {"command": ["python", "worker.py"]}}
```

Only the opaque binding ID and generic JobSpec fields are accepted. Unknown
fields, credential/binding-version/account/project/region fields and reserved
resource-routing options are rejected rather than forwarded. Ownership-label
normalization is validated before consuming the key. Requests are streamed with
a 64 KiB byte cap (413), extra fields are forbidden, numeric values must be
finite and correctly typed, and CPU/memory/disk/deadline/container lengths have
finite ceilings. CPU is at most 256, memory 1,048,576 MiB, disk 16,777,216 MiB,
and timeout 604,800 seconds; host admission must impose appropriate smaller
limits. Env maps have at most 256 entries, labels/options at most 64 each. These
are parser safety bounds, not permission to provision those resources. Invalid
requests do not consume the operation key. Validation responses never include
submitted input. Log tails must be integers from 0 through 1000; unlike the core
clamping helper, HTTP rejects larger values. Returned text remains capped at
1 MiB; logs are intentionally authorized workload output, not automatically
redacted secret storage.

### Cloud Batch options a caller may send

`spec.backend_options` is forwarded to the native Cloud Batch job spec, so the
options below are **caller-settable request fields**. Each one is in the copy
of the spec that `admit` receives, and none of them changes the project,
region, job name, credential account, or ownership label, which stay
server-controlled. Their values are validated before the operation key is
reserved (422, with a fixed message that never repeats the input).

| Option | What the caller controls | What the server keeps |
| --- | --- | --- |
| `secret_env_vars` | Which Secret Manager secret versions become environment variables: `{"NAME": "projects/<project>/secrets/<name>/versions/<version>"}` | `<project>` must be exactly the binding's `project` string; other projects (and the project number in place of its ID, or the reverse) are rejected |
| `runnable: "script"` | Runs `spec.command` (a string) as a shell script on the VM instead of in a container; `spec.image` must then be absent | Nothing more: admission must decide whether tenants may run scripts |
| `boot_disk_image`, `boot_disk_type` | The VM's boot image and disk type | Nothing more: restrict images in admission |
| `install_gpu_drivers` | Driver install for `spec.gpu` or an accelerator-optimized `machine_type` | - |
| `block_project_ssh_keys` | Keeps project-wide SSH keys off the VMs | - |
| `instance_labels` | Extra labels on the VMs only | The `hosted-job-owner` label, in any spelling that normalizes to it, is rejected; the server's ownership label is still applied to the job and its VMs as before |
| `task_count`, `parallelism` | How many tasks the job runs (1 to 10,000, default 1) and how many at once (1 to `task_count`, at most 5,000) | The bounds only: admission must decide how many tasks, and so how many VMs, a tenant may start |

**Secrets.** The request, the job row, the audit trail, and every response
carry at most the secret *reference*, never a value; Batch resolves the value
on the VM. This service **does not check that the calling tenant is entitled
to a secret**. It only checks the reference's format and project. The value is
read with the job's runtime service account (`service_account`, else the
project's default Compute Engine service account), which needs
`roles/secretmanager.secretAccessor` on the secret, so any tenant admitted
with a reference can read whatever that account can read. Restrict references
per tenant in `admit`, for example to an allowlist or a per-tenant name prefix,
and grant the runtime account access to those secrets only. A job's own output
can still print a value it was given; logs are not redacted.

**Scripts.** A script runs on the VM host, outside any container, as the
runtime service account, and `allow_internet=False` cannot be combined with it
(that flag only blocks a container's network). Allowing `runnable: "script"`
is therefore a broader grant than allowing an image; deny it in `admit` unless
the tenant is meant to have it. The script is the `command` string and counts
toward the 64 KiB request cap after JSON escaping (a newline is two bytes), so
the largest script that fits is somewhat under 64 KiB. The cap is not raised
for scripts and there is no separate script limit: a larger payload gets 413.
Keep the script a small bootstrap that fetches the rest from the VM image or
Cloud Storage.

**Tasks.** `task_count` multiplies the job: every task runs the same command
with the same `cpu`, `memory_mb` and `timeout_secs`, on as many VMs as Batch
needs for `parallelism` tasks at a time. Both values are part of the spec that
`admit` receives and of the payload fingerprint, so a replay of the same
operation key with another count is a 409. A spend or quota policy must
multiply by `task_count`; nothing here does it for the host. Each task reads
its 0-based index from Batch's own `BATCH_TASK_INDEX` variable (and the total
from `BATCH_TASK_COUNT`); the service adds no variable.

An admission policy should **allowlist** option keys rather than block known
ones, so an option added in a later release is denied until the host has
reviewed it.

The job DTO exposes only `id`, `backend`, `phase`, `state`, `confidence`,
`cancel_requested`, safe `error_code`, and terminal-only `result`. Results expose
only terminal state, bounded integer exit code, allowlisted reason and parsed
ISO timestamps, plus, for a multi-task job only, the bounded task summary
described under "Result reasons and multi-task results". Provider names,
UIDs, ownership markers, binding account/project,
credentials, fingerprints, raw messages, labels, metadata and leases are never
included, even from an overpopulated stored snapshot. Unexpected adapter/policy
errors use fixed messages. Host bindings must use non-secret backend identifiers.

The routes reuse the existing principal authentication and exact scope checks
(`*` grants administration; sandbox legacy scopes do not grant job access),
cookie CSRF middleware and audit path. Cross-tenant lookups return 404 before
provider access. Job reads and writes are audited by fixed action names
`job.submit/read/result/logs/cancel` with outcome/status only, not request bodies,
headers, query values, log contents or untrusted path identifiers. Responses use
`Cache-Control: no-store`. Infrastructure access logs, proxies and APM must also
be configured not to capture sensitive headers, bodies or query strings.

**This integration is not activation clearance.** Production activation still
requires mandatory host admission tied to existing authority, exclusive provider
namespaces, appropriate IAM and all independent live-provider gates. The
non-atomic UID provider limitation described below is unchanged. Offline fake
HTTP/SDK tests are not evidence of live credential identity, service acceptance,
billing/quota readiness, or external review approval.

## Integration hooks and authorization

The service accepts an already authenticated `Principal` and exposes:

```python
submit(principal, spec, *, idempotency_key, binding_id, binding_version) -> HostedJob
get(principal, job_id) -> HostedJob
cancel(principal, job_id) -> HostedJob
logs(principal, job_id, tail=None) -> str
result(principal, job_id) -> dict | None
```

Each method requires its own explicit `jobs:submit`, `jobs:read`, `jobs:cancel`,
`jobs:logs`, or `jobs:result` scope, respectively. An explicit `*` remains an
administrator grant. Legacy sandbox scopes grant no job permissions. A method
does not require unrelated job scopes internally: for example, `jobs:result`
can retrieve a result without `jobs:read`. Every identifier lookup, including a
cached terminal result, is tenant-scoped in the authority database before any
provider access. Unknown tenant-owned identifiers return not-found rather than
revealing another tenant's job.

Binding selection is checked against the server allowlist. Observation also
compares the full persisted binding identity against the current registry;
a missing or changed binding fails closed before provider access. Cancellation
intent is still persisted if the binding is temporarily unavailable.

Reads are not necessarily RPC-read-only: `get`, `result`, `logs`, or submit
replay may resume a **previously committed, authorized cancellation intent**.
They cannot create fresh intent without `jobs:cancel`; they continue the durable
operation already authorized by a canceller, even if that API key is no longer
present. Hosts must document this behavior rather than promising that read-only
scopes never trigger mutation RPCs. No background reconciler is included.

`HostedJob` separates durable orchestration `phase`, last verified provider
`state`, and `confidence`. It is not a raw provider response and contains no
runtime command, environment, credential, or unsanitized provider details.

## Reservation and dispatch

The repository atomically reserves organization + operation (`submit`) + HMAC
of the idempotency key. Reservations are shared across API keys in the same
organization, not across organizations. A canonical keyed payload fingerprint
covers every `JobSpec` field and the complete binding identity. A replay with a
changed payload or binding conflicts before provider access. Raw keys and
payloads never enter durable storage; commands and environment values are kept
only in request memory for the actual native submission in this control plane.
The provider retains the submitted Batch job definition, including task
environment values; absence from the local database is not provider-side secret
redaction or a substitute for a provider secret-management policy. To keep a
value out of the Batch job definition as well, pass it by reference with the
`secret_env_vars` option (see "Cloud Batch options a caller may send").

Before dispatch, the row contains the full provider resource name, a random
ownership marker, and the trusted binding identity. Batch receives that reserved
name via `backend_options.job_id`. The routing fields (`project`, `region`,
`job_id`, `job_id_prefix`) and `hosted-job-owner` ownership label are reserved
for the server and cannot be overridden by hosted request payloads, including
label spellings that normalize to the reserved label, in `labels` or in the
VM-only `instance_labels` option. Other resource defaults
are unchanged. Pure hosted routing/label validation runs before reservation:
invalid reserved options consume no operation key and perform no provider RPC.
The adapter validates again before submission. A changed policy or other
validation/translation failure after dispatch authorization is conservatively
uncertain; it is never automatically retried.

Phases are:

| Phase | Meaning |
| --- | --- |
| `reserved` | Identity is durable; no dispatch has been authorized. |
| `submitting` | The single dispatch authorization was durably consumed. |
| `uncertain` | Acceptance, current presence, or ownership cannot be verified. |
| `active` | Actual provider ownership and incarnation UID were verified. |
| `terminal` | A verified terminal snapshot, or cancellation before dispatch. |

The `reserved` to `submitting` compare-and-swap is irreversible. Only its winner
calls native submission. After it commits there is **at most one application
submission attempt**, even if the process dies just before the RPC, the response
is lost, an SDK reports a retryable error, or an observation lease expires.
The native SDK can have its own transport retry behavior; this module does not
make an end-to-end exactly-once guarantee.

A process lost while still `reserved` has retained no executable payload. A
matching client replay is required to dispatch it; a status read cannot run it.
A process lost after dispatch authorization can only observe the reserved name,
not submit again. This intentionally trades availability for duplicate avoidance:
some jobs can remain uncertain indefinitely. An operator must not reset their
phase to enable another attempt. A genuinely new execution is a new client
operation with a new idempotency key.

## Ownership, observation, and results

The Batch adapter reads the actual `JobStatus.details["labels"]` and
`details["uid"]` emitted by the public client. It never reconstructs observed
ownership from request labels. It verifies the reserved marker and full resource
name before adoption, pins the first verified UID, and refuses later UID changes.
Before logs or cancellation it rechecks the actual ownership marker and pinned
UID. Missing labels, missing UID, replacement resources, or routing mismatches
fail closed. Arbitrary provider errors become fixed safe error codes, never
exception text, exception context, raw events, or provider messages in the DB.

Observation leases serialize status/cancellation work across processes. Updates
compare row version, lease token, UID, full binding, resource name, and phase.
Expired leases cannot publish observations; a newer lease fences an older
response. Lease expiry permits another observation, **never another submission**.
A stale pending observation cannot regress an already running job. Verified
terminal results are immutable, tenant-scoped, and repeatable after process
restart even if the provider resource later disappears.

`result` returns `None` until a verified terminal snapshot exists. The snapshot
contains state, exit code when known, allowlisted reason, and provider timing
when available. A locally cancelled reservation has a
`cancelled_before_dispatch` reason. It is a native job outcome snapshot, not an
artifact download or cached stdout/stderr. Logs remain a separately authorized,
provider-backed operation and are not retained in the job database.

### Result reasons and multi-task results

`result.reason` is `null` or one of these fixed values; the provider's own
message is never stored or returned:

| `reason` | Meaning |
| --- | --- |
| `exit_code` | The command exited nonzero; see `exit_code` |
| `timeout` | `timeout_secs` stopped the command |
| `vm_preempted`, `vm_lost`, `vm_rebooted`, `vm_unresponsive`, `vm_recreated` | Batch lost the VM under the task |
| `quota_exceeded` | The job failed before any task exited because the project's quota in the region does not cover its VMs. The same request fails the same way until quota is raised or the request shrinks |
| `capacity_unavailable` | The job failed before any task exited because the zone could not provide the VMs (a stock-out), although quota allows them. The same request may succeed later or in another zone or region |
| `batch_failed` | The job failed before any task exited for any other cause, or one Batch did not label. This is the fallback and its meaning is unchanged |
| `cancelled`, `cancelled_before_dispatch` | Cancelled at the provider, or locally before dispatch |

`quota_exceeded` and `capacity_unavailable` are recognized from provider error
codes in the job's newest status events; a failure Batch describes some other
way stays `batch_failed`, so treat `batch_failed` as "cause not classified",
not as "not a capacity problem". A binding has one region, so retrying
elsewhere means submitting to another binding with a new operation key.

A job submitted with `task_count` greater than 1 has one more key in its
result, `tasks`; the other five keys keep their meaning, with `exit_code` and
`reason` describing the failed task with the lowest index:

```json
{
  "state": "failed",
  "exit_code": 2,
  "reason": "exit_code",
  "started_at": "2026-01-01T00:00:05+00:00",
  "finished_at": "2026-01-01T00:04:10+00:00",
  "tasks": {
    "count": 20,
    "succeeded": 17,
    "failed": 2,
    "other": 1,
    "failures": [
      {"index": 3, "exit_code": 2, "reason": "exit_code"},
      {"index": 11, "exit_code": null, "reason": "capacity_unavailable"}
    ],
    "truncated": false
  }
}
```

`other` counts tasks that neither succeeded nor failed (never run, or still
unsettled when the job ended). `failures` lists failed tasks by index and holds
at most 20 entries; `truncated` is `true` when more tasks failed than are
listed (`failed` is still the full count). Each entry has exactly `index`,
`exit_code` and an allowlisted `reason`. A job with `task_count` absent or 1
has **no** `tasks` key: its result is exactly the five keys it always had.

Public `JobState.UNKNOWN` semantics are unchanged. Hosted orchestration does not
use a missing-resource UNKNOWN response as proof of failed dispatch, successful
cancellation, or a terminal result. During `submitting`, a missing observation
before the persisted submit deadline preserves `submitting` and its previous
confidence, with no `provider_missing` claim. Transient observation failures
also preserve that pending phase; an authoritative ownership contradiction
still fails closed. The default deadline is 60 seconds
(configurable as a positive finite `submit_timeout_secs`). At/after that deadline,
or after an explicit ambiguous submit failure, missing observations retain
`phase=uncertain` with `confidence=missing` and any previously pinned UID. The
deadline is an observation boundary, not proof of failed acceptance or permission
to submit again. Typed not-found errors follow the same rules.

Log reads request up to the latest 1,000 entries, clamp requested tails to 1,000,
and cap the returned UTF-8 text at 1 MiB (a chronological prefix, clipped at a
character boundary). Negative, boolean, and non-integer tails are rejected;
zero returns empty output without reading provider logs, after identity
verification. The hosted adapter uses explicit public GAPIC requests ordered
newest first, filtered by the bound project and pinned job UID. Each request's
`page_size` is at most the **remaining** entry budget, including after partial
pages. It consumes only the first response of each SDK pager, carries its
`next_page_token` into the next explicit request, and stops before requesting
another page when the budget is exhausted or the token is empty. At most 20
page RPCs are made (with retries disabled and a 30-second timeout per request),
so empty/partial pages cannot cause unbounded pagination; a read can return
fewer than the requested entries at this page limit. No stream, log cache,
unbounded-entry-count request, or native/high-level Logging iterator is used.

The byte cap bounds returned text, **not transport response bytes**. An
individual provider entry can be arbitrarily large before clipping; likewise a
malformed provider may disregard `page_size`. An oversized entry-count response
is rejected rather than silently clipped or used for further pagination. These
bounds do not promise that the provider cannot send oversized responses. The
adapter verifies Batch ownership/UID before and after a successful log fetch;
only an authoritative Batch identity check establishes job absence, not a
Logging service error. A transient log failure returns a fixed sanitized error
without demoting a verified lifecycle. Typed job absence or ownership
contradictions can still change observation confidence.

## Cancellation and operational limits

Cancellation intent commits before any cancellation RPC. A reserved job can be
cancelled locally without dispatch. A submitted job is observed and its UID
pinned before cancellation; a late submission acceptance triggers the pending
intent when it is observed. If another observer currently holds a lease, the
next authorized read/cancel/result operation resumes that intent. There is no
second provider reaper or background redispatch mechanism in this module.

Only CancelJob is allowed: the hosted adapter never invokes DeleteJob, even
when the SDK or service lacks CancelJob. Missing configuration, a missing SDK
method, or an unimplemented RPC records `cancel_unavailable`; configure a bound
SDK/service supporting CancelJob, or retain the intent until that becomes
available. Intent remains durable, and no terminal cancellation is fabricated.
Other transient cancellation failures record `cancel_unconfirmed`.
Asynchronous cancellation acknowledgements remain nonterminal. A
verified positive terminal completion racing the cancellation request is
preserved; cancellation intent never overwrites an already terminal snapshot.
If the resource disappears after an acknowledgement, the result stays uncertain
rather than inventing a cancelled outcome.

**Provider API limitation:** the public native client has no UID-conditional
cancel or log-read argument. The adapter checks actual ownership immediately
before those calls, but cannot make that check atomic with a provider-side name
replacement during the call. Log reads additionally recheck identity before
returning text; this detects changes but is not an atomic provider precondition.
The hosted adapter re-observes after the actual SDK CancelJob RPC rather than
treating its Operation as a job status.
Database version/UID fences protect durable state, not a provider-side RPC
against a concurrently replaced name. A deployment must keep
this randomly generated resource namespace exclusive, never reuse names, and
restrict external create/delete rights appropriately. If atomic protection
against external name replacement is required, this public API is an exact
blocker; a preflight check must not be represented as an atomic provider UID
precondition. The module does not claim that stronger guarantee.

Observation is demand-driven. Configure RPC timeouts appropriate to the lease
window; an overlong RPC loses its ability to publish a stale observation.
Provider retention, log availability, task retries configured in the native
spec, and application-level side effects remain provider/application concerns.
No live resources are needed by the tests. Per-organization job quotas,
retention/cleanup limits, and rate limiting are not implemented here; this core
alone is not a production-readiness or quota-enforcement claim.

## Offline verification

`tests/test_hosted_jobs.py` uses deterministic native fakes, separate repository
instances, a shared durable authority database, barriers, reopen/restart cases,
lost responses, lease expiry, cancellation races, payload conflicts, scope and
tenant denials, actual ownership/UID changes, credential sentinels, and immutable
terminal result replay. It runs without any Google SDK installed.

`tests/test_hosted_jobs_postgres.py` reruns the storage-independent cases of that
suite against PostgreSQL with both a connection factory and a pool, and adds
concurrent reservation, lease-claim, cancel-versus-update, and simultaneous
migration cases over separate connections. Its database cases are skipped unless
`SANDBOX_TEST_POSTGRES_DSN` names a disposable database; each test uses and
drops its own schema. CI runs them against a PostgreSQL service container.

`tests/test_job_app.py` drives the jobs-only app over HTTP on SQLite stores with
the same native fakes: authentication, per-route scopes, tenant isolation, audit
rows, request limits, both health endpoints, the absence of dashboard and
administration routes, and operator seeding. `tests/test_job_app_postgres.py`
reruns those cases on PostgreSQL under the same environment variable and adds
several app instances on one database, each with its own connections.

`tests/test_hosted_jobs_gcp_batch.py` additionally exercises real public client
translation and actual Batch status detail extraction with class-spec autospec
RPCs and official synthetic protobuf responses when the optional SDK is installed.
It tests created-name normalization, actual UID/labels, CancelJob availability,
unimplemented/missing RPCs, and bounded Cloud Logging requests. Logging tests
exercise the actual public `LoggingServiceV2Client`, official
`ListLogEntriesPager`, and synthetic protobuf responses at the transport
boundary, asserting request sizes, continuation tokens, fetched entry counts,
and RPC counts for full, partial, empty, exhausted, and malformed pages. These
are not live provider fixtures or evidence of provider acceptance. The optional-SDK CI
uses Python 3.12, Batch 0.17.0 and 0.22.5, and Logging 3.16.0 in both jobs.
These exact versions were checked against package-registry metadata. Logging
3.17.0 (the latest published release at verification) requires protobuf 6.33.5+
and cannot resolve with Batch 0.17.0's protobuf <5 requirement. Logging 3.16.0 is
the latest compatible Logging release for both matrix jobs; its resolver selects
compatible transitive versions. The Logging 3.0.0 floor is not tested. Batch
0.17.0 lacks CancelJob and is asserted to fail closed, not treated as
cancellation-capable. Existing direct-backend cancellation tests assume newer
SDK enums/methods and fail on 0.17.0; they are unchanged. Consequently the floor
CI job runs hosted contracts and shared job tests, while the current SDK job
runs the full public offline suite.

Existing direct jobs, GCP Batch, async, and execution-idempotency suites remain
applicable. The SDK workflow deselects only the seven named live-provider/Docker
regression classes and live Boxd cases, and ignores the live Novita E2E module.
It retains `LocalRegressionTests` and the separate fake-HTTP remote regressions;
do not exclude the whole mixed `test_regression.py` module. No tests here contact
provider services.
