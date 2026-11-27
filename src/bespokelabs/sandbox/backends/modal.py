"""Modal sandbox backend."""

from __future__ import annotations

import importlib
import inspect
import math
import pathlib
import re
import threading
import types
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    ErrorOutcome,
    FeatureNotSupportedError,
    SandboxConfigurationError,
    SandboxCreationError,
    SandboxError,
    SandboxExecutionError,
    SandboxTimeoutError,
)
from bespokelabs.sandbox.types import (
    AppListing,
    AppStopResult,
    ExposedPort,
    FileInfo,
    NetworkPolicy,
    SandboxConfig,
    SandboxListing,
    SandboxResult,
    SandboxSessionState,
    SnapshotInfo,
)

T = TypeVar("T")

# Sandboxes created without ``app_name`` all share this app.
DEFAULT_APP_NAME = "sandbox-sdk"
DEFAULT_APP_TIMEOUT_SECS = 30.0

# ``App.lookup`` also resolves an app *id* on newer SDKs; stop_app takes names only.
_APP_ID_RE = re.compile(r"ap-[a-zA-Z0-9]{22}")


class ModalClient:
    """Factory for Modal sandboxes."""

    supports_dockerfile = True
    network_modes = frozenset({"public", "none", "allowlist"})
    # cidr_allowlist is the deprecated name of outbound_cidr_allowlist.
    network_option_keys = frozenset(
        {
            "block_network",
            "outbound_cidr_allowlist",
            "outbound_domain_allowlist",
            "cidr_allowlist",
        }
    )

    def __init__(self) -> None:
        try:
            import modal  # type: ignore[import-untyped]
        except ImportError as exc:
            raise BackendNotInstalledError(
                "Modal SDK not installed. Run: pip install bespokelabs-sandbox[modal]"
            ) from exc
        self._modal = modal

    def create(self, config: SandboxConfig) -> ModalSession:
        modal = self._modal
        try:
            app_name = config.app_name or DEFAULT_APP_NAME
            app = modal.App.lookup(app_name, create_if_missing=True)

            create_kwargs: dict = {
                "app": app,
                "timeout": config.timeout_secs,
                "cpu": config.cpu,
                "memory": config.memory_mb,
            }

            if config.gpu is not None:
                create_kwargs["gpu"] = config.gpu

            if config.image:
                create_kwargs["image"] = modal.Image.from_registry(config.image)

            if config.dockerfile:
                create_kwargs["image"] = _image_from_dockerfile(modal, config)

            if config.snapshot_id:
                create_kwargs["image"] = modal.Image.from_id(config.snapshot_id)

            if config.env_vars:
                create_kwargs["secrets"] = [
                    modal.Secret.from_dict(config.env_vars)
                ]

            network_kwargs = _network_kwargs(config.network)
            if network_kwargs:
                _require_create_kwargs(modal, network_kwargs)
            create_kwargs.update(network_kwargs)

            if config.backend_options:
                create_kwargs.update(config.backend_options)

            sandbox = modal.Sandbox.create(**create_kwargs)
        except FeatureNotSupportedError:
            raise
        except Exception as exc:
            raise SandboxCreationError(
                f"Failed to create Modal sandbox: {exc}"
            ) from exc

        return ModalSession(sandbox=sandbox)

    def list_sandboxes(self, labels: dict[str, str]) -> list[SandboxListing]:
        """Every running sandbox whose tags include all of *labels*.

        Modal filters on tags server-side and lists running sandboxes only;
        its listing carries no creation time, so ``created_at`` is ``None``.
        Where the SDK can read a sandbox's tags back (``get_tags``), one whose
        own tags do not match is dropped rather than trusted; an older SDK's
        server-side match is reported with ``labels_complete=False``.  Any failure raises instead of
        returning part of the list.
        """
        try:
            found = list(self._modal.Sandbox.list(tags=dict(labels)))
            listings: list[SandboxListing] = []
            for sandbox in found:
                sandbox_id = getattr(sandbox, "object_id", None)
                if not sandbox_id:
                    raise SandboxExecutionError(
                        "Modal listed a sandbox without an id.",
                        backend="modal",
                        op="list",
                    )
                get_tags = getattr(sandbox, "get_tags", None)
                # An SDK without get_tags() (older than tag read-back) leaves only the
                # server-side filter, which matched every requested tag; the listing says
                # its labels are that filter, not the sandbox's whole tag set.
                complete = callable(get_tags)
                tags = get_tags() if complete else labels
                own = {
                    str(key): str(value) for key, value in (tags or {}).items()
                }
                if any(own.get(key) != value for key, value in labels.items()):
                    continue
                listings.append(
                    SandboxListing(
                        provider_resource_id=str(sandbox_id),
                        labels=own,
                        state="running",
                        created_at=None,
                        session_state=SandboxSessionState(
                            backend="modal",
                            data={"sandbox_id": str(sandbox_id)},
                        ),
                        labels_complete=complete,
                    )
                )
        except SandboxExecutionError:
            raise
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal list failed: {exc}", backend="modal", op="list"
            ) from exc
        return listings

    def stop_app(
        self,
        app_name: str,
        *,
        timeout_secs: float = DEFAULT_APP_TIMEOUT_SECS,
        allow_default_app: bool = False,
    ) -> AppStopResult:
        """Stop the app named exactly *app_name*, ending all its sandboxes.

        The app is looked up by name without creating it, so an app that
        does not exist (or is already stopped) is reported as
        ``reason="not_found"``.  There is no prefix match and no listing: one
        call stops at most one app.  The shared default app is refused unless
        *allow_default_app* is set, and an app id is refused because only a
        name can be checked against it.

        Uses ``App.stop()`` (modal>=1.6.3), else
        ``modal.experimental.stop_app`` (modal>=1.2.5); an older SDK raises
        ``FeatureNotSupportedError``.  The whole call is abandoned after
        *timeout_secs* with ``SandboxTimeoutError``; the stop may still land.
        """
        name = _checked_app_name(app_name, allow_default_app)
        timeout = _checked_timeout(timeout_secs, "stop_app")
        modal = self._modal
        lookup = getattr(getattr(modal, "App", None), "lookup", None)
        legacy_stop = _experimental_stop_app(modal)
        if not callable(lookup) or not (
            callable(getattr(modal.App, "stop", None)) or callable(legacy_stop)
        ):
            raise FeatureNotSupportedError(
                "Stopping a Modal app needs modal>=1.2.5 "
                "(App.stop or modal.experimental.stop_app); upgrade modal",
                backend="modal",
                op="stop_app",
            )
        return _call_bounded(
            lambda: _stop_app_by_name(modal, name, legacy_stop),
            timeout_secs=timeout,
            op="stop_app",
            outcome=ErrorOutcome.UNKNOWN,
        )

    def list_apps(
        self, *, timeout_secs: float = DEFAULT_APP_TIMEOUT_SECS
    ) -> list[AppListing]:
        """Every app in the configured Modal environment that is not stopped.

        Uses ``Environment.from_context().apps.list()`` and ``App.info()``
        (modal>=1.6.0; one extra call per app before 1.6.3); an older SDK
        raises ``FeatureNotSupportedError``.  The list covers every app the
        credentials can see.  Any failure raises instead of returning part
        of the list.
        """
        timeout = _checked_timeout(timeout_secs, "list_apps")
        modal = self._modal
        environment = getattr(modal, "Environment", None)
        if not (
            callable(getattr(environment, "from_context", None))
            and hasattr(environment, "apps")
            and callable(getattr(getattr(modal, "App", None), "info", None))
        ):
            raise FeatureNotSupportedError(
                "Listing Modal apps needs modal>=1.6.0 "
                "(Environment.apps.list and App.info); upgrade modal",
                backend="modal",
                op="list_apps",
            )
        return _call_bounded(
            lambda: _list_apps(modal),
            timeout_secs=timeout,
            op="list_apps",
            outcome=ErrorOutcome.FAILED,
        )

    def resume(self, data: dict) -> ModalSession:
        try:
            sandbox = self._modal.Sandbox.from_id(data["sandbox_id"])
        except Exception as exc:
            raise SandboxCreationError(
                f"Cannot resume Modal sandbox '{data.get('sandbox_id')}': {exc}"
            ) from exc
        return ModalSession(sandbox=sandbox)


