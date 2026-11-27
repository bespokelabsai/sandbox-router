from __future__ import annotations

import threading
import types
import unittest
from datetime import datetime, timezone
from unittest import mock

from bespokelabs.sandbox import Sandbox, SandboxClient
from bespokelabs.sandbox.backends.modal import ModalClient, ModalSession
from bespokelabs.sandbox.exceptions import (
    ErrorOutcome,
    FeatureNotSupportedError,
    SandboxConfigurationError,
    SandboxExecutionError,
    SandboxTimeoutError,
)
from bespokelabs.sandbox.types import SandboxConfig


class ModalSessionLegacyCleanupTests(unittest.TestCase):

    def test_default_unowned_session_keeps_legacy_destroy_behavior(self):
        for failure in (None, RuntimeError("legacy termination failure")):
            with self.subTest(failure=failure):
                shared_client = mock.MagicMock()
                remote = mock.Mock(client=shared_client)
                remote.terminate.side_effect = failure
                session = ModalSession(sandbox=remote)
                session.destroy()
                session.destroy()
                remote.terminate.assert_called_once_with()
                shared_client.__exit__.assert_not_called()
                self.assertIsNone(session._sandbox)
                self.assertIsNone(session._owned_client)


class ModalClientGpuTests(unittest.TestCase):

    def _client(self) -> tuple[ModalClient, mock.MagicMock]:
        modal = mock.MagicMock()
        modal.App.lookup.return_value = mock.sentinel.app
        modal.Sandbox.create.return_value = mock.MagicMock(object_id="sb-modal")

        client = object.__new__(ModalClient)
        client._modal = modal
        return client, modal

    def test_create_forwards_gpu_reservation(self) -> None:
        client, modal = self._client()

        client.create(SandboxConfig(backend="modal", gpu="H100:2"))

        modal.Sandbox.create.assert_called_once_with(
            app=mock.sentinel.app,
            timeout=600,
            cpu=1.0,
            memory=1024,
            gpu="H100:2",
        )

    def test_create_omits_gpu_when_not_requested(self) -> None:
        client, modal = self._client()

        client.create(SandboxConfig(backend="modal"))

        _, kwargs = modal.Sandbox.create.call_args
        self.assertNotIn("gpu", kwargs)

    def test_backend_options_can_override_gpu(self) -> None:
        client, modal = self._client()

        client.create(
            SandboxConfig(
                backend="modal",
                gpu="L4",
                backend_options={"gpu": "A100-80GB"},
            )
        )

        _, kwargs = modal.Sandbox.create.call_args
        self.assertEqual(kwargs["gpu"], "A100-80GB")


class ModalGpuPublicApiTests(unittest.TestCase):

    def test_sandbox_threads_gpu_into_config(self) -> None:
        backend = mock.MagicMock()
        backend.create.return_value = mock.MagicMock()

        with mock.patch.dict(
            "bespokelabs.sandbox.backends.BACKENDS",
            {"modal": mock.Mock(return_value=backend)},
        ):
            with Sandbox("modal", gpu="A100"):
                pass

        config = backend.create.call_args.args[0]
        self.assertEqual(config.gpu, "A100")

    def test_sandbox_client_threads_gpu_into_config(self) -> None:
        backend = mock.MagicMock()
        backend.create.return_value = mock.MagicMock()

        with mock.patch.dict(
            "bespokelabs.sandbox.backends.BACKENDS",
            {"modal": mock.Mock(return_value=backend)},
        ):
            client = SandboxClient("modal")
            with client.create(gpu="L4"):
                pass

        config = backend.create.call_args.args[0]
        self.assertEqual(config.gpu, "L4")


