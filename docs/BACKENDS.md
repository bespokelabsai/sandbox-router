# Backends and feature support

[Back to README](../README.md)

Provider requirements, Monty usage, adapter capabilities, and authentication settings.

**On this page**

- [Supported Backends](#supported-backends)
- [Local](#local)
- [Cloud](#cloud)
- [Monty: local Python execution](#monty-local-python-execution)
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

## Feature Support Matrix

This table describes the adapters in this repository, not every feature offered
by each provider's native SDK. “Interpreter” means an installed binary that
accepts `-c`; use shell commands for other language runtimes.

Monty's Python-only capability set is described [above](#monty-local-python-execution);
the table below covers the backends that support shell commands.

| Feature | Local | Safehouse | Docker | Ray | Daytona | Tensorlake | Modal | RunPod | E2B | boxd |
|---|---|---|---|---|---|---|---|---|---|---|
| `execute_code` | Interpreter | Interpreter | Interpreter | Interpreter | Python | Interpreter | Interpreter | Interpreter | Python | Interpreter |
| `execute_command` | Shell | Shell | Shell | Shell | Shell | Shell | Shell | SSH | Shell | Shell |
| `list_files` | Native | Native | `find` / `ls` | Native | Native SDK | via `ls` | Native SDK | via SSH | Native SDK | Native SDK |
| `read_file` | Native | Native | `get_archive` | Native | Native SDK | via `cat` | Native SDK | via SSH | Native SDK | Native SDK |
| `write_file` | Native | Native | `put_archive` | Native | Native SDK | via base64 | Native SDK | via SSH | Native SDK | Native SDK |
| `upload_file` | `shutil.copy` | `shutil.copy` | `put_archive` | Actor RPC | Native SDK | via base64 | Native SDK | via SSH | Native SDK | Native SDK |
| `download_file` | `shutil.copy` | `shutil.copy` | `get_archive` | Actor RPC | Native SDK | via base64 | Native SDK | via SSH | Native SDK | Native SDK |
| `snapshot` | No | No | Yes | No | No | Yes | Yes | No | No | Yes |
| `dockerfile=` | Rejected | Rejected | Local build | Rejected | Declarative build | Rejected | `Image.from_dockerfile` | Rejected | Rejected | Rejected |
| `expose_port` | No | No | Published port | No | Preview link + token | No | Tunnel | No | `get_host` | Public HTTPS proxy |
| Resource limits | No | No | cpu, memory | cpu (Ray) | cpu, memory, disk | cpu, memory | cpu, memory, gpu | cpu, memory, disk, gpu | Tier-based | cpu, memory (size class) |
| `allow_internet=False` (legacy, prefer `network=`) | Ignored | Ignored | Yes | Ignored | Ignored | Yes | Ignored | Rejected | Ignored | Yes |
| `network=` modes | public | public | public, none | public | all | public, none | all | public | public | all |
| `set_network()` | No | No | No | No | Yes | No | public, allowlist | No | No | Yes |
| Isolation | Process-level | macOS `sandbox-exec` | Container | Process | Full VM | Container | Container | Container | Full VM | Full VM |
| `native` | No | No | `Container` | Actor handle | SDK `Sandbox` | SDK sandbox | `modal.Sandbox` | No | SDK `Sandbox` | `boxd` client |
| `gpu=` reservation | No | No | No | No | No | No | Yes | Yes | No | No |
| Needs install | Nothing | `safehouse` CLI | Docker daemon | `ray` | API key | `tl login` | API key | API key + OpenSSH | API key | API key |

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

# boxd
export BOXD_API_KEY=your_key
export BOXD_ORG=your_org      # optional; picks the org for a BOXD_TOKEN session

# Google Cloud Batch (jobs) — Application Default Credentials
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=your-project
export GOOGLE_CLOUD_REGION=us-central1
```