def _checked_app_name(app_name: object, allow_default_app: bool) -> str:
    """Return *app_name* if it names exactly one app this call may stop."""
    if (
        not isinstance(app_name, str)
        or not app_name
        or app_name.strip() != app_name
    ):
        raise SandboxConfigurationError(
            "stop_app needs the app's exact, non-empty name",
            backend="modal",
            op="stop_app",
        )
    if _APP_ID_RE.fullmatch(app_name):
        raise SandboxConfigurationError(
            "stop_app takes an app name, not an app id",
            backend="modal",
            op="stop_app",
        )
    if app_name == DEFAULT_APP_NAME and not allow_default_app:
        raise SandboxConfigurationError(
            f"'{DEFAULT_APP_NAME}' is the default app shared by every sandbox "
            "created without app_name; pass allow_default_app=True to stop "
            "them all",
            backend="modal",
            op="stop_app",
        )
    return app_name


def _checked_timeout(timeout_secs: object, op: str) -> float:
    if (
        isinstance(timeout_secs, bool)
        or not isinstance(timeout_secs, (int, float))
        or not math.isfinite(timeout_secs)
        or timeout_secs <= 0
    ):
        raise SandboxConfigurationError(
            "timeout_secs must be a finite number greater than 0",
            backend="modal",
            op=op,
        )
    return float(timeout_secs)


