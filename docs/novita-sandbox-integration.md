# Novita Sandbox integration requirements

**Status: implemented; dedicated offline and public/protected live acceptance
passed with SDK 2.1.3 on 2026-11-13 UTC.**

This document records the first-release scope and acceptance criteria.
Reattachment uses official Code Interpreter connect, with native lifecycle
semantics. See [acceptance tests](BACKENDS.md#dedicated-acceptance-tests) for
reproduction instructions, cleanup requirements and remaining untested cases.

## Objective

Contribute an independent Novita adapter upstream to
[bespokelabsai/sandbox-router](https://github.com/bespokelabsai/sandbox-router).
Select it with `backend="novita"` and use the Novita Python SDK. Keep the existing
`e2b` backend and implementation unchanged. Replacing E2B's underlying provider
or implementing a complete E2B SDK compatibility layer is not part of this work.

The capability baseline is the current
[E2B adapter](../src/bespokelabs/sandbox/backends/e2b.py), interpreted through the
[client/session protocols](../src/bespokelabs/sandbox/protocols.py) and shared
[value types](../src/bespokelabs/sandbox/types.py). This is a scope baseline,
not an instruction to reproduce E2B's error-handling or binary-file limitations.

## Confirmed scope against E2B

| Surface | Current E2B adapter | Required Novita behavior |
|---|---|---|
| Client construction | Lazy SDK import; no provider connection | Same optional-dependency pattern; no network I/O |
| `create(config)` | Code Interpreter sandbox | Create through Novita's Code Interpreter API |
| `resume(data)` | Connect using `sandbox_id` | Use official Code Interpreter connect; retain same ID and Python state |
| `provider_resource_id` | Attached sandbox ID | Novita sandbox ID |
| `execute_code` | Python Code Interpreter | Python execution returning `SandboxResult` |
| `execute_command` | Synchronous command; generic exception wrapper | Synchronous result, including nonzero exits |
| `list_files` | Maps path, `is_dir`, size | Map path, size, and directory status from `NovitaEntryInfo.type` |
| `read_file` | Default read, encoding text if necessary | Explicit binary read returning faithful `bytes` |
| `write_file` | Accepts `bytes` or `str` | Preserve binary and text input semantics |
| `upload_file` / `download_file` | Whole-file transfer | One-shot, binary-safe local/remote transfer |
| `snapshot` | Raises `FeatureNotSupportedError` | Explicitly raise `FeatureNotSupportedError` |
| `session_state` | Sandbox ID payload | JSON-safe ID and necessary non-sensitive region/domain metadata |
| `destroy` | Calls kill but suppresses errors | Terminate the resource; surface failure to the caller |
| `expose_port` | Host URL and traffic-token headers | Return `ExposedPort` with a usable URL and required access headers |

`expose_port` is an additional supported session method, although it is not in
`SandboxBackendSession`. Optional jobs, listing, and network-update protocols
are not added for Novita. Existing shared public-layer behavior remains in force.

## Configuration mapping

The current `SandboxConfig.timeout_secs` default is **600 seconds**. The E2B
adapter constructs creation arguments from timeout, optional template and envs,
then merges `backend_options` last. Novita must preserve that precedence.

| Router input | Novita requirement |
|---|---|
| `backend="novita"` | Register a separate Novita client |
| `template` | Forward as `template` when supplied; verify Code Interpreter compatibility |
| `timeout_secs` | Forward as `timeout`, in seconds; retain the router default of 600 |
| `env_vars` | Forward as `envs` when supplied |
| `backend_options` | Merge last into creation arguments, overriding same-name mapped arguments |
| `NOVITA_API_KEY` | Read credentials from the environment; fail clearly when missing |
| `NOVITA_DOMAIN` | Optional endpoint selection; do not hardcode a region/domain |

Add an optional `novita` installation extra. Importing the router or using a
non-Novita backend must not require the Novita SDK. A missing SDK must raise
`BackendNotInstalledError` with useful installation guidance. Exact package(s)
and a justified minimum version are implementation verification gates below.

Do not independently add mappings for generic fields E2B does not map, including
`cpu`, `memory_mb`, `disk_mb`, `gpu`, `image`, `dockerfile`, and `allow_internet`.
Do not advertise these as supported Novita configuration. Existing shared
validation and rejection behavior still applies; this is not a promise to
silently accept every unsupported input. In particular:

- Keep shared Dockerfile and network-policy capability checks intact.
- Do not claim `allow_internet=False` enforces isolation when it is not mapped.
- Do not declare support for restrictive `NetworkPolicy` modes or add egress
  translation or runtime network updates in this release.
- `backend_options` remains a provider-specific creation escape hatch, not a
  claim of portable support or a reason to bypass shared safety validation.
- Returning service-access headers does not establish outbound network policy.

## Behavioral semantics

### Creation and Python execution

Use the Novita Code Interpreter creation API. Do not assume an ordinary
`novita.sandbox.create()` result provides `run_code`. The documented
`novita.code_interpreter.create(...)` entry point returns `CodeInterpreterSandbox`
in the verified SDK 2.1.3. Its matching `.connect(sandbox_id)` also returns a
Code Interpreter sandbox, as verified with actual SDK MockTransport and live tests.

Both newly created and reattached sessions must execute Python via Code
Interpreter and normalize stdout, stderr, and success/error into `SandboxResult`.
Other execution languages are not promised. Preserve shared execution behavior
rather than adding a separate public Novita execution API.

### Commands and error translation

A completed command returns `SandboxResult` even when its exit code is nonzero.
Translate Novita `CommandExitException` into that result, retaining stdout,
stderr, and the actual exit code. Preserve command/argument quoting semantics.
The shared public API's existing checked-execution behavior remains unchanged.

Provider authentication, connection, timeout, missing-resource, and request
failures must map to appropriate existing
[router exceptions](../src/bespokelabs/sandbox/exceptions.py), not escape as raw
SDK failures or be misreported as completed commands. Use operation-appropriate
creation/execution errors when no more specific existing type applies. Missing
credentials must fail before resource creation or connection. Preserve useful,
non-sensitive diagnostic context without logging credentials or access tokens.
No adapter retry loop, automatic recovery, or automatic recreation is added.

### Files and transfer

`read_file` returns `bytes`, using explicit SDK binary-read behavior rather than
decoding text and re-encoding it. Binary fidelity must also hold for writes,
uploads, and downloads, including non-UTF-8 data and empty files.

Map `NovitaEntryInfo.type` to `FileInfo.is_dir`; do not assume an `is_dir`
attribute exists on provider entries. Verify the concrete enum/string values
and preserve path and available size metadata. Upload and download are whole-file,
one-shot transfers; streaming, resumable transfer, and new transfer modes are
outside the adapter scope.

### Reattachment and state

`resume` reconnects to the same sandbox using
`Novita(domain=...).code_interpreter.connect(sandbox_id)`, consistent with the
E2B adapter's native-connect approach. Do not create a replacement or rerun
presets/workspace materialization. A resumed session retains Code Interpreter
support and the same resource ID.

The SDK owns authentication and connection lifecycle. SDK 2.1.3 sends a connect
POST with default timeout 300 seconds, may extend a running sandbox's expiry,
and can wake paused sandboxes. Do not add running-only/auto-resume-policy gates
or restore the old expiry. This is not a separate adapter pause/recovery feature;
paused, deleted or expired recovery is not promised. Paused behavior was inspected
in SDK source but was not live-tested. Live acceptance records `end_at` before
and after connect without requiring equality.

Store only `sandbox_id` and non-sensitive `domain` in the existing
`SandboxSessionState` envelope. State must survive JSON serialization. Never
include API keys, controller tokens, or traffic access tokens; credentials are
supplied separately at reconnect time. Use a new SDK client, not a cache or a
hand-constructed/deprecated handle. Domain-only serialization does not support
custom URL/debug configurations; no secret-bearing URLs are persisted.

### Port exposure and destruction

Map `get_host(port)` into an `ExposedPort` URL and include any required service
access headers. Verify the exact scheme, traffic-token source, and header name
for the selected SDK and live service. The adapter uses the documented
`Sandbox-Traffic-Access-Token` header, verified live before and after reconnect.
SDK 2.1.3 still uses `E2B-Traffic-Access-Token` internally for Python execution;
do not patch that SDK behavior or change the E2B adapter. `TRAFFIC_ACCESS_TOKEN`
is a credential variable, not a header. Never embed tokens in URLs, session state,
logs, exception messages, or representations. Existing
`ExposedPort.headers` is excluded from its representation and is still secret.
Port exposure does not start a service or require adding a network-update API.

`destroy` must surface a failed provider kill and must not mark the session as
released when termination is unconfirmed. Keep the resource ID available for
cleanup diagnostics. Do not copy E2B's swallowed kill errors. The shared
`Sandbox.__exit__` currently performs best-effort cleanup; do not expand this
work into changing that common behavior. Live tests must explicitly check
cleanup rather than treating context-manager exit as proof of destruction.

## Likely implementation touchpoints

Implementation touchpoints (existing unrelated changes are preserved):

- New `src/bespokelabs/sandbox/backends/novita.py`: Novita client and session.
- [Backend registry](../src/bespokelabs/sandbox/backends/__init__.py): `novita` entry.
- [pyproject.toml](../pyproject.toml): optional dependency extra and justified version bound.
- [Shared configuration](../src/bespokelabs/sandbox/types.py) and
  [public API](../src/bespokelabs/sandbox/sandbox.py): necessary capability/documentation
  integration only; preserve existing validation, lifecycle, and other providers.
- [README](../README.md), [backend guide](BACKENDS.md), and
  [installation guide](INSTALLATION.md): installation, credentials, scope, limitations.
- Dedicated Novita mock and opt-in live test modules under `tests/`, following
  [client/session](../tests/test_client_session.py),
  [session-state](../tests/test_session_state.py), and
  [port-exposure](../tests/test_expose_port.py) test conventions.

## Technical verification gates

These checks establish the adapter's SDK contract against the selected SDK and
[official documentation](https://docs.novita.ai). The tested dependency range remains
`novita-sandbox>=2.1.3,<2.2`; only 2.1.3 was executed, so no wider-version
compatibility is claimed.

- [x] Verify distribution/import names and SDK Python metadata (>=3.9); run on
  Python 3.13. SDK 2.1.3 is the lowest inspected/tested adapter baseline, not proof
  that earlier releases lack these methods.
- [x] Verify exact Code Interpreter create/connect signatures, accepted options,
  return types, Python execution results, and compatible template selection.
- [x] Verify one usable region/template pair without hardcoding that region into
  the adapter; confirm optional `NOVITA_DOMAIN` and state endpoint handling.
- [x] Inspect native connect's paused/lifetime semantics; verify same-ID Python
  state and actual expiry changes for running sandboxes without recreation.
- [ ] Live-test paused recovery (outside this release's acceptance promise).
- [x] Verify `CommandExitException` fields and provider exception mapping,
  binary file-read options, `NovitaEntryInfo.type`, and kill failure behavior.
- [x] Verify host URL construction and token/header requirements, including token
  availability after reattachment, without exposing credentials.

## Tests and acceptance checklist

Mock coverage and the dedicated live run passed. Untested items are explicitly
left unchecked; mocks do not replace live E2E.

### Isolated mock coverage

- [x] Optional import behavior and missing SDK/credentials; construction performs no network I/O.
- [x] Creation mappings, 600-second default, explicit timeout, template/envs,
  last-wins `backend_options`, and optional domain handling.
- [x] Python execution and shell success/nonzero results, including `CommandExitException`.
- [x] Authentication, connection, timeout, and provider request error translation.
- [x] Directory/file type mapping and byte-exact read/write/upload/download.
- [x] JSON state round-trip, secret exclusion, running-instance reconnect,
  retained resource ID/Python execution, and no repeated preset/workspace setup.
- [x] URL/header mapping, protected-service token handling, and no token leakage.
- [x] Explicit unsupported snapshot and existing shared unsupported-capability checks.
- [x] Kill failure reaches the caller and never reports successful release.
- [x] Existing E2B adapter left unchanged; optional import/registry isolation
  tested offline. Full other-provider regression was not run.

### Required real Novita E2E

The opted-in live run used environment credentials. Two
test-owned resources (one public, one protected) were created, tested and
explicitly destroyed with GET/404 absence confirmation. A credential-related
skip is not a pass. Use a dedicated Novita target: collection of
[`tests/test_regression.py`](../tests/test_regression.py) can probe other providers
(including a Modal app lookup with `create_if_missing=True`), so do not run the
full regression suite or rely on name filtering to avoid unrelated cloud activity.

- [x] Record the SDK version and one confirmed working region/template pair.
- [x] Create only test-owned resources with a finite sandbox lifetime.
- [x] Execute Python and shell commands, including successful and nonzero exits.
- [x] List a directory and verify binary read/write/upload/download round-trips.
- [x] Serialize state to JSON, reconnect through a fresh SDK client to the same
  running instance, and execute Python with preserved state again. Record native
  expiry changes rather than enforcing an unchanged deadline.
- [x] Separate-process live acceptance: both public/protected cases launch a fresh
  Python interpreter, pass only `sandbox_id`/`domain` as JSON over stdin, and use
  inherited environment credentials. Same-ID Python state and service access
  passed; missing/wrong protected-service tokens returned 403. Parent `finally`
  cleanup confirmed both resources absent by GET/404.
- [x] Reach public and protected services before and after reconnect; protected
  services reject both missing and wrong tokens. Verify the official
  `Sandbox-Traffic-Access-Token` header without fallback/dual headers; arrange
  readiness without adding a background-jobs API.
- [x] Explicitly destroy both test resources and verify GET/404 cleanup. The
  test implements cleanup in `finally`; this run did not inject a live failure.
- [x] Keep credentials/tokens out of persisted files, logs, and Git. Clean up only
  test-owned resources; report residual sandbox IDs if cleanup fails.
- [x] Record actual pass/fail/skip and cleanup evidence; do not claim completion from mocks alone.

## Non-goals

No full E2B SDK compatibility, E2B replacement, or changes to the E2B adapter.
No Novita snapshots, separate pause/resume lifecycle API, volumes, desktop/VNC, PTY, streaming,
background-job APIs, sandbox listing, or runtime network updates. No new generic
resource/image mappings, network-policy support, retry/recovery mechanisms, or
expansion of Hosted control-plane billing or recovery. No multi-region support
claim based on a single tested region. Native SDK connect effects are allowed;
they are not an extra adapter recovery mechanism.
