# Backends and feature support

[Back to README](../README.md)

Provider requirements, Monty usage, adapter capabilities, and authentication settings.

**On this page**

- [Supported Backends](#supported-backends)
- [Local](#local)
- [Cloud](#cloud)
- [Monty: local Python execution](#monty-local-python-execution)
- [Novita: Code Interpreter](#novita-code-interpreter)
- [Feature Support Matrix](#feature-support-matrix)
- [Environment Variables](#environment-variables)

## Supported Backends

### Local

Local execution needs no provider account. Docker, Ray, and Safehouse have
the runtime requirements listed below.

| Backend | Extra | Requires |
|---|---|---|
| Local subprocess | _(none)_ | Python installed |
| [Monty](https://github.com/pydantic/monty) | `[monty]` | Bundled Monty worker binary |
| [Agent Safehouse](https://github.com/eugene1g/agent-safehouse) | _(none)_ | macOS + `safehouse` CLI |
| [Docker](https://www.docker.com) | `[docker]` | Docker daemon running |
| [Ray](https://www.ray.io) | `[ray]` | Ray installed (local or remote cluster) |

### Cloud

| Backend | Extra | Auth |
|---|---|---|
| [Daytona](https://www.daytona.io) | `[daytona]` | `DAYTONA_API_KEY` |
| [Tensorlake](https://tensorlake.ai) | `[tensorlake]` | `TENSORLAKE_API_KEY` or `tl login` |
| [Modal](https://modal.com) | `[modal]` | `MODAL_TOKEN_ID` + `MODAL_TOKEN_SECRET` |
| [RunPod](https://www.runpod.io) | _(none)_ | `RUNPOD_API_KEY` + registered SSH key |
| [E2B](https://e2b.dev) | `[e2b]` | `E2B_API_KEY` |
| [Novita](https://novita.ai) | `[novita]` | `NOVITA_API_KEY` |
| [boxd](https://boxd.sh) | `[boxd]` | `BOXD_API_KEY` |
| [Google Cloud Batch](https://cloud.google.com/batch) (jobs only) | `[gcp-batch]` | Application Default Credentials |

You only need to install the backend you use. The others are lazily imported.

Local and Ray execute subprocesses on the host or Ray worker and inherit its
environment. Their workspace path rewriting is a convenience, not an OS
security boundary. Safehouse wraps subprocesses with its macOS policy, while
its file helpers still access the host directly. Use the isolation properties
of your selected backend when deciding which workloads to run.

## Monty: local Python execution

Monty runs a Python subset in a local Rust interpreter worker. It needs no
provider account or Docker daemon. Install `bespokelabs-sandbox[monty]`
(Python 3.10+), then use the same code and file APIs:

```python
from bespokelabs.sandbox import NetworkPolicy, Sandbox

with Sandbox(
    "monty",
    memory_mb=64,
    timeout_secs=5,
    network=NetworkPolicy(mode="none"),
    files={"/data/numbers.json": "[1, 2, 3]"},
) as sb:
    result = sb.execute_code(
        "import json\n"
        "numbers = json.loads(open('/data/numbers.json').read())\n"
        "print(sum(numbers))"
    )
    print(result.stdout)  # 6
```

Globals and files persist between calls in one sandbox. `execute_code()`
accepts `python` or `python3` and returns captured stdout/stderr and an exit
code; trailing expression values are not printed automatically. `return_type=`
still parses printed JSON. `SandboxClient` and the async wrappers also work;
each sandbox owns a separate worker pool, closed by `destroy()` or context exit.
Calls on the same session are serialized.

Files live in memory through Monty's `OSAccess`, including writes from code
and single-file uploads/downloads. `workdir` is an absolute **virtual** POSIX
path (default `/`), created automatically and reset before each execution.
Relative SDK paths use that directory. `env_vars` exposes only supplied
values; host files, environment, network, callbacks and modules are not exposed.
Networking is always unavailable, including with the default legacy
`allow_internet=True`; explicit `network=` accepts only `none`.

`memory_mb` limits Monty's allocator usage, not worker RSS or total host memory.
`timeout_secs` sets the per-feed execution budget, per-turn worker deadline,
and sleep budget; it is not a single end-to-end wall-clock deadline.
Timeouts return exit code 124; guest errors return 1. A timeout, memory/output
limit, or worker crash closes the session: create a new sandbox afterward.
Ordinary syntax and runtime errors leave the session usable.

Supported `backend_options` are `binary_path` (explicit worker executable),
`max_output_bytes` (default 10 MiB per execution), `max_file_bytes` (default
50,000,000 total file-content bytes), `max_recursion_depth` (default 1000),
and `max_suspensions` (default 1000 host round trips per session). Numeric limits
must be positive; unknown options are rejected. File and output budgets are
separate from interpreter memory.

Shell commands, CLI agents, package installation, presets, `git_repo`, directory
transfers, executable permissions, images, CPU/GPU reservations, disk volumes,
ports, snapshots, and session reattachment are unsupported. Use `files=` (or
`build_files_map`) to seed directory contents. Monty is a language-level sandbox;
it does not provide container/VM isolation or full CPython/package compatibility.
See the upstream [language limitations](https://pydantic.dev/docs/monty/limitations/)
and [security model](https://pydantic.dev/docs/monty/concepts/security/).

## Novita: Code Interpreter

Install `bespokelabs-sandbox[novita]` and supply `NOVITA_API_KEY` through your
process environment. `NOVITA_DOMAIN` is optional; the adapter leaves endpoint
selection to the SDK. This is a separate backend; the E2B adapter is unchanged.

```python
from bespokelabs.sandbox import Sandbox

sb = Sandbox("novita", timeout_secs=600)
try:
    result = sb.execute_code("print(6 * 7)")
    print(result.stdout)
    sb.write_file("/tmp/data.bin", b"\x00\xff")
    assert sb.read_file("/tmp/data.bin") == b"\x00\xff"
finally:
    sb.destroy()  # Explicit cleanup surfaces errors; context exit is best-effort.
```

The adapter forwards `timeout_secs` as `timeout` (seconds, router default 600),
`template` as `template`, and `env_vars` as `envs`. `backend_options` is merged
last. A custom template must include the Code Interpreter/Jupyter runtime;
an arbitrary base template is not sufficient. The SDK default is
`code-interpreter-v1`. The documented default region is `us-phx-1`, domain
`us-phx-1.sandbox.novita.ai`. This pair passed the dedicated public and protected
live acceptance tests with SDK 2.1.3; other regions/templates were not tested.

Supported adapter operations are Python execution (`python`/`python3`), synchronous
shell commands (including nonzero exit results), file listing, binary-safe
read/write and whole-file upload/download, explicit destruction, and port
exposure. Python execution errors return exit code 1 and traceback in stderr.
`expose_port()` returns HTTPS and, for protected services, the
`Sandbox-Traffic-Access-Token` header recommended by the current
[secured-access guide](https://docs.novita.ai/guides/sandbox-secured-access).
This header passed live protected-service checks before and after reconnect;
missing/wrong tokens were rejected. SDK 2.1.3 itself still uses
`E2B-Traffic-Access-Token` internally for `run_code`; the adapter does not patch
SDK internals or change the E2B adapter. `TRAFFIC_ACCESS_TOKEN` in documentation
examples is a variable holding the credential, not an HTTP header name.
Headers are secrets; do not log them. Start and bind your service to `0.0.0.0` separately.

No snapshots, jobs, listing protocol, separate pause/resume API, runtime network
updates, or generic CPU/RAM/disk/GPU/image mappings are provided. Dockerfiles
and restrictive `NetworkPolicy` modes remain rejected by shared validation.
`allow_internet=False` is **not mapped** and does not enforce isolation. Native
creation options are an escape hatch, not portable network-policy support.

### Reattachment and lifecycle

State contains only the sandbox ID and API domain, never credentials or access
tokens. `Sandbox.resume(state)` creates a fresh `Novita(domain=...)` client and
calls `code_interpreter.connect(sandbox_id)`. The SDK returns a
`CodeInterpreterSandbox` and obtains controller/traffic authentication itself.
The adapter uses no cached handle, raw GET reconstruction, or deprecated handle
constructor. It does not create a replacement or rerun workspace/preset setup.
Credentials must be supplied separately in the reconnecting process environment.

This is native SDK connect behavior, not read-only attachment: SDK 2.1.3 POSTs
`/sandboxes/{id}/connect` with its default `timeout=300`. It may extend a running
sandbox's expiry and can wake a paused sandbox. The adapter neither gates on
running state/auto-resume policy nor restores the old expiry. No additional
pause, snapshot restore, or automatic recreation feature is implemented; recovery
of paused, deleted, or expired resources is not an adapter acceptance guarantee.
Paused recovery was not live-tested. Ambiguous connect timeouts/transport errors
have unknown outcome and are not marked retryable.

State serialization supports domain-based endpoints only: custom
`api_url`/`sandbox_url` overrides and SDK debug mode cannot be serialized. They
remain SDK connection settings, not secret-bearing URLs saved in JSON. Reconnect
uses current environment credentials and SDK connection configuration; callers
must supply consistent settings in a different process.

The verified SDK baseline is **novita-sandbox 2.1.3** (import `novita_sandbox`,
package metadata requires Python >=3.9; this project requires >=3.10). Both
`Novita().code_interpreter.create(...)` and `.connect(id)` return
`CodeInterpreterSandbox`. The optional dependency remains `>=2.1.3,<2.2` as a
conservative tested-baseline bound, not because the adapter still needs the old
internal reconstruction path. Earlier/newer SDK releases and other Python
versions were not tested, so the supported range has not been broadened.

Files use `read(path, format="bytes")` (SDK `bytearray`, converted to `bytes`)
and `EntryInfo.type == FileType.DIR` (`"dir"`, versus `"file"`). A completed
nonzero command raises `CommandExitException` with stdout, stderr and exit code;
the adapter returns those fields as `SandboxResult`. `kill()` returns `False`
only for an already-absent resource (HTTP 404); other failures are surfaced.

References: [PyPI 2.1.3](https://pypi.org/project/novita-sandbox/2.1.3/),
[first Code Interpreter sandbox](https://docs.novita.ai/guides/sandbox-your-first-agent-sandbox),
[regions](https://docs.novita.ai/guides/sandbox-faq-regions),
[connect semantics](https://docs.novita.ai/guides/sandbox-connect).

### Dedicated acceptance tests

Offline, from a checkout installed with `[dev,novita]`:

```bash
python -m pytest -q tests/test_novita_backend.py tests/test_novita_e2e.py
```

The live tests are skipped unless explicitly opted in. After securely injecting
`NOVITA_API_KEY`, choose the domain/template deliberately and run **only** this
Novita target (the general regression suite probes unrelated providers):

```bash
NOVITA_RUN_E2E=1 \
NOVITA_DOMAIN=us-phx-1.sandbox.novita.ai \
NOVITA_E2E_TEMPLATE=code-interpreter-v1 \
python -m pytest -q tests/test_novita_e2e.py --tb=short --show-capture=no \
  -o log_cli=false -o log_level=CRITICAL --junitxml=novita-e2e.xml
```

Do not enable local-variable traceback capture or SDK debug logging. The two
cases test public and protected services, use test-owned resources with a
300-second initial lifetime, reconnect through a new SDK client, and explicitly
destroy and verify absence by GET/404 in `finally`. The report records SDK version,
domain/template, resource ID, pre/post-connect `end_at`, expiry delta and cleanup
evidence, not credentials or headers. Expiry equality is not an acceptance
criterion. Missing credentials or required settings after opting in cause a skip,
not a pass. A protected reconnect failure remains an acceptance blocker; tests
must not switch the sandbox to public or bypass authentication.

Dedicated acceptance passed on 2026-11-13 UTC with Python 3.13.14,
SDK 2.1.3, `us-phx-1.sandbox.novita.ai` / `code-interpreter-v1`:

- Offline adapter and actual SDK `httpx.MockTransport`: **68 passed**, with
  socket connections blocked; opt-out live tests: **2 skipped**.
- Public/protected live tests: **2 passed, 0 failed, 0 skipped**. Python state
  and sandbox ID survived fresh-client and separate-process reconnect. Shell
  success/nonzero results, binary/empty files, transfers, directory listing and
  service access passed. Protected services rejected missing/wrong tokens with
  HTTP 403; both test resources were explicitly destroyed and confirmed absent
  by GET/404. Native connect extended their lifetimes, as expected.

Paused recovery, other regions/templates, and other SDK versions remain
unverified and outside this acceptance scope. Other providers' cloud tests
were not executed. Do not collect `tests/test_regression.py` for Novita-only
validation: its collection can create unrelated provider resources.

## Feature Support Matrix

This table describes the adapters in this repository, not every feature offered
by each provider's native SDK. “Interpreter” means an installed binary that
accepts `-c`; use shell commands for other language runtimes.

Monty's Python-only capability set is described [above](#monty-local-python-execution).
Novita's reattachment limitations are covered in the
[Novita section](#novita-code-interpreter).

| Feature | Local | Safehouse | Docker | Ray | Daytona | Tensorlake | Modal | RunPod | E2B | Novita | boxd |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `execute_code` | Interpreter | Interpreter | Interpreter | Interpreter | Python | Interpreter | Interpreter | Interpreter | Python | Python | Interpreter |
| `execute_command` | Shell | Shell | Shell | Shell | Shell | Shell | Shell | SSH | Shell | Shell | Shell |
| `list_files` | Native | Native | `find` / `ls` | Native | Native SDK | via `ls` | Native SDK | via SSH | Native SDK | Native SDK | Native SDK |
| `read_file` | Native | Native | `get_archive` | Native | Native SDK | via `cat` | Native SDK | via SSH | Native SDK | Binary SDK | Native SDK |
| `write_file` | Native | Native | `put_archive` | Native | Native SDK | via base64 | Native SDK | via SSH | Native SDK | Native SDK | Native SDK |
| `upload_file` | `shutil.copy` | `shutil.copy` | `put_archive` | Actor RPC | Native SDK | via base64 | Native SDK | via SSH | Native SDK | Whole file | Native SDK |
| `download_file` | `shutil.copy` | `shutil.copy` | `get_archive` | Actor RPC | Native SDK | via base64 | Native SDK | via SSH | Native SDK | Whole file | Native SDK |
| `snapshot` | No | No | Yes | No | No | Yes | Yes | No | No | No | Yes |
| `dockerfile=` | Rejected | Rejected | Local build | Rejected | Declarative build | Rejected | `Image.from_dockerfile` | Rejected | Rejected | Rejected | Rejected |
| `expose_port` | No | No | Published port | No | Preview link + token | No | Tunnel | No | `get_host` | `get_host` + token | Public HTTPS proxy |
| Resource limits | No | No | cpu, memory | cpu (Ray) | cpu, memory, disk | cpu, memory | cpu, memory, gpu | cpu, memory, disk, gpu | Tier-based | Template-defined | cpu, memory (size class) |
| `allow_internet=False` (legacy, prefer `network=`) | Ignored | Ignored | Yes | Ignored | Ignored | Yes | Ignored | Rejected | Ignored | Ignored | Yes |
| `network=` modes | public | public | public, none | public | all | public, none | all | public | public | public | all |
| `set_network()` | No | No | No | No | Yes | No | public, allowlist | No | No | No | Yes |
| Isolation | Process-level | macOS `sandbox-exec` | Container | Process | Full VM | Container | Container | Container | Full VM | MicroVM | Full VM |
| `gpu=` reservation | No | No | No | No | No | No | Yes | Yes | No | No | No |
| Needs install | Nothing | `safehouse` CLI | Docker daemon | `ray` | API key | `tl login` | API key | API key + OpenSSH | API key | API key | API key |

Daytona applies CPU/RAM/disk overrides when creating from `image`; the snapshot
creation path uses the snapshot's resources. RunPod CPU/RAM values are minimums
per GPU, not exact allocations. Ray reserves CPU scheduling resources without
requesting GPU or memory limits. Tensorlake currently does not forward the
top-level `env_vars` option; inside-agent `AgentSpec.env` can supply per-command
variables. Its `read_file` converts text stdout to bytes; use `download_file`
for binary data. boxd `expose_port` URLs are public and unauthenticated; the
first one on a machine waits for its TLS certificate, which takes about 20
seconds.

## Environment Variables

```bash
# Docker — no auth needed, just a running Docker daemon

# Local — no auth needed

# Ray — optional remote cluster
export RAY_ADDRESS=ray://head-node:10001  # omit for local cluster

# Daytona
export DAYTONA_API_KEY=your_key
export DAYTONA_API_URL=https://app.daytona.io/api   # optional
export DAYTONA_TARGET=us                              # optional

# Tensorlake
export TENSORLAKE_API_KEY=your_key
# Or authenticate via CLI:
tl login

# Modal
export MODAL_TOKEN_ID=your_id
export MODAL_TOKEN_SECRET=your_secret

# RunPod — add the matching public key to your RunPod account
export RUNPOD_API_KEY=your_key

# E2B
export E2B_API_KEY=your_key

# Novita
export NOVITA_API_KEY=your_key
# NOVITA_DOMAIN is optional; omit it to use the SDK default region.

# boxd
export BOXD_API_KEY=your_key
export BOXD_ORG=your_org      # optional; picks the org for a BOXD_TOKEN session

# Google Cloud Batch (jobs) — Application Default Credentials
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=your-project
export GOOGLE_CLOUD_REGION=us-central1
```
