import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from koyeb.api.models.deployment_status import DeploymentStatus
from koyeb.api.models.service_status import ServiceStatus
from koyeb.sandbox.sandbox import AsyncSandbox, Sandbox, SandboxDeploymentError
from koyeb.sandbox.status import classify_deployment_status, classify_service_status


class TestClassifyServiceStatus(unittest.TestCase):
    """JS SDK parity (claim.ts classifyServiceStatus): HEALTHY/DEGRADED are
    usable, STARTING/RESUMING are in progress, everything else — including
    unknown forward-compat values — is a terminal failure (fail closed)."""

    def test_ready(self):
        self.assertEqual(classify_service_status(ServiceStatus.HEALTHY), "ready")
        self.assertEqual(classify_service_status(ServiceStatus.DEGRADED), "ready")

    def test_in_progress(self):
        self.assertEqual(
            classify_service_status(ServiceStatus.STARTING), "in_progress"
        )
        self.assertEqual(
            classify_service_status(ServiceStatus.RESUMING), "in_progress"
        )

    def test_known_terminal(self):
        for status in (
            ServiceStatus.UNHEALTHY,
            ServiceStatus.DELETING,
            ServiceStatus.DELETED,
            ServiceStatus.PAUSING,
            ServiceStatus.PAUSED,
        ):
            self.assertEqual(
                classify_service_status(status),
                "terminal_failure",
                msg=f"{status} should be terminal_failure",
            )

    def test_unknown_value_fails_closed(self):
        self.assertEqual(
            classify_service_status("SOME_FUTURE_STATE"), "terminal_failure"
        )


class TestClassifyDeploymentStatus(unittest.TestCase):
    """Deployment-level twin: pre-HEALTHY states are in progress, HEALTHY and
    DEGRADED are ready, every other state — SLEEPING/STASHED included, plus
    unknown values — is terminal (it will not become ready on its own)."""

    def test_ready(self):
        self.assertEqual(classify_deployment_status(DeploymentStatus.HEALTHY), "ready")
        self.assertEqual(classify_deployment_status(DeploymentStatus.DEGRADED), "ready")

    def test_in_progress(self):
        for status in (
            DeploymentStatus.PENDING,
            DeploymentStatus.PROVISIONING,
            DeploymentStatus.SCHEDULED,
            DeploymentStatus.ALLOCATING,
            DeploymentStatus.STARTING,
        ):
            self.assertEqual(
                classify_deployment_status(status),
                "in_progress",
                msg=f"{status} should be in_progress",
            )

    def test_terminal(self):
        for status in (
            DeploymentStatus.CANCELING,
            DeploymentStatus.CANCELED,
            DeploymentStatus.STOPPING,
            DeploymentStatus.STOPPED,
            DeploymentStatus.UNHEALTHY,
            DeploymentStatus.ERRORING,
            DeploymentStatus.ERROR,
            DeploymentStatus.STASHED,
            DeploymentStatus.SLEEPING,
        ):
            self.assertEqual(
                classify_deployment_status(status),
                "terminal_failure",
                msg=f"{status} should be terminal_failure",
            )

    def test_unknown_value_fails_closed(self):
        self.assertEqual(
            classify_deployment_status("SOME_FUTURE_STATE"), "terminal_failure"
        )


