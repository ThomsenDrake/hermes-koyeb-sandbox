"""Pins the control-plane seam: neutral info types out, payloads in,
error mapping exactly where the orchestration contract demands it."""

import asyncio
import unittest
from types import SimpleNamespace

from koyeb.api.exceptions import ApiException, NotFoundException
from koyeb.api_async.exceptions import (
    NotFoundException as AsyncNotFoundException,
)

from koyeb.sandbox.control_plane import (
    AsyncControlPlane,
    DeploymentInfo,
    ServiceInfo,
    SyncControlPlane,
)
from koyeb.sandbox.errors import SandboxError


def _fake_service():
    return SimpleNamespace(
        id="svc-1",
        app_id="app-1",
        name="sb-1",
        active_deployment_id="depl-active",
        latest_deployment_id="depl-latest",
    )


def _fake_deployment():
    return SimpleNamespace(
        id="depl-active",
        status="HEALTHY",
        definition=SimpleNamespace(
            env=[SimpleNamespace(key="SANDBOX_SECRET", value="sec")],
            to_dict=lambda: {"env": [{"key": "SANDBOX_SECRET", "value": "sec"}]},
        ),
        metadata=SimpleNamespace(
            sandbox=SimpleNamespace(public_url="https://sb.example", routing_key="rk"),
            proxy_ports=[SimpleNamespace(port=3031, host="h", public_port=1)],
        ),
    )


