"""Offline Novita adapter tests. No provider SDK or credentials required."""

from __future__ import annotations

import builtins
import json
import socket
import sys
import traceback
from enum import Enum
from types import ModuleType, SimpleNamespace
from unittest import mock

import httpx
import pytest

from bespokelabs.sandbox import (
    NetworkPolicy,
    Sandbox,
    SandboxClient,
    SandboxSessionState,
)
from bespokelabs.sandbox.backends import BACKENDS
from bespokelabs.sandbox.backends.e2b import E2BClient
from bespokelabs.sandbox.backends.novita import NovitaClient, NovitaSession
from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    CommandFailedError,
    FeatureNotSupportedError,
    SandboxAuthenticationError,
    SandboxConfigurationError,
    SandboxConnectionError,
    SandboxCreationError,
    SandboxExecutionError,
    SandboxNotFoundError,
    SandboxTimeoutError,
)
from bespokelabs.sandbox.protocols import (
    SandboxBackendClient,
    SandboxBackendSession,
)
from bespokelabs.sandbox.types import SandboxConfig


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Offline tests must not open sockets")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture
def sdk(monkeypatch):
    """Model only the verified 2.1.3 surface, not a permissive SDK mock."""
    monkeypatch.setenv("NOVITA_API_KEY", "offline-placeholder")
    monkeypatch.setenv("NOVITA_DOMAIN", "region.example.test")
    root = ModuleType("novita_sandbox")
    for name in (
        "AuthenticationException",
        "InvalidArgumentException",
        "NotFoundException",
        "TimeoutException",
        "SandboxException",
    ):
        setattr(root, name, type(name, (Exception,), {}))

    class CommandExitException(Exception):

        def __init__(self, *, stdout, stderr, exit_code):
            self.stdout, self.stderr, self.exit_code = stdout, stderr, exit_code

    class FileType(Enum):
        FILE = "file"
        DIR = "dir"

    class ConnectionConfig:
        envd_port = 49983

        @staticmethod
        def _domain():
            import os

            return os.environ["NOVITA_DOMAIN"]

        def __init__(self, domain=None, extra_sandbox_headers=None):
            self.domain = domain or self._domain()
            self.debug = False
            self.api_url = f"https://api.{self.domain}"
            self._sandbox_url = None
            self.sandbox_headers = extra_sandbox_headers or {}

    root.CommandExitException = CommandExitException
    root.FileType = FileType
    root.Novita = mock.Mock()
    sandbox = mock.Mock(
        sandbox_id="sb-test",
        traffic_access_token="traffic-placeholder",
        connection_config=ConnectionConfig(),
    )
    sandbox.get_host.return_value = "8080-sb-test.region.example.test"
    sandbox.commands.run.return_value = SimpleNamespace(
        stdout="ok\n", stderr="", exit_code=0
    )
    sandbox.run_code.return_value = SimpleNamespace(
        logs=SimpleNamespace(stdout=["hello", "\n"], stderr=[]), error=None
    )
    root.Novita.return_value.code_interpreter.create.return_value = sandbox
    root.Novita.return_value.code_interpreter.connect.return_value = sandbox
    connection = ModuleType("novita_sandbox.core.connection_config")
    connection.ConnectionConfig = ConnectionConfig
    monkeypatch.setitem(sys.modules, "novita_sandbox", root)
    monkeypatch.setitem(
        sys.modules, "novita_sandbox.core", ModuleType("novita_sandbox.core")
    )
    monkeypatch.setitem(
        sys.modules, "novita_sandbox.core.connection_config", connection
    )
    return SimpleNamespace(root=root, sandbox=sandbox)


def test_optional_import_and_existing_e2b(monkeypatch):
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name.startswith("novita_sandbox"):
            raise ImportError("SDK absent")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    assert BACKENDS["e2b"] is E2BClient
    assert BACKENDS["novita"] is NovitaClient
    SandboxClient("local")
    with pytest.raises(BackendNotInstalledError, match=r"\[novita\]"):
        SandboxClient("novita")


def test_construct_no_connection(sdk):
    client = NovitaClient()
    assert isinstance(client, SandboxBackendClient)
    sdk.root.Novita.assert_not_called()


@pytest.mark.parametrize("op", ["create", "resume"])
def test_missing_credentials(sdk, monkeypatch, op):
    monkeypatch.delenv("NOVITA_API_KEY")
    client = NovitaClient()
    with pytest.raises(SandboxAuthenticationError, match="NOVITA_API_KEY"):
        getattr(client, op)(
            SandboxConfig("novita")
            if op == "create"
            else {"sandbox_id": "sb-test"}
        )
    sdk.root.Novita.assert_not_called()


