from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from bespokelabs.sandbox import Sandbox
from bespokelabs.sandbox.presets import (
    PRESETS,
    SandboxPreset,
    get_preset,
    register_preset,
)
from bespokelabs.sandbox.types import SandboxResult

BUILT_IN_PRESETS = {
    "claude-code",
    "claude-code-codex",
    "codex",
    "opencode",
    "grok",
}
TENSORLAKE_NPM_PREFIX = "mkdir -p $HOME/.npm-global && npm config set prefix $HOME/.npm-global && npm install -g"
# These are the setup commands for npm presets on Docker, Daytona, and Modal.
# A non-root user runs apt-get with sudo, and npm with sudo when passwordless
# sudo works. npm under sudo keeps the user's environment and npm config file.
CONTAINER_PREREQUISITES = (
    "(command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 && "
    "command -v git >/dev/null 2>&1) || "
    '(s=; [ "$(id -u)" -eq 0 ] || s="sudo -n"; $s apt-get update && '
    "$s env DEBIAN_FRONTEND=noninteractive "
    "apt-get install -y --no-install-recommends nodejs npm git)"
)
CONTAINER_NPM_INSTALL = (
    'if [ "$(id -u)" -ne 0 ] && sudo -n -E -H true 2>/dev/null; then '
    'sudo -n -E -H env PATH="$PATH" '
    'npm_config_userconfig="$(npm config get userconfig)" {install}; '
    "else {install}; fi"
)


class PresetTests(unittest.TestCase):

    def test_opencode_installs_on_all_backends(self) -> None:
        preset = get_preset("opencode")
        self.assertIsNone(preset.image)
        self.assertEqual(preset.setup_commands, ["npm install -g opencode-ai"])
        self.assertTrue(preset.setup_before_workspace)
        self.assertEqual(
            preset.backend_setup_commands["docker"],
            [
                CONTAINER_PREREQUISITES,
                CONTAINER_NPM_INSTALL.format(
                    install="npm install -g opencode-ai"
                ),
            ],
        )
        self.assertEqual(
            preset.backend_setup_commands["tensorlake"],
            [f"{TENSORLAKE_NPM_PREFIX} opencode-ai"],
        )

    def test_grok_preset_installs_official_cli_without_prebuilt_image(self):
        preset = get_preset("grok")
        self.assertIsNone(preset.image)
        self.assertTrue(preset.setup_before_workspace)
        self.assertEqual(
            preset.setup_commands, ["npm install -g @xai-official/grok"]
        )
        self.assertEqual(
            preset.backend_setup_commands["tensorlake"],
            [f"{TENSORLAKE_NPM_PREFIX} @xai-official/grok"],
        )
        self.assertEqual(
            preset.backend_setup_commands["docker"],
            [
                CONTAINER_PREREQUISITES,
                CONTAINER_NPM_INSTALL.format(install=preset.setup_commands[0]),
            ],
        )
        self.assertEqual(preset.memory_mb, 2048)
        self.assertEqual(preset.timeout_secs, 1800)
        self.assertTrue(preset.allow_internet)

    def test_builtin_presets_are_limited_to_agent_clis(self) -> None:
        self.assertEqual(set(PRESETS), BUILT_IN_PRESETS)

    def test_codex_preset_is_registered_with_expected_defaults(self) -> None:
        preset = get_preset("codex")

        self.assertEqual(preset.description, "Sandbox with Codex CLI installed")
        self.assertIsNone(preset.image)
        self.assertEqual(
            preset.setup_commands, ["npm install -g @openai/codex"]
        )
        self.assertEqual(
            preset.backend_setup_commands["tensorlake"],
            [
                f"{TENSORLAKE_NPM_PREFIX} @openai/codex",
            ],
        )
        self.assertEqual(preset.memory_mb, 2048)
        self.assertEqual(preset.timeout_secs, 1800)
        self.assertTrue(preset.allow_internet)

    def test_claude_code_preset_is_registered_with_expected_defaults(
        self,
    ) -> None:
        preset = get_preset("claude-code")

        self.assertEqual(
            preset.description,
            "Sandbox with Claude Code (Anthropic CLI) installed",
        )
        self.assertIsNone(preset.image)
        self.assertEqual(
            preset.setup_commands, ["npm install -g @anthropic-ai/claude-code"]
        )
        self.assertEqual(
            preset.backend_setup_commands["tensorlake"],
            [
                f"{TENSORLAKE_NPM_PREFIX} @anthropic-ai/claude-code",
            ],
        )
        self.assertEqual(preset.memory_mb, 2048)
        self.assertEqual(preset.timeout_secs, 1800)
        self.assertTrue(preset.allow_internet)

    def test_claude_code_codex_preset_is_registered_with_expected_defaults(
        self,
    ) -> None:
        preset = get_preset("claude-code-codex")

        self.assertEqual(
            preset.description,
            "Sandbox with Claude Code and Codex CLI installed",
        )
        self.assertIsNone(preset.image)
        self.assertEqual(
            preset.setup_commands,
            ["npm install -g @anthropic-ai/claude-code @openai/codex"],
        )
        self.assertEqual(
            preset.backend_setup_commands["tensorlake"],
            [
                "mkdir -p $HOME/.npm-global && npm config set prefix $HOME/.npm-global && "
                "npm install -g @anthropic-ai/claude-code @openai/codex",
            ],
        )
        self.assertEqual(preset.memory_mb, 2048)
        self.assertEqual(preset.timeout_secs, 1800)
        self.assertTrue(preset.allow_internet)

    def test_list_presets_includes_only_builtin_agent_clis(self) -> None:
        presets = Sandbox.list_presets()

        self.assertEqual(set(presets), BUILT_IN_PRESETS)


