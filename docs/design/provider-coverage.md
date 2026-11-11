# Design: Provider coverage — core API, optional features, and the `native` escape hatch

- **Status:** Proposed
- **Scope:** `Sandbox`, `AsyncSandbox`, `SandboxBackendSession`, every backend in `backends/`
- **Code changes:** none in this PR. Implementation follows once this design is approved.

## Problem

`bespokelabs-sandbox` gives one API over many sandbox providers (Daytona, E2B, Modal,
Tensorlake, Docker, Ray, Local, Safehouse, with Boxd and Maritime in open PRs). Each
provider's SDK has far more surface than our API covers: preview URLs and port
forwarding, streaming and background processes, pause/resume, volumes, network
policies, GPU controls, and new features every release.

Two bad ways to handle that:

1. **Wrap everything.** Every provider feature gets a portable method. The maintenance
   cost grows with (providers × features), most features have no equivalent on other
   providers, and we are always behind the provider's own releases.
2. **Wrap nothing beyond the core.** Users who need one provider-specific feature after
   creation have to read private attributes such as `sb._session._sandbox`, or drop the
   library for that workload.

Today the library sits between the two, without a stated rule for which features belong
in the unified API and with no supported route to the provider object after creation.

## Goals

- Keep the portable core small and the per-backend cost at one adapter file.
- Never block a user on a missing wrapper: every provider feature is reachable on day one.
- Make it obvious, in code and in docs, when a call stops being portable.
- Give a clear rule for when a provider feature should become part of the portable API.

## Non-goals

- Wrapping provider-specific features such as preview URLs, volumes, or network policies
  in this change.