class ModalListSandboxesTests(unittest.TestCase):
    """list_sandboxes finds running sandboxes by tag and never guesses."""

    def _client(
        self, *sandboxes: mock.MagicMock
    ) -> tuple[ModalClient, mock.MagicMock]:
        modal = mock.MagicMock()
        modal.Sandbox.list.return_value = iter(sandboxes)
        client = object.__new__(ModalClient)
        client._modal = modal
        return client, modal

    @staticmethod
    def _sandbox(object_id: str, tags: dict[str, str]) -> mock.MagicMock:
        sandbox = mock.MagicMock(object_id=object_id)
        sandbox.get_tags.return_value = tags
        return sandbox

    def test_lists_running_sandboxes_by_tag(self) -> None:
        client, modal = self._client(
            self._sandbox("sb-1", {"team": "x", "run": "1"})
        )

        (only,) = client.list_sandboxes({"team": "x"})

        modal.Sandbox.list.assert_called_once_with(tags={"team": "x"})
        self.assertEqual(only.provider_resource_id, "sb-1")
        self.assertEqual(only.labels, {"team": "x", "run": "1"})
        self.assertTrue(only.labels_complete)
        self.assertIsNone(only.created_at)
        self.assertEqual(only.session_state.backend, "modal")
        self.assertEqual(only.session_state.data, {"sandbox_id": "sb-1"})

    def test_a_sandbox_whose_own_tags_do_not_match_is_dropped(self) -> None:
        client, _ = self._client(self._sandbox("sb-2", {"team": "y"}))

        self.assertEqual(client.list_sandboxes({"team": "x"}), [])

    def test_an_sdk_without_tag_read_back_keeps_the_server_side_match(
        self,
    ) -> None:
        old = mock.MagicMock(spec=["object_id"], object_id="sb-old")
        client, _ = self._client(old)

        (only,) = client.list_sandboxes({"team": "x"})

        self.assertEqual(only.provider_resource_id, "sb-old")
        self.assertEqual(only.labels, {"team": "x"})
        self.assertFalse(only.labels_complete)

    def test_a_failed_tag_read_raises_instead_of_returning_part_of_the_list(
        self,
    ) -> None:
        broken = self._sandbox("sb-3", {})
        broken.get_tags.side_effect = RuntimeError("gone")
        client, _ = self._client(self._sandbox("sb-1", {"team": "x"}), broken)

        with self.assertRaises(SandboxExecutionError):
            client.list_sandboxes({"team": "x"})


class SandboxClientListSandboxesTests(unittest.TestCase):

    def test_an_unfiltered_listing_is_refused(self) -> None:
        client = object.__new__(SandboxClient)
        client._backend_name = "modal"
        client._backend_client = mock.MagicMock()
        for labels in ({}, {"team": ""}):
            with self.assertRaises(SandboxConfigurationError):
                client.list_sandboxes(labels)
        client._backend_client.list_sandboxes.assert_not_called()

    def test_a_backend_that_cannot_list_says_so(self) -> None:
        client = SandboxClient("local")

        with self.assertRaises(FeatureNotSupportedError):
            client.list_sandboxes({"team": "x"})


class _NotFound(Exception):
    pass


class _FakeApp:
    """A remote app handle; ``stop`` exists only on SDKs that have it."""

    def __init__(self, sdk, name, app_id, *, state="deployed", created_at=None):
        self.name = name
        self.app_id = app_id
        self._sdk = sdk
        self._state = state
        self._created_at = created_at

    def info(self):
        self._sdk.calls.append(("info", self.app_id))
        return types.SimpleNamespace(
            lifecycle=types.SimpleNamespace(
                state=types.SimpleNamespace(value=self._state),
                created_at=self._created_at,
            )
        )


class _StoppableApp(_FakeApp):

    def stop(self):
        self._sdk.calls.append(("stop", self.app_id))
        if self._sdk.stop_error is not None:
            raise self._sdk.stop_error
        self._sdk.apps.pop(self.name, None)


class _FakeModal:
    """The slice of the Modal SDK the app calls use, with no network."""

    def __init__(self, *, app_stop=True, experimental_stop=False, listing=True):
        sdk = self
        self.calls: list[tuple] = []
        self.apps: dict[str, _FakeApp] = {}
        self.stop_error: Exception | None = None
        self.lookup_error: Exception | None = None
        self.list_error: Exception | None = None
        self.exception = types.SimpleNamespace(NotFoundError=_NotFound)
        self._app_type = _StoppableApp if app_stop else _FakeApp

        class App(self._app_type):

            @staticmethod
            def lookup(name, create_if_missing=False):
                sdk.calls.append(("lookup", name, create_if_missing))
                if sdk.lookup_error is not None:
                    raise sdk.lookup_error
                if name not in sdk.apps:
                    raise _NotFound(name)
                return sdk.apps[name]

        self.App = App
        if experimental_stop:

            def stop_app(name):
                sdk.calls.append(("experimental_stop", name))
                sdk.apps.pop(name, None)

            self.experimental = types.SimpleNamespace(stop_app=stop_app)
        if listing:

            class _Apps:

                @staticmethod
                def list():
                    sdk.calls.append(("list",))
                    if sdk.list_error is not None:
                        raise sdk.list_error
                    return list(sdk.apps.values())

            class Environment:
                apps = _Apps()

                @staticmethod
                def from_context():
                    return Environment()

            self.Environment = Environment

    def add(self, name, app_id, **kwargs):
        self.apps[name] = self._app_type(self, name, app_id, **kwargs)
        return self.apps[name]

    def client(self) -> ModalClient:
        client = object.__new__(ModalClient)
        client._modal = self
        return client


