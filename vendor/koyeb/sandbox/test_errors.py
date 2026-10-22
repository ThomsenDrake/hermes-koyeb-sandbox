import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from koyeb.sandbox.snapshot import Snapshot
from koyeb.sandbox.sandbox import AsyncSandbox, Sandbox
from koyeb.sandbox.clients import get_api_clients, get_async_api_clients
from koyeb.sandbox.errors import (
    InvalidPortError,
    MissingApiTokenError,
    NoSandboxSecretError,
    SandboxError,
)
from koyeb.sandbox.sandbox import validate_port


def _no_token_env():
    """Isolate the process env so KOYEB_API_TOKEN is unset."""
    return patch.dict(os.environ, {}, clear=True)


class TestErrorHierarchy(unittest.TestCase):
    """Back-compat contract: token/port errors are SandboxError AND ValueError
    (published 1.5.x callers except ValueError); NoSandboxSecretError is
    SandboxError-only."""

    def test_missing_api_token_error(self):
        self.assertTrue(issubclass(MissingApiTokenError, SandboxError))
        self.assertTrue(issubclass(MissingApiTokenError, ValueError))

    def test_invalid_port_error(self):
        self.assertTrue(issubclass(InvalidPortError, SandboxError))
        self.assertTrue(issubclass(InvalidPortError, ValueError))

    def test_no_sandbox_secret_error(self):
        self.assertTrue(issubclass(NoSandboxSecretError, SandboxError))
        self.assertFalse(issubclass(NoSandboxSecretError, ValueError))


class TestMissingApiTokenRaiseSites(unittest.TestCase):
    """Every entry point raises MissingApiTokenError, not a plain ValueError."""

    def test_get_api_clients(self):
        with _no_token_env():
            with self.assertRaises(MissingApiTokenError) as cm:
                get_api_clients()
        self.assertIsInstance(cm.exception, ValueError)

    def test_get_async_api_clients(self):
        with _no_token_env():
            with self.assertRaises(MissingApiTokenError):
                get_async_api_clients()

    def test_sandbox_create(self):
        with _no_token_env():
            with self.assertRaises(MissingApiTokenError):
                Sandbox.create()

    def test_sandbox_get_from_id(self):
        with _no_token_env():
            with self.assertRaises(MissingApiTokenError):
                Sandbox.get_from_id("svc-1")

    def test_async_sandbox_create(self):
        with _no_token_env():
            with self.assertRaises(MissingApiTokenError):
                asyncio.run(AsyncSandbox.create())

    def test_async_sandbox_get_from_id(self):
        with _no_token_env():
            with self.assertRaises(MissingApiTokenError):
                asyncio.run(AsyncSandbox.get_from_id("svc-1"))

    def test_snapshot_get(self):
        with _no_token_env():
            with self.assertRaises(MissingApiTokenError) as cm:
                Snapshot.get("snap-1")
        self.assertIsInstance(cm.exception, SandboxError)


class TestInvalidPortRaiseSite(unittest.TestCase):
    def test_below_range(self):
        with self.assertRaises(InvalidPortError) as cm:
            validate_port(0)
        self.assertIsInstance(cm.exception, ValueError)
        self.assertIn("0", str(cm.exception))

    def test_above_range(self):
        with self.assertRaises(InvalidPortError):
            validate_port(70000)

    def test_non_integer(self):
        with self.assertRaises(InvalidPortError):
            validate_port("8080")

    def test_valid_port_unchanged(self):
        self.assertIsNone(validate_port(8080))


_FAKE_SERVICE = SimpleNamespace(
    id="svc-1",
    app_id="app-1",
    name="sb",
    active_deployment_id="dep-1",
    latest_deployment_id="dep-1",
)


def _deployment_with_env(env):
    return SimpleNamespace(
        deployment=SimpleNamespace(
            id="dep-1",
            status="HEALTHY",
            definition=SimpleNamespace(env=env),
            metadata=None,
        )
    )


def _fake_sync_clients(env):
    return SimpleNamespace(
        services=SimpleNamespace(
            get_service=lambda **kw: SimpleNamespace(service=_FAKE_SERVICE)
        ),
        deployments=SimpleNamespace(get_deployment=lambda **kw: _deployment_with_env(env)),
    )


class _FakeAsyncApi:
    def __init__(self, reply):
        self._reply = reply

    async def __call__(self, **kwargs):
        return self._reply


def _fake_async_clients(env):
    class Services:
        async def get_service(self, **kwargs):
            return SimpleNamespace(service=_FAKE_SERVICE)

    class Deployments:
        def __init__(self):
            self._reply = _deployment_with_env(env)

        async def get_deployment(self, **kwargs):
            return self._reply

    return SimpleNamespace(services=Services(), deployments=Deployments())


class TestNoSandboxSecretRaiseSite(unittest.TestCase):
    """get_from_id fails fast when the deployment carries no SANDBOX_SECRET,
    instead of returning a handle that dies later on first connected op."""

    def test_sync_missing_secret_raises(self):
        clients = _fake_sync_clients(env=[])
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients", return_value=clients
        ):
            with self.assertRaises(NoSandboxSecretError) as cm:
                Sandbox.get_from_id("svc-1", api_token="tok")
        self.assertIn("SANDBOX_SECRET", str(cm.exception))

    def test_sync_secret_found_returns_handle(self):
        env = [SimpleNamespace(key="SANDBOX_SECRET", value="sec")]
        clients = _fake_sync_clients(env=env)
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients", return_value=clients
        ):
            sb = Sandbox.get_from_id("svc-1", api_token="tok")
        self.assertEqual(sb.sandbox_secret, "sec")

    def test_async_missing_secret_raises(self):
        clients = _fake_async_clients(env=[])
        with patch(
            "koyeb.sandbox.sandbox.get_async_api_clients", return_value=clients
        ):
            with self.assertRaises(NoSandboxSecretError) as cm:
                asyncio.run(AsyncSandbox.get_from_id("svc-1", api_token="tok"))
        self.assertIn("SANDBOX_SECRET", str(cm.exception))

    def test_async_secret_found_returns_handle(self):
        env = [SimpleNamespace(key="SANDBOX_SECRET", value="sec")]
        clients = _fake_async_clients(env=env)
        with patch(
            "koyeb.sandbox.sandbox.get_async_api_clients", return_value=clients
        ):
            sb = asyncio.run(AsyncSandbox.get_from_id("svc-1", api_token="tok"))
        self.assertEqual(sb.sandbox_secret, "sec")


if __name__ == "__main__":
    unittest.main()