class TestSyncDeploymentHealthWiring(unittest.TestCase):
    """_is_deployment_healthy must fail fast on terminal states (raise
    SandboxDeploymentError naming the status) and treat DEGRADED as ready,
    instead of polling non-HEALTHY states until the wait timeout."""

    def _make_sandbox(self):
        sb = Sandbox.__new__(Sandbox)
        sb.name = "sb"
        sb.api_token = None
        sb.host = None
        sb._deployment_id = "dep-1"
        sb._sandbox_url = None
        return sb

    @staticmethod
    def _fake_clients(status):
        reply = SimpleNamespace(
            deployment=SimpleNamespace(
                id="dep-1", status=status, definition=None, metadata=None
            )
        )
        return SimpleNamespace(
            deployments=SimpleNamespace(get_deployment=lambda id: reply),
        )

    def _patch_clients(self, status):
        return patch(
            "koyeb.sandbox.sandbox.get_api_clients",
            return_value=self._fake_clients(status),
        )

    def test_stopped_raises_naming_status(self):
        sb = self._make_sandbox()
        with self._patch_clients(DeploymentStatus.STOPPED):
            with self.assertRaises(SandboxDeploymentError) as cm:
                sb._is_deployment_healthy()
        self.assertIn("STOPPED", str(cm.exception))
        self.assertIn("wake or redeploy", str(cm.exception))

    def test_sleeping_raises_naming_status(self):
        """A scale-to-zero sandbox must not hang wait_ready until timeout."""
        sb = self._make_sandbox()
        with self._patch_clients(DeploymentStatus.SLEEPING):
            with self.assertRaises(SandboxDeploymentError) as cm:
                sb._is_deployment_healthy()
        self.assertIn("SLEEPING", str(cm.exception))

    def test_degraded_is_ready(self):
        sb = self._make_sandbox()
        with self._patch_clients(DeploymentStatus.DEGRADED):
            self.assertTrue(sb._is_deployment_healthy())

    def test_starting_is_still_in_progress(self):
        sb = self._make_sandbox()
        with self._patch_clients(DeploymentStatus.STARTING):
            self.assertFalse(sb._is_deployment_healthy())

    def test_is_healthy_propagates_terminal_error(self):
        """is_healthy() on a stopped sandbox raises (self-explanatory) instead
        of silently returning False after a full timeout."""
        sb = self._make_sandbox()
        with self._patch_clients(DeploymentStatus.STOPPED):
            with self.assertRaises(SandboxDeploymentError) as cm:
                sb.is_healthy()
        self.assertIn("STOPPED", str(cm.exception))


class TestAsyncDeploymentHealthWiring(unittest.TestCase):
    """Async twin of the fail-closed wiring."""

    def _make_sandbox(self):
        sb = AsyncSandbox.__new__(AsyncSandbox)
        sb.name = "sb"
        sb.api_token = None
        sb.host = None
        sb._deployment_id = "dep-1"
        sb._sandbox_url = None
        return sb

    @staticmethod
    def _fake_clients(status):
        class FakeDeployments:
            async def get_deployment(self, id):
                return SimpleNamespace(
                    deployment=SimpleNamespace(
                        id="dep-1", status=status, definition=None, metadata=None
                    )
                )

        return SimpleNamespace(deployments=FakeDeployments())

    def _patch_clients(self, status):
        return patch(
            "koyeb.sandbox.sandbox.get_async_api_clients",
            return_value=self._fake_clients(status),
        )

    def test_stopped_raises_naming_status(self):
        sb = self._make_sandbox()

        async def run():
            with self._patch_clients(DeploymentStatus.STOPPED):
                await sb._async_is_deployment_healthy()

        with self.assertRaises(SandboxDeploymentError) as cm:
            asyncio.run(run())
        self.assertIn("STOPPED", str(cm.exception))
        self.assertIn("wake or redeploy", str(cm.exception))

    def test_sleeping_raises(self):
        sb = self._make_sandbox()

        async def run():
            with self._patch_clients(DeploymentStatus.SLEEPING):
                await sb._async_is_deployment_healthy()

        with self.assertRaises(SandboxDeploymentError) as cm:
            asyncio.run(run())
        self.assertIn("SLEEPING", str(cm.exception))

    def test_degraded_is_ready(self):
        sb = self._make_sandbox()

        async def run():
            with self._patch_clients(DeploymentStatus.DEGRADED):
                return await sb._async_is_deployment_healthy()

        self.assertTrue(asyncio.run(run()))

    def test_starting_is_still_in_progress(self):
        sb = self._make_sandbox()

        async def run():
            with self._patch_clients(DeploymentStatus.STARTING):
                return await sb._async_is_deployment_healthy()

        self.assertFalse(asyncio.run(run()))


if __name__ == "__main__":
    unittest.main()