class ModalStopAppTests(unittest.TestCase):
    """stop_app stops exactly one app, by exact name, and never creates one."""

    def test_stops_the_named_app_and_nothing_else(self) -> None:
        sdk = _FakeModal()
        sdk.add("run-42", "ap-1")
        sdk.add("run-421", "ap-2")

        result = sdk.client().stop_app("run-42")

        self.assertTrue(result.stopped)
        self.assertFalse(result.not_found)
        self.assertEqual(
            (result.app_name, result.app_id, result.reason),
            ("run-42", "ap-1", None),
        )
        self.assertEqual(
            sdk.calls, [("lookup", "run-42", False), ("stop", "ap-1")]
        )
        self.assertEqual(list(sdk.apps), ["run-421"])

    def test_a_missing_app_is_not_found_and_is_not_created(self) -> None:
        sdk = _FakeModal()
        sdk.add("run-421", "ap-2")

        result = sdk.client().stop_app("run-42")

        self.assertFalse(result.stopped)
        self.assertTrue(result.not_found)
        self.assertEqual(result.reason, "not_found")
        self.assertIsNone(result.app_id)
        self.assertEqual(sdk.calls, [("lookup", "run-42", False)])
        self.assertEqual(list(sdk.apps), ["run-421"])

    def test_an_app_that_vanishes_before_the_stop_is_not_found(self) -> None:
        sdk = _FakeModal()
        sdk.add("run-42", "ap-1")
        sdk.stop_error = _NotFound("gone")

        self.assertTrue(sdk.client().stop_app("run-42").not_found)

    def test_the_shared_default_app_needs_an_explicit_opt_in(self) -> None:
        sdk = _FakeModal()
        sdk.add("sandbox-sdk", "ap-shared")

        with self.assertRaises(SandboxConfigurationError):
            sdk.client().stop_app("sandbox-sdk")
        self.assertEqual(sdk.calls, [])

        result = sdk.client().stop_app("sandbox-sdk", allow_default_app=True)
        self.assertTrue(result.stopped)

    def test_only_an_exact_name_is_accepted(self) -> None:
        sdk = _FakeModal()
        for bad in ("", " run-42", "run-42 ", None, "ap-" + "a" * 22):
            with self.subTest(name=bad):
                with self.assertRaises(SandboxConfigurationError):
                    sdk.client().stop_app(bad)
        for bad in (0, -1, float("inf"), float("nan"), True, "5"):
            with self.subTest(timeout=bad):
                with self.assertRaises(SandboxConfigurationError):
                    sdk.client().stop_app("run-42", timeout_secs=bad)
        self.assertEqual(sdk.calls, [])

    def test_a_failed_lookup_is_a_proven_failure(self) -> None:
        sdk = _FakeModal()
        sdk.lookup_error = RuntimeError("unavailable")

        with self.assertRaises(SandboxExecutionError) as caught:
            sdk.client().stop_app("run-42")

        self.assertEqual(caught.exception.outcome, ErrorOutcome.FAILED)
        self.assertEqual(caught.exception.op, "stop_app")

    def test_a_failed_stop_has_an_unknown_outcome(self) -> None:
        sdk = _FakeModal()
        sdk.add("run-42", "ap-1")
        sdk.stop_error = RuntimeError("connection reset")

        with self.assertRaises(SandboxExecutionError) as caught:
            sdk.client().stop_app("run-42")

        self.assertEqual(caught.exception.outcome, ErrorOutcome.UNKNOWN)

    def test_a_stuck_sdk_call_times_out(self) -> None:
        sdk = _FakeModal()
        release = threading.Event()
        self.addCleanup(release.set)

        def stuck(name, create_if_missing=False):
            release.wait(30)
            raise _NotFound(name)

        sdk.App.lookup = staticmethod(stuck)

        with self.assertRaises(SandboxTimeoutError) as caught:
            sdk.client().stop_app("run-42", timeout_secs=0.05)

        self.assertEqual(caught.exception.outcome, ErrorOutcome.UNKNOWN)
        self.assertTrue(caught.exception.retryable)

    def test_an_sdk_before_app_stop_uses_the_experimental_call(self) -> None:
        sdk = _FakeModal(app_stop=False, experimental_stop=True)
        sdk.add("run-42", "ap-1")

        result = sdk.client().stop_app("run-42")

        self.assertTrue(result.stopped)
        self.assertEqual(result.app_id, "ap-1")
        self.assertEqual(
            sdk.calls,
            [("lookup", "run-42", False), ("experimental_stop", "run-42")],
        )

    def test_an_sdk_that_cannot_stop_apps_says_so(self) -> None:
        sdk = _FakeModal(app_stop=False)
        sdk.add("run-42", "ap-1")
        no_app = types.SimpleNamespace()
        for modal in (sdk, no_app):
            client = object.__new__(ModalClient)
            client._modal = modal
            with self.assertRaises(FeatureNotSupportedError):
                client.stop_app("run-42")
        self.assertEqual(sdk.calls, [])