def _experimental_stop_app(modal: object) -> Callable[[str], object] | None:
    """``modal.experimental.stop_app``, the stop call before ``App.stop``."""
    experimental = getattr(modal, "experimental", None)
    if experimental is None and isinstance(modal, types.ModuleType):
        # Older SDKs do not import the submodule with the package.
        try:
            experimental = importlib.import_module(
                f"{modal.__name__}.experimental"
            )
        except Exception:
            return None
    return getattr(experimental, "stop_app", None)


def _not_found_errors(modal: object) -> tuple[type[BaseException], ...]:
    error = getattr(getattr(modal, "exception", None), "NotFoundError", None)
    if isinstance(error, type) and issubclass(error, BaseException):
        return (error,)
    return ()


def _stop_app_by_name(
    modal: Any, app_name: str, legacy_stop: Callable[[str], object] | None
) -> AppStopResult:
    """Look *app_name* up without creating it, then stop that one app."""
    not_found = _not_found_errors(modal)
    absent = AppStopResult(app_name=app_name, stopped=False, reason="not_found")
    try:
        app = modal.App.lookup(app_name, create_if_missing=False)
    except not_found:
        return absent
    except Exception as exc:
        raise SandboxExecutionError(
            f"Modal app lookup failed: {exc}", backend="modal", op="stop_app"
        ) from exc
    stop = getattr(app, "stop", None)
    try:
        if callable(stop):
            stop()
        elif legacy_stop is not None:
            legacy_stop(app_name)
        else:
            raise FeatureNotSupportedError(
                "This Modal SDK cannot stop an app; upgrade modal",
                backend="modal",
                op="stop_app",
            )
    except FeatureNotSupportedError:
        raise
    except not_found:
        # Stopped by someone else between the lookup and the stop.
        return absent
    except Exception as exc:
        # The request may have reached Modal before it failed.
        raise SandboxExecutionError(
            f"Modal app stop failed: {exc}",
            backend="modal",
            op="stop_app",
            outcome=ErrorOutcome.UNKNOWN,
        ) from exc
    app_id = getattr(app, "app_id", None)
    return AppStopResult(
        app_name=app_name,
        stopped=True,
        app_id=str(app_id) if app_id else None,
    )