class ContainerNpmInstallTests(unittest.TestCase):
    """Run the npm presets' container setup against stub system commands.

    The user's PATH has two folders. The first has a stub npm, as nvm
    installs it, and the second has the system stubs. node and git are
    missing unless a test adds them. The user also sets an npm registry in
    the environment.

    The stub sudo runs the command as root, so the stub id prints 0. Like the
    real sudo, it sets a PATH without the npm folder. Without -E, it drops
    the caller's environment and sets HOME to /root. With -E, it keeps the
    environment, and it sets HOME to /root only with -H.
    """

    PACKAGES = "@anthropic-ai/claude-code @openai/codex"
    APT_INSTALL = "apt-get install -y --no-install-recommends nodejs npm git"
    NPM_INSTALL = f"npm install -g {PACKAGES}"
    REGISTRY = "https://registry.example.test/"
    # The stub npm prints the default npm config file path, like the real
    # npm. For an install, it logs the HOME and npm settings it sees.
    NPM_STUB = textwrap.dedent(
        """\
        if [ "$*" = "config get userconfig" ]; then
          echo "${npm_config_userconfig:-$HOME/.npmrc}"
          exit
        fi
        echo "npm $* (uid $(id -u), HOME=$HOME," \\
          "userconfig=${npm_config_userconfig:-default}," \\
          "registry=${npm_config_registry:-default})" >> "$LOG"
    """
    )

    def run_steps(self, shell, uid, *, sudo="nopasswd", tools=()):
        """Run each setup step in its own shell and return the stub log."""
        steps = get_preset("claude-code-codex").backend_setup_commands["docker"]
        with tempfile.TemporaryDirectory() as temp:
            system, nvm = Path(temp, "system"), Path(temp, "nvm")
            system.mkdir()
            nvm.mkdir()
            root = f"PATH={shlex.quote(str(system))} FAKE_UID=0"
            sudo_stubs = {
                "nopasswd": textwrap.dedent(
                    f"""\
                    echo "sudo $*" >> "$LOG"
                    keep=
                    while :; do
                      case $1 in
                        -n) shift ;;
                        -E) keep=1; shift ;;
                        -H) HOME=/root; shift ;;
                        *) break ;;
                      esac
                    done
                    [ -n "$keep" ] ||
                      exec /usr/bin/env -i {root} HOME=/root LOG="$LOG" "$@"
                    exec /usr/bin/env {root} "$@"
                """
                ),
                "password": (
                    'echo "sudo $*" >> "$LOG"; '
                    'echo "sudo: a password is required" >&2; exit 1'
                ),
            }
            apt_get = 'echo "apt-get $* (${DEBIAN_FRONTEND:-unset})" >> "$LOG"'
            stubs = {
                system / "id": 'echo "$FAKE_UID"',
                system / "apt-get": apt_get,
                nvm / "npm": self.NPM_STUB,
                **{system / tool: "exit 0" for tool in tools},
            }
            if sudo:
                stubs[system / "sudo"] = sudo_stubs[sudo]
            for stub, body in stubs.items():
                stub.write_text(f"#!/bin/sh\n{body}\n")
                stub.chmod(0o755)
            for tool in ("env", "true"):
                (system / tool).symlink_to(f"/usr/bin/{tool}")
            log = Path(temp, "log")
            env = {
                "PATH": f"{nvm}:{system}",
                "HOME": str(Path(temp, "home")),
                "LOG": str(log),
                "FAKE_UID": str(uid),
                "npm_config_registry": self.REGISTRY,
            }
            for step in steps:
                subprocess.run([shell, "-c", step], env=env, check=True)
            lines = log.read_text().splitlines() if log.exists() else []
            return [line.replace(temp, "$TMP") for line in lines]

    def shells(self):
        found = {name: shutil.which(name) for name in ("sh", "bash", "dash")}
        return {name: path for name, path in found.items() if path}

    def npm_log(self, uid, home, userconfig="default"):
        """Return the line the stub npm logs for the install."""
        return (
            f"{self.NPM_INSTALL} (uid {uid}, HOME={home}, "
            f"userconfig={userconfig}, registry={self.REGISTRY})"
        )

    def sudo_npm_log(self):
        """Return the log lines for npm with passwordless sudo."""
        return [
            "sudo -n -E -H true",
            "sudo -n -E -H env PATH=$TMP/nvm:$TMP/system "
            f"npm_config_userconfig=$TMP/home/.npmrc {self.NPM_INSTALL}",
            # npm runs as root with root's HOME, the user's registry setting,
            # and the user's npm config file.
            self.npm_log(0, "/root", "$TMP/home/.npmrc"),
        ]

    def test_root_runs_apt_get_and_npm_directly(self):
        for name, shell in self.shells().items():
            with self.subTest(shell=name):
                self.assertEqual(
                    self.run_steps(shell, 0),
                    [
                        "apt-get update (unset)",
                        f"{self.APT_INSTALL} (noninteractive)",
                        self.npm_log(0, "$TMP/home"),
                    ],
                )

    def test_non_root_user_runs_apt_get_and_npm_with_sudo(self):
        for name, shell in self.shells().items():
            with self.subTest(shell=name):
                self.assertEqual(
                    self.run_steps(shell, 1000),
                    [
                        "sudo -n apt-get update",
                        "apt-get update (unset)",
                        "sudo -n env DEBIAN_FRONTEND=noninteractive "
                        + self.APT_INSTALL,
                        f"{self.APT_INSTALL} (noninteractive)",
                        *self.sudo_npm_log(),
                    ],
                )

    def test_installed_tools_skip_apt_get(self):
        for name, shell in self.shells().items():
            with self.subTest(shell=name):
                self.assertEqual(
                    self.run_steps(shell, 1000, tools=("node", "git")),
                    self.sudo_npm_log(),
                )

    def test_user_without_passwordless_sudo_runs_npm_directly(self):
        # npm works without sudo when the user owns its global prefix.
        expected = {
            "password": [
                "sudo -n -E -H true",
                self.npm_log(1000, "$TMP/home"),
            ],
            None: [self.npm_log(1000, "$TMP/home")],
        }
        for name, shell in self.shells().items():
            for sudo, log in expected.items():
                with self.subTest(shell=name, sudo=sudo):
                    self.assertEqual(
                        self.run_steps(
                            shell, 1000, sudo=sudo, tools=("node", "git")
                        ),
                        log,
                    )


