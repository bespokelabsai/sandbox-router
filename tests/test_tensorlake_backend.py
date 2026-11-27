from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from bespokelabs.sandbox.backends.tensorlake import (
    TensorlakeClient,
    TensorlakeSession,
)
from bespokelabs.sandbox.exceptions import (
    ErrorOutcome,
    FeatureNotSupportedError,
    SandboxAuthenticationError,
    SandboxConnectionError,
    SandboxCreationError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.types import SandboxConfig


class _FakeTensorlakeSandbox:
    sandbox_id = "sbx-test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def run(self, command: str, args: list[str]) -> object:
        self.calls.append((command, args))
        return SimpleNamespace(stdout="ok", stderr="", exit_code=0)

    def close(self) -> None:
        pass


class TensorlakeSessionTests(unittest.TestCase):

    def test_execute_command_runs_from_default_writable_workdir_with_user_npm_path(
        self,
    ) -> None:
        sandbox = _FakeTensorlakeSandbox()
        session = TensorlakeSession(client=object(), sandbox=sandbox)

        result = session.execute_command(
            "git",
            [
                "clone",
                "--depth",
                "1",
                "https://github.com/acme/project.git",
                "project",
            ],
        )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(sandbox.calls[0][0], "bash")
        self.assertEqual(
            sandbox.calls[0][1],
            [
                "-c",
                'export PATH="$HOME/.npm-global/bin:$PATH"; '
                "mkdir -p /tmp && cd /tmp && "
                "git clone --depth 1 https://github.com/acme/project.git project",
            ],
        )

    def test_execute_command_quotes_custom_workdir(self) -> None:
        sandbox = _FakeTensorlakeSandbox()
        session = TensorlakeSession(
            client=object(),
            sandbox=sandbox,
            workdir="/tmp/sandbox work",
        )

        session.execute_command("pwd")

        self.assertEqual(
            sandbox.calls[0][1],
            [
                "-c",
                'export PATH="$HOME/.npm-global/bin:$PATH"; '
                "mkdir -p '/tmp/sandbox work' && cd '/tmp/sandbox work' && pwd",
            ],
        )

    def test_session_state_preserves_workdir(self) -> None:
        session = TensorlakeSession(
            client=object(),
            sandbox=_FakeTensorlakeSandbox(),
            workdir="/tmp/project",
        )

        self.assertEqual(
            session.session_state(),
            {"sandbox_id": "sbx-test", "workdir": "/tmp/project"},
        )


# Stands in for the SDK's connection error, which the backend matches by name.
_SdkConnectionError = type("SandboxConnectionError", (Exception,), {})


class _ApiError(Exception):
    """Stands in for the SDK's RemoteAPIError."""

    def __init__(self, status_code: int, sandbox_id: str | None = None) -> None:
        super().__init__(f"API error (status {status_code})")
        self.status_code = status_code
        self.sandbox_id = sandbox_id


class _StartupError(Exception):
    """Stands in for an SDK error about a sandbox that was already created."""

    def __init__(self, sandbox_id: str) -> None:
        super().__init__(f"Sandbox {sandbox_id} is still pending")
        self.sandbox_id = sandbox_id


def _client(sdk: object) -> TensorlakeClient:
    """Wrap a faked SDK client in a backend client that never waits."""
    client = object.__new__(TensorlakeClient)
    client._client = sdk
    client.retry_backoff_secs = 0.0
    return client


def _info(
    sandbox_id: str,
    name: str | None,
    status: str = "running",
    created_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        sandbox_id=sandbox_id,
        name=name,
        status=SimpleNamespace(value=status),
        created_at=created_at,
    )


class TensorlakeListSandboxesTests(unittest.TestCase):
    """list_sandboxes finds sandboxes by name, the only label Tensorlake has."""

    def test_lists_live_sandboxes_with_the_name(self) -> None:
        created = datetime(2026, 1, 2, 3, 4, 5)
        sdk = mock.MagicMock()
        sdk.list.return_value = iter(
            [
                _info("sbx-1", "job-a", created_at=created),
                _info("sbx-2", "job-b"),
                _info("sbx-3", None),
                _info("sbx-4", "job-a", status="suspended"),
                _info("sbx-5", "job-a", status="terminated"),
            ]
        )

        first, second = _client(sdk).list_sandboxes({"name": "job-a"})

        sdk.list.assert_called_once_with()
        self.assertEqual(first.provider_resource_id, "sbx-1")
        self.assertEqual(first.labels, {"name": "job-a"})
        self.assertTrue(first.labels_complete)
        self.assertEqual(first.state, "running")
        self.assertEqual(first.created_at, created.replace(tzinfo=timezone.utc))
        self.assertEqual(first.session_state.backend, "tensorlake")
        self.assertEqual(first.session_state.data, {"sandbox_id": "sbx-1"})
        self.assertEqual(second.provider_resource_id, "sbx-4")
        self.assertEqual(second.state, "suspended")
        self.assertIsNone(second.created_at)

    def test_any_label_other_than_name_is_refused_without_a_request(
        self,
    ) -> None:
        sdk = mock.MagicMock()
        for labels in ({"team": "x"}, {"name": "job-a", "team": "x"}):
            with self.assertRaises(FeatureNotSupportedError):
                _client(sdk).list_sandboxes(labels)
        sdk.list.assert_not_called()

    def test_a_transient_failure_is_retried(self) -> None:
        sdk = mock.MagicMock()
        sdk.list.side_effect = [
            _SdkConnectionError("reset"),
            _ApiError(429),
            [_info("sbx-1", "job-a")],
        ]

        (only,) = _client(sdk).list_sandboxes({"name": "job-a"})

        self.assertEqual(only.provider_resource_id, "sbx-1")
        self.assertEqual(sdk.list.call_count, 3)

    def test_retries_are_bounded_and_then_raise(self) -> None:
        sdk = mock.MagicMock()
        sdk.list.side_effect = _SdkConnectionError("reset")
        client = _client(sdk)

        with self.assertRaises(SandboxConnectionError) as caught:
            client.list_sandboxes({"name": "job-a"})

        self.assertEqual(sdk.list.call_count, client.retry_attempts)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.backend, "tensorlake")

    def test_a_permanent_failure_is_not_retried(self) -> None:
        sdk = mock.MagicMock()
        sdk.list.side_effect = _ApiError(403)

        with self.assertRaises(SandboxAuthenticationError):
            _client(sdk).list_sandboxes({"name": "job-a"})

        sdk.list.assert_called_once_with()

    def test_a_sandbox_without_an_id_raises_instead_of_a_partial_list(
        self,
    ) -> None:
        sdk = mock.MagicMock()
        sdk.list.return_value = [_info("sbx-1", "job-a"), _info("", "job-a")]

        with self.assertRaises(SandboxExecutionError):
            _client(sdk).list_sandboxes({"name": "job-a"})

    def test_the_wait_between_retries_doubles_up_to_a_cap(self) -> None:
        sdk = mock.MagicMock()
        sdk.list.side_effect = _ApiError(503)
        client = _client(sdk)
        client.retry_attempts = 5
        client.retry_backoff_secs = 1.0
        client.retry_backoff_max_secs = 3.0

        with mock.patch(
            "bespokelabs.sandbox.backends.tensorlake.time.sleep"
        ) as sleep:
            with self.assertRaises(SandboxExecutionError):
                client.list_sandboxes({"name": "job-a"})

        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [1.0, 2.0, 3.0, 3.0],
        )


