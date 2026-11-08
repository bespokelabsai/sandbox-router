"""Local Monty Python interpreter with an in-memory filesystem."""

from __future__ import annotations

import math
import posixpath
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxConfigurationError,
    SandboxCreationError,
    SandboxError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.types import (
    FileInfo,
    SandboxConfig,
    SandboxResult,
    SnapshotInfo,
)


def _unsupported(feature: str, op: str) -> FeatureNotSupportedError:
    return FeatureNotSupportedError(
        f"Monty does not support {feature}", backend="monty", op=op
    )


def _positive(name: str, value: object, *, integer: bool = False) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        or (integer and not isinstance(value, int))
    ):
        raise SandboxConfigurationError(
            f"{name} must be a positive {'integer' if integer else 'number'}",
            backend="monty",
            op="create",
        )


class MontyClient:
    """Lazy SDK import; each session owns and closes its single-worker pool."""

    supports_commands = False
    supports_cpu_limits = False
    network_modes = {"none"}

    def __init__(self) -> None:
        try:
            import pydantic_monty
        except ImportError as exc:
            raise BackendNotInstalledError(
                "Monty is not installed. Run: pip install "
                "'bespokelabs-sandbox[monty]'",
                backend="monty",
            ) from exc
        self._sdk = pydantic_monty

    def create(self, config: SandboxConfig) -> MontySession:
        for name in (
            "image",
            "dockerfile",
            "context_dir",
            "template",
            "snapshot_id",
            "gpu",
            "disk_mb",
            "app_name",
        ):
            if getattr(config, name) is not None:
                raise _unsupported(f"{name}=", "create")
        if config.network is not None and config.network.mode != "none":
            raise _unsupported("network access", "create")
        _positive("memory_mb", config.memory_mb, integer=True)
        _positive("timeout_secs", config.timeout_secs)
        options = dict(config.backend_options)
        allowed = {
            "binary_path",
            "max_output_bytes",
            "max_file_bytes",
            "max_recursion_depth",
            "max_suspensions",
        }
        if unknown := options.keys() - allowed:
            raise SandboxConfigurationError(
                f"Unknown Monty backend_options: {', '.join(sorted(unknown))}",
                backend="monty",
                op="create",
            )
        for name, default in (
            ("max_output_bytes", 10 * 1024 * 1024),
            ("max_file_bytes", 50_000_000),
            ("max_recursion_depth", 1000),
            ("max_suspensions", 1000),
        ):
            options.setdefault(name, default)
            _positive(name, options[name], integer=True)
        workdir = config.workdir or "/"
        if not workdir.startswith("/") or "\x00" in workdir:
            raise SandboxConfigurationError(
                "Monty workdir must be an absolute virtual POSIX path",
                backend="monty",
                op="create",
            )
        return MontySession(self._sdk, config, options, workdir)

    def resume(self, data: dict) -> MontySession:
        raise _unsupported("session reattachment", "resume")


