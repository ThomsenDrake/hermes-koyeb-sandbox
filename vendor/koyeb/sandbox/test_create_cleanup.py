import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from koyeb.api.exceptions import ApiException
from koyeb.api_async.exceptions import ApiException as AsyncApiException

from koyeb.sandbox.sandbox import (
    AsyncSandbox,
    Sandbox,
    SandboxDeploymentError,
    SandboxError,
    SandboxTimeoutError,
)


class FakeApps:
    def __init__(self):
        self.created = []
        self.deleted = []

    def create_app(self, app):
        self.created.append(app)
        return SimpleNamespace(app=SimpleNamespace(id="app-1"))

    def delete_app(self, app_id):
        self.deleted.append(app_id)


class FakeServices:
    def __init__(self, exc=None):
        self.exc = exc
        self.deleted = []

    def create_service(self, service):
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(service=SimpleNamespace(id="svc-1"))

    def delete_service(self, id):
        self.deleted.append(id)


def _fake_sync_clients(exc=None):
    return SimpleNamespace(apps=FakeApps(), services=FakeServices(exc=exc))


class FakeAsyncApps:
    def __init__(self):
        self.created = []
        self.deleted = []

    async def create_app(self, app):
        self.created.append(app)
        return SimpleNamespace(app=SimpleNamespace(id="app-1"))

    async def delete_app(self, app_id):
        self.deleted.append(app_id)


class FakeAsyncServices:
    def __init__(self, exc=None):
        self.exc = exc
        self.deleted = []

    async def create_service(self, service):
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(service=SimpleNamespace(id="svc-1"))

    async def delete_service(self, id):
        self.deleted.append(id)


def _fake_async_clients(exc=None):
    return SimpleNamespace(apps=FakeAsyncApps(), services=FakeAsyncServices(exc=exc))


class TestCreateServiceFailureCleanup(unittest.TestCase):
    """Service-creation failures wrap as SandboxError and delete the app this
    call created (JS sandbox.ts:236-237) — but never a caller-provided app."""

    def test_sync_wraps_and_deletes_created_app(self):
        clients = _fake_sync_clients(
            exc=ApiException(status=500, reason="Internal Server Error")
        )
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients", return_value=clients
        ):
            with self.assertRaises(SandboxError) as cm:
                Sandbox.create(api_token="tok", wait_ready=False)
        self.assertNotIsInstance(cm.exception, ApiException)
        self.assertEqual(clients.apps.deleted, ["app-1"])

    def test_sync_keeps_caller_provided_app(self):
        clients = _fake_sync_clients(
            exc=ApiException(status=500, reason="Internal Server Error")
        )
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients", return_value=clients
        ):
            with self.assertRaises(SandboxError):
                Sandbox.create(
                    api_token="tok", app_id="existing-app", wait_ready=False
                )
        self.assertEqual(clients.apps.deleted, [])
        self.assertEqual(clients.apps.created, [])

    def test_async_wraps_and_deletes_created_app(self):
        clients = _fake_async_clients(
            exc=AsyncApiException(status=500, reason="Internal Server Error")
        )
        with patch(
            "koyeb.sandbox.sandbox.get_async_api_clients", return_value=clients
        ):
            with self.assertRaises(SandboxError) as cm:
                asyncio.run(AsyncSandbox.create(api_token="tok", wait_ready=False))
        self.assertNotIsInstance(cm.exception, ApiException)
        self.assertEqual(clients.apps.deleted, ["app-1"])


