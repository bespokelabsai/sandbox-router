"""Tensorlake sandbox backend."""

from __future__ import annotations

import base64
import logging
import pathlib
import shlex
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    ErrorOutcome,
    FeatureNotSupportedError,
    SandboxAuthenticationError,
    SandboxConnectionError,
    SandboxCreationError,
    SandboxError,
    SandboxExecutionError,
    SandboxTimeoutError,
)
from bespokelabs.sandbox.types import (
    FileInfo,
    SandboxConfig,
    SandboxListing,
    SandboxResult,
    SandboxSessionState,
    SnapshotInfo,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# HTTP statuses after which the same request may succeed if sent again.
_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
# Sandboxes in these states have ended; a listing leaves them out.
_ENDED_STATES = frozenset({"terminated", "timeout", "failed"})
# Tensorlake sandboxes carry no labels or tags.  The one caller-chosen value a
# listing reports is the sandbox name, exposed as this single label key.
_NAME_LABEL = "name"


def _http_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _is_connection_error(exc: BaseException) -> bool:
    # The SDK's own SandboxConnectionError is matched by name so that telling
    # errors apart never needs the SDK imported.
    return (
        isinstance(exc, ConnectionError)
        or type(exc).__name__ == "SandboxConnectionError"
    )


def _is_not_found(exc: BaseException) -> bool:
    return (
        type(exc).__name__ == "SandboxNotFoundError" or _http_status(exc) == 404
    )


def _is_transient(exc: BaseException) -> bool:
    """Whether *exc* is a transport, rate-limit or quota failure worth a resend."""
    if _is_not_found(exc):
        return False
    if _is_connection_error(exc) or isinstance(exc, TimeoutError):
        return True
    return _http_status(exc) in _TRANSIENT_STATUSES


def _is_rejection(exc: BaseException) -> bool:
    """Whether the API refused the request outright, so nothing was created."""
    status = _http_status(exc)
    return status is not None and 400 <= status < 500 and status != 408


def _tensorlake_error(
    exc: BaseException,
    *,
    op: str,
    outcome: ErrorOutcome = ErrorOutcome.FAILED,
    context: dict[str, Any] | None = None,
    default: type[SandboxError] = SandboxExecutionError,
    detail: str = "",
) -> SandboxError:
    """Translate a Tensorlake SDK error into the public error taxonomy.

    *default* is the type for a failure that is neither an authentication,
    connection nor timeout error.  A creation error (``default`` is
    ``SandboxCreationError``) keeps that type whatever the cause, so callers
    that catch ``SandboxCreationError`` see every failed create.
    """
    status = _http_status(exc)
    ctx: dict[str, Any] = dict(context or {})
    if status is not None:
        ctx["provider_status"] = status
    # Sending the request again can only help when nothing was left behind.
    retryable = _is_transient(exc) and outcome is ErrorOutcome.FAILED
    common: dict[str, Any] = {
        "backend": "tensorlake",
        "op": op,
        "context": ctx,
        "outcome": outcome,
    }
    if status in (401, 403):
        return SandboxAuthenticationError(
            "Tensorlake authentication failed.", **common
        )
    message = f"Tensorlake {op} failed: {exc}{detail}"
    if default is not SandboxCreationError:
        if _is_connection_error(exc):
            return SandboxConnectionError(
                message, retryable=retryable, **common
            )
        if isinstance(exc, TimeoutError):
            return SandboxTimeoutError(message, retryable=retryable, **common)
    return default(message, retryable=retryable, **common)


def _aware_time(value: object) -> datetime | None:
    """Return a provider timestamp as an aware datetime (naive means UTC), or None."""
    if not isinstance(value, datetime):
        return None
    return (
        value
        if value.tzinfo is not None
        else value.replace(tzinfo=timezone.utc)
    )


class TensorlakeClient:
    """Factory for Tensorlake sandboxes.

    The Tensorlake SDK client is built on first create() and reused for
    every session created through this client.

    Calls that are safe to repeat (listing, reattaching by id, deleting) are
    retried on transport, rate-limit and quota errors, at most
    ``retry_attempts`` times in all with a doubling delay.  ``create`` is sent
    once: a create that failed after the request left may still have built a
    sandbox, and sending it again could build a second one.
    """

    # Attempts in all for a repeatable call, and the delay before the second.
    retry_attempts = 4
    retry_backoff_secs = 0.5
    retry_backoff_max_secs = 8.0

    def __init__(self) -> None:
        try:
            from tensorlake.sandbox import (
                SandboxClient as TensorlakeSandboxClient,  # type: ignore[import-untyped]
            )
        except ImportError as exc:
            raise BackendNotInstalledError(
                "Tensorlake SDK not installed. Run: pip install bespokelabs-sandbox[tensorlake]"
            ) from exc
        self._client_cls = TensorlakeSandboxClient
        self._client: object = None
        # create() may run concurrently (e.g. via AsyncSandboxClient);
        # guard the one-time SDK client construction.
        self._connect_lock = threading.Lock()

    # "none" is enforced through the derived allow_internet flag.
    network_modes = frozenset({"public", "none"})
    network_option_keys = frozenset({"allow_internet_access"})

    def _ensure_client(self) -> object:
        if self._client is None:
            with self._connect_lock:
                if self._client is None:
                    self._client = self._client_cls()
        return self._client

    def _retry(self, op: str, call: Callable[[], _T]) -> _T:
        """Run a repeatable *call*, sending it again after a transient failure.

        Only for calls where a repeat cannot add a second resource.  The last
        failure is raised as the SDK raised it, for the caller to translate.
        """
        attempts = max(1, int(self.retry_attempts))
        for attempt in range(attempts):
            try:
                return call()
            except Exception as exc:
                if attempt == attempts - 1 or not _is_transient(exc):
                    raise
                delay = min(
                    self.retry_backoff_max_secs,
                    self.retry_backoff_secs * (2**attempt),
                )
                logger.warning(
                    "Tensorlake %s failed (attempt %d of %d), retrying in %.1fs: %s",
                    op,
                    attempt + 1,
                    attempts,
                    delay,
                    exc,
                )
                time.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _delete_failed_create(self, client: object, sandbox_id: str) -> bool:
        """Delete the sandbox a failed create left behind; True once it is gone."""
        try:
            self._retry("delete", lambda: client.delete(sandbox_id))
        except Exception as exc:
            if _is_not_found(exc):
                return True
            logger.warning(
                "Could not delete Tensorlake sandbox %s left by a failed create; "
                "it may still be running and billing.",
                sandbox_id,
                exc_info=True,
            )
            return False
        return True

    def _create_error(
        self, exc: Exception, client: object, name: object
    ) -> SandboxError:
        """Say what a failed create left behind, and clean up what is known.

        Three cases.  The SDK names the sandbox it created (it never started,
        or was still queued): that sandbox is deleted, since no caller holds a
        handle to it.  The request was refused: nothing exists.  Anything
        else (a dropped connection, a timeout, a 5xx) leaves it unknown
        whether a sandbox was built, and the error says so instead of the
        create being sent again.
        """
        context: dict[str, Any] = {}
        detail = ""
        sandbox_id = getattr(exc, "sandbox_id", None)
        if isinstance(sandbox_id, str) and sandbox_id:
            context["provider_resource_id"] = sandbox_id
            if self._delete_failed_create(client, sandbox_id):
                context["cleanup_status"] = "deleted"
                outcome = ErrorOutcome.FAILED
            else:
                context["cleanup_status"] = "unknown"
                outcome = ErrorOutcome.UNKNOWN
                detail = (
                    f" (sandbox {sandbox_id} could not be deleted and may"
                    " still exist)"
                )
        elif _is_rejection(exc) or isinstance(exc, (TypeError, ValueError)):
            # Refused by the API, or by the SDK before any request was sent.
            outcome = ErrorOutcome.FAILED
        else:
            outcome = ErrorOutcome.UNKNOWN
            hint = ""
            if isinstance(name, str) and name:
                context[_NAME_LABEL] = name
                hint = (
                    "; look for it with"
                    f" list_sandboxes({{'{_NAME_LABEL}': {name!r}}})"
                )
            detail = (
                " (the request may have reached Tensorlake, so a sandbox may"
                f" exist; the create was not sent again{hint})"
            )
        return _tensorlake_error(
            exc,
            op="create",
            outcome=outcome,
            context=context,
            default=SandboxCreationError,
            detail=detail,
        )

    def create(self, config: SandboxConfig) -> TensorlakeSession:
        try:
            client = self._ensure_client()
        except Exception as exc:
            # No request was sent, so there is nothing to clean up.
            raise _tensorlake_error(
                exc, op="create", default=SandboxCreationError
            ) from exc
        kwargs: dict = {
            "cpus": config.cpu,
            "memory_mb": config.memory_mb,
            "timeout_secs": config.timeout_secs,
            "allow_internet_access": config.allow_internet,
        }
        if config.image:
            kwargs["image"] = config.image
        if config.snapshot_id:
            kwargs["snapshot_id"] = config.snapshot_id
        if config.backend_options:
            kwargs.update(config.backend_options)
        try:
            # Sent once: see the class docstring.
            sandbox = client.create_and_connect(**kwargs)
        except Exception as exc:
            raise self._create_error(exc, client, kwargs.get("name")) from exc

        return TensorlakeSession(
            client=client,
            sandbox=sandbox,
            workdir=config.workdir or "/tmp",
        )

    def resume(self, data: dict) -> TensorlakeSession:
        client = self._ensure_client()
        connect = getattr(client, "connect", None)
        if connect is None:
            raise FeatureNotSupportedError(
                "This Tensorlake SDK version has no connect(); cannot resume by id"
            )
        try:
            sandbox_id = data["sandbox_id"]
            # Attaching to an existing sandbox by id is safe to repeat.
            sandbox = self._retry("connect", lambda: connect(sandbox_id))
        except Exception as exc:
            raise SandboxCreationError(
                f"Cannot resume Tensorlake sandbox '{data.get('sandbox_id')}': {exc}"
            ) from exc
        return TensorlakeSession(
            client=client,
            sandbox=sandbox,
            workdir=data.get("workdir", "/tmp"),
        )

    def list_sandboxes(self, labels: dict[str, str]) -> list[SandboxListing]:
        """Every sandbox that has not ended and is named ``labels["name"]``.

        Tensorlake sandboxes have no labels or tags, and the SDK's ``list()``
        takes no filter.  The only caller-chosen value a sandbox carries is
        its name (``backend_options={"name": ...}`` at creation), so ``name``
        is the one label key accepted here; any other key raises
        ``FeatureNotSupportedError`` rather than matching nothing.  The whole
        project listing is fetched and filtered client-side.  Pending, running
        and suspended sandboxes are returned with their ``state``; terminated
        ones are left out.  Any failure raises instead of returning part of
        the list.
        """
        unsupported = sorted(set(labels) - {_NAME_LABEL})
        if unsupported or not labels.get(_NAME_LABEL):
            raise FeatureNotSupportedError(
                "Tensorlake sandboxes carry no labels; they can only be listed"
                f" by name, as {{'{_NAME_LABEL}': ...}}",
                backend="tensorlake",
                op="list",
                context={"unsupported_labels": ", ".join(unsupported)},
            )
        wanted = labels[_NAME_LABEL]
        try:
            client = self._ensure_client()
            lister = getattr(client, "list", None)
        except Exception as exc:
            raise _tensorlake_error(exc, op="list") from exc
        if not callable(lister):
            raise FeatureNotSupportedError(
                "This Tensorlake SDK version has no list(); cannot list sandboxes",
                backend="tensorlake",
                op="list",
            )
        try:
            # list() may return a lazy iterable, so materialize it in the guard.
            found = self._retry("list", lambda: list(lister()))
        except Exception as exc:
            raise _tensorlake_error(exc, op="list") from exc
        listings: list[SandboxListing] = []
        for info in found:
            if getattr(info, "name", None) != wanted:
                continue
            state = getattr(info, "status", None)
            state = getattr(state, "value", state)
            state = None if state is None else str(state)
            if state is not None and state.lower() in _ENDED_STATES:
                continue
            sandbox_id = getattr(info, "sandbox_id", None)
            if not sandbox_id:
                raise SandboxExecutionError(
                    "Tensorlake listed a sandbox without an id.",
                    backend="tensorlake",
                    op="list",
                )
            listings.append(
                SandboxListing(
                    provider_resource_id=str(sandbox_id),
                    labels={_NAME_LABEL: wanted},
                    state=state,
                    created_at=_aware_time(getattr(info, "created_at", None)),
                    session_state=SandboxSessionState(
                        backend="tensorlake",
                        data={"sandbox_id": str(sandbox_id)},
                    ),
                )
            )
        return listings


class TensorlakeSession:
    """One live Tensorlake sandbox."""

    def __init__(
        self, *, client: object, sandbox: object, workdir: str = "/tmp"
    ) -> None:
        self._client = client
        self._sandbox: object = sandbox
        self._workdir = workdir

    @property
    def provider_resource_id(self) -> str | None:
        """Return the Tensorlake sandbox ID while it is attached."""
        value = getattr(self._sandbox, "sandbox_id", None)
        return str(value) if value is not None else None

    @property
    def native(self) -> Any:
        """Return the Tensorlake SDK sandbox object.

        Not portable: see ``Sandbox.native``.
        """
        return self._sandbox

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        try:
            result = self._sandbox.run(language, ["-c", code])
            return SandboxResult(
                stdout=getattr(result, "stdout", "") or "",
                stderr=getattr(result, "stderr", "") or "",
                exit_code=getattr(result, "exit_code", 0) or 0,
            )
        except Exception as exc:
            raise SandboxExecutionError(
                f"Tensorlake code execution failed: {exc}"
            ) from exc

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        try:
            command_line = (
                command
                if not args
                else f"{command} {' '.join(shlex.quote(a) for a in args)}"
            )
            script = (
                'export PATH="$HOME/.npm-global/bin:$PATH"; '
                f"mkdir -p {shlex.quote(self._workdir)} && "
                f"cd {shlex.quote(self._workdir)} && "
                f"{command_line}"
            )
            cmd_args = ["-c", script]
            result = self._sandbox.run("bash", cmd_args)
            return SandboxResult(
                stdout=getattr(result, "stdout", "") or "",
                stderr=getattr(result, "stderr", "") or "",
                exit_code=getattr(result, "exit_code", 0) or 0,
            )
        except Exception as exc:
            raise SandboxExecutionError(
                f"Tensorlake command execution failed: {exc}"
            ) from exc

    def list_files(self, path: str = "/") -> list[FileInfo]:
        """List files by running ls inside the sandbox (no native file API)."""
        try:
            result = self._sandbox.run(
                "bash", ["-c", f"ls -1F {shlex.quote(path)}"]
            )
            files: list[FileInfo] = []
            for raw_line in (result.stdout or "").strip().splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                is_dir = line.endswith("/")
                name = line.rstrip("*/=>@|")
                files.append(
                    FileInfo(path=f"{path.rstrip('/')}/{name}", is_dir=is_dir)
                )
            return files
        except Exception as exc:
            raise SandboxExecutionError(
                f"Tensorlake list_files failed: {exc}"
            ) from exc

    def read_file(self, path: str) -> bytes:
        """Read file by running cat inside the sandbox."""
        try:
            result = self._sandbox.run("cat", [path])
            return (result.stdout or "").encode()
        except Exception as exc:
            raise SandboxExecutionError(
                f"Tensorlake read_file failed: {exc}"
            ) from exc

    def write_file(self, path: str, content: bytes | str) -> None:
        """Write file via base64 pipe inside the sandbox."""
        try:
            data = content if isinstance(content, bytes) else content.encode()
            encoded = base64.b64encode(data).decode()
            self._sandbox.run(
                "bash",
                [
                    "-c",
                    f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}",
                ],
            )
        except Exception as exc:
            raise SandboxExecutionError(
                f"Tensorlake write_file failed: {exc}"
            ) from exc

    def upload_file(self, local_path: str, remote_path: str) -> None:
        """Upload a local file by base64-encoding and writing via shell."""
        try:
            data = pathlib.Path(local_path).read_bytes()
            encoded = base64.b64encode(data).decode()
            self._sandbox.run(
                "bash",
                [
                    "-c",
                    f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(remote_path)}",
                ],
            )
        except Exception as exc:
            raise SandboxExecutionError(
                f"Tensorlake upload_file failed: {exc}"
            ) from exc

    def download_file(self, remote_path: str, local_path: str) -> None:
        """Download a file by base64-encoding its contents from the sandbox."""
        try:
            result = self._sandbox.run(
                "bash", ["-c", f"base64 {shlex.quote(remote_path)}"]
            )
            exit_code = getattr(result, "exit_code", 0) or 0
            if exit_code != 0:
                stderr = getattr(result, "stderr", "") or ""
                raise SandboxExecutionError(
                    f"Tensorlake download_file failed: remote command exited {exit_code}\nstderr={stderr}"
                )
            data = base64.b64decode(result.stdout.strip())
            pathlib.Path(local_path).write_bytes(data)
        except SandboxExecutionError:
            raise
        except Exception as exc:
            raise SandboxExecutionError(
                f"Tensorlake download_file failed: {exc}"
            ) from exc

    def snapshot(self) -> SnapshotInfo:
        try:
            result = self._client.snapshot_and_wait(self._sandbox.sandbox_id)
            return SnapshotInfo(
                snapshot_id=getattr(result, "snapshot_id", str(result)),
                backend="tensorlake",
            )
        except Exception as exc:
            raise SandboxExecutionError(
                f"Tensorlake snapshot failed: {exc}"
            ) from exc

    def session_state(self) -> dict:
        return {
            "sandbox_id": str(self._sandbox.sandbox_id),
            "workdir": self._workdir,
        }

    def destroy(self) -> None:
        try:
            if self._sandbox:
                self._sandbox.close()
        except Exception:
            pass
        self._sandbox = None
