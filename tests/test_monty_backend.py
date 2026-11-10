"""Monty contract tests; real workers are local and require the monty extra."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from unittest import mock

import pytest

from bespokelabs.sandbox import (
    AsyncSandbox,
    NetworkPolicy,
    Sandbox,
    SandboxClient,
)
from bespokelabs.sandbox.backends.monty import MontyClient
from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxConfigurationError,
    SandboxCreationError,
    SandboxExecutionError,
)


@pytest.fixture
def monty():
    return pytest.importorskip("pydantic_monty")


def test_missing_extra_has_install_hint():
    with mock.patch.dict(sys.modules, {"pydantic_monty": None}):
        with pytest.raises(BackendNotInstalledError, match=r"sandbox\[monty\]"):
            MontyClient()


@pytest.mark.parametrize(
    "options",
    [
        {"cpu": 1},
        {"preset": "codex"},
        {"git_repo": "https://example.com/repo"},
        {"network": NetworkPolicy(mode="allowlist", domains=("example.com",))},
    ],
)
def test_unsupported_setup_before_sdk_import(options):
    with mock.patch.dict(sys.modules, {"pydantic_monty": None}):
        with pytest.raises(FeatureNotSupportedError):
            Sandbox("monty", **options)


def test_execution_state_and_streams(monty):
    with Sandbox("monty") as sb:
        result = sb.execute_code(
            "import sys\nx = 41\nprint('out')\nprint('err', file=sys.stderr)"
        )
        assert (result.stdout, result.stderr, result.exit_code) == (
            "out\n",
            "err\n",
            0,
        )
        assert (
            sb.execute_code("print(x + 1)", language="python3").stdout == "42\n"
        )
        assert sb.execute_code("x + 1").stdout == ""
        assert sb.provider_resource_id is None
        assert sb.estimate_compute_cost(60) == 0


def test_structured_result(monty):
    @dataclass
    class Answer:
        value: int

    with Sandbox("monty") as sb:
        assert sb.execute_code(
            "print('{\"value\": 42}')", return_type=Answer
        ) == Answer(42)


@pytest.mark.parametrize(
    "code, error",
    [
        ("if", "SyntaxError"),
        ("print('before'); 1 / 0", "ZeroDivisionError"),
    ],
)
def test_guest_errors_preserve_session(monty, code, error):
    with Sandbox("monty") as sb:
        result = sb.execute_code(code)
        assert result.exit_code == 1
        assert error in result.stderr
        if "before" in code:
            assert result.stdout == "before\n"
        assert sb.execute_code("print('still alive')").stdout == "still alive\n"


def test_virtual_files_and_environment(monty, tmp_path, monkeypatch):
    monkeypatch.setenv("MONTY_HOST_SECRET", "must not leak")
    with Sandbox(
        "monty",
        workdir="/workspace",
        env_vars={"APP": "test"},
        files={"a/in.txt": "héllo"},
    ) as sb:
        assert sb.read_file("a/in.txt") == "héllo".encode()
        assert (
            sb.execute_code("print(open('a/in.txt').read())").stdout
            == "héllo\n"
        )
        assert (
            sb.execute_code(
                "import os\nprint(os.getenv('APP'), os.getenv('MONTY_HOST_SECRET'))"
            ).stdout
            == "test None\n"
        )
        result = sb.execute_code(
            "from pathlib import Path\nPath('a/out.bin').write_bytes(b'\\x00\\xff')"
        )
        assert result.exit_code == 0
        assert sb.read_file("/workspace/a/out.bin") == b"\x00\xff"
        files = sb.list_files("a")
        assert [(f.path, f.size) for f in files] == [
            ("/workspace/a/in.txt", 6),
            ("/workspace/a/out.bin", 2),
        ]
        assert sb.list_files("/workspace")[0].is_dir
        local = tmp_path / "binary"
        local.write_bytes(b"\x00\xff")
        sb.upload_file(str(local), "nested/upload.bin")
        downloaded = tmp_path / "downloaded"
        sb.download_file("nested/upload.bin", str(downloaded))
        assert downloaded.read_bytes() == b"\x00\xff"
        sb.write_file("../root.txt", "virtual")
        assert sb.read_file("/root.txt") == b"virtual"
        # chdir is scoped to the current feed; API paths and later feeds use workdir.
        sb.execute_code("os.chdir('/')")
        assert sb.execute_code("print(os.getcwd())").stdout == "/workspace\n"


def test_no_ambient_host_access(monty, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret")
    with Sandbox("monty", network=NetworkPolicy(mode="none")) as sb:
        assert (
            sb.execute_code(f"print(open({str(secret)!r}).read())").exit_code
            == 1
        )
        for module in ("subprocess", "socket", "numpy"):
            assert sb.execute_code(f"import {module}").exit_code == 1
        sb.write_file(str(secret), "virtual replacement")
        assert secret.read_text() == "host secret"


def test_client_sessions_are_independent_and_workers_close(monty):
    client = SandboxClient("monty")
    with client.create() as first, client.create() as second:
        pid = first._session._session.worker_pid
        first.execute_code("x = 1")
        first.write_file("/only-first", "yes")
        assert second.list_files() == []
        assert second.execute_code("print(x)").exit_code == 1
        first.destroy()
        first.destroy()
        assert second.execute_code("print('ok')").stdout == "ok\n"
    if os.name == "posix":
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    with client.create() as third:
        assert third.execute_code("print(3)").stdout == "3\n"


@pytest.mark.parametrize(
    "options,code,exit_code",
    [
        ({"timeout_secs": 0.02}, "while True: pass", 124),
        ({"memory_mb": 1}, "x = 'a' * 100_000_000", 1),
        ({"backend_options": {"max_output_bytes": 10}}, "print('x' * 100)", 1),
    ],
)
def test_resource_failures_discard_worker(monty, options, code, exit_code):
    with Sandbox("monty", **options) as sb:
        pid = sb._session._session.worker_pid
        result = sb.execute_code(code)
        assert result.exit_code == exit_code
        assert result.stderr
        with pytest.raises(SandboxExecutionError, match="closed"):
            sb.execute_code("print('must not reuse heap')")
        if os.name == "posix":
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)


def test_virtual_file_budget_applies_to_code_and_sdk(monty):
    with Sandbox("monty", backend_options={"max_file_bytes": 4}) as sb:
        sb.write_file("a", "1234")
        with pytest.raises(SandboxExecutionError):
            sb.write_file("b", "5")
        assert sb.execute_code("open('b', 'w').write('5')").exit_code == 1
        assert sb.read_file("a") == b"1234"


@pytest.mark.parametrize(
    "options",
    [
        {"image": "python"},
        {"gpu": "L4"},
        {"disk_mb": 10},
        {"template": "x"},
        {"snapshot_id": "x"},
        {"app_name": "x"},
        {"network": NetworkPolicy(mode="public")},
    ],
)
def test_unsupported_configuration(monty, options):
    with mock.patch.object(monty, "Monty") as pool:
        with pytest.raises(FeatureNotSupportedError):
            Sandbox("monty", **options)
        pool.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [
        {"timeout_secs": 0},
        {"memory_mb": 0},
        {"workdir": "relative"},
        {"backend_options": {"external_lookup": {}}},
        {"backend_options": {"max_output_bytes": None}},
        {"backend_options": {"max_file_bytes": -1}},
        {"backend_options": {"max_suspensions": True}},
        {"backend_options": {"max_recursion_depth": 1.5}},
    ],
)
def test_invalid_configuration(monty, options):
    with pytest.raises(SandboxConfigurationError):
        Sandbox("monty", **options)


def test_unsupported_operations(monty, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("x")
    with Sandbox("monty") as sb:
        calls = [
            lambda: sb.execute_command("echo hi"),
            lambda: sb.execute_code("console.log(1)", "javascript"),
            sb.snapshot,
            sb.session_state,
            lambda: sb.expose_port(8000),
            lambda: sb.upload_dir(source, "/data"),
            lambda: sb.upload_dir(source, "/data", method="per_file"),
            lambda: sb.download_dir("/", tmp_path / "output"),
        ]
        for call in calls:
            with pytest.raises(FeatureNotSupportedError):
                call()
    with pytest.raises(FeatureNotSupportedError):
        MontyClient().resume({})


def test_failed_checkout_closes_pool(monty):
    with mock.patch.object(monty, "Monty") as pool_type:
        pool = pool_type.return_value.__enter__.return_value
        pool.checkout.side_effect = RuntimeError("checkout failed")
        with pytest.raises(SandboxCreationError, match="checkout failed"):
            Sandbox("monty")
        pool_type.return_value.__exit__.assert_called_once()


def test_failed_file_seeding_closes_worker(monty):
    with mock.patch(
        "bespokelabs.sandbox.backends.monty.MontySession.destroy", autospec=True
    ) as destroy:
        # Wrap the original cleanup so this regression test does not leak workers.

        # The actual lifecycle is also exercised by the worker-exit tests.
        destroy.side_effect = lambda session: session._stack.close()
        with pytest.raises(SandboxExecutionError):
            Sandbox(
                "monty",
                files={"x": "too large"},
                backend_options={"max_file_bytes": 1},
            )
        destroy.assert_called_once()


def test_control_plane_can_create_and_execute(monty, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from bespokelabs.sandbox.control_plane.api import create_app
    from bespokelabs.sandbox.control_plane.service import ControlPlane
    from bespokelabs.sandbox.control_plane.store import SQLiteStore

    store = SQLiteStore(tmp_path / "monty.db", key_pepper="test")
    service = ControlPlane(
        store, allowed_backends={"monty"}, supervision_interval_secs=0
    )
    _, key = service.bootstrap_organization("Monty test")
    try:
        with TestClient(create_app(service)) as client:
            headers = {"Authorization": f"Bearer {key.secret}"}
            response = client.post(
                "/v1/sandboxes", headers=headers, json={"backend": "monty"}
            )
            assert response.status_code == 201, response.text
            sandbox_id = response.json()["id"]
            response = client.post(
                f"/v1/sandboxes/{sandbox_id}/execute",
                headers=headers,
                json={"code": "print(42)"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["stdout"] == "42\n"
            response = client.post(
                f"/v1/sandboxes/{sandbox_id}/execute",
                headers=headers,
                json={"command": "echo hi"},
            )
            assert response.status_code >= 400
            assert "feature_not_supported" in response.text
    finally:
        service.close()


def test_async_calls_are_serialized(monty):
    async def run():
        async with await AsyncSandbox.create("monty") as sb:
            await sb.execute_code("x = 0")
            results = await asyncio.gather(
                *(sb.execute_code("x += 1\nprint(x)") for _ in range(8))
            )
            assert sorted(int(r.stdout) for r in results) == list(range(1, 9))
            await sb.write_file("test", "data")
            assert await sb.read_file("test") == b"data"

    asyncio.run(run())