class TensorlakeCreateTests(unittest.TestCase):
    """create is sent once and says what a failure may have left behind."""

    def _create(self, sdk: mock.MagicMock, **options: object) -> object:
        return _client(sdk).create(
            SandboxConfig(backend="tensorlake", backend_options=options)
        )

    def test_a_dropped_connection_is_not_retried_and_is_reported_unknown(
        self,
    ) -> None:
        sdk = mock.MagicMock()
        sdk.create_and_connect.side_effect = _SdkConnectionError("reset")

        with self.assertRaises(SandboxCreationError) as caught:
            self._create(sdk, name="job-a")

        sdk.create_and_connect.assert_called_once()
        sdk.delete.assert_not_called()
        error = caught.exception
        self.assertIs(error.outcome, ErrorOutcome.UNKNOWN)
        self.assertFalse(error.retryable)
        self.assertEqual(error.context["name"], "job-a")
        self.assertIn("list_sandboxes({'name': 'job-a'})", error.message)

    def test_a_server_error_is_reported_unknown(self) -> None:
        sdk = mock.MagicMock()
        sdk.create_and_connect.side_effect = _ApiError(502)

        with self.assertRaises(SandboxCreationError) as caught:
            self._create(sdk)

        sdk.create_and_connect.assert_called_once()
        self.assertIs(caught.exception.outcome, ErrorOutcome.UNKNOWN)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.context, {"provider_status": 502})

    def test_a_quota_rejection_created_nothing_and_may_be_retried(self) -> None:
        sdk = mock.MagicMock()
        sdk.create_and_connect.side_effect = _ApiError(429)

        with self.assertRaises(SandboxCreationError) as caught:
            self._create(sdk)

        sdk.create_and_connect.assert_called_once()
        self.assertIs(caught.exception.outcome, ErrorOutcome.FAILED)
        self.assertTrue(caught.exception.retryable)

    def test_a_refused_request_created_nothing(self) -> None:
        for raised, error_type in (
            (_ApiError(409), SandboxCreationError),
            (_ApiError(401), SandboxAuthenticationError),
            (TypeError("unexpected keyword"), SandboxCreationError),
        ):
            sdk = mock.MagicMock()
            sdk.create_and_connect.side_effect = raised

            with self.assertRaises(error_type) as caught:
                self._create(sdk)

            self.assertIs(caught.exception.outcome, ErrorOutcome.FAILED)
            self.assertFalse(caught.exception.retryable)

    def test_a_sandbox_that_never_started_is_deleted(self) -> None:
        sdk = mock.MagicMock()
        sdk.create_and_connect.side_effect = _StartupError("sbx-9")
        sdk.delete.side_effect = [_ApiError(503), None]

        with self.assertRaises(SandboxCreationError) as caught:
            self._create(sdk)

        self.assertEqual(sdk.delete.call_args_list, [mock.call("sbx-9")] * 2)
        error = caught.exception
        self.assertIs(error.outcome, ErrorOutcome.FAILED)
        self.assertEqual(
            error.context,
            {"provider_resource_id": "sbx-9", "cleanup_status": "deleted"},
        )

    def test_an_already_gone_sandbox_counts_as_deleted(self) -> None:
        sdk = mock.MagicMock()
        sdk.create_and_connect.side_effect = _StartupError("sbx-9")
        sdk.delete.side_effect = _ApiError(404)

        with self.assertRaises(SandboxCreationError) as caught:
            self._create(sdk)

        self.assertIs(caught.exception.outcome, ErrorOutcome.FAILED)

    def test_a_failed_cleanup_is_reported_unknown_with_the_sandbox_id(
        self,
    ) -> None:
        sdk = mock.MagicMock()
        sdk.create_and_connect.side_effect = _StartupError("sbx-9")
        sdk.delete.side_effect = _ApiError(400)

        with self.assertRaises(SandboxCreationError) as caught:
            self._create(sdk)

        sdk.delete.assert_called_once_with("sbx-9")
        error = caught.exception
        self.assertIs(error.outcome, ErrorOutcome.UNKNOWN)
        self.assertEqual(error.context["provider_resource_id"], "sbx-9")
        self.assertEqual(error.context["cleanup_status"], "unknown")

    def test_a_successful_create_passes_options_through(self) -> None:
        sdk = mock.MagicMock()

        session = self._create(sdk, name="job-a")

        self.assertEqual(
            sdk.create_and_connect.call_args.kwargs["name"], "job-a"
        )
        self.assertIs(session.native, sdk.create_and_connect.return_value)


class TensorlakeResumeTests(unittest.TestCase):

    def test_reattaching_retries_a_transient_failure(self) -> None:
        sdk = mock.MagicMock()
        sandbox = _FakeTensorlakeSandbox()
        sdk.connect.side_effect = [_ApiError(503), sandbox]

        session = _client(sdk).resume({"sandbox_id": "sbx-test"})

        self.assertEqual(
            sdk.connect.call_args_list, [mock.call("sbx-test")] * 2
        )
        self.assertIs(session.native, sandbox)

    def test_a_missing_sandbox_is_not_retried(self) -> None:
        sdk = mock.MagicMock()
        sdk.connect.side_effect = _ApiError(404)

        with self.assertRaises(SandboxCreationError):
            _client(sdk).resume({"sandbox_id": "sbx-test"})

        sdk.connect.assert_called_once_with("sbx-test")


if __name__ == "__main__":
    unittest.main()
