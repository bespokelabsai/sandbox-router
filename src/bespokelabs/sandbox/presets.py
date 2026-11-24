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
    """Install prerequisites on Debian/Ubuntu base images, then the CLI.

    Root runs both steps directly. A non-root user, such as Daytona's
    `daytona` user, runs apt-get with passwordless sudo, because apt-get needs
    root. sudo resets the environment, so env passes DEBIAN_FRONTEND through.

    A non-root user runs npm with sudo too, because the global npm folders are
    usually owned by root, e.g., apt puts them under /usr/local. In the
    daytonaio/sandbox:latest image, root also installed some of these CLIs
    into npm folders that the `daytona` user owns. npm cannot replace those
    copies as that user. A user without passwordless sudo runs npm directly,
    which works when that user owns the global npm folders.

    The sudo command keeps the user's npm settings:
    - The -E flag keeps the user's environment, e.g., npm_config_* and proxy
      variables. sudo-rs ignores -E, so these variables are lost there.
    - env passes PATH through, because sudo sets its own PATH, and that PATH
      does not include an npm installed with nvm.
    - The -H flag sets HOME to root's home, so root does not write files into
      the user's home, e.g., the npm cache.
    - HOME is different, so the command sets npm_config_userconfig to the
      user's npm config file. That file can hold registry settings and tokens.
    """
    install = f"npm install -g {packages}"
    sudo = "sudo -n -E -H"
    return [
        "(command -v node >/dev/null 2>&1 && "
        "command -v npm >/dev/null 2>&1 && "
        "command -v git >/dev/null 2>&1) || "
        '(s=; [ "$(id -u)" -eq 0 ] || s="sudo -n"; '
        "$s apt-get update && $s env DEBIAN_FRONTEND=noninteractive "
        "apt-get install -y --no-install-recommends nodejs npm git)",
        f'if [ "$(id -u)" -ne 0 ] && {sudo} true 2>/dev/null; then '
        f'{sudo} env PATH="$PATH" '
        'npm_config_userconfig="$(npm config get userconfig)" '
        f"{install}; else {install}; fi",
    ]


def _tensorlake_npm_install(packages: str) -> str:
    return (
        "mkdir -p $HOME/.npm-global && "
        "npm config set prefix $HOME/.npm-global && "
        f"npm install -g {packages}"
    )


_ANTIGRAVITY_INSTALLER_URL = "https://antigravity.google/cli/install.sh"


def _antigravity_install(*, sudo_link: bool = False) -> str:
    """Install `agy` with Google's script unless it is already on PATH.

    The script's default directory, ~/.local/bin, is often not on PATH for
    the non-login commands that backends run, so prefer /usr/local/bin. In
    isolated containers, a non-root user with passwordless sudo also links a
    ~/.local/bin install there. The binary stays user-owned so the CLI can
    update itself. The local backend rewrites absolute-path redirects and
    `rm` arguments, so these commands avoid both.
    """
    link = (
        'command -v agy || sudo -n ln -sf "$dir/agy" /usr/local/bin/agy; '
        if sudo_link
        else ""
    )
    return (
        "if command -v agy; then agy --version; else "
        'dir=/usr/local/bin; [ -w "$dir" ] || dir="$HOME/.local/bin"; '
        "installer=$(mktemp) && "
        f'curl -fsSL {_ANTIGRAVITY_INSTALLER_URL} -o "$installer" && '
        'bash "$installer" --dir "$dir"; '
        'status=$?; command rm -f "$installer"; '
        '[ "$status" -eq 0 ] || exit "$status"; '
        f"{link}"
        'command -v agy || echo "agy is in $dir, which is not on PATH. '
        'Pass command=$dir/agy to run_agent." >&2; '
        '"$dir/agy" --version; fi'
    )


# apt-get needs root, so a non-root container user runs it with passwordless
# sudo. sudo resets the environment, so env passes DEBIAN_FRONTEND through.
_ANTIGRAVITY_CONTAINER_PREREQUISITES = (
    "(command -v curl >/dev/null 2>&1 && "
    "command -v git >/dev/null 2>&1) || "
    '(s=; [ "$(id -u)" -eq 0 ] || s="sudo -n"; '
    "$s apt-get update && $s env DEBIAN_FRONTEND=noninteractive "
    "apt-get install -y --no-install-recommends ca-certificates curl git)"
)
# A GEMINI_API_KEY is ignored unless settings select the "gemini" provider,
# and the CLI will not start with that provider but no key. Select it only
# when the key is present at creation, ADC is not requested, and no settings
# file exists yet. Relative paths keep the local backend's HOME rebasing out.
_ANTIGRAVITY_API_KEY_SETTINGS = (
    'if [ -n "${GEMINI_API_KEY:-}" ] && [ -z "${AGY_ADC_AUTH:-}" ]; then '
    'cd "$HOME" && '
    "if [ ! -e .gemini/antigravity-cli/settings.json ]; then "
    "mkdir -p .gemini/antigravity-cli && "
    "printf '%s\\n' '{\"modelProvider\": \"gemini\"}' "
    "> .gemini/antigravity-cli/settings.json; "
    "fi; fi"
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
        name="antigravity",
        description="Sandbox with Google Antigravity CLI (agy) installed",
        setup_commands=[
            _antigravity_install(),
            _ANTIGRAVITY_API_KEY_SETTINGS,
        ],
        backend_setup_commands={
            backend: [
                _ANTIGRAVITY_CONTAINER_PREREQUISITES,
                _antigravity_install(sudo_link=True),
                _ANTIGRAVITY_API_KEY_SETTINGS,
            ]
            for backend in ("docker", "daytona", "modal")
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