def _list_apps(modal: Any) -> list[AppListing]:
    try:
        listings: list[AppListing] = []
        for app in modal.Environment.from_context().apps.list():
            app_id = getattr(app, "app_id", None)
            if not app_id:
                raise SandboxExecutionError(
                    "Modal listed an app without an id.",
                    backend="modal",
                    op="list_apps",
                )
            lifecycle = app.info().lifecycle
            state = getattr(lifecycle, "state", None)
            state = getattr(state, "value", state)
            name = getattr(app, "name", None) or getattr(
                app, "description", None
            )
            listings.append(
                AppListing(
                    name=str(name) if name else None,
                    app_id=str(app_id),
                    state=str(state) if state else None,
                    created_at=_known_time(
                        getattr(lifecycle, "created_at", None)
                    ),
                )
            )
    except SandboxExecutionError:
        raise
    except Exception as exc:
        raise SandboxExecutionError(
            f"Modal app list failed: {exc}", backend="modal", op="list_apps"
        ) from exc
    return listings


def _known_time(value: object) -> datetime | None:
    """Return a timezone-aware time after the epoch, else ``None``.

    A creation time the server left unset arrives as the epoch itself; that
    is an unknown age, not a very old app.
    """
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value if value.timestamp() > 0 else None


def _call_bounded(
    call: Callable[[], T],
    *,
    timeout_secs: float,
    op: str,
    outcome: ErrorOutcome,
) -> T:
    """Run *call* in a daemon thread and give up after *timeout_secs*.

    The SDK's own calls take no deadline, so the bound is enforced here.  A
    thread still running past it is abandoned; it can only finish *call*.
    """
    result: dict[str, Any] = {}

    def target() -> None:
        try:
            result["value"] = call()
        except BaseException as exc:
            result["error"] = exc

    worker = threading.Thread(target=target, name=f"modal-{op}", daemon=True)
    worker.start()
    worker.join(timeout_secs)
    if worker.is_alive():
        raise SandboxTimeoutError(
            f"Modal {op} did not finish within {timeout_secs:g}s",
            backend="modal",
            op=op,
            outcome=outcome,
        )
    error = result.get("error")
    if isinstance(error, Exception):
        raise error
    if error is not None:
        raise SandboxExecutionError(
            f"Modal {op} was interrupted: {type(error).__name__}",
            backend="modal",
            op=op,
            outcome=outcome,
        )
    return result["value"]


def _image_from_dockerfile(modal: object, config: SandboxConfig) -> object:
    """Build ``Image.from_dockerfile`` with the build context.

    ``context_dir`` arrived in modal 0.74; older SDKs only take a
    ``context_mount``, so fail clearly instead of with a TypeError.
    """
    from_dockerfile = modal.Image.from_dockerfile  # type: ignore[attr-defined]
    try:
        params = inspect.signature(from_dockerfile).parameters
    except (TypeError, ValueError):
        params = None
    if params is not None and not (
        "context_dir" in params
        or any(p.kind is p.VAR_KEYWORD for p in params.values())
    ):
        raise FeatureNotSupportedError(
            "Building from a Dockerfile on Modal needs modal>=0.74 "
            "(Image.from_dockerfile(context_dir=...)); upgrade modal",
            backend="modal",
            op="create",
        )
    return from_dockerfile(config.dockerfile, context_dir=config.context_dir)


def _require_create_kwargs(modal: object, kwargs: dict) -> None:
    """Refuse a network policy this Modal SDK's ``Sandbox.create`` can't take.

    The outbound allowlist arguments are newer than the ``modal>=0.60``
    floor; an older SDK would reject them with a TypeError, so fail as
    unsupported instead.
    """
    try:
        params = inspect.signature(
            modal.Sandbox.create  # type: ignore[attr-defined]
        ).parameters
    except (TypeError, ValueError):
        return
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return
    missing = sorted(k for k in kwargs if k not in params)
    if missing:
        raise FeatureNotSupportedError(
            "This Modal SDK's Sandbox.create does not accept "
            f"{', '.join(missing)}, so it cannot enforce this network "
            "policy; upgrade modal",
            backend="modal",
            op="create",
        )