def test_creation_defaults_and_protocol(sdk):
    session = NovitaClient().create(SandboxConfig("novita"))
    sdk.root.Novita.return_value.code_interpreter.create.assert_called_once_with(
        timeout=600
    )
    assert isinstance(session, SandboxBackendSession)
    assert session.provider_resource_id == "sb-test"


def test_creation_mapping_last_wins(sdk):
    config = SandboxConfig(
        "novita",
        timeout_secs=42,
        template="custom",
        env_vars={"A": "B"},
        backend_options={
            "timeout": 15,
            "template": "other",
            "envs": {"C": "D"},
            "domain": "other.example.test",
        },
        cpu=8,
        memory_mb=8192,
        disk_mb=20000,
        gpu="ignored",
        image="ignored",
        allow_internet=False,
    )
    NovitaClient().create(config)
    sdk.root.Novita.return_value.code_interpreter.create.assert_called_once_with(
        timeout=15,
        template="other",
        envs={"C": "D"},
        domain="other.example.test",
    )


def test_creation_explicit_timeout_template_env(sdk):
    NovitaClient().create(
        SandboxConfig(
            "novita", timeout_secs=42, template="custom", env_vars={"A": "B"}
        )
    )
    sdk.root.Novita.return_value.code_interpreter.create.assert_called_once_with(
        timeout=42, template="custom", envs={"A": "B"}
    )


def test_python_result_and_errors(sdk):
    session = NovitaSession(sandbox=sdk.sandbox)
    result = session.execute_code("print('hello')")
    assert (result.stdout, result.stderr, result.exit_code) == (
        "hello\n",
        "",
        0,
    )
    sdk.sandbox.run_code.assert_called_once_with("print('hello')")
    sdk.sandbox.run_code.return_value.error = SimpleNamespace(
        name="ValueError", value="oops", traceback="ValueError: oops"
    )
    assert session.execute_code("bad").stderr == "ValueError: oops"
    assert session.execute_code("bad").exit_code == 1
    with pytest.raises(FeatureNotSupportedError):
        session.execute_code("x", "javascript")


def test_command_quoting_success_and_nonzero(sdk):
    session = NovitaSession(sandbox=sdk.sandbox)
    assert (
        session.execute_command("echo", ["two words", "'", "$(no)"]).stdout
        == "ok\n"
    )
    sdk.sandbox.commands.run.assert_called_once_with(
        "echo 'two words' ''\"'\"'' '$(no)'"
    )
    sdk.sandbox.commands.run.side_effect = sdk.root.CommandExitException(
        stdout="out", stderr="err", exit_code=17
    )
    result = session.execute_command("exit 17")
    assert (result.stdout, result.stderr, result.exit_code) == (
        "out",
        "err",
        17,
    )
    public = Sandbox._from_session("novita", session, SandboxConfig("novita"))
    with pytest.raises(CommandFailedError) as failure:
        public.execute_command("exit 17", return_type=dict)
    assert failure.value.exit_code == 17


@pytest.mark.parametrize(
    "op", ["create", "resume", "execute_command", "read_file", "destroy"]
)
@pytest.mark.parametrize(
    "kind,expected",
    [
        ("AuthenticationException", SandboxAuthenticationError),
        ("TimeoutException", SandboxTimeoutError),
        ("NotFoundException", SandboxNotFoundError),
        ("InvalidArgumentException", SandboxConfigurationError),
        ("connection", SandboxConnectionError),
        ("request", None),
    ],
)
def test_errors_are_translated_and_redacted(sdk, op, kind, expected):
    error = (
        httpx.ConnectError("secret-placeholder")
        if kind == "connection"
        else RuntimeError("secret-placeholder")
        if kind == "request"
        else getattr(sdk.root, kind)("secret-placeholder")
    )
    if op == "create":
        sdk.root.Novita.return_value.code_interpreter.create.side_effect = error

        def action():
            return NovitaClient().create(SandboxConfig("novita"))

    elif op == "resume":
        sdk.root.Novita.return_value.code_interpreter.connect.side_effect = (
            error
        )

        def action():
            return NovitaClient().resume({"sandbox_id": "sb-test"})

    else:
        session = NovitaSession(sandbox=sdk.sandbox)
        if op == "destroy":
            sdk.sandbox.kill.side_effect = error
            action = session.destroy
        elif op == "read_file":
            sdk.sandbox.files.read.side_effect = error

            def action():
                return session.read_file("/file")

        else:
            sdk.sandbox.commands.run.side_effect = error

            def action():
                return session.execute_command("cmd")

    expected = expected or (
        SandboxCreationError
        if op in {"create", "resume"}
        else SandboxExecutionError
    )
    with pytest.raises(expected) as failure:
        action()
    assert failure.value.op == op
    assert "secret-placeholder" not in "".join(
        traceback.format_exception(failure.value)
    )


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, SandboxAuthenticationError),
        (403, SandboxAuthenticationError),
        (404, SandboxNotFoundError),
        (500, SandboxCreationError),
    ],
)
def test_resume_http_errors(sdk, status, expected):
    response = httpx.Response(
        status, request=httpx.Request("POST", "https://example.test")
    )
    sdk.root.Novita.return_value.code_interpreter.connect.side_effect = (
        httpx.HTTPStatusError(
            "secret-placeholder", request=response.request, response=response
        )
    )
    with pytest.raises(expected):
        NovitaClient().resume({"sandbox_id": "sb-test"})