class MontySession:
    """Stateful Python execution, serialized across SDK worker threads.

    No host callbacks, mounts, packages or ambient environment are exposed.
    The pool belongs to this session, including for SandboxClient.create().
    """

    supports_directory_transfer = False

    def __init__(self, sdk, config: SandboxConfig, options: dict, workdir: str):
        self._sdk = sdk
        self._lock = threading.RLock()
        self._stack = ExitStack()
        self._closed = False
        self._workdir = posixpath.normpath("/" + workdir.lstrip("/"))
        self._max_output = options["max_output_bytes"]
        try:
            self._fs = sdk.OSAccess(
                environ=dict(config.env_vars),
                max_total_file_bytes=options["max_file_bytes"],
            )
            self._fs.path_mkdir(PurePosixPath(self._workdir), True, True)
            pool = self._stack.enter_context(
                sdk.Monty(
                    binary_path=options.get("binary_path"),
                    min_processes=1,
                    max_processes=1,
                    request_timeout=config.timeout_secs,
                )
            )
            self._session = self._stack.enter_context(
                pool.checkout(
                    limits={
                        "max_memory": config.memory_mb * 1024 * 1024,
                        "max_feed_duration_secs": config.timeout_secs,
                        "max_total_sleep_secs": config.timeout_secs,
                        "max_recursion_depth": options["max_recursion_depth"],
                        "max_suspensions": options["max_suspensions"],
                    }
                )
            )
        except Exception as exc:
            self._stack.close()
            raise SandboxCreationError(
                f"Failed to create Monty sandbox: {exc}",
                backend="monty",
                op="create",
            ) from exc

    @property
    def provider_resource_id(self) -> None:
        """Monty workers have no external provider resource."""
        return None

    def _check_alive(self) -> None:
        if self._closed:
            raise SandboxExecutionError(
                "Monty session is closed; create a new sandbox. "
                "Resource-limit failures invalidate the session.",
                backend="monty",
                op="exec",
            )

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        with self._lock:
            self._check_alive()
            if language not in {"python", "python3"}:
                raise _unsupported(
                    f"language {language!r}; use 'python'", "exec"
                )
            collector = self._sdk.CollectStreams(max_bytes=self._max_output)
            error = ""
            exit_code = 0
            try:
                # Ignore the trailing expression value: the public contract
                # returns printed stdout, also used by return_type=.
                self._session.feed_run(
                    code,
                    os=self._fs,
                    cwd=self._workdir,
                    print_callback=collector,
                )
            except (
                self._sdk.MontySyntaxError,
                self._sdk.MontyTypingError,
            ) as exc:
                error, exit_code = exc.display(), 1
            except self._sdk.MontyRuntimeError as exc:
                inner = exc.exception()
                error = exc.display()
                exit_code = 124 if isinstance(inner, TimeoutError) else 1
                # Heap and output-cap errors are indistinguishable. Discard
                # conservatively, including explicit guest MemoryError.
                if isinstance(inner, (TimeoutError, MemoryError)):
                    self.destroy()
            except self._sdk.MontyCrashedError as exc:
                error = str(exc)
                exit_code = 124 if exc.timed_out else 1
                self.destroy()
            except Exception as exc:
                self.destroy()
                raise SandboxExecutionError(
                    f"Monty execution failed: {exc}",
                    backend="monty",
                    op="exec",
                ) from exc
            stdout = "".join(
                text for stream, text in collector.output if stream == "stdout"
            )
            stderr = "".join(
                text for stream, text in collector.output if stream == "stderr"
            )
            if error:
                stderr += (
                    "\n" if stderr and not stderr.endswith("\n") else ""
                ) + error
            return SandboxResult(
                stdout=stdout, stderr=stderr, exit_code=exit_code
            )

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        raise _unsupported(
            "shell commands, package installation or CLI agents", "exec"
        )

    def _path(self, path: str) -> PurePosixPath:
        if not path or "\x00" in path:
            raise ValueError(
                "Expected a non-empty virtual path without NUL bytes"
            )
        return PurePosixPath(
            posixpath.normpath(
                "/" + posixpath.join(self._workdir, path).lstrip("/")
            )
        )

    @contextmanager
    def _files(self, op: str):
        with self._lock:
            self._check_alive()
            try:
                yield
            except SandboxError:
                raise
            except Exception as exc:
                raise SandboxExecutionError(
                    f"Monty {op} failed: {exc}",
                    backend="monty",
                    op=op,
                ) from exc

    def list_files(self, path: str = "/") -> list[FileInfo]:
        with self._files("list_files"):
            return [
                FileInfo(
                    path=str(entry),
                    is_dir=self._fs.path_is_dir(entry),
                    size=None
                    if self._fs.path_is_dir(entry)
                    else self._fs.path_stat(entry).st_size,
                )
                for entry in sorted(self._fs.path_iterdir(self._path(path)))
            ]

    def read_file(self, path: str) -> bytes:
        with self._files("read_file"):
            return self._fs.path_read_bytes(self._path(path))

    def write_file(self, path: str, content: bytes | str) -> None:
        with self._files("write_file"):
            dest = self._path(path)
            self._fs.path_mkdir(dest.parent, True, True)
            self._fs.path_write_bytes(
                dest, content.encode() if isinstance(content, str) else content
            )

    def upload_file(self, local_path: str, remote_path: str) -> None:
        with self._files("upload_file"):
            self.write_file(remote_path, Path(local_path).read_bytes())

    def download_file(self, remote_path: str, local_path: str) -> None:
        with self._files("download_file"):
            Path(local_path).write_bytes(self.read_file(remote_path))

    def snapshot(self) -> SnapshotInfo:
        raise _unsupported("snapshots", "snapshot")

    def session_state(self) -> dict:
        raise _unsupported("session reattachment", "session_state")

    def destroy(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._stack.close()
                finally:
                    self._fs = None