def _network_kwargs(policy: NetworkPolicy | None) -> dict:
    """Map a policy onto ``Sandbox.create`` keyword arguments."""
    if policy is None or policy.mode == "public":
        return {}
    if policy.mode == "none":
        return {"block_network": True}
    kwargs: dict = {}
    if policy.cidrs:
        kwargs["outbound_cidr_allowlist"] = list(policy.cidrs)
    if policy.domains:
        kwargs["outbound_domain_allowlist"] = list(policy.domains)
    return kwargs


class ModalSession:
    """One live Modal sandbox."""

    def __init__(
        self, *, sandbox: object, owned_client: object | None = None
    ) -> None:
        self._sandbox: object = sandbox
        # Only hosted reconnect's explicit factory client is owned. Native SDK
        # sessions (including implicit/shared clients) retain legacy behavior.
        self._owned_client = owned_client
        # Custody can be consumed, but a hosted handle must never fall back to
        # legacy exception-swallowing semantics after transport disposal.
        self._hosted_strict = owned_client is not None
        self._owned_client_lock = threading.Lock()
        # One immutable local cache-publication claim for this hosted wrapper.
        # Never clear it on transfer/disposal: aliases must not be republished.
        self._attachment_custody: object | None = None

    @property
    def provider_resource_id(self) -> str | None:
        """Return the Modal sandbox object ID while it is attached."""
        value = getattr(self._sandbox, "object_id", None)
        return str(value) if value is not None else None

    @property
    def native(self) -> Any:
        """Return the ``modal.Sandbox`` object.

        Not portable: see ``Sandbox.native``.
        """
        return self._sandbox

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        try:
            process = self._sandbox.exec(language, "-c", code)
            stdout = process.stdout.read()
            stderr = process.stderr.read()
            process.wait()
            return SandboxResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=process.returncode or 0,
            )
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal code execution failed: {exc}"
            ) from exc

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        try:
            cmd_parts = (
                ["bash", "-c", command] if not args else [command] + args
            )
            process = self._sandbox.exec(*cmd_parts)
            stdout = process.stdout.read()
            stderr = process.stderr.read()
            process.wait()
            return SandboxResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=process.returncode or 0,
            )
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal command execution failed: {exc}"
            ) from exc

    def list_files(self, path: str = "/") -> list[FileInfo]:
        try:
            entries = self._sandbox.ls(path)
            return [
                FileInfo(
                    path=f"{path.rstrip('/')}/{e}"
                    if isinstance(e, str)
                    else getattr(e, "path", str(e)),
                    is_dir=getattr(e, "is_dir", False)
                    if not isinstance(e, str)
                    else False,
                )
                for e in entries
            ]
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal list_files failed: {exc}"
            ) from exc

    def read_file(self, path: str) -> bytes:
        try:
            with self._sandbox.open(path, "rb") as f:
                return f.read()
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal read_file failed: {exc}"
            ) from exc

    def write_file(self, path: str, content: bytes | str) -> None:
        try:
            data = content if isinstance(content, bytes) else content.encode()
            with self._sandbox.open(path, "wb") as f:
                f.write(data)
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal write_file failed: {exc}"
            ) from exc

    def upload_file(self, local_path: str, remote_path: str) -> None:
        try:
            data = pathlib.Path(local_path).read_bytes()
            with self._sandbox.open(remote_path, "wb") as f:
                f.write(data)
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal upload_file failed: {exc}"
            ) from exc

    def download_file(self, remote_path: str, local_path: str) -> None:
        try:
            with self._sandbox.open(remote_path, "rb") as f:
                data = f.read()
            pathlib.Path(local_path).write_bytes(data)
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal download_file failed: {exc}"
            ) from exc

    def expose_port(self, port: int) -> ExposedPort:
        """Return the tunnel URL for a port opened at creation."""
        try:
            tunnels = self._sandbox.tunnels()
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal tunnel lookup failed: {exc}"
            ) from exc
        url = _tunnel_url(tunnels, port)
        if url is None:
            raise SandboxConfigurationError(
                f"Modal opens ports only at creation; pass "
                f"backend_options={{'encrypted_ports': [{port}]}}",
                backend="modal",
                op="expose_port",
            )
        return ExposedPort(port=port, url=url)

    def set_network(self, policy: NetworkPolicy) -> None:
        """Replace the outbound policy via Modal's experimental API."""
        setter = getattr(
            self._sandbox, "_experimental_set_outbound_network_policy", None
        )
        if setter is None:
            raise FeatureNotSupportedError(
                "This Modal SDK cannot change the network policy of a "
                "running sandbox",
                backend="modal",
                op="set_network",
            )
        if policy.mode == "none":
            raise FeatureNotSupportedError(
                "Modal blocks all egress only at creation; pass "
                "network=NetworkPolicy(mode='none') to the constructor",
                backend="modal",
                op="set_network",
            )
        kwargs: dict = {}
        if policy.mode == "allowlist":
            kwargs = {
                "outbound_cidr_allowlist": list(policy.cidrs) or None,
                "outbound_domain_allowlist": list(policy.domains) or None,
            }
        try:
            setter(**kwargs)
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal set_network failed: {exc}"
            ) from exc

    def snapshot(self) -> SnapshotInfo:
        try:
            image = self._sandbox.snapshot_filesystem()
            return SnapshotInfo(
                snapshot_id=getattr(image, "object_id", str(image)),
                backend="modal",
            )
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal snapshot failed: {exc}"
            ) from exc

    def session_state(self) -> dict:
        return {"sandbox_id": self._sandbox.object_id}

    def _claim_attachment_custody(self, custody: object) -> bool:
        """Claim one cache publication, not provider ownership/delete rights."""
        with self._owned_client_lock:
            if not self._hosted_strict:
                return True
            if self._owned_client is None:
                return False
            if self._attachment_custody is None:
                self._attachment_custody = custody
            return self._attachment_custody is custody

    def _release_owned_client(
        self, *, custody: object | None = None, unclaimed_only: bool = False
    ) -> None:
        """Release local custody only; never delete a provider resource.

        Rejected reconnects may dispose only unclaimed clients. Published cache
        custody must match its immutable claim, even after cache displacement.
        Direct session/service owners retain the existing no-argument disposal.
        """
        with self._owned_client_lock:
            if custody is not None and self._attachment_custody is not custody:
                return
            if unclaimed_only and self._attachment_custody is not None:
                return
            client, self._owned_client = self._owned_client, None
        if client is not None:
            try:
                client.__exit__(None, None, None)
            except Exception:
                # Transport cleanup cannot change a confirmed provider outcome
                # or expose credentials. Consume ownership even if exit fails.
                pass

    def destroy(self) -> None:
        if self._hosted_strict:
            if self._sandbox is None:
                # A prior confirmed termination stays idempotent after disposal.
                return
            if self._owned_client is not None:
                try:
                    self._sandbox.terminate()
                except Exception:
                    # Keep owned transport for an explicit retry until disposed.
                    # Raise outside the handler so SDK text is not chained.
                    pass
                else:
                    self._sandbox = None
                    self._release_owned_client()
                    return
            # A released hosted client is not a native/shared session. Refuse
            # without an RPC, rather than silently claiming cloud termination.
            raise SandboxError(
                "Modal sandbox termination could not be confirmed.",
                backend="modal",
                op="destroy",
                outcome=ErrorOutcome.UNKNOWN,
            )
        # Preserve native/default SDK cleanup semantics for unowned clients.
        try:
            if self._sandbox:
                self._sandbox.terminate()
        except Exception:
            pass
        self._sandbox = None


def _tunnel_url(tunnels: object, port: int) -> str | None:
    """Find *port*'s URL in a ``Sandbox.tunnels()`` result.

    modal>=0.64.153 returns ``{port: Tunnel}``; earlier releases return a
    list of ``TunnelData`` protos, whose URL we build the way ``Tunnel.url``
    does.
    """
    if isinstance(tunnels, dict):
        tunnel = tunnels.get(port)
        return None if tunnel is None else tunnel.url
    for data in tunnels:  # type: ignore[attr-defined]
        if data.container_port == port:
            url = f"https://{data.host}"
            return url if data.port == 443 else f"{url}:{data.port}"
    return None