@pytest.mark.parametrize("payload", [b"\x00\xff\x80\xfe", b""])
def test_binary_files_and_transfer(sdk, tmp_path, payload):
    session = NovitaSession(sandbox=sdk.sandbox)
    sdk.sandbox.files.read.return_value = bytearray(payload)
    assert session.read_file("/file") == payload
    assert type(session.read_file("/file")) is bytes
    sdk.sandbox.files.read.assert_called_with("/file", format="bytes")
    session.write_file("/file", payload)
    sdk.sandbox.files.write.assert_called_with("/file", payload)
    session.write_file("/text", "unicode ü")
    sdk.sandbox.files.write.assert_called_with("/text", "unicode ü")
    source, target = tmp_path / "in", tmp_path / "out"
    source.write_bytes(payload)
    session.upload_file(str(source), "/uploaded")
    sdk.sandbox.files.write.assert_called_with("/uploaded", payload)
    session.download_file("/uploaded", str(target))
    assert target.read_bytes() == payload


def test_file_type_mapping(sdk):
    sdk.sandbox.files.list.return_value = [
        SimpleNamespace(path="/dir", type=sdk.root.FileType.DIR, size=4096),
        SimpleNamespace(path="/file", type=sdk.root.FileType.FILE, size=0),
    ]
    entries = NovitaSession(sandbox=sdk.sandbox).list_files("/")
    assert [(entry.path, entry.is_dir, entry.size) for entry in entries] == [
        ("/dir", True, 4096),
        ("/file", False, 0),
    ]


def test_json_state_resume_uses_fresh_sdk_client_without_setup(
    sdk, monkeypatch
):
    session = NovitaSession(sandbox=sdk.sandbox)
    state = Sandbox._from_session(
        "novita", session, SandboxConfig("novita")
    ).session_state()
    serialized = state.to_json()
    assert json.loads(serialized)["data"] == {
        "sandbox_id": "sb-test",
        "domain": "region.example.test",
    }
    assert "placeholder" not in serialized
    monkeypatch.setenv("NOVITA_DOMAIN", "changed.example.test")
    resumed = Sandbox.resume(SandboxSessionState.from_json(serialized))
    assert resumed.provider_resource_id == "sb-test"
    assert resumed.execute_code("print('hello')").stdout == "hello\n"
    sdk.root.Novita.assert_called_once_with(domain="region.example.test")
    sdk.root.Novita.return_value.code_interpreter.connect.assert_called_once_with(
        "sb-test"
    )
    sdk.root.Novita.return_value.code_interpreter.create.assert_not_called()
    sdk.sandbox.commands.run.assert_not_called()
    sdk.sandbox.files.write.assert_not_called()


@pytest.mark.parametrize("token", [None, "traffic-placeholder"])
def test_expose_port_and_secret_repr(sdk, token):
    sdk.sandbox.traffic_access_token = token
    exposed = NovitaSession(sandbox=sdk.sandbox).expose_port(8080)
    assert exposed.url == "https://8080-sb-test.region.example.test"
    assert exposed.headers == (
        {"Sandbox-Traffic-Access-Token": token} if token else {}
    )
    assert "placeholder" not in repr(exposed)
    assert "placeholder" not in exposed.url


