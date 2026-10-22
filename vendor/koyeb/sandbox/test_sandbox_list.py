import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from koyeb.sandbox.executor_client import ConnectionInfo
from koyeb.sandbox.sandbox import AsyncSandbox, Sandbox
from koyeb.sandbox.clients import create_sandbox_client
from koyeb.sandbox.errors import NoSandboxSecretError, SandboxError


def _service(idx):
    return SimpleNamespace(
        id=f"svc-{idx}",
        app_id=f"app-{idx}",
        name=f"sb-{idx}",
        active_deployment_id=f"depl-{idx}",
        latest_deployment_id=f"depl-{idx}",
    )


class FakeListServicesApi:
    """Realistic pagination: up to 100 items per page, count totals."""

    def __init__(self, count=150):
        self.calls = []
        self._count = count

    def list_services(self, **kwargs):
        self.calls.append(kwargs)
        limit = int(kwargs.get("limit") or 100)
        offset = int(kwargs.get("offset") or 0)
        services = [
            _service(i) for i in range(offset, min(offset + limit, self._count))
        ]
        return SimpleNamespace(services=services, count=self._count, has_next=False)


class FakeAsyncListServicesApi(FakeListServicesApi):
    async def list_services(self, **kwargs):
        return FakeListServicesApi.list_services(self, **kwargs)


class TestSandboxList(unittest.TestCase):
    def test_filters_types_and_paginates(self):
        services = FakeListServicesApi(count=150)
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients",
            return_value=SimpleNamespace(services=services),
        ):
            found = Sandbox.list(app_id="app-9", name="sb", api_token="tok")
        self.assertEqual(len(found), 150)
        self.assertEqual(found[0].service_id, "svc-0")
        self.assertEqual(found[149].service_id, "svc-149")
        self.assertEqual(len(services.calls), 2)
        first, second = services.calls
        self.assertEqual(first.get("types"), ["SANDBOX"])
        self.assertEqual(first.get("app_id"), "app-9")
        self.assertEqual(first.get("name"), "sb")
        self.assertEqual(first.get("limit"), "100")
        self.assertEqual(first.get("offset"), "0")
        self.assertEqual(second.get("offset"), "100")

    def test_handles_are_lazy(self):
        services = FakeListServicesApi(count=1)
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients",
            return_value=SimpleNamespace(services=services),
        ):
            found = Sandbox.list(api_token="tok")
        self.assertIsNone(found[0].sandbox_secret)
        self.assertEqual(found[0].name, "sb-0")

    def test_empty(self):
        services = FakeListServicesApi(count=0)
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients",
            return_value=SimpleNamespace(services=services),
        ):
            found = Sandbox.list(api_token="tok")
        self.assertEqual(found, [])


class TestAsyncSandboxList(unittest.TestCase):
    def test_filters_types_and_paginates(self):
        services = FakeAsyncListServicesApi(count=150)
        with patch(
            "koyeb.sandbox.sandbox.get_async_api_clients",
            return_value=SimpleNamespace(services=services),
        ):
            found = asyncio.run(AsyncSandbox.list(app_id="app-9", api_token="tok"))
        self.assertEqual(len(found), 150)
        self.assertEqual(len(services.calls), 2)
        self.assertEqual(services.calls[0].get("types"), ["SANDBOX"])
        self.assertIsNone(found[0].sandbox_secret)


class TestLazyHandleConnectedOp(unittest.TestCase):
    """A lazily-connected handle (from Sandbox.list()) fails on the first
    connected op with NoSandboxSecretError naming the fix."""

    def test_validate_raises_no_sandbox_secret_error_naming_fix(self):
        conn = ConnectionInfo(public_url="https://sb.example", routing_key=None, secret=None)
        with self.assertRaises(NoSandboxSecretError) as cm:
            conn.validate()
        self.assertIn("get_from_id", str(cm.exception))

    def test_create_sandbox_client_propagates_typed_error(self):
        conn = ConnectionInfo(public_url="https://sb.example", routing_key=None, secret=None)
        with self.assertRaises(NoSandboxSecretError) as cm:
            create_sandbox_client(conn)
        self.assertIsInstance(cm.exception, SandboxError)
        self.assertIn("get_from_id", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
