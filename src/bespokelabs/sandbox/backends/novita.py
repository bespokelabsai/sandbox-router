"""Novita Code Interpreter backend (the optional ``novita`` extra)."""

from __future__ import annotations

import os
import pathlib
import re
import shlex
from contextlib import contextmanager
from typing import Iterator

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    ErrorOutcome,
    FeatureNotSupportedError,
    SandboxAuthenticationError,
    SandboxConfigurationError,
    SandboxConnectionError,
    SandboxCreationError,
    SandboxError,
    SandboxExecutionError,
    SandboxNotFoundError,
    SandboxTimeoutError,
)
from bespokelabs.sandbox.types import (
    ExposedPort,
    FileInfo,
    SandboxConfig,
    SandboxResult,
    SnapshotInfo,
)


@contextmanager
def _operation(op: str) -> Iterator[None]:
    """Translate failures without exposing SDK messages, URLs or tokens."""
    import httpcore
    import httpx
    from novita_sandbox import (
        AuthenticationException,
        InvalidArgumentException,
        NotFoundException,
        TimeoutException,
    )
    from novita_sandbox import (
        SandboxException as NovitaSandboxException,
    )

    try:
        yield
    except SandboxError:
        raise
    except Exception as exc:
        error_type: type[SandboxError]
        status = (
            exc.response.status_code
            if isinstance(exc, httpx.HTTPStatusError)
            else None
        )
        # SDK API/code-interpreter errors sometimes discard the response and
        # retain only an HTTP status prefix. Extract that numeric field only.
        if status is None and isinstance(exc, NovitaSandboxException):
            match = re.match(r"^(\d{3}):", str(exc))
            if match:
                status = int(match[1])
            elif str(exc).startswith("Code.permission_denied:"):
                # envd's RPC mapper wraps HTTP 403 this way in 2.1.3.
                status = 403
        if isinstance(exc, AuthenticationException) or status in (401, 403):
            error_type = SandboxAuthenticationError
        elif isinstance(
            exc,
            (
                TimeoutException,
                TimeoutError,
                httpx.TimeoutException,
                httpcore.TimeoutException,
            ),
        ):
            error_type = SandboxTimeoutError
        elif isinstance(
            exc,
            (
                ConnectionError,
                httpx.TransportError,
                httpcore.NetworkError,
                httpcore.ProtocolError,
                httpcore.ProxyError,
            ),
        ):
            error_type = SandboxConnectionError
        elif isinstance(exc, NotFoundException) or status == 404:
            error_type = SandboxNotFoundError
        elif isinstance(exc, InvalidArgumentException):
            error_type = SandboxConfigurationError
        else:
            error_type = (
                SandboxCreationError
                if op in {"create", "resume"}
                else SandboxExecutionError
            )
        context: dict[str, str | int] = {
            "provider_error_type": type(exc).__name__
        }
        if status is not None:
            context["provider_status"] = status
        # Connect can change lifecycle too; ambiguous mutations are not retried.
        unknown = op in {
            "create",
            "resume",
            "destroy",
            "execute_code",
            "execute_command",
        } and error_type not in {
            SandboxAuthenticationError,
            SandboxConfigurationError,
            SandboxNotFoundError,
        }
        raise error_type(
            f"Novita {op} failed.",
            backend="novita",
            op=op,
            context=context,
            outcome=ErrorOutcome.UNKNOWN if unknown else ErrorOutcome.FAILED,
            retryable=False if unknown else error_type.retryable,
        ) from None


def _credentials(op: str) -> None:
    if not os.environ.get("NOVITA_API_KEY"):
        raise SandboxAuthenticationError(
            "NOVITA_API_KEY environment variable is not set",
            backend="novita",
            op=op,
        )


def _domain(value: object) -> str:
    # State contains a hostname, never a URL (userinfo/query strings can be
    # credentials). Do not echo invalid input into the exception.
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", value
    ):
        raise SandboxConfigurationError("Novita domain must be a hostname")
    return value


