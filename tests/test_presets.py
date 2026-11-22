from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
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
    "antigravity",
    "claude-code",
    "claude-code-codex",
    "codex",
    "opencode",
    "grok",
}
TENSORLAKE_NPM_PREFIX = "mkdir -p $HOME/.npm-global && npm config set prefix $HOME/.npm-global && npm install -g"


class PresetTests(unittest.TestCase):

    def test_opencode_installs_on_all_backends(self) -> None:
        preset = get_preset("opencode")
        self.assertIsNone(preset.image)
        self.assertEqual(preset.setup_commands, ["npm install -g opencode-ai"])
        self.assertTrue(preset.setup_before_workspace)
        self.assertEqual(
            preset.backend_setup_commands["docker"],
            [
                "(command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 && command -v git >/dev/null 2>&1) || "
                "(apt-get update && DEBIAN_FRONTEND=noninteractive "
                "apt-get install -y --no-install-recommends nodejs npm git)",
                "npm install -g opencode-ai",
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
        self.assertIn(
            "apt-get install -y --no-install-recommends nodejs npm git",
            preset.backend_setup_commands["docker"][0],
        )
        self.assertEqual(
            preset.backend_setup_commands["docker"][1], preset.setup_commands[0]
        )
        self.assertEqual(preset.memory_mb, 2048)
        self.assertEqual(preset.timeout_secs, 1800)
        self.assertTrue(preset.allow_internet)

    def test_antigravity_installs_native_cli_on_path(self):
        preset = get_preset("antigravity")
        install, settings = preset.setup_commands
        self.assertIsNone(preset.image)
        self.assertTrue(preset.setup_before_workspace)
        self.assertIn(
            "curl -fsSL https://antigravity.google/cli/install.sh", install
        )
        # ~/.local/bin is only the fallback because exec calls often miss it.
        self.assertIn("dir=/usr/local/bin", install)
        self.assertIn('--dir "$dir"', install)
        self.assertTrue(
            install.startswith("if command -v agy; then agy --version; else")
        )
        self.assertTrue(install.endswith('"$dir/agy" --version; fi'))
        # Hosts never get sudo; only isolated containers may link with it.
        self.assertNotIn("sudo", install)
        self.assertIn('[ -n "${GEMINI_API_KEY:-}" ]', settings)
        self.assertIn('{"modelProvider": "gemini"}', settings)
        link = (
            'command -v agy || sudo -n ln -sf "$dir/agy" /usr/local/bin/agy; '
        )
        for backend in ("docker", "daytona", "modal"):
            prerequisites, container_install, container_settings = (
                preset.backend_setup_commands[backend]
            )
            self.assertIn(
                "apt-get install -y --no-install-recommends "
                "ca-certificates curl git",
                prerequisites,
            )
            self.assertIn(link, container_install)
            self.assertEqual(container_install.replace(link, ""), install)
            self.assertEqual(container_settings, settings)
        self.assertNotIn("tensorlake", preset.backend_setup_commands)
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


class AntigravityContainerPrerequisiteTests(unittest.TestCase):
    """Run the container prerequisite step against stub system commands.

    PATH holds only the stubs, so curl and git are missing unless a test
    adds them. Like the real sudo, the stub sudo drops the caller's
    environment before it runs the command.
    """

    INSTALL = (
        "apt-get install -y --no-install-recommends ca-certificates curl git"
    )

    def run_step(self, shell, uid, tools=()):
        stubs = {
            "id": 'echo "$FAKE_UID"',
            "sudo": (
                'echo "sudo $*" >> "$LOG"; [ "$1" = -n ] && shift; '
                'exec /usr/bin/env -i PATH="$PATH" LOG="$LOG" "$@"'
            ),
            "apt-get": 'echo "apt-get $* (${DEBIAN_FRONTEND:-unset})" >> "$LOG"',
            **dict.fromkeys(tools, "exit 0"),
        }
        step = get_preset("antigravity").backend_setup_commands["docker"][0]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, body in stubs.items():
                stub = root / name
                stub.write_text(f"#!/bin/sh\n{body}\n")
                stub.chmod(0o755)
            (root / "env").symlink_to("/usr/bin/env")
            log = root / "log"
            subprocess.run(
                [shell, "-c", step],
                env={"PATH": temp, "LOG": str(log), "FAKE_UID": str(uid)},
                check=True,
            )
            return log.read_text().splitlines() if log.exists() else []

    def shells(self):
        found = {name: shutil.which(name) for name in ("sh", "bash", "dash")}
        return {name: path for name, path in found.items() if path}

    def test_root_runs_apt_get_directly(self):
        for name, shell in self.shells().items():
            with self.subTest(shell=name):
                self.assertEqual(
                    self.run_step(shell, 0),
                    [
                        "apt-get update (unset)",
                        f"{self.INSTALL} (noninteractive)",
                    ],
                )

    def test_non_root_user_runs_apt_get_with_sudo(self):
        for name, shell in self.shells().items():
            with self.subTest(shell=name):
                self.assertEqual(
                    self.run_step(shell, 1000),
                    [
                        "sudo -n apt-get update",
                        "apt-get update (unset)",
                        "sudo -n env DEBIAN_FRONTEND=noninteractive "
                        + self.INSTALL,
                        f"{self.INSTALL} (noninteractive)",
                    ],
                )

    def test_installed_tools_skip_apt_get(self):
        for name, shell in self.shells().items():
            with self.subTest(shell=name):
                self.assertEqual(
                    self.run_step(shell, 1000, tools=("curl", "git")), []
                )


class AntigravityLocalSetupTests(unittest.TestCase):
    """Run the setup commands through the local backend's shell prelude.

    A stub ``agy`` on PATH skips the download. The local backend sets HOME
    to the workdir and rewrites absolute paths, so this also checks that
    the settings file lands in the sandbox HOME exactly once.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        agy = self.bin / "agy"
        agy.write_text('#!/bin/sh\necho "agy 1.5.13"\n')
        agy.chmod(0o755)
        self.workdir = root / "work"
        self.workdir.mkdir()
        self.settings = self.workdir / ".gemini/antigravity-cli/settings.json"

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **env):
        path = os.pathsep.join([str(self.bin), "/usr/bin", "/bin"])
        return Sandbox(
            "local",
            preset="antigravity",
            workdir=str(self.workdir),
            env_vars={"PATH": path, "AGY_ADC_AUTH": "", **env},
        )

    def test_api_key_selects_gemini_provider_in_sandbox_home(self):
        with self.create(GEMINI_API_KEY="test-key"):
            self.assertEqual(
                json.loads(self.settings.read_text()),
                {"modelProvider": "gemini"},
            )

    def test_install_passes_target_dir_to_google_script(self):
        # A stub curl serves a fake installer, and a temp directory stands
        # in for /usr/local/bin, so the test needs no network or root.
        (self.bin / "agy").unlink()
        target = Path(self.temp.name) / "target"
        target.mkdir()
        installer = (
            'while [ $# -gt 0 ]; do [ "$1" = --dir ] && dir=$2; shift; done\n'
            "printf '#!/bin/sh\\necho agy 1.5.13\\n' > \"$dir/agy\"\n"
            'chmod +x "$dir/agy"\n'
        )
        curl = self.bin / "curl"
        curl.write_text(
            "#!/bin/sh\n"
            'while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
            f"cat > \"$out\" <<'EOF'\n{installer}EOF\n"
        )
        curl.chmod(0o755)
        preset = SandboxPreset(
            name="_antigravity_install_check",
            description="Antigravity install with a stub installer",
            setup_commands=[
                command.replace("/usr/local/bin", str(target))
                for command in get_preset("antigravity").setup_commands
            ],
        )
        path = os.pathsep.join([str(self.bin), str(target), "/usr/bin", "/bin"])
        with Sandbox(
            "local",
            preset=preset,
            workdir=str(self.workdir),
            env_vars={"PATH": path, "GEMINI_API_KEY": ""},
        ) as sb:
            self.assertEqual(
                sb.execute_command("agy", ["--version"]).stdout,
                "agy 1.5.13\n",
            )

    def test_settings_untouched_without_key_with_adc_or_existing(self):
        for env in (
            {"GEMINI_API_KEY": ""},
            {"GEMINI_API_KEY": "test-key", "AGY_ADC_AUTH": "true"},
        ):
            with self.subTest(env=env), self.create(**env):
                self.assertFalse(self.settings.exists())
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text('{"keep": true}')
        with self.create(GEMINI_API_KEY="test-key"):
            self.assertEqual(self.settings.read_text(), '{"keep": true}')


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
                setup = get_preset(name).backend_setup_commands["docker"]
                self.assertIsNone(sb._config.image)
                self.assertEqual(
                    sb._session.mock_calls,
                    [mock.call.execute_command(command) for command in setup]
                    + [
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
            setup = get_preset(name).backend_setup_commands["docker"]
            for failed_step in range(len(setup)):
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
                        commands = [
                            call.args[0]
                            for call in sb._session.execute_command.call_args_list
                        ]
                        self.assertEqual(
                            commands, preset.backend_setup_commands[backend]
                        )
                        self.assertIn(
                            "apt-get install -y --no-install-recommends",
                            commands[0],
                        )
                        # After provisioning, npm presets run the same install
                        # as other backends. Antigravity's container install
                        # adds a sudo link step, covered by its own test.
                        if name != "antigravity":
                            self.assertEqual(
                                commands[1:], preset.setup_commands
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
                        mock.call("npm install -g @openai/codex"),
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
