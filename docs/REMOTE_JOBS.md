# Remote hosted-job SDK

`RemoteJobClient` is a new, opt-in synchronous HTTP client for the
[existing hosted-job API](HOSTED_JOBS.md). It does not enable that API, deploy a
service, or change `Sandbox`, `SandboxClient`, `RemoteSandboxClient`, native
job methods, their defaults, or their return types. Importing it requires only
the core SDK dependencies, not FastAPI, provider SDKs, or cloud credentials.

## Submit and observe

```python
import os

from bespokelabs.sandbox import JobSpec, RemoteJobClient, RemoteJobError

client = RemoteJobClient(
    "https://jobs.example.test",
    os.environ["SANDBOX_API_KEY"],
    timeout_secs=60,
    trust_env=False,
)
spec = JobSpec(["python", "worker.py"], image="python:3.12-slim")
# Persist this operation key and the exact spec/binding before sending.
operation_key = "example-operation-001"
try:
    job = client.submit(spec, binding_id="batch", idempotency_key=operation_key)
except RemoteJobError:
    # Outcome may be unknown. Do not generate another key or cancel resources.
    # After diagnosing the error, replay this exact call with the SAME key.
    raise

observation = client.status(job.id)
print(observation.phase, observation.state, observation.confidence)
print(client.logs(job.id, tail=100))
result = client.result(job.id)  # RemoteJobResult | None; None is not success.
if result is not None:
    print(result.state, result.exit_code, result.reason)

# Optional, explicit intent only; cancellation may lose a completion race.
# cancellation = client.cancel(job.id)
```

The server must already have an allowed binding, replay-safe admission, durable
storage, and credentials configured by its operator. Only the opaque binding ID
and existing `JobSpec` fields are sent. Server admission and validation remain
authoritative; this client does not add policy or discover provider credentials.

| Client method | Existing route | Existing scope | Return |
| --- | --- | --- | --- |
| `submit(spec, *, binding_id, idempotency_key)` | `POST /v1/jobs` | `jobs:submit` | `RemoteJobStatus` (HTTP 202) |
| `status(job_id)` | `GET /v1/jobs/{id}` | `jobs:read` | `RemoteJobStatus` |
| `result(job_id)` | `GET /v1/jobs/{id}/result` | `jobs:result` | `RemoteJobResult` or `None` |
| `logs(job_id, *, tail=1000)` | `GET /v1/jobs/{id}/logs` | `jobs:logs` | `str` |
| `cancel(job_id)` | `POST /v1/jobs/{id}/cancel` | `jobs:cancel` | `RemoteJobStatus` |

There is no list, wait, streaming log, delete, binding discovery, async wrapper,
or provider fallback method. Clients in separate processes can observe the same
opaque job ID; no local handle registry is required. Retain IDs yourself because
the existing API has no list/discovery route.

## Idempotency, timeouts, and uncertainty

Submission requires a **caller-owned**, nonempty visible-ASCII idempotency key
of at most 512 characters. No key is generated, no request is automatically
retried, and no resource is cancelled on an exception or client destruction.
The server scopes keys to the authenticated organization and rejects changed
payloads/bindings with 409. Replays must retain the original spec and binding;
server admission is evaluated again. Binding-version rotation can conflict with
an old submission replay; the client cannot select an old version. Operators
must preserve recovery access to existing reservations.

A transport timeout or invalid response does not establish whether submission
or cancellation took effect. `RemoteJobError.outcome` stays `"unknown"`.
After diagnosing a lost submission response, replay with the **same key and
payload**, never a new key. This is not a claim of exactly-once workload execution.
The client uses urllib's socket timeout, not an end-to-end workload deadline.
`JobSpec.timeout_secs` separately describes the requested command limit.

`RemoteJobStatus` is a strict, frozen response model with exactly `id`,
`backend`, `phase`, `state`, `confidence`, `cancel_requested`, `error_code`, and
`result`. State uses the existing `JobState` enum, but this is **not** a native
`JobStatus`: it deliberately has no `done` property. Preserve phase/confidence
and nullable result fields. A missing/unavailable/ownership-conflicting
observation, an uncertain submission, a last-known state, or cancellation intent
must not be treated as verified completion or successful cloud cleanup.
`RemoteJobResult` preserves nullable state, exit code, reason, and timezone-aware
ISO timestamp strings. Missing exit codes are never replaced with zero.

## HTTP and security behavior

- `base_url` must be an HTTP(S) origin with an optional plain path prefix, such
  as `https://jobs.example.test/gateway`. No userinfo, query, fragment, encoded
  paths, or dot segments are accepted. Job IDs must be bounded opaque
  ASCII letters/digits/underscore/hyphen identifiers, never paths or URLs.
- Use HTTPS outside trusted local tests. The client sends only the explicit
  bearer key; no dashboard cookies, CSRF tokens, ambient API credentials, or
  admin headers are supplied. Existing server bearer/cookie/CSRF precedence is
  unchanged. Do not include keys in URLs or log client credentials.
- All redirects, including same-origin redirects, are rejected rather than
  forwarding credentials or implicitly replaying a POST. Configure the final
  service URL, including any reverse-proxy prefix.
- The default `timeout_secs=60` and standard-library HTTP transport follow the
  existing remote-client pattern. `trust_env=True` (default) honors urllib's
  environment proxy settings; `False` disables environment proxies. Configure
  proxy and TLS trust deliberately; certificate verification is not disabled.
- Requests are capped at 64 KiB. Log tails must be integers from 0 to 1000;
  returned log text is capped at 1 MiB UTF-8 (8 MiB JSON envelope limit).
  Other response envelopes are capped at 64 KiB. Unknown fields, invalid types,
  and malformed/oversized responses raise sanitized protocol errors.
- HTTP errors expose a status code and fixed message through `RemoteJobError`
  (a `RemoteSandboxError` subclass), never the response body, URL, credentials,
  or raw transport exception. At most 4 KiB of an error body is read and then
  discarded. `retryable=True` on transport errors is not authorization to
  create a new submission operation. Log content is authorized workload output,
  not automatically redacted data; callers must handle it accordingly.

## Service limitations remain unchanged

This client supports distributed **callers**, not horizontally scaled service
replicas. The [existing SQLite operations constraints](CONTROL_PLANE_OPERATIONS.md)
still apply: one active API process per SQLite database, a single writer, and one
active supervisor. A client does not replace durable host storage, shared runtime
custody, restart recovery, or operator admission/security/live-provider gates.
No database rewrite, workers, hosting configuration, or deployment is included.
This SDK addition is not evidence that a service is production-ready or deployed.

The API currently lacks listing/discovery, version-pinned submission replay,
streaming logs, and a server-side wait endpoint. Those gaps cannot be solved
by inventing client routes; adding them requires separately approved server work.