class TestWaitReadyCleanup(unittest.TestCase):
    """cleanup_on_failure=True (default): wait timeout or terminal state
    deletes the sandbox before raising, and the error says so."""

    def _create_ok_clients(self):
        return _fake_sync_clients()

    def test_sync_timeout_deletes_and_says_so(self):
        clients = self._create_ok_clients()
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients", return_value=clients
        ):
            with patch.object(Sandbox, "wait_ready", return_value=False):
                with patch.object(Sandbox, "delete") as delete:
                    with self.assertRaises(SandboxTimeoutError) as cm:
                        Sandbox.create(api_token="tok", timeout=1)
        delete.assert_called_once()
        self.assertIn("deleted", str(cm.exception))
        self.assertNotIn("wait_ready() again", str(cm.exception))

    def test_sync_cleanup_on_failure_false_keeps_sandbox(self):
        clients = self._create_ok_clients()
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients", return_value=clients
        ):
            with patch.object(Sandbox, "wait_ready", return_value=False):
                with patch.object(Sandbox, "delete") as delete:
                    with self.assertRaises(SandboxTimeoutError) as cm:
                        Sandbox.create(
                            api_token="tok", timeout=1, cleanup_on_failure=False
                        )
        delete.assert_not_called()
        self.assertIn("wait_ready() again", str(cm.exception))

    def test_sync_terminal_error_deletes_and_annotates(self):
        clients = self._create_ok_clients()
        terminal = SandboxDeploymentError(
            "Sandbox 'quick-sandbox' deployment reached status STOPPED "
            "— it will not become ready; wake or redeploy the sandbox."
        )
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients", return_value=clients
        ):
            with patch.object(Sandbox, "wait_ready", side_effect=terminal):
                with patch.object(Sandbox, "delete") as delete:
                    with self.assertRaises(SandboxDeploymentError) as cm:
                        Sandbox.create(api_token="tok", timeout=1)
        delete.assert_called_once()
        self.assertIn("STOPPED", str(cm.exception))
        self.assertIn("The sandbox was deleted.", str(cm.exception))

    def test_sync_cleanup_failure_preserves_original_error(self):
        clients = self._create_ok_clients()
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients", return_value=clients
        ):
            with patch.object(Sandbox, "wait_ready", return_value=False):
                with patch.object(
                    Sandbox, "delete", side_effect=RuntimeError("api down")
                ) as delete:
                    with self.assertRaises(SandboxTimeoutError) as cm:
                        Sandbox.create(api_token="tok", timeout=1)
        delete.assert_called_once()
        self.assertNotIsInstance(cm.exception, RuntimeError)
        self.assertIn("could not be deleted", str(cm.exception))

    def test_sync_success_does_not_delete(self):
        clients = self._create_ok_clients()
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients", return_value=clients
        ):
            with patch.object(Sandbox, "wait_ready", return_value=True):
                with patch.object(Sandbox, "delete") as delete:
                    sb = Sandbox.create(api_token="tok")
        delete.assert_not_called()
        self.assertEqual(sb.service_id, "svc-1")

    def test_async_cleanup_on_failure_false_keeps_sandbox(self):
        clients = _fake_async_clients()
        with patch(
            "koyeb.sandbox.sandbox.get_async_api_clients", return_value=clients
        ):
            with patch.object(AsyncSandbox, "wait_ready", return_value=False):
                with patch.object(AsyncSandbox, "delete") as delete:
                    with self.assertRaises(SandboxTimeoutError) as cm:
                        asyncio.run(
                            AsyncSandbox.create(
                                api_token="tok", timeout=1, cleanup_on_failure=False
                            )
                        )
        delete.assert_not_called()
        self.assertIn("wait_ready() again", str(cm.exception))

    def test_async_cleanup_failure_preserves_original_error(self):
        clients = _fake_async_clients()
        with patch(
            "koyeb.sandbox.sandbox.get_async_api_clients", return_value=clients
        ):
            with patch.object(AsyncSandbox, "wait_ready", return_value=False):
                with patch.object(
                    AsyncSandbox, "delete", side_effect=RuntimeError("api down")
                ) as delete:
                    with self.assertRaises(SandboxTimeoutError) as cm:
                        asyncio.run(AsyncSandbox.create(api_token="tok", timeout=1))
        self.assertEqual(delete.await_count, 1)
        self.assertNotIsInstance(cm.exception, RuntimeError)
        self.assertIn("could not be deleted", str(cm.exception))

    def test_async_success_does_not_delete(self):
        clients = _fake_async_clients()
        with patch(
            "koyeb.sandbox.sandbox.get_async_api_clients", return_value=clients
        ):
            with patch.object(AsyncSandbox, "wait_ready", return_value=True):
                with patch.object(AsyncSandbox, "delete") as delete:
                    sb = asyncio.run(AsyncSandbox.create(api_token="tok"))
        delete.assert_not_called()
        self.assertEqual(sb.service_id, "svc-1")

    def test_async_timeout_deletes_and_says_so(self):
        clients = _fake_async_clients()
        with patch(
            "koyeb.sandbox.sandbox.get_async_api_clients", return_value=clients
        ):
            with patch.object(AsyncSandbox, "wait_ready", return_value=False):
                with patch.object(AsyncSandbox, "delete") as delete:
                    with self.assertRaises(SandboxTimeoutError) as cm:
                        asyncio.run(AsyncSandbox.create(api_token="tok", timeout=1))
        self.assertEqual(delete.await_count, 1)
        self.assertIn("deleted", str(cm.exception))
        self.assertNotIn("wait_ready() again", str(cm.exception))

    def test_async_terminal_error_deletes_and_annotates(self):
        clients = _fake_async_clients()
        terminal = SandboxDeploymentError(
            "Sandbox 'quick-sandbox' deployment reached status STOPPED "
            "— it will not become ready; wake or redeploy the sandbox."
        )
        with patch(
            "koyeb.sandbox.sandbox.get_async_api_clients", return_value=clients
        ):
            with patch.object(AsyncSandbox, "wait_ready", side_effect=terminal):
                with patch.object(AsyncSandbox, "delete") as delete:
                    with self.assertRaises(SandboxDeploymentError) as cm:
                        asyncio.run(AsyncSandbox.create(api_token="tok", timeout=1))
        self.assertEqual(delete.await_count, 1)
        self.assertIn("STOPPED", str(cm.exception))
        self.assertIn("The sandbox was deleted.", str(cm.exception))


