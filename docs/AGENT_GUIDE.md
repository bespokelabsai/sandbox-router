# Running agents

[Back to README](../README.md)

Start with the [first agent example](../README.md#run-your-first-agent). This guide covers custom agent placement, CLI harnesses, token and cost reporting, and installation presets.

**On this page**

- [Agent-ready sandboxes](#agent-ready-sandboxes)
- [Token usage & cost](#token-usage--cost)
- [Presets](#presets)
- [Installation and custom images](#installation-and-custom-images)
- [Non-interactive web access](#non-interactive-web-access)
- [Envy notifications for agents](#envy-notifications-for-agents)

## Agent-ready sandboxes

Sandboxes can also be bound to agents without replacing the low-level sandbox
API. Agent placement is explicit:

- `inside`: the agent process runs inside the sandbox, useful for CLI agents
  such as Codex CLI, Claude Code, or a custom inference runner.
- `outside`: the agent process runs outside the sandbox and drives it through
  capability-checked sandbox tools.

Inside-sandbox agent:

```python
from bespokelabs.sandbox import AgentSpec, Sandbox

with Sandbox(
    "docker",
    preset="codex",
    git_repo="https://github.com/bespokelabsai/sandbox",
) as sb:
    agent = sb.agent(AgentSpec.inside(
        name="codex",
        command=["codex", "exec"],
        cwd="sandbox",
    ))

    result = agent.run("Run the eval suite and summarize failures")
    print(result.stdout)
```

Outside-driver agent:

```python
from bespokelabs.sandbox import AgentSpec, Sandbox

def run_eval(ctx, prompt: str) -> str:
    ctx.write_file("/workspace/task.txt", prompt)
    result = ctx.shell("python3", ["/workspace/eval.py"])
    return result.stdout

with Sandbox(
    "docker",
    files={"/workspace/eval.py": "print('ok')"},
) as sb:
    agent = sb.agent(AgentSpec.outside(
        name="eval-runner",
        capabilities=["shell", "files"],
        runner=run_eval,
    ))

    print(agent.run("Evaluate this input"))
```

For a complete Claude Code outside-sandbox example, see
[`examples/claude_code_outside.py`](../examples/claude_code_outside.py). It starts
Claude Code on the host, clones a repository into a sandbox with `git_repo=...`,
and gives Claude a localhost bridge for running commands inside the sandbox. The
example asks Claude to count regular files directly in the cloned repository
root.

For outside agent frameworks, use `agent_tools(...)` directly:

```python
with Sandbox("docker") as sb:
    tools = sb.agent_tools(capabilities=["shell", "files", "patch"])
    tools.write_file("/workspace/input.txt", "hello")
    print(tools.shell("cat", ["/workspace/input.txt"]).stdout)
```

The generic context exposes `shell`, `files`, and `patch` operations. `ports`
and `artifacts` are accepted capability names but have no operations yet.
Capabilities gate context methods, not the sandbox OS: `shell` can access files,
and the runner can access the underlying sandbox through `ctx.sandbox`.

Inside agents accept `input_mode="stdin"` (default), `"argv"` (append the
prompt), `"file"` (write the prompt and append its path), or `"none"`. They
return `SandboxResult`; outside agents return their runner's value. Install and
authenticate the agent CLI separately from provider authentication. Presets
install tools but do not supply agent credentials.

## Token usage & cost

The hosted dashboard shows model usage separately from sandbox compute:
input/output/cache token counts, reported model cost, and labeled Codex
estimates. The overview follows the selected spend period; sandbox details
show lifetime totals and individual runs, including unavailable/partial usage.

To collect this data, run agents through `RemoteSandbox.run_agent` (or
`POST /v1/sandboxes/{id}/agent`):

```python
import os
from bespokelabs.sandbox import RemoteSandboxClient

client = RemoteSandboxClient(
    "https://sandbox.example.com", os.environ["BESPOKE_API_KEY"],
    timeout_secs=1800,
)
with client.create(
    "docker", preset="codex",
    env_vars={"CODEX_API_KEY": os.environ["CODEX_API_KEY"]},
) as sb:
    result = sb.run_agent(
        "Explain the current workspace.", harness="codex",
        model="gpt-5.3-codex", extra_args=["--skip-git-repo-check"],
        idempotency_key="workspace-review-1",
    )
    print(result.text)
    print(result.agent_usage)  # tokens, cost_usd, cost_state, pricing provenance
    print(result.usage)        # separate sandbox compute metering
```

Use `harness="grok"` with the `grok` preset and `XAI_API_KEY` to record
Grok's reported spend. JSON output is selected automatically. Hosted runs
are persisted once per idempotency key; repeating the same request returns
its saved response. A changed request with the same key is rejected.
An in-progress, failed, or interrupted request without a saved result returns
409 and is never automatically rerun, since it may already have produced side
effects or incurred costs. After a server crash, inspect the provider sandbox
and confirm the prior process has stopped before deliberately starting a new
request with a new key (and a new hosted sandbox after a control-plane restart).
The API requires `sandboxes:execute`; viewing model usage requires
`usage:read`, including fresh responses and idempotent replays. Without that
scope, `RemoteAgentRunResult.agent_usage` is empty. The database migration runs
automatically at server startup.

Codex estimates multiply uncached input, cache reads, and output by bundled
[standard short-context API rates](https://developers.openai.com/api/docs/pricing),
verified November 7, 2026. Supported explicit model IDs are `gpt-5.3-codex`,
`gpt-6-astra`, `gpt-6.1-sol`, `gpt-6.1-terra`, and `gpt-6-luna`.
Each run stores its rates and source date, so future price changes do not
rewrite history. These estimates exclude fast/long-context premiums, tool
charges, regional charges, and subscription billing. Unknown/default models,
model/provider overrides in `extra_args`, and missing/incomplete usage show
**Unavailable**, never a fabricated zero-dollar estimate.

Model costs are observational: they are not added to compute billing,
customer markup, compute budget guardrails, or existing compute CSV exports.
Direct `Sandbox.run_agent` calls and raw shell commands are not uploaded to
the control plane; earlier runs cannot be backfilled automatically.

`run_agent` supports `harness="claude-code"` (default), `"opencode"`,
`"grok"`, and `"codex"`. Select the installation preset and runtime harness
separately. The sync and async clients expose the same options.

For [Grok Build](https://docs.x.ai/build/cli/headless-scripting):

```python
import os
from bespokelabs.sandbox import Sandbox

with Sandbox(
    "local", preset="grok", workdir="/absolute/path/to/your/project",
    env_vars={"XAI_API_KEY": os.environ["XAI_API_KEY"]},
) as sb:
    result = sb.run_agent("Explain this project.", harness="grok")
    print(result.text, result.usage.total_tokens, result.usage.total_cost_usd)
    print(result.exit_code, result.stderr)
    sb.run_agent("Which modules need tests?", harness="grok", resume=True)
```

The Grok preset installs `@xai-official/grok` via npm (Node.js 20+ required).
Docker setup installs Node.js, npm, and Git on the default Debian image before
cloning the workspace. Tensorlake uses its writable npm prefix. Other backends
need Node.js/npm and installation permissions. No prebuilt Grok image is
assumed. Local execution runs on the host; use Docker or a cloud backend for
an isolated environment.

For [Codex](https://developers.openai.com/codex/noninteractive), the existing
`codex` preset now works with the usage-aware helper:

```python
with Sandbox(
    "local", preset="codex", workdir="/absolute/path/to/your/project",
    env_vars={"CODEX_API_KEY": os.environ["CODEX_API_KEY"]},
) as sb:
    result = sb.run_agent("Explain this project.", harness="codex")
    print(result.text, result.usage.total_tokens)
    sb.run_agent("Which modules need tests?", harness="codex", resume=True)
```

Both harnesses accept `model=...`, `command=...` to override the binary, and
`extra_args=[...]` for CLI flags. Codex uses `exec --json`, and `resume=True`
uses `exec resume --last`; Grok uses `--single` and `-c`. Keep the same workspace
and the CLI's session files to resume. Authentication and tool permissions
come from the CLI configuration; the helper adds no approval-bypass flags.
Codex requires a Git working directory unless you explicitly pass
`extra_args=["--skip-git-repo-check"]`.

Codex accepts `output_format="json"` or `"text"`. Grok also accepts `"plain"`
(`"text"` aliases it), `"streaming-json"`, and `"streaming-messages-json"`.
JSON is the default; plain output has no token/cost metadata. Streaming formats
are parsed after command completion, not delivered as live callbacks.

Usage normalization follows the CLI reports:

- Codex input includes cache reads; the SDK splits those into `input_tokens`
  (uncached) and `cache_read_tokens`. Output already includes reasoning.
  The final completed agent message becomes `result.text`.
- Grok's headless input already excludes cache reads. Its output includes
  reasoning, so `reasoning_tokens` is not added again. Final spend overrides
  per-response events; interrupted streams retain completed response usage.
- Grok's reported `total_cost_usd` is parsed when available and complete.
  Standard Codex events do not report dollar cost. Missing/incomplete LLM cost
  stays `0` under the existing `Usage` contract; this means **unknown**, not
  free. The direct SDK does not infer model pricing. `total_cost_usd` may therefore
  include only estimated compute cost.
- Raw events and session metadata remain in `result.raw`; inspect Grok's
  `usage_is_incomplete` / `cost_is_partial` flags when present. stdout, stderr,
  exit status, and any parsed usage are retained on failure.

To run GLM through the [OpenCode harness](https://opencode.ai/docs/cli/#run):

```python
import os
from bespokelabs.sandbox import Sandbox

with Sandbox(
    "local", preset="opencode",
    env_vars={"ZHIPU_API_KEY": os.environ["ZHIPU_API_KEY"]},
) as sb:
    result = sb.run_agent(
        "Review this project.", harness="opencode", model="zai/glm-4.7",
    )
    print(result.text)
    print(result.usage.total_tokens, result.usage.total_cost_usd)
```

For a [Z.AI Coding Plan](https://opencode.ai/docs/providers/#zai), use
`model="zai-coding-plan/glm-4.7"` with the same environment variable. Other
GLM models can be selected using their OpenCode `provider/model` identifier.
`resume=True` continues the latest conversation in the same workspace;
`extra_args=["--session", session_id]` selects a specific session.
The preset installs OpenCode at startup, requiring npm and install permissions
on the sandbox; its Docker setup installs npm and Git on the default Debian image
before cloning repositories. All built-in agent presets use
`setup_before_workspace=True`; custom presets default to setup after workspace
materialization.
No prebuilt OpenCode image is assumed. The preset installs the CLI; select the
harness separately with `harness="opencode"` when calling `run_agent`.
OpenCode JSON step costs and token counts are summed across the run, including
cache usage and reasoning tokens (included in `output_tokens`). Original events
are available in `result.raw["events"]`; stdout, stderr and exit status are
preserved. See [the runnable GLM example](../examples/opencode_glm.py). Its local
workspace defaults to the Git-ignored `examples/.sandbox_workdir/opencode_glm`
directory and survives cleanup, so `--resume` works across invocations. Use
`--workdir` to override it. Recreating a cloud sandbox does not retain its files.

By default, `run_agent(...)` runs Claude Code on a prompt and reports what the run cost.
It drives the CLI with JSON output under the hood, so it can return both the
assistant's text answer and a `Usage` breakdown — LLM token counts and dollar
cost (from Claude Code's own `total_cost_usd`), plus an estimate of the sandbox
compute the call consumed (elapsed wall-clock × the backend's per-second price).

```python
with Sandbox("local", preset="claude-code") as sb:
    result = sb.run_agent("Review /code.py and suggest fixes.")
    print(result.text)                       # the assistant's answer

    u = result.usage                         # usage for this call
    print(u.input_tokens, u.output_tokens)   # token counts
    print(u.llm_cost_usd)                    # tokens, in dollars
    print(u.compute_cost_usd)                # sandbox compute, in dollars
    print(u.total_cost_usd)                  # llm + compute

    # Continue the conversation; usage accumulates on the sandbox.
    sb.run_agent("Now apply those fixes.", resume=True)
    print(sb.usage.total_cost_usd)           # running total across both calls
    print(sb.usage.total_tokens)
```

`sb.usage` is the running total across every `run_agent` call in the sandbox's
lifetime (its compute component sums the agent-call durations, not idle time
between calls). Pass extra CLI flags with `extra_args=[...]`. Token counts
and `llm_cost_usd` reflect what the CLI's
JSON output reports, so `llm_cost_usd` can be `0` under subscription auth that
omits `total_cost_usd` (the token counts are still captured). The async
`AsyncSandbox` exposes the same `run_agent(...)` / `usage`. This is distinct
from `bespokelabs.sandbox.pricing`, which prices sandbox compute on its own.

For standalone sandbox compute estimates, use the bundled pricing helpers:

```python
from bespokelabs.sandbox.pricing import cost_per_second, get_backend_pricing

print(get_backend_pricing("modal"))          # raw bundled pricing metadata
print(cost_per_second("modal", vcpu=2.0))    # estimated $/sec for a sandbox
```

`sb.estimate_compute_cost(elapsed_secs)` estimates a measured interval using
the live session's hourly rate when available (RunPod), otherwise bundled
CPU/RAM rates. Bundled estimates omit GPU, disk, network, and other charges;
an unknown backend also returns zero, which does not establish that it is free.
Direct SDK `sb.usage` tracks only `run_agent` calls. Remote execution usage
tracks each command's runtime, while hosted cost summaries use lifecycle costs
when available, including idle time, without adding execution costs twice.

Pricing data is best-effort and local to the installed package. To compare
available backends for a real workload, see
[`examples/find_cheapest.py`](../examples/find_cheapest.py), which benchmarks
cold-start and execution time, then estimates cost with the same pricing data.

## Presets

Presets combine agent installation commands with resource and environment
defaults. The built-in presets are `codex`, `grok`, `claude-code`,
`claude-code-codex`, and `opencode`.

### Installation and custom images

Built-in presets install their CLIs at sandbox creation; they do not select
Bespoke-hosted images. In particular, `preset="codex"` no longer pulls the old
`ghcr.io/bespokelabsai/sandbox/codex:v2` image.

```python
from bespokelabs.sandbox import Sandbox

with Sandbox("docker", preset="codex") as sb:
    print(sb.execute_command("codex --version").stdout)
```

On Docker, Daytona, and Modal, setup checks for Node.js, npm, and Git, installing
missing prerequisites with `apt-get` on Debian/Ubuntu base images before
running `npm install -g`. Custom images need those tools already available or
root access to a working `apt-get`. Choose a Node.js version supported by the
CLI you install. Setup runs before repository cloning and file/workspace
materialization. A failed setup triggers sandbox cleanup.

Installation requires network access, adds startup time, and uses the npm
package version resolved at creation. Credentials are supplied separately.
Tensorlake uses a writable npm prefix; boxd checks the tools already present
for its supported presets. Other backends need Node.js/npm and installation
permissions. On Local and Ray, setup runs on the host/worker and can install
global npm packages.

Passing `image=` with a built-in preset changes the base image and still runs
installation. To use your own image with the tools already installed and skip
setup on Docker, Daytona, or Modal, supply a custom preset:

```python
from bespokelabs.sandbox import Sandbox, SandboxPreset

codex_image = SandboxPreset(
    name="my-codex",
    description="Codex installed in an image maintained by my team",
    image="registry.example.com/my-team/codex:2026-11-11",
    setup_commands=["npm install -g @openai/codex"],
    memory_mb=2048,
    timeout_secs=1800,
)
with Sandbox("docker", preset=codex_image) as sb:
    print(sb.execute_command("codex --version").stdout)
```

Use an image you have built and published, preferably pinned by digest for
reproducibility. The matching preset image skips setup on supported backends;
an explicit different `image=` or `dockerfile=` runs the custom preset's setup
commands. Tensorlake uses `tensorlake_image=` on the preset for its
project-scoped image names. The Dockerfiles under `images/` remain available
for building your own images; built-in presets do not depend on published tags.

Use `Sandbox.list_presets()` to inspect registered configurations.

Built-in presets:

| Preset | What it installs | Defaults |
|---|---|---|
| `claude-code` | `@anthropic-ai/claude-code` via npm | 2GB RAM, 30min timeout |
| `claude-code-codex` | `@anthropic-ai/claude-code` and `@openai/codex` via npm | 2GB RAM, 30min timeout |
| `codex` | `@openai/codex` via npm | 2GB RAM, 30min timeout |
| `grok` | `@xai-official/grok` via npm (Node.js 20+) | 2GB RAM, 30min timeout |
| `opencode` | `opencode-ai` via npm | 2GB RAM, 30min timeout |

### Non-interactive web access

The preset controls which CLI is installed in the sandbox. It does not grant
the CLI permission to use its own web tools. When you run an agent inside a
remote sandbox such as Daytona, preconfigure the CLI for non-interactive runs
instead of waiting for an in-terminal approval prompt.

For Claude Code, `WebFetch` and `WebSearch` are permission-gated tools. Use
`--permission-mode dontAsk` with the narrowest `--allowedTools` entries that
fit the task:

```python
agent = sb.agent(AgentSpec.inside(
    name="claude",
    command=[
        "claude",
        "-p",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        "WebFetch(domain:github.com)",
        "WebSearch",
    ],
    input_mode="argv",
))

result = agent.run("Summarize https://github.com/bespokelabsai/sandbox")
```

For Codex CLI, use `codex exec` with explicit approval, sandbox, and search
settings. For read-only website summaries, keep the Codex sandbox read-only,
disable approval prompts, and enable live search:

```python
agent = sb.agent(AgentSpec.inside(
    name="codex",
    command=[
        "codex",
        "exec",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "read-only",
        "--search",
    ],
    input_mode="argv",
))

result = agent.run("Summarize https://github.com/bespokelabsai/sandbox")
```

Use broader modes only when the outer sandbox is the trust boundary. For
example, `codex exec --sandbox workspace-write --ask-for-approval never` lets
Codex edit files without pausing, and Claude Code's `--permission-mode
bypassPermissions` skips most permission prompts. Those modes are best kept to
isolated sandboxes with scoped credentials.

Create your own:

```python
from bespokelabs.sandbox import Sandbox, SandboxPreset

Sandbox.register_preset(SandboxPreset(
    name="my-stack",
    description="My custom environment",
    setup_commands=["pip install my-library", "npm install -g my-tool"],
    cpu=2.0,
    memory_mb=4096,
))

with Sandbox("docker", preset="my-stack") as sb:
    ...
```

Explicit kwargs always override preset defaults.

## Envy notifications for agents

The optional [Envy supervisor](ENVY.md) registers sandbox agents with a
shared human token and wakes their Codex or Claude Code conversations through
an authenticated event stream. It queues incoming messages, resumes the saved
conversation, posts replies, and retries interrupted deliveries. No inbox
polling or model calls happen while idle. Start with
[`examples/envy_listener.py`](../examples/envy_listener.py).