def test_destroy_failure_retains_resource_and_alive_state(sdk):
    session = NovitaSession(sandbox=sdk.sandbox)
    public = Sandbox._from_session("novita", session, SandboxConfig("novita"))
    sdk.sandbox.kill.side_effect = RuntimeError("failure")
    with pytest.raises(SandboxExecutionError):
        public.destroy()
    assert public.is_alive
    assert public.provider_resource_id == "sb-test"
    sdk.sandbox.kill.side_effect = None
    sdk.sandbox.kill.return_value = False  # SDK's confirmed 404
    public.destroy()
    public.destroy()
    assert not public.is_alive
    assert sdk.sandbox.kill.call_count == 2


def test_unsupported_and_shared_safety(sdk, tmp_path):
    with pytest.raises(FeatureNotSupportedError):
        NovitaSession(sandbox=sdk.sandbox).snapshot()
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12")
    with pytest.raises(FeatureNotSupportedError):
        Sandbox("novita", dockerfile=dockerfile)
    for mode in ("none", "allowlist"):
        with pytest.raises(FeatureNotSupportedError):
            Sandbox(
                "novita",
                network=NetworkPolicy(
                    mode=mode,
                    domains=("example.test",) if mode == "allowlist" else (),
                ),
            )
    with pytest.raises(SandboxConfigurationError):
        Sandbox(
            "novita",
            network=NetworkPolicy(mode="public"),
            backend_options={"network": {"allow_public_traffic": False}},
        )
    sdk.root.Novita.assert_not_called()


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, SandboxAuthenticationError),
        (403, SandboxAuthenticationError),
        (404, SandboxNotFoundError),
    ],
)
def test_sdk_status_prefix_errors(sdk, status, expected):
    sdk.sandbox.run_code.side_effect = sdk.root.SandboxException(
        f"{status}: secret-placeholder"
    )
    with pytest.raises(expected) as failure:
        NovitaSession(sandbox=sdk.sandbox).execute_code("x")
    assert failure.value.context["provider_status"] == status
    assert "secret-placeholder" not in str(failure.value)


def test_unconfirmed_kill_keeps_session_attached(sdk):
    session = NovitaSession(sandbox=sdk.sandbox)
    sdk.sandbox.kill.return_value = None
    with pytest.raises(SandboxExecutionError, match="not confirmed"):
        session.destroy()
    assert not session._destroyed
    assert session.provider_resource_id == "sb-test"
    sdk.sandbox.kill.return_value = True
    session.destroy()
    assert session._destroyed


@pytest.mark.parametrize("token", [None, "fresh-traffic-placeholder"])
def test_resume_uses_sdk_authentication(sdk, token):
    sdk.sandbox.traffic_access_token = token
    resumed = NovitaClient().resume({"sandbox_id": "sb-test"})
    sdk.root.Novita.assert_called_once_with()
    sdk.root.Novita.return_value.code_interpreter.connect.assert_called_once_with(
        "sb-test"
    )
    assert resumed.expose_port(8080).headers == (
        {"Sandbox-Traffic-Access-Token": token} if token else {}
    )


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"sandbox_id": "../other"},
        {"sandbox_id": "sb-test", "domain": "https://user:secret@example.test"},
    ],
)
def test_invalid_state_rejected_before_request(sdk, data):
    with pytest.raises(SandboxConfigurationError) as failure:
        NovitaClient().resume(data)
    assert "secret" not in str(failure.value)
    sdk.root.Novita.assert_not_called()


@pytest.mark.parametrize(
    "operation", ["execute_code", "execute_command", "resume"]
)
@pytest.mark.parametrize("kind", ["timeout", "disconnect"])
def test_execution_transport_failure_has_unknown_outcome(sdk, operation, kind):
    error = (
        httpx.ReadTimeout("secret-placeholder")
        if kind == "timeout"
        else httpx.ReadError("secret-placeholder")
    )
    target = (
        sdk.root.Novita.return_value.code_interpreter.connect
        if operation == "resume"
        else sdk.sandbox.run_code
        if operation == "execute_code"
        else sdk.sandbox.commands.run
    )
    target.side_effect = error
    session = NovitaSession(sandbox=sdk.sandbox)
    with pytest.raises(
        (SandboxTimeoutError, SandboxConnectionError)
    ) as failure:
        if operation == "resume":
            NovitaClient().resume({"sandbox_id": "sb-test"})
        else:
            getattr(session, operation)("possibly_side_effecting_operation")
    assert failure.value.outcome.value == "unknown"
    assert failure.value.retryable is False