class TestCallerAppCleanup(unittest.TestCase):
    """Wait-failure cleanup must never delete a caller-provided app — only
    the sandbox service this call created inside it."""

    def test_sync_timeout_keeps_caller_app_deletes_service(self):
        clients = _fake_sync_clients()
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients", return_value=clients
        ):
            with patch.object(Sandbox, "wait_ready", return_value=False):
                with self.assertRaises(SandboxTimeoutError):
                    Sandbox.create(
                        api_token="tok", app_id="existing-app", timeout=1
                    )
        self.assertEqual(clients.apps.deleted, [])
        self.assertEqual(clients.services.deleted, ["svc-1"])

    def test_sync_timeout_deletes_app_created_by_call(self):
        clients = _fake_sync_clients()
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients", return_value=clients
        ):
            with patch.object(Sandbox, "wait_ready", return_value=False):
                with self.assertRaises(SandboxTimeoutError):
                    Sandbox.create(api_token="tok", timeout=1)
        self.assertEqual(clients.apps.deleted, ["app-1"])
        self.assertEqual(clients.services.deleted, [])

    def test_async_timeout_keeps_caller_app_deletes_service(self):
        clients = _fake_async_clients()
        with patch(
            "koyeb.sandbox.sandbox.get_async_api_clients", return_value=clients
        ):
            with patch.object(AsyncSandbox, "wait_ready", return_value=False):
                with self.assertRaises(SandboxTimeoutError):
                    asyncio.run(
                        AsyncSandbox.create(
                            api_token="tok", app_id="existing-app", timeout=1
                        )
                    )
        self.assertEqual(clients.apps.deleted, [])
        self.assertEqual(clients.services.deleted, ["svc-1"])

    def test_async_timeout_deletes_app_created_by_call(self):
        clients = _fake_async_clients()
        with patch(
            "koyeb.sandbox.sandbox.get_async_api_clients", return_value=clients
        ):
            with patch.object(AsyncSandbox, "wait_ready", return_value=False):
                with self.assertRaises(SandboxTimeoutError):
                    asyncio.run(AsyncSandbox.create(api_token="tok", timeout=1))
        self.assertEqual(clients.apps.deleted, ["app-1"])
        self.assertEqual(clients.services.deleted, [])

    def test_sync_terminal_error_keeps_caller_app_deletes_service(self):
        # The except-branch cleanup, unlike the timeout branch pinned above.
        clients = _fake_sync_clients()
        terminal = SandboxDeploymentError(
            "Sandbox 'quick-sandbox' deployment reached status STOPPED "
            "— it will not become ready; wake or redeploy the sandbox."
        )
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients", return_value=clients
        ):
            with patch.object(Sandbox, "wait_ready", side_effect=terminal):
                with self.assertRaises(SandboxDeploymentError) as cm:
                    Sandbox.create(
                        api_token="tok", app_id="existing-app", timeout=1
                    )
        self.assertEqual(clients.apps.deleted, [])
        self.assertEqual(clients.services.deleted, ["svc-1"])
        self.assertIn("The sandbox was deleted.", str(cm.exception))

    def test_async_terminal_error_keeps_caller_app_deletes_service(self):
        clients = _fake_async_clients()
        terminal = SandboxDeploymentError(
            "Sandbox 'quick-sandbox' deployment reached status STOPPED "
            "— it will not become ready; wake or redeploy the sandbox."
        )
        with patch(
            "koyeb.sandbox.sandbox.get_async_api_clients", return_value=clients
        ):
            with patch.object(AsyncSandbox, "wait_ready", side_effect=terminal):
                with self.assertRaises(SandboxDeploymentError) as cm:
                    asyncio.run(
                        AsyncSandbox.create(
                            api_token="tok", app_id="existing-app", timeout=1
                        )
                    )
        self.assertEqual(clients.apps.deleted, [])
        self.assertEqual(clients.services.deleted, ["svc-1"])
        self.assertIn("The sandbox was deleted.", str(cm.exception))

    def test_sync_cleanup_failure_keeps_caller_app_and_tells_truth(self):
        clients = _fake_sync_clients()

        def boom(id):
            raise RuntimeError("api down")

        clients.services.delete_service = boom
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients", return_value=clients
        ):
            with patch.object(Sandbox, "wait_ready", return_value=False):
                with self.assertRaises(SandboxTimeoutError) as cm:
                    Sandbox.create(
                        api_token="tok", app_id="existing-app", timeout=1
                    )
        self.assertEqual(clients.apps.deleted, [])
        self.assertIn("could not be deleted", str(cm.exception))

    def test_async_cleanup_failure_keeps_caller_app_and_tells_truth(self):
        clients = _fake_async_clients()

        async def boom(id):
            raise RuntimeError("api down")

        clients.services.delete_service = boom
        with patch(
            "koyeb.sandbox.sandbox.get_async_api_clients", return_value=clients
        ):
            with patch.object(AsyncSandbox, "wait_ready", return_value=False):
                with self.assertRaises(SandboxTimeoutError) as cm:
                    asyncio.run(
                        AsyncSandbox.create(
                            api_token="tok", app_id="existing-app", timeout=1
                        )
                    )
        self.assertEqual(clients.apps.deleted, [])
        self.assertIn("could not be deleted", str(cm.exception))

    def test_handle_built_from_id_keeps_app_delete_default(self):
        # get_from_id()/list() handles have unknown provenance and keep the
        # historical whole-app delete.
        clients = _fake_sync_clients()
        sandbox = Sandbox(
            sandbox_id="s", app_id="app-x", service_id="svc-x", api_token="tok"
        )
        with patch(
            "koyeb.sandbox.sandbox.get_api_clients", return_value=clients
        ):
            sandbox.delete()
        self.assertEqual(clients.apps.deleted, ["app-x"])
        self.assertEqual(clients.services.deleted, [])


if __name__ == "__main__":
    unittest.main()