class PresetImageResolutionTests(unittest.TestCase):
    """Verify how presets resolve the image field.

    These tests also decide whether to run setup_commands. They cover the
    OCI-vs-provider split: preset.image targets docker/daytona/modal, while
    preset.tensorlake_image targets tensorlake and RunPod uses an SSH-ready
    base image.
    """

    PRESET_NAME = "_test_dual_image"
    BACKEND_ONLY_PRESET_NAME = "_test_backend_only_setup"

    def setUp(self) -> None:
        register_preset(
            SandboxPreset(
                name=self.PRESET_NAME,
                description="dual-image preset for testing",
                image="ghcr.io/test/img:latest",
                tensorlake_image="tl-name",
                setup_commands=["echo hi"],
            )
        )
        register_preset(
            SandboxPreset(
                name=self.BACKEND_ONLY_PRESET_NAME,
                description="backend-only setup preset for testing",
                backend_setup_commands={
                    "tensorlake": ["echo tensorlake"],
                },
            )
        )

        def _make_backend_client() -> mock.MagicMock:
            session = mock.MagicMock()
            session.execute_command.return_value = SandboxResult(
                stdout="",
                stderr="",
                exit_code=0,
            )
            client = mock.MagicMock()
            client.create.return_value = session
            return client

        self._backends_patch = mock.patch.dict(
            "bespokelabs.sandbox.backends.BACKENDS",
            {
                "docker": lambda: _make_backend_client(),
                "daytona": lambda: _make_backend_client(),
                "modal": lambda: _make_backend_client(),
                "runpod": lambda: _make_backend_client(),
                "tensorlake": lambda: _make_backend_client(),
            },
            clear=False,
        )
        self._backends_patch.start()

    def tearDown(self) -> None:
        self._backends_patch.stop()
        PRESETS.pop(self.PRESET_NAME, None)
        PRESETS.pop(self.BACKEND_ONLY_PRESET_NAME, None)

    def test_custom_oci_image_skips_setup_on_supported_backends(self) -> None:
        for backend in ("docker", "daytona", "modal"):
            with self.subTest(backend=backend):
                with Sandbox(backend=backend, preset=self.PRESET_NAME) as sb:
                    self.assertEqual(
                        sb._config.image, "ghcr.io/test/img:latest"
                    )
                    sb._session.execute_command.assert_not_called()

    def test_runtime_install_precedes_clone_and_files(self):
        for name in BUILT_IN_PRESETS:
            with (
                self.subTest(preset=name),
                Sandbox(
                    "docker",
                    preset=name,
                    git_repo="https://example.com/repo.git",
                    files={"repo/input.txt": "data"},
                ) as sb,
            ):
                provisioning, install = get_preset(name).backend_setup_commands[
                    "docker"
                ]
                self.assertIsNone(sb._config.image)
                self.assertEqual(
                    sb._session.mock_calls,
                    [
                        mock.call.execute_command(provisioning),
                        mock.call.execute_command(install),
                        mock.call.execute_command(
                            "git clone --depth 1 https://example.com/repo.git repo",
                            None,
                        ),
                        mock.call.write_file("repo/input.txt", "data"),
                    ],
                )

    def test_early_setup_failure_cleans_up_before_clone(self):
        from bespokelabs.sandbox.exceptions import SandboxCreationError

        for name in BUILT_IN_PRESETS:
            for failed_step in (0, 1):
                with self.subTest(preset=name, failed_step=failed_step):
                    session = mock.MagicMock()
                    session.execute_command.side_effect = [
                        SandboxResult() for _ in range(failed_step)
                    ] + [SandboxResult(exit_code=1, stderr="install failed")]
                    client = mock.MagicMock()
                    client.create.return_value = session
                    with self.assertRaises(SandboxCreationError):
                        Sandbox(
                            "docker",
                            preset=name,
                            git_repo="https://example.com/repo.git",
                            files={"repo/input.txt": "data"},
                            _backend_client=client,
                        )
                    self.assertEqual(
                        session.execute_command.call_count, failed_step + 1
                    )
                    session.write_file.assert_not_called()
                    session.destroy.assert_called_once()

    def test_builtin_presets_install_without_registry_images(self):
        for backend in ("docker", "daytona", "modal"):
            for name in BUILT_IN_PRESETS:
                with self.subTest(backend=backend, preset=name):
                    preset = get_preset(name)
                    with Sandbox(backend, preset=name) as sb:
                        self.assertIsNone(sb._config.image)
                        calls = sb._session.execute_command.call_args_list
                        commands = [call.args[0] for call in calls]
                        # The second command runs the same npm install as
                        # other backends, with sudo for a non-root user.
                        self.assertEqual(
                            commands,
                            [
                                CONTAINER_PREREQUISITES,
                                CONTAINER_NPM_INSTALL.format(
                                    install=preset.setup_commands[0]
                                ),
                            ],
                        )

    def test_builtin_preset_keeps_explicit_base_image_and_installs(self):
        for backend in ("docker", "daytona", "modal"):
            with self.subTest(backend=backend):
                with Sandbox(
                    backend, preset="codex", image="python:3.12-slim"
                ) as sb:
                    self.assertEqual(sb._config.image, "python:3.12-slim")
                    self.assertEqual(
                        sb._session.execute_command.call_args_list[-1],
                        mock.call(
                            CONTAINER_NPM_INSTALL.format(
                                install="npm install -g @openai/codex"
                            )
                        ),
                    )

    def test_tensorlake_inherits_tensorlake_image_and_skips_setup(self) -> None:
        sb = Sandbox(backend="tensorlake", preset=self.PRESET_NAME)

        self.assertEqual(sb._config.image, "tl-name")
        sb._session.execute_command.assert_not_called()

    def test_tensorlake_without_tensorlake_image_runs_setup(self) -> None:
        # The built-in preset uses Tensorlake's writable npm prefix.
        sb = Sandbox(backend="tensorlake", preset="claude-code")

        self.assertIsNone(sb._config.image)
        sb._session.execute_command.assert_called_once_with(
            f"{TENSORLAKE_NPM_PREFIX} @anthropic-ai/claude-code"
        )

    def test_runpod_uses_ssh_ready_default_image_and_runs_setup(self) -> None:
        sb = Sandbox(backend="runpod", preset=self.PRESET_NAME)

        self.assertIsNone(sb._config.image)
        sb._session.execute_command.assert_called_once_with("echo hi")

    def test_backend_only_setup_commands_run(self) -> None:
        sb = Sandbox(backend="tensorlake", preset=self.BACKEND_ONLY_PRESET_NAME)

        sb._session.execute_command.assert_called_once_with("echo tensorlake")

    def test_explicit_image_override_on_tensorlake_runs_setup(self) -> None:
        # Explicit override doesn't match preset.tensorlake_image, so we
        # don't treat it as the preset's pre-baked image → setup runs.
        sb = Sandbox(
            backend="tensorlake",
            preset=self.PRESET_NAME,
            image="user-override",
        )

        self.assertEqual(sb._config.image, "user-override")
        sb._session.execute_command.assert_called()

    def test_tensorlake_git_repo_uses_relative_destination(self) -> None:
        sb = Sandbox(
            backend="tensorlake", git_repo="https://github.com/acme/project.git"
        )

        sb._session.execute_command.assert_called_once_with(
            "git clone --depth 1 https://github.com/acme/project.git project",
            None,
        )


if __name__ == "__main__":
    unittest.main()