@pytest.mark.parametrize("override_domain", [False, True])
def test_real_sdk_contract_offline(monkeypatch, override_domain):
    """Exercise actual 2.1.3 SDK through a mocked HTTP transport if installed."""
    novita = pytest.importorskip("novita_sandbox")
    from novita_sandbox.core.api import ApiClient

    for name in (
        "NOVITA_API_URL",
        "NOVITA_SANDBOX_URL",
        "NOVITA_ACCESS_TOKEN",
        "NOVITA_DEBUG",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NOVITA_API_KEY", "offline-placeholder")
    monkeypatch.setenv("NOVITA_DOMAIN", "region.example.test")
    domain = (
        "override.example.test" if override_domain else "region.example.test"
    )
    requests = []

    def handler(request):
        assert request.url.host == f"api.{domain}"
        requests.append((request.method, request.url.path))
        if request.url.path == "/sandboxes":
            assert request.method == "POST"
            assert (
                json.loads(request.content)["templateID"]
                == "code-interpreter-v1"
            )
            assert json.loads(request.content)["timeout"] == 600
        else:
            assert request.method == "POST"
            assert request.url.path == "/sandboxes/sb-test/connect"
            assert json.loads(request.content) == {"timeout": 300}
        return httpx.Response(
            201 if request.url.path == "/sandboxes" else 200,
            json={
                "sandboxID": "sb-test",
                "clientID": "",
                "state": "running",
                "templateID": "code-interpreter-v1",
                "envdVersion": "0.5.7",
                "domain": domain,
                "envdAccessToken": (
                    "initial-controller-placeholder"
                    if request.url.path == "/sandboxes"
                    else "controller-placeholder"
                ),
                "trafficAccessToken": (
                    "initial-traffic-placeholder"
                    if request.url.path == "/sandboxes"
                    else "traffic-placeholder"
                ),
                "network": {"allowPublicTraffic": False},
            },
        )

    transport = httpx.MockTransport(handler)
    original_init = ApiClient.__init__

    def mocked_init(self, config, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, config, *args, **kwargs)

    monkeypatch.setattr(ApiClient, "__init__", mocked_init)
    client = NovitaClient()
    session = client.create(
        SandboxConfig(
            "novita",
            backend_options={"domain": domain} if override_domain else {},
        )
    )
    assert session.session_state()["domain"] == domain
    assert isinstance(session._sandbox, novita.CodeInterpreterSandbox)
    state = json.loads(json.dumps(session.session_state()))
    monkeypatch.setenv("NOVITA_DOMAIN", "changed.example.test")
    resumed = NovitaClient().resume(state)
    assert isinstance(resumed._sandbox, novita.CodeInterpreterSandbox)
    assert resumed._sandbox is not session._sandbox
    assert resumed.provider_resource_id == session.provider_resource_id
    assert "placeholder" not in json.dumps(state)
    assert resumed.expose_port(8080).headers == {
        "Sandbox-Traffic-Access-Token": "traffic-placeholder"
    }
    assert requests == [
        ("POST", "/sandboxes"),
        ("POST", "/sandboxes/sb-test/connect"),
    ]
    assert (
        resumed._sandbox._jupyter_headers["X-Access-Token"]
        == "controller-placeholder"
    )
    assert (
        resumed._sandbox._jupyter_headers["E2B-Traffic-Access-Token"]
        == "traffic-placeholder"
    )

    def execute(request):
        assert request.url.path == "/execute"
        assert request.headers["X-Access-Token"] == "controller-placeholder"
        assert (
            request.headers["E2B-Traffic-Access-Token"] == "traffic-placeholder"
        )
        return httpx.Response(
            200, content=b'{"type":"stdout","text":"42\\n"}\n'
        )

    with mock.patch.object(
        novita.CodeInterpreterSandbox,
        "_client",
        new_callable=mock.PropertyMock,
        return_value=httpx.Client(transport=httpx.MockTransport(execute)),
    ):
        assert resumed.execute_code("print(42)").stdout == "42\n"

    from novita_sandbox.core.connection_config import ConnectionConfig

    for override in ("NOVITA_API_URL", "NOVITA_SANDBOX_URL", "NOVITA_DEBUG"):
        with monkeypatch.context() as scoped:
            scoped.setenv(
                override,
                "true"
                if override == "NOVITA_DEBUG"
                else "https://example.test/secret-placeholder",
            )
            unusual = NovitaSession(
                sandbox=SimpleNamespace(
                    sandbox_id="sb-test", connection_config=ConnectionConfig()
                )
            )
            with pytest.raises(FeatureNotSupportedError):
                unusual.session_state()
    assert requests == [
        ("POST", "/sandboxes"),
        ("POST", "/sandboxes/sb-test/connect"),
    ]
