# Hosted control-plane API contract

Tenant resources returned by `/v1` are scoped to the authenticated product key.
Provider configuration and health from `GET /v1/providers` and
`POST /v1/providers/{backend}/health-check` are deployment-scoped metadata,
redacted and protected by provider scopes rather than tenant-specific provider
accounts. JSON errors use `{"detail": ...}`. Authentication failure is `401`,
missing scope or policy denial is `403`, missing tenant resource is `404`,
stale/idempotency conflict is `409`, and invalid input is `422`. Provider errors
expose only the stable, redacted fields `code`, `backend`, `op`, `retryable`,
`outcome`, and `context`. Unexpected errors return a fixed `500` message.

## Authentication and request safety

Non-browser clients send `Authorization: Bearer bsk_live_...`. Browser users
submit the product key once to `POST /v1/dashboard/session`; the response sets
an expiring `bespoke_dashboard_session` cookie with `HttpOnly`, `Secure`,
`SameSite=Strict`, and `Path=/`. The key and raw session/CSRF tokens are never
stored in the database. `GET /v1/session` returns capabilities and the stable
CSRF token for that session. Cookie-authenticated `POST`, `PUT`, `PATCH`, and
`DELETE` requests must send that value in `X-CSRF-Token`. Bearer requests do
not use CSRF.

`/dashboard/local` is available only when explicitly enabled. It uses a Bearer
key held in tab-local `sessionStorage` and is for loopback development only.
All responses include CSP, clickjacking, MIME-sniffing, referrer, permissions,
opener, and HSTS protections.

## Scopes

| Area | Read | Mutate |
|---|---|---|
| Sandboxes | `sandboxes:read` | `sandboxes:create`, `sandboxes:execute`, `sandboxes:terminate`, `sandboxes:network` |
| Usage/cost | `usage:read` | — |
| Policy | `policies:read` | `policies:write` |
| Providers | `providers:read` | `providers:write`, `providers:reconcile` |
| Alerts | `alerts:read` | `alerts:write` |
| Audit | `audit:read` | — |
| CSV exports | `exports:read` | — |
| Retention | `retention:read` | `retention:write` |
| API keys | — | `keys:write` |

`*` grants all scopes. Legacy `sandboxes:write` maps to create, execute,
terminate, and provider reconciliation only.
Changing a running sandbox's network policy requires `sandboxes:network` or
`*`; neither `sandboxes:execute` nor legacy `sandboxes:write` grants it.

## Endpoints

| Method and path | Scope | Contract |
|---|---|---|
| `POST /v1/dashboard/session` | valid product key | Create browser session; returns CSRF and expiry |
| `DELETE /v1/dashboard/session` | session | Revoke and delete cookie |
| `GET /v1/session` | authenticated | Identity role, scopes, capabilities, session CSRF |
| `POST /v1/organizations` | admin header | Bootstrap tenant and reveal initial key once |
| `GET /v1/api-keys` | `keys:write` | List tenant keys |
| `POST /v1/api-keys` | `keys:write` | Issue a tenant key and reveal its secret once |
| `DELETE /v1/api-keys/{key_id}` | `keys:write` | Revoke a tenant key by ID |
| `POST /v1/sandboxes` | `sandboxes:create` | Policy-atomic, idempotent provider creation |
| `GET /v1/sandboxes` | `sandboxes:read` | Tenant inventory |
| `GET /v1/sandboxes/{id}` | `sandboxes:read` | Lifecycle record |
| `GET /v1/sandboxes/{id}/detail` | `sandboxes:read` | Timeline, attempts, executions, cost, observations |
| `PUT /v1/sandboxes/{id}/network` | `sandboxes:network` | Replace egress policy; returns updated lifecycle record |
| `POST /v1/sandboxes/{id}/execute` | `sandboxes:execute` | Exactly one code or command request |
| `DELETE /v1/sandboxes/{id}` | `sandboxes:terminate` | Idempotent termination; optional revision `If-Match` |
| `GET /v1/costs` | `usage:read` | Group by `sandbox`, `backend`, or `day` |
| `GET/PUT /v1/policies/current` | policy scope | Tenant guardrails |
| `GET /v1/policy-summary` | policy + usage read | Limits, utilization, denials |
| `GET /v1/providers` | `providers:read` | Redacted configuration/health |
| `POST /v1/providers/{backend}/health-check` | `providers:write` | Run configured checker |
| `GET /v1/reconciliation` | `sandboxes:read` | Reconciliation summary |
| `POST /v1/reconciliation/{backend}` | `providers:reconcile` | Apply provider observations |
| `GET/PUT /v1/alerts/configuration` | alert scope | Thresholds and feature toggles |
| `GET /v1/alerts` | `alerts:read` | Cursor-paginated durable events |
| `GET /v1/audit` | `audit:read` | Cursor-paginated append-only history |
| `GET/PUT /v1/retention/current` | retention scope | Operational/audit days |
| `POST /v1/retention/apply` | `retention:write` | Apply tenant purge and return counts |
| `GET /v1/exports/{kind}.csv` | `exports:read` | `usage`, `costs`, or `ledger` page |