class NovitaClient:
    """Create Code Interpreter sessions without connecting at construction."""

    # Shared validation must not let provider options bypass NetworkPolicy.
    network_option_keys = frozenset({"network", "allow_internet_access"})

    def __init__(self) -> None:
        try:
            from novita_sandbox import Novita
        except ImportError:
            raise BackendNotInstalledError(
                "Novita SDK not installed. Run: "
                "pip install 'bespokelabs-sandbox[novita]'"
            ) from None
        self._novita_cls = Novita

    def create(self, config: SandboxConfig) -> NovitaSession:
        _credentials("create")
        kwargs: dict = {"timeout": config.timeout_secs}
        if config.template:
            kwargs["template"] = config.template
        if config.env_vars:
            kwargs["envs"] = config.env_vars
        kwargs.update(config.backend_options)
        with _operation("create"):
            from novita_sandbox.core.connection_config import ConnectionConfig

            _domain(kwargs.get("domain") or ConnectionConfig._domain())
            # Resolve connection options before Novita derives api_url from
            # domain. Overriding domain only on create leaves a stale API URL.
            connection_keys = {
                "domain",
                "debug",
                "api_key",
                "api_url",
                "sandbox_url",
                "access_token",
                "request_timeout",
                "headers",
                "extra_sandbox_headers",
                "proxy",
            }
            connection = {
                key: kwargs[key] for key in connection_keys if key in kwargs
            }
            sandbox = self._novita_cls(**connection).code_interpreter.create(
                **kwargs
            )
        return NovitaSession(sandbox=sandbox)

    def resume(self, data: dict) -> NovitaSession:
        """Reconnect through the SDK, including its native lifecycle effects."""
        _credentials("resume")
        sandbox_id = data.get("sandbox_id")
        if not isinstance(sandbox_id, str) or not re.fullmatch(
            r"[a-zA-Z0-9_-]+", sandbox_id
        ):
            raise SandboxConfigurationError("Invalid Novita sandbox_id")
        connection = (
            {"domain": _domain(data["domain"])} if "domain" in data else {}
        )
        with _operation("resume"):
            # The SDK obtains fresh controller/traffic credentials. Its native
            # connect may extend lifetime or wake a paused sandbox; no adapter
            # preflight, cached handle, timeout restoration or recreation.
            sandbox = self._novita_cls(**connection).code_interpreter.connect(
                sandbox_id
            )
        return NovitaSession(sandbox=sandbox)


class NovitaSession:
    """One Novita sandbox; credentials and traffic tokens stay in memory."""

    def __init__(self, *, sandbox: object) -> None:
        self._sandbox = sandbox
        self._destroyed = False

    @property
    def provider_resource_id(self) -> str:
        return self._sandbox.sandbox_id

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        if language not in {"python", "python3"}:
            raise FeatureNotSupportedError(
                "Novita execute_code supports Python only"
            )
        with _operation("execute_code"):
            execution = self._sandbox.run_code(code)
            stderr = "".join(execution.logs.stderr)
            if execution.error is not None:
                error = execution.error
                detail = error.traceback or f"{error.name}: {error.value}"
                stderr += (
                    "\n" if stderr and not stderr.endswith("\n") else ""
                ) + detail
            return SandboxResult(
                stdout="".join(execution.logs.stdout),
                stderr=stderr,
                exit_code=0 if execution.error is None else 1,
            )

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        from novita_sandbox import CommandExitException

        full_command = command if not args else f"{command} {shlex.join(args)}"
        with _operation("execute_command"):
            try:
                result = self._sandbox.commands.run(full_command)
            except CommandExitException as exc:
                result = exc
            return SandboxResult(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
            )

    def list_files(self, path: str = "/") -> list[FileInfo]:
        from novita_sandbox import FileType

        with _operation("list_files"):
            return [
                FileInfo(
                    path=entry.path,
                    is_dir=entry.type == FileType.DIR,
                    size=entry.size,
                )
                for entry in self._sandbox.files.list(path)
            ]

    def read_file(self, path: str) -> bytes:
        with _operation("read_file"):
            return bytes(self._sandbox.files.read(path, format="bytes"))

    def write_file(self, path: str, content: bytes | str) -> None:
        with _operation("write_file"):
            self._sandbox.files.write(path, content)

    def upload_file(self, local_path: str, remote_path: str) -> None:
        with _operation("upload_file"):
            self.write_file(remote_path, pathlib.Path(local_path).read_bytes())

    def download_file(self, remote_path: str, local_path: str) -> None:
        with _operation("download_file"):
            pathlib.Path(local_path).write_bytes(self.read_file(remote_path))

    def expose_port(self, port: int) -> ExposedPort:
        with _operation("expose_port"):
            token = self._sandbox.traffic_access_token
            scheme = (
                "http" if self._sandbox.connection_config.debug else "https"
            )
            return ExposedPort(
                port=port,
                url=f"{scheme}://{self._sandbox.get_host(port)}",
                headers={"Sandbox-Traffic-Access-Token": token}
                if token
                else {},
            )

    def snapshot(self) -> SnapshotInfo:
        raise FeatureNotSupportedError(
            "Novita snapshots are not supported by this adapter"
        )

    def session_state(self) -> dict:
        config = self._sandbox.connection_config
        domain = _domain(config.domain)
        if (
            config.debug
            or config.api_url != f"https://api.{domain}"
            or config._sandbox_url is not None
        ):
            raise FeatureNotSupportedError(
                "Novita state supports domain-based endpoints only; "
                "debug and custom API/sandbox URLs cannot be serialized"
            )
        return {"sandbox_id": self.provider_resource_id, "domain": domain}

    def destroy(self) -> None:
        if self._destroyed:
            return
        with _operation("destroy"):
            # SDK False specifically means HTTP 404 (already absent), not a
            # failed termination. Exceptions leave the attachment intact.
            result = self._sandbox.kill()
            if not isinstance(result, bool):
                raise SandboxExecutionError(
                    "Novita termination was not confirmed",
                    backend="novita",
                    op="destroy",
                    outcome=ErrorOutcome.UNKNOWN,
                )
        self._destroyed = True
