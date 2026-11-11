"""Built-in sandbox presets and the preset registration API."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SandboxPreset:
    """A predefined sandbox configuration with setup commands.

    Attributes:
        name: Human-readable preset name.
        description: What this preset provides.
        image: Pre-built OCI container image for docker/daytona/modal backends
            (skips setup_commands if the backend supports images).
        tensorlake_image: Tensorlake project-scoped image name. Tensorlake
            doesn't accept arbitrary OCI URLs, so this is a separate field.
            Users register a project image with `tl sbx image build` (our
            Dockerfiles under images/ can be used as-is) and then set this.
            When set, setup_commands are skipped on the Tensorlake backend.
        setup_commands: Shell commands run after sandbox creation (in order).
        backend_setup_commands: Backend-specific setup commands. When present
            for a backend, these replace setup_commands on that backend.
        cpu: Minimum recommended vCPUs.
        memory_mb: Minimum recommended RAM in MB.
        timeout_secs: Recommended timeout.
        env_vars: Environment variables to set.
        allow_internet: Whether internet access is needed (e.g. for installs).
        setup_before_workspace: Run setup before cloning and materializing
            files, for presets that install workspace prerequisites like git.

    """

    name: str
    description: str
    image: str | None = None
    tensorlake_image: str | None = None
    setup_commands: list[str] = field(default_factory=list)
    backend_setup_commands: dict[str, list[str]] = field(default_factory=dict)
    cpu: float = 1.0
    memory_mb: int = 1024
    timeout_secs: int = 600
    env_vars: dict[str, str] = field(default_factory=dict)
    allow_internet: bool = True
    setup_before_workspace: bool = False


# ---------------------------------------------------------------------------
# Built-in presets
# ---------------------------------------------------------------------------

PRESETS: dict[str, SandboxPreset] = {}


def register_preset(preset: SandboxPreset) -> SandboxPreset:
    """Register a preset so it can be referenced by name."""
    PRESETS[preset.name] = preset
    return preset


def get_preset(name: str) -> SandboxPreset:
    """Look up a preset by name. Raises KeyError if not found."""
    if name not in PRESETS:
        available = ", ".join(sorted(PRESETS)) or "(none)"
        raise KeyError(
            f"Unknown preset '{name}'. Available presets: {available}"
        )
    return PRESETS[name]


def _container_npm_install(packages: str) -> list[str]:
    """Install prerequisites on Debian/Ubuntu base images, then the CLI."""
    return [
        "(command -v node >/dev/null 2>&1 && "
        "command -v npm >/dev/null 2>&1 && "
        "command -v git >/dev/null 2>&1) || "
        "(apt-get update && DEBIAN_FRONTEND=noninteractive "
        "apt-get install -y --no-install-recommends nodejs npm git)",
        f"npm install -g {packages}",
    ]


def _tensorlake_npm_install(packages: str) -> str:
    return (
        "mkdir -p $HOME/.npm-global && "
        "npm config set prefix $HOME/.npm-global && "
        f"npm install -g {packages}"
    )


# -- Agent presets ---------------------------------------------------------

register_preset(
    SandboxPreset(
        name="grok",
        description="Sandbox with Grok Build CLI installed",
        setup_commands=["npm install -g @xai-official/grok"],
        backend_setup_commands={
            "docker": _container_npm_install("@xai-official/grok"),
            "daytona": _container_npm_install("@xai-official/grok"),
            "modal": _container_npm_install("@xai-official/grok"),
            "tensorlake": [_tensorlake_npm_install("@xai-official/grok")],
        },
        memory_mb=2048,
        timeout_secs=1800,
        setup_before_workspace=True,
    )
)

register_preset(
    SandboxPreset(
        name="opencode",
        description="Sandbox with OpenCode CLI installed (including GLM support)",
        setup_commands=["npm install -g opencode-ai"],
        backend_setup_commands={
            "docker": _container_npm_install("opencode-ai"),
            "daytona": _container_npm_install("opencode-ai"),
            "modal": _container_npm_install("opencode-ai"),
            "tensorlake": [_tensorlake_npm_install("opencode-ai")],
            "boxd": ["opencode --version"],
        },
        memory_mb=2048,
        timeout_secs=1800,
        setup_before_workspace=True,
    )
)

register_preset(
    SandboxPreset(
        name="claude-code",
        description="Sandbox with Claude Code (Anthropic CLI) installed",
        setup_commands=[
            "npm install -g @anthropic-ai/claude-code",
        ],
        backend_setup_commands={
            "docker": _container_npm_install("@anthropic-ai/claude-code"),
            "daytona": _container_npm_install("@anthropic-ai/claude-code"),
            "modal": _container_npm_install("@anthropic-ai/claude-code"),
            "tensorlake": [
                _tensorlake_npm_install("@anthropic-ai/claude-code"),
            ],
            "boxd": ["claude --version"],
        },
        memory_mb=2048,
        timeout_secs=1800,
        setup_before_workspace=True,
    )
)

register_preset(
    SandboxPreset(
        name="codex",
        description="Sandbox with Codex CLI installed",
        setup_commands=[
            "npm install -g @openai/codex",
        ],
        backend_setup_commands={
            "docker": _container_npm_install("@openai/codex"),
            "daytona": _container_npm_install("@openai/codex"),
            "modal": _container_npm_install("@openai/codex"),
            "tensorlake": [
                _tensorlake_npm_install("@openai/codex"),
            ],
            "boxd": ["codex --version"],
        },
        memory_mb=2048,
        timeout_secs=1800,
        setup_before_workspace=True,
    )
)

register_preset(
    SandboxPreset(
        name="claude-code-codex",
        description="Sandbox with Claude Code and Codex CLI installed",
        setup_commands=[
            "npm install -g @anthropic-ai/claude-code @openai/codex",
        ],
        backend_setup_commands={
            "docker": _container_npm_install(
                "@anthropic-ai/claude-code @openai/codex"
            ),
            "daytona": _container_npm_install(
                "@anthropic-ai/claude-code @openai/codex"
            ),
            "modal": _container_npm_install(
                "@anthropic-ai/claude-code @openai/codex"
            ),
            "tensorlake": [
                _tensorlake_npm_install(
                    "@anthropic-ai/claude-code @openai/codex"
                ),
            ],
            "boxd": ["claude --version", "codex --version"],
        },
        memory_mb=2048,
        timeout_secs=1800,
        setup_before_workspace=True,
    )
)
