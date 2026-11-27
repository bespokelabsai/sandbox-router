# Deploying the jobs-only service

The jobs-only service is the [hosted-jobs HTTP API](HOSTED_JOBS.md) on its own:
the five `/v1/jobs` routes, bearer API-key authentication, audit records, and
two health endpoints. It keeps no state in the process, so several instances
can serve one PostgreSQL database behind a load balancer on any container
platform.

It is built by `create_job_app` in
`bespokelabs.sandbox.control_plane.job_app`. Compared with `create_app`, it
does **not**:

- construct a sandbox `ControlPlane` or serve any `/v1/sandboxes` route;
- mount the dashboard, issue dashboard sessions, or accept cookies;
- expose organization bootstrap, API-key, policy, or audit-listing routes, or
  the interactive API documentation;
- start a supervisor or any other background thread.

The job routes, scopes, request limits, response shapes, audit names, security
headers, and error responses are the ones described in
[Durable hosted native jobs](HOSTED_JOBS.md#opt-in-http-host-configuration).
Read that page first: its rules for bindings, credentials, and the admission
callback apply here unchanged.

## What you provide

The service has no configuration file and no environment variable that turns
it on. You write a small Python **host module** that builds the app from your
own bindings, admission policy, and secrets, and you tell the web server to
serve it. This is deliberate: which provider accounts may be used, by whom,
and for what, is code you review, not a setting that can be flipped.

| You provide | Notes |
| --- | --- |
| A PostgreSQL database | Any PostgreSQL the instances can reach. The role needs to create tables in its schema on first start. |
| API-key pepper | Secret string. Keys are stored only as a digest keyed with it. |
| HMAC secret | At least 32 random bytes. Keys idempotency reservations. |
| Binding registry | One `ConfiguredJobBinding` per provider account and version, with an explicit organization allowlist. |
| Admission policy | The mandatory `admit(principal, binding, spec)` callback. |
| Provider credentials | Normally the identity attached to the service by the platform. Never accepted from a request. |

## Write the host module

[`examples/jobs_service_host.py`](../examples/jobs_service_host.py) is a
complete module to copy. Its shape is:

```python
from psycopg_pool import ConnectionPool

from bespokelabs.sandbox.control_plane.job_api import HostedJobConfiguration
from bespokelabs.sandbox.control_plane.job_app import create_job_app
from bespokelabs.sandbox.control_plane.job_store import hosted_job_hmac
from bespokelabs.sandbox.control_plane.job_store_postgres import (
    PostgresHostedJobRepository,
)
from bespokelabs.sandbox.control_plane.store_postgres import (
    PostgresAuthorityStore,
)

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, timeout=5,
                      check=ConnectionPool.check_connection, open=True)
authority = PostgresAuthorityStore(pool, key_pepper=KEY_PEPPER)
repository = PostgresHostedJobRepository(pool, hosted_job_hmac(HMAC_SECRET))

app = create_job_app(
    authority=authority,
    repository=repository,
    configuration=HostedJobConfiguration(
        bindings=(configured_binding,),
        submit_bindings={"batch": 1},
        hmac_secret=HMAC_SECRET,
        admit=admit,
    ),
    on_shutdown=pool.close,
)
```

Points that are easy to get wrong:

- Build the authority store before the job repository; the repository refuses
  a database that has no `organizations` table. Both must use the same
  database, and the repository's HMAC function must be
  `hosted_job_hmac(configuration.hmac_secret)`.
- Both stores apply their migrations when constructed, so importing the module
  needs a reachable database. An instance that cannot reach it exits during
  startup and the platform restarts it. Instances that start together apply
  each migration once.
- Read secrets from wherever your platform injects them (the example reads
  environment variables). Do not put them in the image or the module.
- `admit` must be a real policy. Restrict images, resources, deadlines,
  provider options, and which organizations may use the binding. It must be
  pure: no quota holds, debits, or other side effects, because it runs again
  on every replay. Anything other than exactly `True` denies.
- Provider client factories should return one reused client per binding and
  must authenticate as the account named in the binding.
- `create_job_app` works with the SQLite stores too (`SQLiteStore` and
  `HostedJobRepository`), which is convenient for a single local instance.
  Several instances need PostgreSQL.

## Seed an organization and API key

The service has no route that creates organizations or keys. Seed them from an
operator shell that has the same configuration as the service, naming the
authority store object in your host module:

```bash
python -m bespokelabs.sandbox.control_plane.job_seed my_host:authority \
    --organization "Example" --key-name ci
```

```text
organization_id=org_...
api_key_id=key_...
api_key=bsk_live_...
```

The secret is printed once and cannot be recovered; only its digest is stored.
Running the command again with the same names creates nothing and prints no
secret. Without `--scope` the key gets the five job scopes (`jobs:submit`,
`jobs:read`, `jobs:result`, `jobs:logs`, `jobs:cancel`); repeat `--scope` to
grant fewer. Use a new `--key-name` to issue another key for the organization.

The same operation is available as a function:

```python
from bespokelabs.sandbox.control_plane.job_seed import seed_job_access

seeded = seed_job_access(authority, organization="Example", key_name="ci")
if seeded.issued is not None:
    print(seeded.issued.secret)
```

Seeding only creates access rows. A new organization can submit nothing until
you add its ID to a binding's allowlist in the host module and redeploy.
There is no revocation route either: to revoke a key, set `revoked_at` on its
`api_keys` row to the current time.

## Build and run the image

`images/service/Dockerfile` installs the package with the `server`,
`postgres`, and `gcp-batch` extras and runs as an unprivileged user. Build it
from the repository root, because the build context is the package source:

```bash
docker build -f images/service/Dockerfile -t sandbox-jobs-service .
```

The image contains no host module. Add yours in a derived image:

```dockerfile
FROM sandbox-jobs-service
COPY my_host.py /app/my_host.py
ENV APP_MODULE=my_host:app
```

The container runs `uvicorn "$APP_MODULE" --host 0.0.0.0 --port "$PORT"`.
`PORT` defaults to 8080; most platforms set it for you. The module may also be
given as the container argument, which takes precedence over `APP_MODULE`.
`/app` is the working directory and is on the import path.

```bash
docker run --rm -p 8080:8080 \
    -e JOBS_DATABASE_URL="postgresql://user:password@db.example.internal/jobs" \
    -e JOBS_API_KEY_PEPPER="replace-with-a-secret" \
    -e JOBS_HMAC_SECRET_HEX="replace-with-64-or-more-hex-characters" \
    -e JOBS_BATCH_PROJECT="example-project" \
    -e JOBS_BATCH_REGION="us-central1" \
    -e JOBS_BATCH_ACCOUNT="jobs-service@example-project.example" \
    my-jobs-service
```

Each container runs one server process. Scale by adding instances, not by
adding worker processes inside a container. Terminate TLS in front of the
service; responses carry a strict transport security header on the assumption
that clients reach it over HTTPS. Configure the platform's access logs and any
tracing not to record request headers, bodies, or query strings.

## Health endpoints

Neither endpoint needs credentials, and neither contacts a provider.

| Endpoint | Meaning | Use it for |
| --- | --- | --- |
| `GET /healthz` | Always `200 {"status": "ok"}` while the process serves requests. Touches nothing. | Liveness and startup probes. |
| `GET /readyz` | `200 {"status": "ready"}` after one trivial database read; `503 {"status": "unavailable"}` if the read fails or does not finish within 5 seconds. | Readiness probes and load-balancer health checks. |

Do not use `/readyz` for liveness: a database outage would then restart every
instance instead of only taking them out of rotation. The readiness deadline
is `create_job_app(readiness_timeout_secs=...)`; set the platform's probe
timeout a little above it. `readiness_check=` replaces the database read with
your own callable, which must raise when the instance is not ready.

## What must be identical on every instance

Instances coordinate only through the database, so they must agree on
everything that gives stored rows their meaning:

- **HMAC secret.** It keys idempotency reservations. An instance with a
  different secret does not recognise existing reservations and could dispatch
  a replayed submission a second time.
- **API-key pepper.** An instance with a different pepper rejects every key.
- **Binding registry and admission policy.** Same binding IDs, versions,
  accounts, projects, regions, and allowlists. Roll out a changed binding as a
  new version, keep the old version registered while its jobs still need
  status, logs, or cancellation, and never repoint an existing version.
- **Database.** One database and schema for all instances.
- **Clocks.** Reasonably synchronised; dispatch and observation leases expire
  by wall-clock time.

Treat a change to either secret as a migration, not a rolling update: during a
rollout, old and new instances serve traffic at the same time.

## Request timeouts and CPU

`POST /v1/jobs` does the provider submission inside the request and answers
when the provider has accepted the job or the attempt is known to be
unconfirmed. The service allows that attempt a 60-second submit deadline.

- Set the platform's request timeout, and any proxy or load-balancer timeout
  in front of it, comfortably above 60 seconds (for example 120 seconds).
  A request cut off earlier leaves the job in an uncertain state that is only
  resolved when the client replays the same `Idempotency-Key` or polls the
  job.
- Keep CPU allocated for the whole request. Nothing runs after the response is
  sent, so the service does not need CPU between requests, but a platform that
  throttles CPU mid-request can stall a submission past its deadline.
- Clients should use an HTTP timeout above the submit deadline and retry with
  the same `Idempotency-Key`.
- Give instances a graceful shutdown period longer than the request timeout so
  in-flight submissions finish before the process is stopped.

## Jobs advance only when polled

There is no background sweeper or reconciler. A job's stored state changes
only while a request for that job is being served: the read, result, logs,
and cancel routes observe the provider as needed and record what they see.
Consequences:

- A job nobody polls stays at its last recorded phase, even after the provider
  finished it. Clients must poll until the phase is `terminal`.
- A requested cancellation is durable intent; it is confirmed by later polls.
- A submission whose outcome was lost (for example an instance stopped
  mid-request) is resolved by a replay of the same `Idempotency-Key` or by a
  poll, not by a timer.
- Scaling to zero instances is safe for correctness. Nothing is missed while
  no instance runs; state simply does not advance until the next request.

## Connection-pool sizing

Each store operation borrows one connection for one short transaction and
returns it. Connections are not held while a provider call is in progress,
and a single request performs several operations in sequence.

Size the pool so that the fleet can never exceed what the database allows:

```text
max_instances x pool max_size  <=  database connection limit - reserved
```

where `reserved` covers administrative sessions, migrations run from operator
shells, and any other application using the database. For example, with a
limit of 100 connections and 20 reserved, 8 instances may each have a pool of
at most 10. If the platform autoscales, use its maximum instance count, not
the usual one. Put a connection pooler in front of the database if you need
more instances than this allows. Transaction-level pooling is compatible
because no session state is kept between operations, provided the driver's
server-side prepared statements are turned off as such poolers require (for
`psycopg`, pass `prepare_threshold=None` in the pool's connection arguments).

Also set:

- a short pool checkout timeout (the example uses 5 seconds), so that a
  saturated pool or an unreachable database fails requests quickly instead of
  queueing them behind the request timeout;
- a connection check on checkout, so that connections closed by a database
  restart or an idle timeout are replaced rather than handed to a request;
- a per-instance request concurrency limit on the platform that is not far
  above the pool size, so that excess load is spread to other instances
  rather than waiting for connections.

## Limits

This service adds no rate limiting, per-organization quota, retention, or
cleanup; enforce spend and resource policy in `admit` and at the provider. It
provides the hosted-job path only. The sandbox control plane, its dashboard,
and its billing records still require `create_app` on a single-process SQLite
deployment, as described in
[Control plane operations](CONTROL_PLANE_OPERATIONS.md).