class ModalListAppsTests(unittest.TestCase):

    def test_lists_name_id_state_and_creation_time(self) -> None:
        created = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        sdk = _FakeModal()
        sdk.add("run-42", "ap-1", created_at=created)
        sdk.add("run-43", "ap-2", state="ephemeral", created_at=created)

        first, second = sdk.client().list_apps()

        self.assertEqual(
            (first.name, first.app_id, first.state, first.created_at),
            ("run-42", "ap-1", "deployed", created),
        )
        self.assertEqual(second.state, "ephemeral")

    def test_a_stopped_app_is_left_out(self) -> None:
        sdk = _FakeModal()
        sdk.add("gone", "ap-1", state="stopped")
        sdk.add("live", "ap-2")

        self.assertEqual(
            [app.name for app in sdk.client().list_apps()], ["live"]
        )

    def test_an_unset_or_naive_creation_time_is_unknown_not_old(self) -> None:
        sdk = _FakeModal()
        sdk.add("a", "ap-1", created_at=datetime.fromtimestamp(0, timezone.utc))
        sdk.add("b", "ap-2", created_at=datetime(2026, 1, 2))
        sdk.add("c", "ap-3", created_at=None)

        for listing in sdk.client().list_apps():
            self.assertIsNone(listing.created_at)

    def test_an_unnamed_app_falls_back_to_its_description(self) -> None:
        sdk = _FakeModal()
        sdk.add("x", "ap-1").name = None
        described = sdk.add("y", "ap-2")
        described.name = None
        described.description = "run-9"

        first, second = sdk.client().list_apps()

        self.assertIsNone(first.name)
        self.assertEqual(second.name, "run-9")

    def test_a_failure_raises_instead_of_returning_part_of_the_list(
        self,
    ) -> None:
        sdk = _FakeModal()
        sdk.add("run-42", "ap-1")
        sdk.add("run-43", "")
        with self.assertRaises(SandboxExecutionError):
            sdk.client().list_apps()

        sdk.list_error = RuntimeError("unavailable")
        with self.assertRaises(SandboxExecutionError) as caught:
            sdk.client().list_apps()
        self.assertEqual(caught.exception.op, "list_apps")

    def test_an_sdk_that_cannot_list_apps_says_so(self) -> None:
        sdk = _FakeModal(listing=False)

        with self.assertRaises(FeatureNotSupportedError):
            sdk.client().list_apps()
        with self.assertRaises(SandboxConfigurationError):
            sdk.client().list_apps(timeout_secs=0)


class SandboxClientAppTests(unittest.TestCase):

    def test_the_client_forwards_to_the_modal_backend(self) -> None:
        sdk = _FakeModal()
        sdk.add("run-42", "ap-1")
        client = object.__new__(SandboxClient)
        client._backend_name = "modal"
        client._backend_client = sdk.client()

        self.assertEqual([a.name for a in client.list_apps()], ["run-42"])
        self.assertTrue(client.stop_app("run-42", timeout_secs=5).stopped)
        with self.assertRaises(SandboxConfigurationError):
            client.stop_app("sandbox-sdk")

    def test_a_backend_without_apps_says_so(self) -> None:
        client = SandboxClient("local")

        with self.assertRaises(FeatureNotSupportedError):
            client.stop_app("run-42")
        with self.assertRaises(FeatureNotSupportedError):
            client.list_apps()


if __name__ == "__main__":
    unittest.main()