- Changing the existing 10-method backend protocol.
- Cost-based routing across providers (the README's "coming soon" item), beyond leaving
  room for it.

## How other multi-provider libraries handle this

| Library | Portable layer | Escape hatch |
|---|---|---|
| OpenRouter | OpenAI-style chat API | Provider-specific parameters passed through untouched |
| SQLAlchemy | Engine, Session, query layer | `connection.connection` returns the raw DB-API driver connection |
| Apache Libcloud | `create_node`, `list_nodes`, … | Provider-specific methods prefixed `ex_` |

The common pattern is a small portable core, optional features that some providers
decline, and a documented way to reach the provider directly. This design adopts that
pattern explicitly.

## Current state

```mermaid
flowchart TB
    U["User code"] --> S["Sandbox / AsyncSandbox<br/>public API"]
    S --> P["SandboxBackendSession protocol<br/>10 methods"]
    P --> D["DaytonaSession"]
    P --> E["E2BSession"]
    P --> M["ModalSession"]
    P --> T["TensorlakeSession"]
    P --> K["DockerSession"]
    P --> R["RaySession"]
    P --> L["LocalSession / SafehouseSession"]
    D --> DS["daytona SDK Sandbox"]
    E --> ES["e2b SDK Sandbox"]
    M --> MS["modal.Sandbox"]
    T --> TS["tensorlake SDK sandbox"]
    K --> KS["docker Container"]
    R --> RS["Ray actor handle"]
    L --> LS["host subprocesses + temp dir"]

    U -. "backend_options at create time<br/>(supported)" .-> DS
    U -. "after create: only via private<br/>sb._session._sandbox (unsupported)" .-> ES
```

What exists today:

1. **Core contract** — `protocols.py` defines `SandboxBackendSession` with 10 methods:
   `execute_code`, `execute_command`, `list_files`, `read_file`, `write_file`,
   `upload_file`, `download_file`, `snapshot`, `session_state`, `destroy`. Plus
   `SandboxBackendClient.create` / `resume`.
2. **Optional features** — a backend that cannot do something raises
   `FeatureNotSupportedError`. Examples: `snapshot` on Daytona, E2B, Ray, Local and
   Safehouse; `resume` on Ray; `resume` on older Daytona and Tensorlake SDKs.
3. **Escape hatches**
   - `execute_command` — anything doable from a shell inside the sandbox needs no
     wrapper. This covers most real usage.
   - `backend_options` — merged into the provider SDK's create call
     (`docker.py`, `modal.py`, `e2b.py`, `tensorlake.py`, `daytona.py`). Provider-specific
     creation settings work without the library knowing about them.
   - **Gap:** no supported way to reach the live provider object after creation. It is
     held in a private attribute (`self._sandbox`, `self._container`, `self._actor`) and
     `Sandbox` exposes only `backend_name`, `is_alive` and `usage`.

## Proposal

### 1. The three-tier model (stated policy)

```mermaid
flowchart LR
    subgraph T1["Tier 1: Portable core"]
        C1["exec, files, snapshot,<br/>session_state, destroy"]
    end
    subgraph T2["Tier 2: Optional portable features"]
        C2["Same signature everywhere.<br/>Unsupported backends raise<br/>FeatureNotSupportedError"]
    end
    subgraph T3["Tier 3: Native escape hatch"]
        C3["backend_options at create<br/>sb.native after create<br/>Not portable, by design"]
    end
    T1 --> T2 --> T3
```

| Tier | Contract | Who implements | Portable? |
|---|---|---|---|
| 1. Core | Every backend implements every method | Every backend | Yes |
| 2. Optional | Same signature; may raise `FeatureNotSupportedError` | Backends that can | Yes, with a clear error elsewhere |
| 3. Native | `backend_options` in, `sb.native` out | Every backend exposes its object | No |

### 2. Add `Sandbox.native` (and `AsyncSandbox.native`)

A read-only property returning the provider's own live object:

| Backend | `sb.native` returns |
|---|---|
| `daytona` | Daytona SDK `Sandbox` |
| `e2b` | E2B SDK `Sandbox` |
| `modal` | `modal.Sandbox` |
| `tensorlake` | Tensorlake SDK sandbox object |
| `docker` | `docker.models.containers.Container` |
| `ray` | The Ray actor handle |
| `local`, `safehouse` | Raises `FeatureNotSupportedError` (there is no provider object) |

Behaviour:

- Raises the same error as other methods once the sandbox is destroyed (`_check_alive`).
- Documented as non-portable. The docstring and README section state that code using it
  is tied to one backend and that its type depends on the provider SDK version.
- The library still owns the lifecycle. Calling the provider's own terminate/kill through
  `native` is unsupported; use `sb.destroy()` so usage tracking and cleanup stay correct.
- Typed as `Any` on `Sandbox`. Users narrow it themselves (for example
  `cast(e2b.Sandbox, sb.native)`), which keeps provider SDKs out of our import graph.

```mermaid
sequenceDiagram
    participant U as User code
    participant S as Sandbox
    participant B as E2BSession
    participant P as e2b SDK Sandbox

    U->>S: Sandbox(backend="e2b", backend_options={...})
    S->>B: E2BClient.create(config)
    B->>P: e2b Sandbox.create(**kwargs incl. backend_options)
    U->>S: sb.execute_command("pip install ...")
    S->>B: execute_command(...)
    B->>P: commands.run(...)
    Note over U,P: Portable path above. Native path below.
    U->>S: sb.native
    S->>B: session.native
    B-->>U: e2b SDK Sandbox
    U->>P: native.get_host(8000)  (provider-only feature)
    U->>S: sb.destroy()
    S->>B: destroy()
    B->>P: kill()
```

Protocol change, kept backward compatible for third-party backends:

```python
class SandboxBackendSession(Protocol):
    ...
    @property
    def native(self) -> Any: ...
```

`Sandbox.native` reads `getattr(session, "native", ...)` and raises
`FeatureNotSupportedError` if a backend does not define it, so out-of-tree backends keep
working until they add it. All in-tree backends, plus Boxd (#52) and Maritime (#45), add
the property.

### 3. Rule for promoting a feature into the portable API

```mermaid
flowchart TD
    A["Users need a provider feature"] --> B{"Doable with<br/>execute_command?"}
    B -- Yes --> Z1["No wrapper.<br/>Document the shell recipe."]
    B -- No --> C{"Supported by 3+ backends<br/>with compatible semantics?"}
    C -- No --> Z2["Stay in Tier 3.<br/>Use backend_options / sb.native"]
    C -- Yes --> D{"Requested repeatedly,<br/>or used via native in<br/>several places?"}
    D -- No --> Z2
    D -- Yes --> E["Promote to Tier 2:<br/>add a method + FeatureNotSupportedError<br/>on backends without it"]
    E --> F{"Every backend<br/>supports it?"}
    F -- Yes --> G["Promote to Tier 1"]
    F -- No --> H["Stays Tier 2"]
```

Likely early candidates for Tier 2, to be decided case by case: exposing a port as a URL,
streaming command output, and pause/resume.

### 4. (Optional, phase 2) Capability declaration

Each backend client declares what it supports, so callers and future cost-based routing
can check before running instead of failing mid-run:

```python
class E2BClient:
    capabilities = frozenset({"resume"})

class ModalClient:
    capabilities = frozenset({"snapshot", "resume", "gpu"})
```

Exposed as `Sandbox.capabilities` / `SandboxClient.capabilities`. Kept out of phase 1 so
the first change stays small.

## Maintenance cost

| Change | Cost |
|---|---|
| New backend | One adapter file: 10 protocol methods + `native` property |
| New provider feature | Zero. Reachable immediately through `sb.native` |
| New portable feature (Tier 2) | One method on the protocol, one implementation per supporting backend, `FeatureNotSupportedError` elsewhere |

## Out of scope: batch job services (GCP Batch, AWS Batch, Kubernetes Jobs)

Batch services are a different execution model, not another sandbox provider, so they do
not get a backend under this protocol.

```mermaid
flowchart LR
    subgraph SB["Sandbox model (this library)"]
        direction TB
        s1["create()"] --> s2["live machine stays up"]
        s2 --> s3["exec / read / write<br/>many times, interactively"]
        s3 --> s2
        s3 --> s4["destroy()"]
    end
    subgraph BJ["Batch job model (GCP Batch)"]
        direction TB
        b1["submit(job spec:<br/>image + command + resources)"] --> b2["queued, scheduled<br/>by the service"]
        b2 --> b3["runs to completion,<br/>no exec in between"]
        b3 --> b4["poll status, read outputs<br/>from GCS / logs"]
    end
```

| | Sandbox | Batch job |
|---|---|---|
| Lifetime | Stays up until `destroy()` | Ends when the command exits |
| Interaction | Many `execute_command` / file calls | None after submission |
| Files | Read and write live | Staged in and out through object storage |
| Startup | Seconds | Queue + VM provisioning, often minutes |
| Fits `SandboxBackendSession`? | Yes | No: `execute_command`, `read_file`, `write_file` have no equivalent on a running job |

Forcing Batch behind this protocol would mean faking a live session (for example, a long
`sleep` job plus an SSH or agent channel), which loses what Batch is good at: cheap,
queued, fan-out of many independent runs.

If we need it, it belongs in a separate, small **jobs** API (`submit`, `status`, `logs`,
`outputs`, `cancel`) with its own backends (GCP Batch, AWS Batch, Kubernetes Jobs, Modal
functions, Ray jobs). Sandbox and jobs APIs can share config types such as image,
resources and env, and a job can run the same agent presets this library already has.
That is a separate design.

## Alternatives considered

- **Proxy unknown attributes with `__getattr__`** so `sb.get_host(...)` forwards to the
  provider. Rejected: it hides the portability boundary, breaks type checking and
  autocompletion, and makes name clashes between our methods and the provider's silent.
- **Libcloud-style `ex_` methods on `Sandbox`.** Rejected for now: still a wrapper per
  feature, so it does not fix the maintenance problem. Can coexist with `native` later.
- **Do nothing; tell users to read `_session._sandbox`.** Rejected: private, undocumented,
  and differs per backend (`_sandbox`, `_container`, `_actor`).

## Implementation plan (after approval)

1. Add `native` property to `SandboxBackendSession` and every in-tree backend session.
2. Add `Sandbox.native` and `AsyncSandbox.native` with alive-checks and the fallback for
   backends that lack the property.
3. Tests: one per backend asserting the returned type (with SDKs mocked where the suite
   already mocks them), plus destroyed-sandbox and local/safehouse error cases.
4. README: a "Provider-specific features" section describing the three tiers, with one
   `backend_options` and one `sb.native` example.
5. Ask the Boxd (#52) and Maritime (#45) PRs to add `native`.
6. Phase 2, separately: capability declaration.

## Open questions

1. For `local` and `safehouse`, should `native` raise, or return the working directory as
   a `Path`? This proposal raises, because a path is not a provider object.
2. Should `native` be typed per backend via `typing.overload` on a `backend` literal, or
   stay `Any`? This proposal keeps `Any` for simplicity.
3. Do we want the phase 2 capability declaration now, given cost-based routing is on the
   roadmap?
