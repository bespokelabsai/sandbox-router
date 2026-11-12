"""Tests for the ``native`` escape hatch on sessions, Sandbox and AsyncSandbox."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from bespokelabs.sandbox import AsyncSandbox, Sandbox, SandboxError
from bespokelabs.sandbox.backends.boxd import BoxdSession
from bespokelabs.sandbox.backends.daytona import DaytonaSession
from bespokelabs.sandbox.backends.docker import DockerSession
from bespokelabs.sandbox.backends.e2b import E2BSession
from bespokelabs.sandbox.backends.local import LocalSession
from bespokelabs.sandbox.backends.modal import ModalSession
from bespokelabs.sandbox.backends.monty import MontySession
from bespokelabs.sandbox.backends.ray import RaySession
from bespokelabs.sandbox.backends.runpod import RunpodSession
from bespokelabs.sandbox.backends.safehouse import SafehouseSession
from bespokelabs.sandbox.backends.tensorlake import TensorlakeSession
from bespokelabs.sandbox.exceptions import FeatureNotSupportedError
from bespokelabs.sandbox.types import SandboxConfig


def _sandbox(backend: str, session: object) -> Sandbox:
    return Sandbox._from_session(
        backend, session, SandboxConfig(backend=backend)
    )


class NativeReturnsProviderObjectTests(unittest.TestCase):

    def _cases(self) -> list[tuple[str, object, object]]:
        provider = object()
        return [
            (
                "daytona",
                DaytonaSession(client=object(), sandbox=provider),
                provider,
            ),
            ("e2b", E2BSession(sandbox=provider), provider),
            ("modal", ModalSession(sandbox=provider), provider),
            (
                "tensorlake",
                TensorlakeSession(client=object(), sandbox=provider),
                provider,
            ),
            ("docker", DockerSession(container=provider, timeout=60), provider),
            ("ray", RaySession(ray=object(), actor=provider), provider),
            (
                "boxd",
                BoxdSession(client=provider, machine_id="m-1", name="box"),
                provider,
            ),
        ]

    def test_session_and_sandbox_return_the_provider_object(self) -> None:
        for backend, session, provider in self._cases():
            with self.subTest(backend=backend):
                self.assertIs(session.native, provider)
                self.assertIs(_sandbox(backend, session).native, provider)


class NativeUnsupportedTests(unittest.TestCase):

    def test_backends_without_a_provider_object_raise(self) -> None:
        for backend, cls in [
            ("runpod", RunpodSession),
            ("monty", MontySession),
            ("safehouse", SafehouseSession),
            ("local", LocalSession),
        ]:
            with self.subTest(backend=backend):
                session = cls.__new__(cls)
                with self.assertRaises(FeatureNotSupportedError) as ctx:
                    _ = session.native
                self.assertEqual(ctx.exception.backend, backend)
                self.assertEqual(ctx.exception.op, "native")

    def test_live_local_sandbox_raises_feature_not_supported(self) -> None:
        with Sandbox("local") as sb:
            with self.assertRaises(FeatureNotSupportedError):
                _ = sb.native

    def test_session_without_native_property_raises(self) -> None:
        sb = _sandbox("third_party", SimpleNamespace(destroy=lambda: None))
        with self.assertRaises(FeatureNotSupportedError) as ctx:
            _ = sb.native
        self.assertEqual(ctx.exception.backend, "third_party")
        self.assertEqual(ctx.exception.op, "native")


class NativeLifecycleTests(unittest.TestCase):

    def test_destroyed_sandbox_raises_sandbox_error(self) -> None:
        sb = _sandbox("e2b", E2BSession(sandbox=mock.Mock()))
        sb.destroy()
        with self.assertRaisesRegex(SandboxError, "destroyed") as ctx:
            _ = sb.native
        self.assertNotIsInstance(ctx.exception, FeatureNotSupportedError)


class AsyncNativeTests(unittest.IsolatedAsyncioTestCase):

    async def test_async_sandbox_returns_the_provider_object(self) -> None:
        provider = object()
        sb = AsyncSandbox(_sandbox("modal", ModalSession(sandbox=provider)))
        self.assertIs(sb.native, provider)


if __name__ == "__main__":
    unittest.main()