class TestSyncControlPlane(unittest.TestCase):
    def _clients(self, **overrides):
        defaults = dict(
            apps=SimpleNamespace(
                get_app=lambda app_id: SimpleNamespace(
                    app=SimpleNamespace(id=app_id, name="sb-app", domains=[SimpleNamespace(name="d.example")])
                )
            ),
            services=SimpleNamespace(
                get_service=lambda id: SimpleNamespace(service=_fake_service()),
                update_service=lambda id, service: SimpleNamespace(
                    service=SimpleNamespace(latest_deployment_id="depl-new")
                ),
            ),
            deployments=SimpleNamespace(
                get_deployment=lambda id: SimpleNamespace(deployment=_fake_deployment())
            ),
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_get_service_maps_to_neutral_info(self):
        cp = SyncControlPlane(self._clients())
        service = cp.get_service("svc-1")
        self.assertEqual(
            service,
            ServiceInfo(
                id="svc-1",
                app_id="app-1",
                name="sb-1",
                active_deployment_id="depl-active",
                latest_deployment_id="depl-latest",
            ),
        )

    def test_get_service_not_found_maps_to_sandbox_error(self):
        def raise_not_found(id):
            raise NotFoundException(status=404, reason="Not Found")

        cp = SyncControlPlane(
            self._clients(
                services=SimpleNamespace(get_service=raise_not_found)
            )
        )
        with self.assertRaises(SandboxError) as cm:
            cp.get_service("svc-1")
        self.assertIn("Sandbox not found with id: svc-1", str(cm.exception))

    def test_get_service_api_error_maps_to_sandbox_error(self):
        def raise_api(id):
            raise ApiException(status=500, reason="Internal Server Error")

        cp = SyncControlPlane(
            self._clients(services=SimpleNamespace(get_service=raise_api))
        )
        with self.assertRaises(SandboxError) as cm:
            cp.get_service("svc-1")
        self.assertIn("Failed to retrieve sandbox with id: svc-1", str(cm.exception))

    def test_get_service_none_service_maps_not_found(self):
        cp = SyncControlPlane(
            self._clients(
                services=SimpleNamespace(get_service=lambda id: SimpleNamespace(service=None))
            )
        )
        with self.assertRaises(SandboxError) as cm:
            cp.get_service("svc-1")
        self.assertIn("Sandbox not found with id: svc-1", str(cm.exception))

    def test_get_deployment_extracts_env_metadata_proxy_ports(self):
        cp = SyncControlPlane(self._clients())
        deployment = cp.get_deployment("depl-active")
        self.assertIsInstance(deployment, DeploymentInfo)
        self.assertEqual(deployment.env, {"SANDBOX_SECRET": "sec"})
        self.assertEqual(deployment.sandbox_url, "https://sb.example")
        self.assertEqual(deployment.routing_key, "rk")
        self.assertEqual(
            deployment.proxy_ports, [{"port": 3031, "host": "h", "public_port": 1}]
        )
        self.assertEqual(deployment.status, "HEALTHY")

    def test_get_app_returns_domain_names(self):
        cp = SyncControlPlane(self._clients())
        app = cp.get_app("app-1")
        self.assertEqual(app.id, "app-1")
        self.assertEqual(app.domains, ["d.example"])

    def test_list_services_filters_sandbox_type_with_string_pagination(self):
        calls = []

        def list_services(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(services=[_fake_service()], count=1, has_next=False)

        cp = SyncControlPlane(
            self._clients(services=SimpleNamespace(list_services=list_services))
        )
        services, count = cp.list_services(app_id="app-1", name="sb", offset=0, limit=100)
        self.assertEqual(count, 1)
        self.assertEqual(services[0].id, "svc-1")
        self.assertEqual(calls[0]["types"], ["SANDBOX"])
        self.assertEqual(calls[0]["app_id"], "app-1")
        self.assertEqual(calls[0]["name"], "sb")
        self.assertEqual(calls[0]["limit"], "100")
        self.assertEqual(calls[0]["offset"], "0")

    def test_update_service_coerces_definition_dict(self):
        sent = []

        def update_service(id, service):
            sent.append(service)
            return SimpleNamespace(service=SimpleNamespace(latest_deployment_id="depl-new"))

        cp = SyncControlPlane(
            self._clients(services=SimpleNamespace(update_service=update_service))
        )
        new_id = cp.update_service("svc-1", {"env": []})
        self.assertEqual(new_id, "depl-new")
        self.assertIsNotNone(sent[0].definition)

    def test_deployment_definition_returns_wire_dict(self):
        cp = SyncControlPlane(self._clients())
        self.assertEqual(
            cp.deployment_definition("depl-active"),
            {"env": [{"key": "SANDBOX_SECRET", "value": "sec"}]},
        )

    def test_create_app_and_service_return_ids(self):
        cp = SyncControlPlane(
            self._clients(
                apps=SimpleNamespace(
                    create_app=lambda app: SimpleNamespace(
                        app=SimpleNamespace(id="app-1")
                    )
                ),
                services=SimpleNamespace(
                    create_service=lambda service: SimpleNamespace(
                        service=SimpleNamespace(id="svc-1")
                    )
                ),
            )
        )
        self.assertEqual(cp.create_app({"name": "a", "life_cycle": {}}), "app-1")
        self.assertEqual(cp.create_service({"app_id": "app-1"}), "svc-1")


class TestAsyncControlPlane(unittest.TestCase):
    def _clients(self, **overrides):
        async def get_service(id):
            return SimpleNamespace(service=_fake_service())

        async def get_deployment(id):
            return SimpleNamespace(deployment=_fake_deployment())

        async def get_app(app_id):
            return SimpleNamespace(
                app=SimpleNamespace(id=app_id, name="sb-app", domains=[SimpleNamespace(name="d.example")])
            )

        defaults = dict(
            apps=SimpleNamespace(get_app=get_app),
            services=SimpleNamespace(get_service=get_service),
            deployments=SimpleNamespace(get_deployment=get_deployment),
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_get_service_maps_to_neutral_info(self):
        cp = AsyncControlPlane(self._clients())
        service = asyncio.run(cp.get_service("svc-1"))
        self.assertEqual(service.id, "svc-1")
        self.assertEqual(service.active_deployment_id, "depl-active")

    def test_get_service_not_found_maps_to_sandbox_error(self):
        async def raise_not_found(id):
            raise AsyncNotFoundException(status=404, reason="Not Found")

        cp = AsyncControlPlane(
            self._clients(services=SimpleNamespace(get_service=raise_not_found))
        )
        with self.assertRaises(SandboxError) as cm:
            asyncio.run(cp.get_service("svc-1"))
        self.assertIn("Sandbox not found with id: svc-1", str(cm.exception))

    def test_get_deployment_extracts_env_metadata_proxy_ports(self):
        cp = AsyncControlPlane(self._clients())
        deployment = asyncio.run(cp.get_deployment("depl-active"))
        self.assertEqual(deployment.env, {"SANDBOX_SECRET": "sec"})
        self.assertEqual(deployment.sandbox_url, "https://sb.example")
        self.assertEqual(
            deployment.proxy_ports, [{"port": 3031, "host": "h", "public_port": 1}]
        )

    def test_list_services_filters_sandbox_type_with_string_pagination(self):
        calls = []

        async def list_services(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(services=[_fake_service()], count=1, has_next=False)

        cp = AsyncControlPlane(
            self._clients(services=SimpleNamespace(list_services=list_services))
        )
        services, count = asyncio.run(
            cp.list_services(app_id="app-1", name="sb", offset=0, limit=100)
        )
        self.assertEqual(count, 1)
        self.assertEqual(calls[0]["types"], ["SANDBOX"])
        self.assertEqual(calls[0]["limit"], "100")

    def test_deployment_definition_returns_wire_dict(self):
        cp = AsyncControlPlane(self._clients())
        self.assertEqual(
            asyncio.run(cp.deployment_definition("depl-active")),
            {"env": [{"key": "SANDBOX_SECRET", "value": "sec"}]},
        )

    def test_update_service_coerces_definition_dict(self):
        sent = []

        async def update_service(id, service):
            sent.append(service)
            return SimpleNamespace(service=SimpleNamespace(latest_deployment_id="depl-new"))

        cp = AsyncControlPlane(
            self._clients(services=SimpleNamespace(update_service=update_service))
        )
        new_id = asyncio.run(cp.update_service("svc-1", {"env": []}))
        self.assertEqual(new_id, "depl-new")
        self.assertIsNotNone(sent[0].definition)


if __name__ == "__main__":
    unittest.main()
