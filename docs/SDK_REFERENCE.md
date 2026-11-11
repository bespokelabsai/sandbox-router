# Python SDK reference

[Back to README](../README.md)

For a complete first workflow, see [process a file in a sandbox](../README.md#process-a-file-in-a-sandbox). Agent execution and presets are covered in the [agent guide](AGENT_GUIDE.md).

**On this page**

- [API Reference](#api-reference)
- [Creating a Sandbox](#creating-a-sandbox)
- [Building from a Dockerfile](#building-from-a-dockerfile)
- [Executing Code](#executing-code)
- [Running Shell Commands](#running-shell-commands)
- [Structured output](#structured-output)
- [File Operations](#file-operations)
- [Exposing ports](#exposing-ports)
- [Declarative workspaces](#declarative-workspaces)
- [Errors](#errors)
- [Workspace convenience arguments](#workspace-convenience-arguments)
- [Network policy](#network-policy)
- [Backend-specific options](#backend-specific-options)
- [Session state (resume)](#session-state-resume)
- [Listing sandboxes by label](#listing-sandboxes-by-label)
- [Detached jobs (Google Cloud Batch)](#detached-jobs-google-cloud-batch)
- [Snapshots](#snapshots)
- [Lifecycle](#lifecycle)
- [Reusing a client across many sandboxes](#reusing-a-client-across-many-sandboxes)
- [Async](#async)
- [Exceptions](#exceptions)
- [Lifecycle telemetry](#lifecycle-telemetry)

## API Reference

### Creating a Sandbox

```python
from bespokelabs.sandbox import Sandbox

sb = Sandbox(
    "local",              # "local" | "safehouse" | "docker" | "ray" | "daytona" | "tensorlake" | "modal" | "runpod" | "e2b" | "boxd"
    preset=None,          # Preset name or SandboxPreset object
    cpu=1.0,              # vCPUs (Tensorlake, Modal, Docker, Daytona, RunPod, boxd)
    memory_mb=1024,       # RAM in MB (Tensorlake, Modal, Docker, Daytona, RunPod, boxd)
    disk_mb=None,         # Disk in MB (Daytona, RunPod)
    gpu=None,             # GPU type/count (Modal, RunPod)
    timeout_secs=None,    # Omitted: preset default, otherwise 600 seconds
    image=None,           # OCI image, or Tensorlake project image name
    dockerfile=None,      # Local Dockerfile to build instead of image (Docker, Daytona, Modal)
    context_dir=None,     # Build context for dockerfile (default: its directory)
    template=None,        # Template ID (E2B, RunPod)
    env_vars=None,        # dict of environment variables
    allow_internet=True,  # Legacy best-effort flag; prefer network= (see Network policy)
    network=None,         # NetworkPolicy: enforced egress policy, or an error
    app_name=None,        # App name (Modal)
    snapshot_id=None,     # Restore from snapshot (Tensorlake, Modal, Daytona, boxd)
    workdir=None,         # Sandbox root or command working directory
    backend_options=None, # dict merged into the backend's native create call
    files=None,           # {path: bytes|str} written into the sandbox on create
    git_repo=None,        # repo URL cloned into the sandbox on create
    git_ref=None,         # branch/tag for git_repo
    workspace=None,       # Manifest of files/dirs/repos to materialize on create
)
```

Not every backend uses every parameter. Most unsupported parameters are
ignored; a backend rejects values it cannot safely honor, such as network
isolation on RunPod.

Modal GPU sandboxes accept Modal's GPU reservation strings, including a GPU
type such as `gpu="L4"` or a type and count such as `gpu="H100:2"`:

```python
with Sandbox("modal", gpu="A100") as sb:
    result = sb.execute_command("nvidia-smi")
    print(result.stdout)
```

GPU sandboxes can be preempted, so GPU workloads should tolerate interruption.

RunPod uses exact GPU type IDs and requires a GPU selection. Its official
PyTorch image is used by default because it includes SSH support:

```python
with Sandbox(
    "runpod",
    gpu="NVIDIA H100 80GB HBM3:2",
    backend_options={"ssh_private_key_path": "~/.ssh/id_ed25519"},
) as sb:
    result = sb.execute_command("nvidia-smi")
    print(result.stdout)
```

For capacity fallback, pass RunPod's native creation fields through
`backend_options`, such as a `gpuTypeIds` list containing `"NVIDIA L40S"` and
`"NVIDIA RTX A6000"`, with `gpuTypePriority="availability"`. Custom images and
templates must run an SSH daemon on port 22. Add the matching public key to the
RunPod account before creating a sandbox. `ssh_private_key_path` is optional
when the key is already discoverable by OpenSSH.

boxd rounds `cpu` and `memory_mb` up to the smallest machine size that holds
both: 1 vCPU/4 GiB, 2/8, 4/16, or 8/32. Machines boot boxd's Ubuntu 24.04 image
with Python, Node, Docker, git, and the Claude Code, Codex, and OpenCode CLIs,
so `image=`, `dockerfile=`, and `gpu=` are rejected; restore a boxd snapshot
with `snapshot_id`, or run an image inside the machine with Docker. Every
machine is created isolated, without the in-VM boxd CLI, account integrations,
or agent logins:

```python
with Sandbox("boxd", cpu=2, memory_mb=8192) as sb:
    result = sb.execute_command("docker run --rm hello-world")
    print(result.stdout)
```

Commands run as the `boxd` user, who has passwordless sudo, in `/home/boxd`.
`/workspace` is created for that user. The file helpers use boxd's file API as
that user, so a write creates missing parent directories, keeps an existing
file's mode and writes through a symlink; for root-owned paths they fall back
to sudo. `env_vars` are exported into each command rather than written to the
machine, so they stay out of its disk and its snapshots. boxd closes a command
that prints nothing for 30 minutes, so the adapter writes a NUL byte to stderr
every five minutes while one runs and strips it from the result.

`timeout_secs` is a command timeout on Local, Safehouse, Docker, Ray, and
RunPod. E2B and Modal pass it as the provider sandbox timeout; Tensorlake passes
it to `create_and_connect`. Daytona maps an explicit value to a wall-clock
`ttl_minutes` deadline, rounded up to at least one minute.
RunPod creation has a separate 10-minute default readiness timeout, adjustable
with `backend_options={"create_timeout_secs": ...}`. Daytona
destroys the sandbox when that deadline elapses in whatever state it is in,
running work included; no activity resets the clock, and the clock starts at
creation, so image pull and boot count against it. The mapping happens only
when you pass `timeout_secs` yourself. Omit it — including when a preset
supplies its own recommended value — and the Daytona sandbox gets no TTL at
all, leaving it bounded only by Daytona's default 15-minute idle auto-stop.
boxd treats an explicit value the same way, as a `delete_after` deadline that
the platform enforces even while the machine sleeps. Without one, a boxd
machine lives until `destroy()`, and hibernates after 4 hours without network
traffic.

Constructing a `Sandbox` creates the underlying sandbox immediately. To launch
many sandboxes on one backend, or to use `async`/`await`, see
[Reusing a client across many sandboxes](#reusing-a-client-across-many-sandboxes)
and [Async](#async).

### Building from a Dockerfile

Pass a local Dockerfile instead of an image name. The build context defaults
to the Dockerfile's directory:

```python
with Sandbox("daytona", dockerfile="env/Dockerfile") as sb:
    sb.execute_command("cat /etc/os-release")

with Sandbox("docker", dockerfile="env/Dockerfile", context_dir=".") as sb:
    ...
```

Docker builds the image with the local daemon and leaves it untagged. Daytona
builds it declaratively (`Image.from_dockerfile`) as part of the create call,
so raise `backend_options={"create_timeout": ...}` for slow builds; it uses the
Dockerfile's directory as the context and rejects any other `context_dir`.
Modal uses `Image.from_dockerfile`. `dockerfile` cannot be combined with
`image` or `snapshot_id`, and with a preset it replaces the preset image, so
the preset's setup commands run. Other backends raise
`FeatureNotSupportedError`.

### Executing Code

```python
result = sb.execute_code('print(1 + 1)', language="python")

print(result.stdout)     # "2"
print(result.stderr)     # ""
print(result.exit_code)  # 0
```

`language` defaults to `"python"`. The Daytona and E2B adapters ignore this
argument and call their Python code endpoints. Other adapters invoke an
installed interpreter as `<language> -c <code>`; for runtimes with different
flags, use `execute_command` explicitly.

By default, inspect `exit_code` on the returned `SandboxResult`; a nonzero
exit is not uniformly converted into an exception. Local, Safehouse, Docker,
and Ray return exit code `124` on execution timeout. Provider/transport
failures can instead raise `SandboxError` subclasses.

### Running Shell Commands

```python
result = sb.execute_command("ls -la /tmp")
result = sb.execute_command("grep", args=["-r", "TODO", "/app"])
```

### Structured output

Pass `return_type` to `execute_code` or `execute_command` to parse stdout into
a Pydantic model, dataclass, or class accepting keyword arguments:

```python
from pydantic import BaseModel
from bespokelabs.sandbox import Sandbox

class Stats(BaseModel):
    count: int
    mean: float

with Sandbox("local") as sb:
    stats = sb.execute_code(
        'import json; print(json.dumps(dict(count=3, mean=2.0)))',
        return_type=Stats,
    )
    print(stats.count, stats.mean)

# Parse existing text, including a downloaded file's decoded contents.
stats = Sandbox.parse_as('{"count": 3, "mean": 2.0}', Stats)
```

Parsing accepts JSON objects, including objects in Markdown fences or
surrounding text. It raises `CommandFailedError` if execution exited nonzero,
and `SandboxExecutionError` if no object can be parsed or constructed.
`json_schema(Stats)` generates a prompt instruction; for command calls,
`inject_schema=True` appends that instruction to the last item in `args` when
`return_type` is supplied. These helpers do not force the command to emit JSON.

### File Operations

```python
# List files
files = sb.list_files("/home")
for f in files:
    print(f.path, f.is_dir, f.size)

# Read / write in-memory content
sb.write_file("/tmp/config.json", '{"key": "value"}')
data = sb.read_file("/tmp/config.json")  # returns bytes

# Upload a local file into the sandbox
sb.upload_file("./local_data.csv", "/home/user/data.csv")

# Download a file from the sandbox to local disk
sb.download_file("/home/user/results.json", "./results.json")

# Move a whole directory tree in or out (preserves structure + executable bits).
# Defaults to a single tar.gz transfer, falling back to a per-file loop.
sb.upload_dir("~/.claude/skills/my-skill", ".claude/skills/my-skill")
sb.download_dir("/workspace/output", "./results")
```

> Directory transfer is built on the single-file primitives above, so it works
> on every backend with the required shell tools. To seed a tree during
> creation, use `build_files_map(local, remote)` with
> `Sandbox(files=...)`, or a `Manifest` containing `LocalDir`.
> See [`examples/move_files_into_sandbox.py`](../examples/move_files_into_sandbox.py).

Directory methods return the number of transferred files. Uploads skip symlinks
and empty directories. `method="auto"` uses tar when available, otherwise
`"per_file"`; the per-file download fallback does not preserve executable bits.
`build_files_map` and `files=` carry contents only. Some provider file APIs
require parent directories to exist, so create them before writing nested paths.
On macOS, `COPYFILE_DISABLE=1` avoids AppleDouble metadata files in local tar
downloads.

### Exposing ports

`expose_port(port)` returns an `ExposedPort` with a `url` that reaches a server
listening on that port inside the sandbox, plus any `headers` the request
needs:

```python
import httpx

sb.execute_command("nohup python3 -m http.server 8080 >/dev/null 2>&1 &")
exposed = sb.expose_port(8080)
httpx.get(exposed.url, headers=exposed.headers)
```

The URLs are authenticated where the provider supports it. Daytona returns the
sandbox's preview link and keeps the sandbox private, so the link answers 401
without the `X-Daytona-Preview-Token` header in `headers`. E2B returns the
`get_host` address, and adds its traffic access token to `headers` when the
sandbox was created with restricted public traffic. Treat `headers` as a
secret; it is left out of `repr`.

Docker and Modal can only open ports at creation, and their URLs carry no
authentication. On Docker, publish the port with
`backend_options={"ports": {"8080/tcp": ("127.0.0.1", None)}}`; the URL points
at the host side of that mapping. On Modal, pass
`backend_options={"encrypted_ports": [8080]}`; the URL is the tunnel address.
Other backends raise `FeatureNotSupportedError`.

### Declarative workspaces

`files=`, `git_repo=`, and `upload_dir()` cover the imperative cases. For
anything richer, pass a `Manifest` — a typed, ordered map of *destination →
source* materialized at creation (built on the primitives above, so it works on
every backend):

```python
from bespokelabs.sandbox import Sandbox, Manifest, GitRepo, LocalDir, LocalFile, File

with Sandbox("daytona", workspace=Manifest(entries={
    "repo": GitRepo("https://github.com/org/proj", ref="main"),
    ".claude/skills/pirate": LocalDir("~/.claude/skills/talk-like-a-pirate"),
    "data/seed.csv": LocalFile("./seed.csv"),
    "config.json": File('{"env": "prod"}'),
})) as sb:
    ...
```

Custom presets default to `git_repo` clone → `files` → `workspace` entries →
preset setup (when needed). Built-in agent presets install before workspace
materialization so npm and Git are ready before cloning. All of this happens
after the provider resource exists; setup/materialization failure triggers a
cleanup attempt. Entries materialize
in insertion order, so a later one overlays an earlier one.
`GitRepo` clones, `LocalDir` / `LocalFile` upload (preserving the executable
bit), and `File` writes in-memory content. Subclass `WorkspaceEntry` for custom
sources, or call `manifest.apply(sb)` on a live sandbox.

### Errors

SDK operation errors use `SandboxError` subclasses carrying a machine-readable
`code`, the `backend` and `op` when supplied, a `retryable` flag, `outcome`, and
a `context` dict. Branch on these fields instead of parsing message strings:

```python
from bespokelabs.sandbox import Sandbox, SandboxError

try:
    with Sandbox("daytona") as sb:
        sb.execute_command("…")
except SandboxError as e:
    print(e.code, e.backend, e.op, e.retryable, e.outcome, e.context)
    # Decide whether retrying this operation is safe before repeating it.
```

Subtypes include `SandboxConfigurationError`, `SandboxCreationError`,
`CommandFailedError` (with `exit_code` / `stdout` / `stderr`),
`SandboxTimeoutError` and `SandboxConnectionError` (both `retryable`),
`SandboxAuthenticationError`, `SandboxNotFoundError`, `WorkspaceError`,
`BackendNotInstalledError`, and `FeatureNotSupportedError`. Invalid helper
arguments can still raise standard Python exceptions, such as `KeyError` for
an unknown preset. `RemoteSandboxError` is a separate HTTP-client exception
and does not inherit from `SandboxError`.

A retryable transport failure alone does not prove that repeating a create or
command is safe. Check `outcome` and cleanup state: `"unknown"` can mean the
provider performed the operation but its response was lost.

### Workspace convenience arguments

Populate the sandbox at creation instead of scripting uploads afterwards.
`files` are written after any `git_repo` clone. Built-in agent presets run setup
before workspace materialization. Custom presets default to setup afterward;
set `setup_before_workspace=True` when they install workspace prerequisites.

`files` needs nothing special and works on every backend and image:

```python
with Sandbox(
    "docker",
    image="python:3.12-slim",
    files={"/work/run.py": "print('ready')"},
) as sb:
    print(sb.execute_command("python", ["/work/run.py"]).stdout)
```

`files` values may be `str` or `bytes`.

`git_repo` runs `git clone` **inside** the sandbox, so `git` must be present in
the image. Built-in agent presets install missing Git before cloning on
Docker/Daytona/Modal when the base image supports their prerequisite setup.
For other images, make sure `git` is installed; the host-based backends
(`local`, `safehouse`) use the host's `git`.

```python
with Sandbox(
    "local",
    git_repo="https://github.com/psf/requests",
    git_ref="main",                      # optional branch/tag
) as sb:
    entries = sb.list_files("/requests")  # local workspace root / repo name
    print(f"cloned {len(entries)} entries")
```

`git_repo=` clones into a relative `<repo-name>` under the command working
directory; the location is not always `/<repo-name>` on cloud backends. Use
`Manifest(entries={"/explicit/path": GitRepo(...)})` to choose a destination.
For Local and Safehouse, `workdir` is a host directory: an explicit directory
survives `destroy()`, while an automatically created temporary directory is
removed. Daytona and Tensorlake use `workdir` for shell commands only; their
code and file APIs keep provider path semantics. RunPod defaults command
execution to `/workspace`, while its file helpers use SSH path semantics.
boxd runs commands in `/home/boxd` unless `workdir` is set, and resolves
relative file paths there.
Docker, Modal, E2B, and Ray do not map the top-level `workdir` option.

### Network policy

`network=` sets an egress policy that the backend enforces or refuses:

```python
from bespokelabs.sandbox import NetworkPolicy, Sandbox

hub_only = NetworkPolicy(mode="allowlist", domains=["8080-abc.daytonaproxy01.net"])
with Sandbox("daytona", network=hub_only) as sb:
    ...
    sb.set_network(NetworkPolicy(mode="none"))    # cut egress mid-run
    sb.set_network(NetworkPolicy())               # back to public
```

`mode` is `"public"` (the default), `"none"`, or `"allowlist"` with `domains`
and/or `cidrs`. A backend that cannot enforce the requested mode raises
`FeatureNotSupportedError` instead of creating an open sandbox. `public` is
accepted everywhere.

- Daytona: every mode, at creation and through `set_network()`. Allow-lists
  take domains or IPv4 CIDRs, not both. Preview hosts share proxy IPs, so a
  CIDR that reaches one preview host reaches all of them; allow a preview host
  by domain, which Daytona filters by SNI.
- Modal: every mode at creation (`block_network`, `outbound_cidr_allowlist`,
  `outbound_domain_allowlist`). `set_network()` uses Modal's experimental
  outbound policy API and supports `public` and `allowlist`, not `none`.
- Docker and Tensorlake: `public` and `none` at creation.
- boxd: every mode, at creation and through `set_network()`, as an egress
  allow-list. HTTP and HTTPS are admitted by hostname and other traffic by
  address, so a CIDR does not open HTTPS to a bare IP in it. `none` admits
  nothing: DNS lookups fail and every connection is dropped. The policy is
  enforced before `Sandbox(...)` or `set_network()` returns.

`allow_internet` keeps its old best-effort behavior (see the matrix) for
compatibility. Passing both `network` and `allow_internet` is an error; with
`network`, `allow_internet` is derived from it.

### Backend-specific options

`backend_options` is an escape hatch: the dict is merged into the backend's
native creation call, so you can reach provider features the unified API
doesn't model — without waiting for a new keyword. It is forwarded to Docker
`containers.run`, Modal `Sandbox.create`, E2B `Sandbox.create`, Tensorlake
`create_and_connect`, Daytona's create params, the RunPod REST creation
payload, and boxd `machines.create`; ignored by local, safehouse, and ray.

```python
# e.g. set the container hostname (a Docker-only knob)
with Sandbox("docker", backend_options={"hostname": "build-box"}) as sb:
    sb.execute_command("hostname")
```

On Daytona, `env_vars` given here is merged over the `env_vars=` parameter
rather than replacing it, and `create_timeout` (seconds) is consumed by the SDK
adapter to bound the create call itself instead of being passed as a sandbox
parameter.

On RunPod, transport-only options are consumed by the adapter rather than sent
to the REST API: `create_timeout_secs`, `api_timeout_secs`,
`poll_interval_secs`, `ssh_private_key_path`, `ssh_user`,
`ssh_connect_timeout_secs`, `ssh_strict_host_key_checking`, and
`ssh_known_hosts_file`. Other keys use RunPod's native camelCase field names.

On boxd, `name`, `name_prefix`, `ready_timeout_secs`, `snapshot_timeout_secs`,
and `auto_hibernate_timeout` are consumed by the adapter. Other keys are
`machines.create` arguments such as `org`, `labels`, `auto_destroy_timeout`,
or `idle_ignore_outbound`; an unknown key is rejected before anything is
created. `isolated` cannot be turned off.

### Session state (resume)

A **snapshot** saves state to restore later; **session state** is a
lightweight, serializable handle that reattaches to a sandbox that is *still
running* — including from another process or machine:

```python
sb = Sandbox("e2b", timeout_secs=600)
sb.execute_command("echo hi > /tmp/work.txt")

state = sb.session_state()
blob = state.to_json()          # JSON-safe; stash in a queue/DB/file
# ... do NOT destroy sb — the sandbox must stay alive to reattach ...

# Elsewhere — another worker, another process:
from bespokelabs.sandbox import Sandbox, SandboxSessionState

sb2 = Sandbox.resume(SandboxSessionState.from_json(blob))
print(sb2.read_file("/tmp/work.txt"))   # b"hi\n"
```

`SandboxClient("e2b").resume(state)` is equivalent; an existing client can
reuse its provider connection.
Resume skips preset setup and workspace materialization. Provider credentials
must be available in the resuming process; host-directory sessions require the
same accessible directory. Session state is not a complete configuration or
usage checkpoint: wrapper configuration and cumulative agent usage reset on
resume. Local, Safehouse, and boxd state include the supplied environment
overlay, which may contain secrets. Resuming a boxd machine that is in standby,
hibernated, or stopped wakes it first. Both attached handles refer to the same resource, so
destroying either affects the other.

| Backend | Resume by | session_state payload |
|---|---|---|
| Docker | container id (`containers.get`) | `container_id` |
| E2B | sandbox id (`Sandbox.connect`) | `sandbox_id` |
| Modal | sandbox id (`Sandbox.from_id`) | `sandbox_id` |
| Tensorlake | sandbox id (`client.connect`) | `sandbox_id` |
| Daytona | sandbox id (`client.get`) | `sandbox_id`, `workdir` (when set) |
| RunPod | Pod id (`GET /pods/{id}`) | `pod_id`, workdir and SSH transport settings |
| boxd | machine id (`machines.get`) | `machine_id`, workdir, env vars, org |
| Local, Safehouse | host workdir | `workdir`, env overlay |
| Ray | — (not supported) | raises `FeatureNotSupportedError` |

### Listing sandboxes by label

`SandboxClient.list_sandboxes(labels)` returns the sandboxes that carry *all*
of the given labels, on Daytona (resource labels) and Modal (sandbox tags).
Each `SandboxListing` has the provider id, the sandbox's own labels, the raw
provider state, the creation time when the provider reports one (`None` on
Modal), and a `session_state` to pass to `resume()`, for example to destroy a
sandbox nobody owns any more:

```python
client = SandboxClient("daytona")
for found in client.list_sandboxes({"team": "research", "run": "42"}):
    client.resume(found.session_state).destroy()
```

At least one non-empty label is required, so an unfiltered listing is
refused. A provider failure raises; a partial list is never returned. Daytona
lists sandboxes in any state (a stopped one's session can be destroyed, not run
in, until it is started); Modal lists running ones only. On a Modal SDK that
cannot read tags back, `labels` is only the matched filter and
`labels_complete` is `False`. Other backends raise `FeatureNotSupportedError`.

### Detached jobs (Google Cloud Batch)

A job runs one command to completion without the submitting process staying
attached, like an AWS Batch job. Jobs run on the `gcp_batch` backend, as
Google Cloud Batch jobs on GCP VMs; every other backend raises
`FeatureNotSupportedError` ("does not support jobs") from the job methods.
Submit returns a job id at once; the job keeps running if your process exits,
and any process holding the id can check on it, wait for it, read its logs,
or cancel it. This is the reattach path for a worker that restarts mid-job.

```python
from bespokelabs.sandbox import JobSpec, SandboxClient

client = SandboxClient("gcp_batch")
job_id = client.submit_job(JobSpec(
    command=["python", "train.py", "--epochs", "3"],   # argv, or a shell string
    image="us-docker.pkg.dev/my-project/repo/trainer@sha256:...",
    env_vars={"WANDB_API_KEY": key},                   # exported for the command
    cpu=4, memory_mb=8192, timeout_secs=2 * 3600,
    labels={"team": "evals"},
    backend_options={"project": "my-project", "region": "us-central1"},
))
# ... store job_id (a plain string) anywhere ...

# Later, possibly in a new process:
client = SandboxClient("gcp_batch")
status = client.wait_job(job_id, timeout_secs=3 * 3600, poll_secs=30,
                         on_poll=lambda s: print(s.state.value))
print(status.state, status.exit_code, status.reason)
print(client.job_logs(job_id, tail=100))     # combined stdout + stderr
client.cancel_job(job_id)                    # stop it
```

| Method | Returns |
|---|---|
| `submit_job(spec: JobSpec)` | job id (`str`) |
| `job_status(job_id)` | `JobStatus` |
| `wait_job(job_id, timeout_secs=None, poll_secs=10.0, *, on_poll=None)` | terminal `JobStatus`; `SandboxTimeoutError` if `timeout_secs` passes first |
| `job_logs(job_id, tail=None)` | combined stdout/stderr (`str`), or its last `tail` lines |
| `cancel_job(job_id)` | `JobStatus` after the cancel request |

`AsyncSandboxClient` has the same methods as coroutines; its `wait_job` sleeps
on the event loop and accepts a coroutine `on_poll`.

`JobStatus.state` is a `JobState`: `pending`, `running`, `succeeded`,
`failed`, `cancelled` or `unknown`. `succeeded` and `failed` normally come from the
command's own exit code (`reason="timeout"` when `timeout_secs` stopped it);
Cloud Batch can also fail a job whose command never ran, with
`exit_code=None` and `reason="batch_failed"`.
`unknown` is terminal and means the outcome can no longer be established: the
Batch job is gone (`reason="job_not_found"`). It is never reported as success.
`JobStatus.done` is true for every terminal state; `started_at` /
`finished_at` are UTC datetimes when known, and `details` carries provider
facts such as the raw Batch state (`details["batch_state"]`) and, on
`gcp_batch`, the job's labels as Batch stored them (`details["labels"]`), so a
caller can check a job is one it started before it cancels it.

`wait_job` rides out provider hiccups: before the job has been seen, four
consecutive failed reads are retried and the fifth raises; once it has been
seen, failed reads are retried with backoff (capped at 60s) until the deadline. Configuration,
authentication and not-installed errors raise at once.

**How a Batch job runs.** `SandboxClient("gcp_batch")` runs each job as a Batch
job with one container task; interactive `Sandbox("gcp_batch")` raises
`FeatureNotSupportedError`. The job id is the job's resource name,
`projects/<project>/locations/<region>/jobs/<id>`. `image` is any container
URI the VMs can pull (e.g. Artifact Registry); `cpu` / `memory_mb` become the
task's compute resource, `disk_mb` the boot disk, `timeout_secs` its
`maxRunDuration`, `env_vars` its environment, `labels` the job's and VMs'
labels (coerced to GCP label syntax; keys that collide once coerced
are rejected), `workdir` the container's `--workdir`,
and `allow_internet=False` blocks the container's external network. `gpu`
takes `L4`, `T4`, `V100`, `P100`, `P4`, `A100`, `A100-80GB`, `H100`,
`H200`, `B200` or a Compute Engine type such as `nvidia-l4`, optionally with
`:<count>`; Batch installs the driver, and the smallest machine type with that
GPU count that fits `cpu` and `memory_mb` is chosen (H100 counts below 8 need
`provisioning_model` `SPOT` or `FLEX_START`). An accelerator-optimized machine
type (A2, A3, A4, G2) carries its GPUs, so the job names only the machine type;
T4, V100, P100 and P4 attach to an N1 VM as accelerators. GPUs reach the
container through Batch's own driver install; check that the image sees them
(`nvidia-smi`) before relying on it, and add `--gpus all` through
`container_options` if it does not. Pin an image by digest (`.../repo/image@sha256:...`) to run exactly what was
built. Status maps Batch's job state;
the exit code comes from the task, and Batch's reserved exit codes become
reasons (`50005` is `"timeout"`, `50001` `"vm_preempted"`, ...). Logs are read
from Cloud Logging (`batch_task_logs`, by job uid) and may lag a few seconds.
`cancel_job` uses Batch's CancelJob on a pending or running job and keeps its
record. Batch cancels asynchronously, so `cancel_job` (and `job_status`)
return `running` with `reason="cancelling"` while it stops the VMs, then
`job_status` reads `cancelled`; `wait_job` after `cancel_job` to confirm. If
the installed SDK or the service lacks CancelJob, the job is deleted instead
(`reason="deleting"`, again what `job_status` says while Batch deletes it)
and later reads as `unknown` /
`"job_not_found"`. When Batch reports an event description, the status
carries the latest one in `details["message"]` (for a failure, why it failed:
the task's exit, or the job-level cause when no task exited); the key is
absent when Batch has given none, so read it with `details.get("message")`. Files reach the job through the
image or Cloud Storage. Credentials are Application Default Credentials.

| `backend_options` key | Meaning |
|---|---|
| `project`, `region` | Where to submit (else `GOOGLE_CLOUD_PROJECT`, and `GOOGLE_CLOUD_REGION` or `CLOUDSDK_COMPUTE_REGION`) |
| `service_account` | Service account email the VMs run as (it needs Artifact Registry read and whatever the job touches, e.g. GCS) |
| `service_account_scopes` | OAuth scopes for that service account (list) |
| `network`, `subnetwork` | VPC network and subnetwork for the VMs (`projects/<p>/global/networks/<n>`, `projects/<p>/regions/<r>/subnetworks/<s>`) |
| `no_external_ip` | `True` gives the VMs no external IP; the subnet then needs Private Google Access (Artifact Registry, GCS) or Cloud NAT |
| `machine_type` | Overrides the chosen machine type |
| `provisioning_model` | `STANDARD`, `SPOT`, `PREEMPTIBLE` or `FLEX_START` |
| `allowed_locations` | e.g. `["zones/us-central1-a"]` |
| `max_retry_count` | Task retries (default 0) |
| `container_options` | Extra `docker run` options |
| `gpu_driver_version` | GPU driver version for Batch to install |
| `job_id`, `job_id_prefix` | Explicit Batch job id, or its prefix (default `job`) |

### Snapshots

```python
snap = sb.snapshot()
print(snap.snapshot_id)

# Restore later
sb2 = Sandbox("tensorlake", snapshot_id=snap.snapshot_id)
```

| Backend | Snapshot support |
|---|---|
| Docker | Yes (`container.commit()`) |
| Tensorlake | Yes (filesystem + memory) |
| Modal | Yes (filesystem) |
| boxd | Yes (memory and disk, running processes included) |
| Daytona, E2B, Local, Ray, RunPod, Safehouse | No |

Restore a Docker snapshot using `image=snap.snapshot_id`; Docker does not map
`snapshot_id`. Modal, Tensorlake, and boxd accept `snapshot_id`; boxd snapshots
outlive the sandbox, so remove them with `boxd snapshot remove` when you are
done. Daytona can create
from an existing provider snapshot via `snapshot_id`, although this adapter
cannot create snapshots with `snapshot()`.

### Lifecycle

```python
# Context manager (recommended) — auto-destroys on exit
with Sandbox("local") as sb:
    sb.execute_code("print('hi')")

# Manual cleanup
sb = Sandbox("docker")
sb.execute_code("print('hi')")
sb.destroy()

# Check state
sb.is_alive       # True/False
sb.backend_name   # "docker"
sb.provider_resource_id  # provider ID, or None for local-style backends
```

`is_alive` tracks whether this wrapper has been destroyed; it does not poll the
provider or detect external termination. Cleanup behavior varies by adapter:
some suppress provider deletion errors. Use provider inventory or configured
control-plane reconciliation when you need confirmation of resource removal.

### Reusing a client across many sandboxes

`Sandbox(backend, ...)` builds a fresh provider connection per sandbox. When
launching many sandboxes on one backend, create a `SandboxClient` once and
reuse it — provider-level state (the Docker daemon connection, Daytona auth,
the Ray runtime) is shared across `create()` calls:

```python
from bespokelabs.sandbox import SandboxClient

client = SandboxClient("docker")

for task in tasks:
    with client.create(image="python:3.12-slim") as sb:
        sb.execute_code(task)
```

`client.create(...)` accepts the same keyword arguments as `Sandbox(...)`
and returns a regular `Sandbox` session. `SandboxClient(backend)` validates
the backend name and SDK availability up front — it raises
`BackendNotInstalledError` immediately if the backend's extra isn't
installed — but performs no network I/O until `create()`.

### Async

`AsyncSandboxClient` / `AsyncSandbox` mirror the sync API with coroutine
methods, so you can create and drive many sandboxes concurrently from one
event loop:

```python
import asyncio
from bespokelabs.sandbox import AsyncSandbox, AsyncSandboxClient

async def run_snippet(client: AsyncSandboxClient, code: str) -> str:
    async with await client.create() as sb:
        result = await sb.execute_code(code)
        return result.stdout

async def main():
    client = AsyncSandboxClient("local")
    snippets = ["print(1 + 1)", "print(2 + 2)"]
    outputs = await asyncio.gather(*(run_snippet(client, c) for c in snippets))
    print(outputs)

asyncio.run(main())
```

One-step creation works too: `sb = await AsyncSandbox.create("local")`.
Use one `AsyncSandboxClient` per event loop. Execution, files, `run_agent`,
snapshots, resume, and destruction are awaitable; `session_state()` and
properties remain synchronous. The wrapper does not expose `agent()` or
`agent_tools()`.

Backend SDKs are synchronous, so async calls are offloaded to worker
threads — the event loop is never blocked. Note that the missing-SDK check
(`BackendNotInstalledError`) surfaces at the first `await client.create()`
rather than at `AsyncSandboxClient(...)` construction, which does no I/O.

## Exceptions

```python
from bespokelabs.sandbox import (
    SandboxError,              # Base class for all errors
    SandboxCreationError,      # Sandbox failed to start
    SandboxExecutionError,     # Code or command execution failed
    BackendNotInstalledError,  # pip package missing for chosen backend
    FeatureNotSupportedError,  # Backend doesn't support this operation
)
```

All exceptions inherit from `SandboxError`, so you can catch broadly or narrowly:

```python
try:
    sb.snapshot()
except FeatureNotSupportedError:
    print("This backend doesn't support snapshots")
except SandboxError as e:
    print(f"Something else went wrong: {e}")
```

## Lifecycle telemetry

Every `create`, `setup` (preset, clone, files, workspace), `exec`, and
`destroy` emits one `telemetry.SandboxEvent` with backend, duration, outcome,
and the stable error code. Events are logged to
`bespokelabs.sandbox.telemetry` (WARNING on failure) and passed to listeners:

```python
from bespokelabs.sandbox import telemetry

telemetry.add_listener(
    lambda e: statsd.timing(
        f"sandbox.{e.op}", e.duration_secs * 1000,
        tags=[f"backend:{e.backend}", f"outcome:{e.outcome}"],
    )
)
```

A command that exits non-zero is `outcome="ok"` with `exit_code` set. Events
never include commands, code, paths, env vars, or error messages.