Creation and execution accept `Idempotency-Key`. Reusing a creation key with an
identical request returns the same logical sandbox; changing the request returns
`409`. Termination accepts a positive version in `If-Match` and rejects a stale
revision with `409`, while an already completed termination remains idempotent.

## Network policies

`POST /v1/sandboxes` accepts a `network` object using the SDK's `NetworkPolicy`
fields:

```json
{
  "backend": "daytona",
  "network": {
    "mode": "allowlist",
    "domains": ["api.example.com"],
    "cidrs": []
  }
}
```

`mode` is `public` (the default), `none`, or `allowlist`. `domains` and `cidrs`
are arrays of strings, defaulting to empty. An allowlist needs at least one
entry; the other modes cannot include entries. CIDRs must be valid IP networks.
Unknown policy fields are rejected. Supplying both a non-null `network` and
non-null `allow_internet` returns `422`, even when their intent agrees. Omitting
`network` preserves the existing legacy behavior.

To replace a running sandbox's policy, send the policy itself as the body of
`PUT /v1/sandboxes/{id}/network`, for example `{"mode":"none"}`. This replaces
the entire policy, including previous allowlist entries; it is not a patch.
The response is the sandbox record with `config.network` set to the last
successfully applied policy and an incremented `version`. An old
`config.allow_internet` field is removed after a successful replacement.
Creation idempotency still compares the original creation request: replaying
it returns the existing record and does not restore the original policy.

Backend capabilities and restrictions match the
[direct SDK](SDK_REFERENCE.md#network-policy). Unsupported modes or runtime
updates return `422` with `detail.code="feature_not_supported"`; invalid
provider-specific configuration also returns `422`. Provider errors retain
their structured fields, including `op="set_network"`. Detached runtimes
(including after a server restart) and non-running sandboxes return `409`.
Updates are tenant-scoped, require `sandboxes:network`, obey browser CSRF
protection, and produce `sandbox.set_network` audit records.

Network updates and operator/shutdown termination serialize per sandbox,
including the provider call and policy persistence. TTL cleanup defers a
sandbox with an active lifecycle operation until a later supervisor pass;
other expired sandboxes can still be cleaned up in the current pass.

The server records the new policy only after the provider call succeeds.
A timeout or connection error can leave the provider state unknown; the
stored policy is the last confirmed value, not independent verification of
current egress. Updates do not use creation/execution idempotency keys and
the client does not retry them automatically.

With `RemoteSandboxClient`, pass the existing Python value object:

```python
from bespokelabs.sandbox import NetworkPolicy, RemoteSandboxClient

client = RemoteSandboxClient("https://sandbox.example", product_api_key)
with client.create(
    "daytona",
    network=NetworkPolicy(mode="allowlist", domains=["api.example.com"]),
) as sandbox:
    sandbox.set_network(NetworkPolicy(mode="none"))
```

This example's key needs `sandboxes:create`, `sandboxes:network`, and
`sandboxes:terminate` (or `*`). `RemoteSandbox.set_network()` returns `None`
on success and raises `RemoteSandboxError` for HTTP/provider failures.
`client.get(sandbox.id)` exposes the recorded policy with `sandboxes:read`.

## Pagination and CSV

`GET /v1/alerts` and `/v1/audit` accept opaque `cursor` plus `limit` from 1 to
200 and return `{"items": [...], "next_cursor": string|null}`. Do not parse or
modify cursors. CSV endpoints accept `limit` from 1 to 1000 and return the next
opaque value in `X-Next-Cursor`; absence means the export is complete. Each CSV
page repeats a stable header. Follow the cursor until absent and concatenate
data rows without repeating headers.

To prevent spreadsheet formula execution, the HTTP export layer prefixes text
cells beginning with `=`, `+`, `-`, `@`, tab, carriage return, or line feed with
a single quote. Fixed numeric columns—including signed reconciliation deltas—
remain numeric and are not altered. Consumers that need the original text may
remove exactly one leading quote only when the following character is one of
those protected prefixes.

Exports are snapshot-like pages over ordered current data, not a transaction
spanning every page. If exact point-in-time evidence is required, quiesce tenant
writes or export from a consistent database backup. Financial ledger rows are
not affected by operational retention.

## Alert and retention payloads

Alert configuration fields are `budget_threshold_percent` (1–100 or null),
`repeated_failures_count` (positive or null),
`repeated_failures_window_minutes` (1–10080),
`provider_degradation_enabled`, `long_running_secs` (positive or null), and
`failed_cleanup_enabled`. Alert types are `budget_threshold`,
`repeated_failures`, `provider_degradation`, `long_running`, and
`failed_cleanup`.

Retention accepts `operational_days` and `audit_days`, each from 1 to 3650.
Applying it deletes only aged alerts, policy denials, provider observations,
and audit events in the authenticated tenant. The response reports one deletion
count per dataset.
