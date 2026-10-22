import asyncio
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from koyeb.api.exceptions import ApiException
from koyeb.api_async.exceptions import ApiException as AsyncApiException
from koyeb.api.models.deployment_definition_type import DeploymentDefinitionType
from koyeb.api.models.deployment_mesh import DeploymentMesh
from koyeb.api.models.deployment_port import DeploymentPort
from koyeb.api.models.deployment_route import DeploymentRoute
from koyeb.api.models.pool_claim_status import PoolClaimStatus
from koyeb.api.models.service_pool_status import ServicePoolStatus
from koyeb.api.models.service_status import ServiceStatus

from koyeb.sandbox.pool import (
    AsyncServicePool,
    ClaimResult,
    ServicePool,
    claim,
    claim_async,
    get_claim,
    get_claim_async,
    list_claims,
    list_claims_async,
    wait_claim_ready,
    wait_claim_ready_async,
)
from koyeb.sandbox.errors import (
    PoolClaimError,
    SandboxError,
    ServicePoolError,
    ServiceTerminalStateError,
)


def _pool_model(ready_count=1, size=2):
    return SimpleNamespace(
        id="pool-1",
        name="my-pool",
        size=size,
        ready_count=ready_count,
        status=ServicePoolStatus.READY,
        definition=None,
    )


_POOL_CLAIM = SimpleNamespace(
    id="claim-1",
    pool_id="pool-1",
    service_id="svc-1",
    request_id="req-1",
    status=PoolClaimStatus.FULFILLED,
)


class FakeServicePoolsApi:
    def __init__(self, pool=None, pools=None):
        self.created = []
        self.updates = []
        self.deleted = []
        self.got = []
        self.list_kwargs = None
        self._pool = pool or _pool_model()
        self._pools = pools if pools is not None else [_pool_model()]

    def create_service_pool(self, service_pool):
        self.created.append(service_pool)
        return SimpleNamespace(service_pool=self._pool)

    def get_service_pool(self, id):
        self.got.append(id)
        return SimpleNamespace(service_pool=self._pool)

    def list_service_pools(self, **kwargs):
        self.list_kwargs = kwargs
        return SimpleNamespace(
            service_pools=self._pools, count=len(self._pools), has_next=False
        )

    def update_service_pool(self, id, service_pool, update_mask=None):
        self.updates.append((id, service_pool, update_mask))
        return SimpleNamespace(service_pool=self._pool)

    def delete_service_pool(self, id):
        self.deleted.append(id)


class FakePoolClaimsApi:
    def __init__(self, reply=None, excs=None):
        self.claim_bodies = []
        self.last_get = None
        self.last_list = None
        self._reply = reply or SimpleNamespace(
            claim_id="claim-1", service_id="svc-1", prewarmed=True
        )
        self._excs = list(excs or [])

    def claim(self, body):
        self.claim_bodies.append(body)
        if self._excs:
            exc = self._excs.pop(0)
            if exc is not None:
                raise exc
        return self._reply

    def get_claim(self, claim_id):
        self.last_get = claim_id
        return SimpleNamespace(claim=_POOL_CLAIM)

    def list_claim(self, **kwargs):
        self.last_list = kwargs
        return SimpleNamespace(claims=[_POOL_CLAIM], count=1, has_next=False)


class FakeServicesApi:
    def __init__(self, statuses=None, excs=None):
        self.calls = 0
        self._statuses = list(statuses or [])
        self._excs = list(excs or [])

    def get_service(self, id):
        self.calls += 1
        if self._excs:
            exc = self._excs.pop(0)
            if exc is not None:
                raise exc
        status = self._statuses.pop(0) if self._statuses else ServiceStatus.STARTING
        return SimpleNamespace(service=SimpleNamespace(status=status))


def _fake_sync_clients(pools_api=None, claims_api=None, services_api=None):
    return SimpleNamespace(
        service_pools=pools_api or FakeServicePoolsApi(),
        pool_claims=claims_api or FakePoolClaimsApi(),
        services=services_api or FakeServicesApi(),
    )


class FakeAsyncServicePoolsApi(FakeServicePoolsApi):
    async def create_service_pool(self, service_pool):
        return FakeServicePoolsApi.create_service_pool(self, service_pool)

    async def get_service_pool(self, id):
        return FakeServicePoolsApi.get_service_pool(self, id)

    async def list_service_pools(self, **kwargs):
        return FakeServicePoolsApi.list_service_pools(self, **kwargs)

    async def update_service_pool(self, id, service_pool, update_mask=None):
        return FakeServicePoolsApi.update_service_pool(
            self, id, service_pool, update_mask
        )

    async def delete_service_pool(self, id):
        return FakeServicePoolsApi.delete_service_pool(self, id)


class FakeAsyncPoolClaimsApi(FakePoolClaimsApi):
    async def claim(self, body):
        return FakePoolClaimsApi.claim(self, body)

    async def get_claim(self, claim_id):
        return FakePoolClaimsApi.get_claim(self, claim_id)

    async def list_claim(self, **kwargs):
        return FakePoolClaimsApi.list_claim(self, **kwargs)


class FakeAsyncServicesApi(FakeServicesApi):
    async def get_service(self, id):
        return FakeServicesApi.get_service(self, id)


def _fake_async_clients(pools_api=None, claims_api=None, services_api=None):
    return SimpleNamespace(
        service_pools=pools_api or FakeAsyncServicePoolsApi(),
        pool_claims=claims_api or FakeAsyncPoolClaimsApi(),
        services=services_api or FakeAsyncServicesApi(),
    )


class TestServicePoolCreate(unittest.TestCase):
    def test_create_builds_sandbox_definition_with_defaults(self):
        pools = FakeServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(pools_api=pools),
        ):
            pool = ServicePool.create(name="my-pool", api_token="tok")
        body = pools.created[0]
        self.assertEqual(body.name, "my-pool")
        self.assertEqual(body.size, 1)
        self.assertEqual(body.definition.type, DeploymentDefinitionType.SANDBOX)
        # SANDBOX auto-wiring: the sandbox pair owns ports and routes, and the
        # platform mints the executor secret — the definition carries none.
        self.assertEqual([p.port for p in body.definition.ports], [3030, 3031])
        self.assertEqual(
            [(r.port, r.path) for r in body.definition.routes],
            [(3030, "/koyeb-sandbox/"), (3031, "/")],
        )
        env_keys = [e.key for e in body.definition.env]
        self.assertNotIn("SANDBOX_SECRET", env_keys)
        self.assertEqual(pool.id, "pool-1")
        self.assertEqual(pool.name, "my-pool")
        self.assertEqual(pool.ready_count, 1)
        self.assertEqual(pool.status, ServicePoolStatus.READY)

    def test_create_sandbox_pool_rejects_explicit_ports_and_routes(self):
        pools = FakeServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(pools_api=pools),
        ):
            for kwargs in (
                {"ports": [{"port": 8080, "protocol": "http"}]},
                {"routes": [{"port": 8080, "path": "/"}]},
            ):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(ServicePoolError) as cm:
                        ServicePool.create(name="p", api_token="tok", **kwargs)
                    self.assertIn("SANDBOX", str(cm.exception))
                    self.assertIn("3030", str(cm.exception))
        self.assertEqual(pools.created, [])

    def test_create_non_sandbox_pool_rejects_sandbox_only_options(self):
        pools = FakeServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(pools_api=pools),
        ):
            for kwargs in (
                {"type": "WEB", "exposed_port_protocol": "http2"},
                {"type": "WEB", "enable_tcp_proxy": True},
            ):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(ServicePoolError) as cm:
                        ServicePool.create(name="p", api_token="tok", **kwargs)
                    self.assertIn("sandbox", str(cm.exception).lower())
                    self.assertIn("WEB", str(cm.exception))
        self.assertEqual(pools.created, [])

    def test_create_explicit_sandbox_secret_rides_env_verbatim(self):
        pools = FakeServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(pools_api=pools),
        ):
            ServicePool.create(
                name="sbx-pool",
                env={"SANDBOX_SECRET": "tok-1", "A": "b"},
                api_token="tok",
            )
        env = {e.key: e.value for e in pools.created[0].definition.env}
        self.assertEqual(env["SANDBOX_SECRET"], "tok-1")
        self.assertEqual(env["A"], "b")

    def test_create_explicit_size(self):
        pools = FakeServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(pools_api=pools),
        ):
            ServicePool.create(name="p", size=3, api_token="tok")
        self.assertEqual(pools.created[0].size, 3)

    def test_create_type_web_has_no_sandbox_wiring(self):
        pools = FakeServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(pools_api=pools),
        ):
            ServicePool.create(name="web-pool", type="WEB", api_token="tok")
        definition = pools.created[0].definition
        self.assertEqual(definition.type, DeploymentDefinitionType.WEB)
        self.assertIsNone(definition.ports)
        self.assertIsNone(definition.routes)
        self.assertEqual(definition.mesh, DeploymentMesh.DEPLOYMENT_MESH_AUTO)

    def test_create_web_pool_carries_user_ports_and_routes_verbatim(self):
        pools = FakeServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(pools_api=pools),
        ):
            ServicePool.create(
                name="web-pool",
                type="WEB",
                ports=[{"port": 8080, "protocol": "http"}],
                routes=[{"port": 8080, "path": "/api"}],
                api_token="tok",
            )
        definition = pools.created[0].definition
        self.assertEqual(len(definition.ports), 1)
        self.assertEqual(definition.ports[0].port, 8080)
        self.assertEqual(definition.ports[0].protocol, "http")
        self.assertEqual(len(definition.routes), 1)
        self.assertEqual(definition.routes[0].port, 8080)
        self.assertEqual(definition.routes[0].path, "/api")
        env_keys = [e.key for e in definition.env]
        self.assertNotIn("SANDBOX_SECRET", env_keys)

    def test_create_docker_overrides_pass_through(self):
        pools = FakeServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(pools_api=pools),
        ):
            ServicePool.create(
                name="web-pool",
                type="WEB",
                entrypoint=["/bin/sh", "-c"],
                command="python app.py",
                args=["--port", "8080"],
                api_token="tok",
            )
        docker = pools.created[0].definition.docker
        self.assertEqual(docker.entrypoint, ["/bin/sh", "-c"])
        self.assertEqual(docker.command, "python app.py")
        self.assertEqual(docker.args, ["--port", "8080"])

    def test_create_type_database_rejected_before_api_call(self):
        pools = FakeServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(pools_api=pools),
        ):
            with self.assertRaises(ServicePoolError) as cm:
                ServicePool.create(name="db-pool", type="DATABASE", api_token="tok")
        self.assertIn("DATABASE", str(cm.exception))
        self.assertIn("WEB", str(cm.exception))
        self.assertEqual(pools.created, [])

    def test_create_unknown_type_rejected(self):
        pools = FakeServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(pools_api=pools),
        ):
            with self.assertRaises(ServicePoolError) as cm:
                ServicePool.create(name="p", type="SIDEKICK", api_token="tok")
        self.assertIn("SIDEKICK", str(cm.exception))
        self.assertIn("WEB", str(cm.exception))
        self.assertEqual(pools.created, [])


class TestServicePoolCrud(unittest.TestCase):
    def _pool(self, pools):
        return ServicePool(
            id="pool-1",
            name="my-pool",
            size=2,
            ready_count=1,
            status=ServicePoolStatus.READY,
            api_token="tok",
        )

    def test_get(self):
        pools = FakeServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(pools_api=pools),
        ):
            pool = ServicePool.get("pool-1", api_token="tok")
        self.assertEqual(pools.got, ["pool-1"])
        self.assertEqual(pool.id, "pool-1")

    def test_list(self):
        pools = FakeServicePoolsApi(pools=[_pool_model(), _pool_model()])
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(pools_api=pools),
        ):
            found = ServicePool.list(api_token="tok")
        self.assertEqual(len(found), 2)
        self.assertEqual(pools.list_kwargs.get("name"), None)

    def test_update_size(self):
        pools = FakeServicePoolsApi()
        pool = self._pool(pools)
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(pools_api=pools),
        ):
            updated = pool.update(size=5)
        pool_id, body, update_mask = pools.updates[0]
        self.assertEqual(pool_id, "pool-1")
        self.assertEqual(body.size, 5)
        self.assertEqual(update_mask, "size")
        self.assertEqual(updated.size, 2)  # mapped from reply model

    def test_delete(self):
        pools = FakeServicePoolsApi()
        pool = self._pool(pools)
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(pools_api=pools),
        ):
            pool.delete()
        self.assertEqual(pools.deleted, ["pool-1"])

    def test_delete_failure_wraps_as_service_pool_error(self):
        class FailingPools(FakeServicePoolsApi):
            def delete_service_pool(self, id):
                raise ApiException(status=500, reason="boom")

        pool = ServicePool(
            id="pool-1",
            name="p",
            size=1,
            ready_count=0,
            status=ServicePoolStatus.READY,
            api_token="tok",
        )
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(pools_api=FailingPools()),
        ):
            with self.assertRaises(ServicePoolError):
                pool.delete()

    def test_refresh_updates_fields(self):
        pools = FakeServicePoolsApi(pool=_pool_model(ready_count=7))
        pool = self._pool(pools)
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(pools_api=pools),
        ):
            pool.refresh()
        self.assertEqual(pool.ready_count, 7)

    def test_claims_delegates_with_filters(self):
        claims = FakePoolClaimsApi()
        pool = self._pool(None)
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(claims_api=claims),
        ):
            found = pool.claims(status="FULFILLED", limit=10, offset=0)
        self.assertEqual(claims.last_list.get("pool_id"), "pool-1")
        self.assertEqual(claims.last_list.get("status"), "FULFILLED")
        self.assertEqual(claims.last_list.get("limit"), "10")
        self.assertEqual(claims.last_list.get("offset"), "0")
        self.assertEqual(found, [_POOL_CLAIM])


class TestClaim(unittest.TestCase):
    def test_claim_happy_path(self):
        claims = FakePoolClaimsApi()
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(claims_api=claims),
        ):
            result = claim("pool-1", api_token="tok")
        self.assertIsInstance(result, ClaimResult)
        self.assertEqual(result.claim_id, "claim-1")
        self.assertEqual(result.pool_id, "pool-1")
        self.assertEqual(result.service_id, "svc-1")
        self.assertTrue(result.prewarmed)
        # default request_id is a generated UUID, echoed on the request body
        self.assertEqual(claims.claim_bodies[0].request_id, result.request_id)
        uuid.UUID(result.request_id)  # valid UUID

    def test_claim_retry_preserves_request_id(self):
        claims = FakePoolClaimsApi(
            excs=[
                ApiException(status=429, reason="Too Many Requests"),
                ApiException(status=503, reason="Service Unavailable"),
            ]
        )
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(claims_api=claims),
        ):
            with patch("koyeb.sandbox.pool.time.sleep") as sleep:
                result = claim("pool-1", api_token="tok")
        self.assertEqual(len(claims.claim_bodies), 3)
        request_ids = {b.request_id for b in claims.claim_bodies}
        self.assertEqual(len(request_ids), 1)
        self.assertEqual(request_ids.pop(), result.request_id)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 2.0])

    def test_claim_missing_service_id_raises(self):
        claims = FakePoolClaimsApi(
            reply=SimpleNamespace(claim_id="claim-1", service_id=None, prewarmed=None)
        )
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(claims_api=claims),
        ):
            with self.assertRaises(PoolClaimError):
                claim("pool-1", api_token="tok")

    def test_claim_non_retryable_status_raises_immediately(self):
        claims = FakePoolClaimsApi(
            excs=[ApiException(status=400, reason="Bad Request")]
        )
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(claims_api=claims),
        ):
            with patch("koyeb.sandbox.pool.time.sleep") as sleep:
                with self.assertRaises(PoolClaimError):
                    claim("pool-1", api_token="tok")
        self.assertEqual(len(claims.claim_bodies), 1)
        sleep.assert_not_called()

    def test_claim_exhausted_wraps_last_error(self):
        claims = FakePoolClaimsApi(excs=[ApiException(status=429, reason="slow")] * 3)
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(claims_api=claims),
        ):
            with patch("koyeb.sandbox.pool.time.sleep") as sleep:
                with self.assertRaises(PoolClaimError) as cm:
                    claim("pool-1", api_token="tok")
        self.assertIsInstance(cm.exception.__cause__, ApiException)
        self.assertIsInstance(cm.exception, SandboxError)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 2.0])


class TestClaimHelpers(unittest.TestCase):
    def test_get_claim(self):
        claims = FakePoolClaimsApi()
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(claims_api=claims),
        ):
            found = get_claim("claim-1", api_token="tok")
        self.assertEqual(claims.last_get, "claim-1")
        self.assertEqual(found, _POOL_CLAIM)

    def test_list_claims_converts_pagination_to_strings(self):
        claims = FakePoolClaimsApi()
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(claims_api=claims),
        ):
            found = list_claims(
                "pool-1", status="PENDING", limit=5, offset=0, api_token="tok"
            )
        self.assertEqual(claims.last_list.get("pool_id"), "pool-1")
        self.assertEqual(claims.last_list.get("status"), "PENDING")
        self.assertEqual(claims.last_list.get("limit"), "5")
        self.assertEqual(claims.last_list.get("offset"), "0")
        self.assertEqual(found, [_POOL_CLAIM])


class TestWaitClaimReady(unittest.TestCase):
    def test_ready_after_progress(self):
        services = FakeServicesApi(
            statuses=[ServiceStatus.STARTING, ServiceStatus.HEALTHY]
        )
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(services_api=services),
        ):
            with patch("koyeb.sandbox.pool.time.sleep") as sleep:
                ready = wait_claim_ready(
                    "svc-1", timeout=10, poll_interval=2, api_token="tok"
                )
        self.assertTrue(ready)
        self.assertEqual(services.calls, 2)
        sleep.assert_called_once_with(2)

    def test_accepts_claim_result(self):
        services = FakeServicesApi(statuses=[ServiceStatus.HEALTHY])
        result = ClaimResult(
            claim_id="c",
            pool_id="pool-1",
            request_id="r",
            service_id="svc-9",
            prewarmed=False,
        )
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(services_api=services),
        ):
            ready = wait_claim_ready(
                result, timeout=10, poll_interval=1, api_token="tok"
            )
        self.assertTrue(ready)

    def test_terminal_state_raises(self):
        services = FakeServicesApi(statuses=[ServiceStatus.UNHEALTHY])
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(services_api=services),
        ):
            with self.assertRaises(ServiceTerminalStateError) as cm:
                wait_claim_ready("svc-1", timeout=10, poll_interval=1, api_token="tok")
        self.assertIn("svc-1", str(cm.exception))
        self.assertIn("UNHEALTHY", str(cm.exception))

    def test_transient_get_service_error_is_retried(self):
        services = FakeServicesApi(
            excs=[ApiException(status=500, reason="boom")],
            statuses=[ServiceStatus.HEALTHY],
        )
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(services_api=services),
        ):
            with patch("koyeb.sandbox.pool.time.sleep"):
                ready = wait_claim_ready(
                    "svc-1", timeout=10, poll_interval=1, api_token="tok"
                )
        self.assertTrue(ready)

    def test_timeout_returns_false(self):
        services = FakeServicesApi(statuses=[ServiceStatus.STARTING] * 50)
        with patch(
            "koyeb.sandbox.pool.get_api_clients",
            return_value=_fake_sync_clients(services_api=services),
        ):
            with patch("koyeb.sandbox.pool.time.sleep"):
                with patch(
                    "koyeb.sandbox.pool.time.time", side_effect=list(range(0, 1000))
                ):
                    ready = wait_claim_ready(
                        "svc-1", timeout=10, poll_interval=1, api_token="tok"
                    )
        self.assertFalse(ready)


class TestAsyncPoolMirror(unittest.TestCase):
    def test_async_create(self):
        pools = FakeAsyncServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_async_api_clients",
            return_value=_fake_async_clients(pools_api=pools),
        ):
            pool = asyncio.run(AsyncServicePool.create(name="p", api_token="tok"))
        body = pools.created[0]
        self.assertEqual(body.size, 1)
        self.assertEqual(body.name, "p")
        self.assertEqual(pool.id, "pool-1")

    def test_async_create_type_worker_has_no_sandbox_wiring(self):
        pools = FakeAsyncServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_async_api_clients",
            return_value=_fake_async_clients(pools_api=pools),
        ):
            asyncio.run(
                AsyncServicePool.create(name="p", type="WORKER", api_token="tok")
            )
        definition = pools.created[0].definition
        self.assertEqual(definition.type, DeploymentDefinitionType.WORKER)
        self.assertIsNone(definition.ports)
        self.assertIsNone(definition.routes)

    def test_async_create_worker_pool_carries_user_ports_and_routes_verbatim(self):
        pools = FakeAsyncServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_async_api_clients",
            return_value=_fake_async_clients(pools_api=pools),
        ):
            asyncio.run(
                AsyncServicePool.create(
                    name="p",
                    type="WORKER",
                    ports=[DeploymentPort(port=9000, protocol="http")],
                    routes=[DeploymentRoute(port=9000, path="/w")],
                    api_token="tok",
                )
            )
        definition = pools.created[0].definition
        self.assertEqual(definition.ports[0].port, 9000)
        self.assertEqual(definition.routes[0].path, "/w")

    def test_async_create_sandbox_pool_rejects_explicit_ports(self):
        pools = FakeAsyncServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_async_api_clients",
            return_value=_fake_async_clients(pools_api=pools),
        ):
            with self.assertRaises(ServicePoolError) as cm:
                asyncio.run(
                    AsyncServicePool.create(
                        name="p",
                        ports=[{"port": 8080, "protocol": "http"}],
                        api_token="tok",
                    )
                )
        self.assertIn("SANDBOX", str(cm.exception))
        self.assertEqual(pools.created, [])

    def test_async_create_non_sandbox_pool_rejects_tcp_proxy(self):
        pools = FakeAsyncServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_async_api_clients",
            return_value=_fake_async_clients(pools_api=pools),
        ):
            with self.assertRaises(ServicePoolError):
                asyncio.run(
                    AsyncServicePool.create(
                        name="p",
                        type="WORKER",
                        enable_tcp_proxy=True,
                        api_token="tok",
                    )
                )
        self.assertEqual(pools.created, [])

    def test_async_create_type_database_rejected_before_api_call(self):
        pools = FakeAsyncServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_async_api_clients",
            return_value=_fake_async_clients(pools_api=pools),
        ):
            with self.assertRaises(ServicePoolError) as cm:
                asyncio.run(
                    AsyncServicePool.create(name="p", type="DATABASE", api_token="tok")
                )
        self.assertIn("DATABASE", str(cm.exception))
        self.assertIn("WEB", str(cm.exception))
        self.assertEqual(pools.created, [])

    def test_async_create_sandbox_defaults_no_client_secret(self):
        pools = FakeAsyncServicePoolsApi()
        with patch(
            "koyeb.sandbox.pool.get_async_api_clients",
            return_value=_fake_async_clients(pools_api=pools),
        ):
            asyncio.run(AsyncServicePool.create(name="p", api_token="tok"))
        definition = pools.created[0].definition
        self.assertEqual(definition.type, DeploymentDefinitionType.SANDBOX)
        self.assertEqual([p.port for p in definition.ports], [3030, 3031])
        env_keys = [e.key for e in definition.env]
        self.assertNotIn("SANDBOX_SECRET", env_keys)

    def test_async_claim_retry_preserves_request_id(self):
        claims = FakeAsyncPoolClaimsApi(
            excs=[
                AsyncApiException(status=500, reason="boom"),
                AsyncApiException(status=429, reason="slow"),
            ]
        )
        with patch(
            "koyeb.sandbox.pool.get_async_api_clients",
            return_value=_fake_async_clients(claims_api=claims),
        ):
            with patch("koyeb.sandbox.pool.asyncio.sleep") as sleep:

                async def run():
                    return await claim_async("pool-1", api_token="tok")

                result = asyncio.run(run())
        self.assertEqual(len(claims.claim_bodies), 3)
        request_ids = {b.request_id for b in claims.claim_bodies}
        self.assertEqual(len(request_ids), 1)
        self.assertEqual(request_ids.pop(), result.request_id)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 2.0])

    def test_async_get_and_list_claims(self):
        claims = FakeAsyncPoolClaimsApi()
        with patch(
            "koyeb.sandbox.pool.get_async_api_clients",
            return_value=_fake_async_clients(claims_api=claims),
        ):

            async def run():
                got = await get_claim_async("claim-1", api_token="tok")
                listed = await list_claims_async(
                    "pool-1", status="PENDING", limit=5, offset=0, api_token="tok"
                )
                return got, listed

            got, listed = asyncio.run(run())
        self.assertEqual(got, _POOL_CLAIM)
        self.assertEqual(listed, [_POOL_CLAIM])
        self.assertEqual(claims.last_list.get("limit"), "5")

    def test_async_wait_claim_ready_terminal(self):
        services = FakeAsyncServicesApi(statuses=[ServiceStatus.UNHEALTHY])
        with patch(
            "koyeb.sandbox.pool.get_async_api_clients",
            return_value=_fake_async_clients(services_api=services),
        ):

            async def run():
                return await wait_claim_ready_async(
                    "svc-1", timeout=10, poll_interval=1, api_token="tok"
                )

            with self.assertRaises(ServiceTerminalStateError):
                asyncio.run(run())

    def test_async_pool_update_delete_refresh(self):
        pools = FakeAsyncServicePoolsApi(pool=_pool_model(ready_count=9))
        pool = AsyncServicePool(
            id="pool-1",
            name="my-pool",
            size=2,
            ready_count=1,
            status=ServicePoolStatus.READY,
            api_token="tok",
        )
        with patch(
            "koyeb.sandbox.pool.get_async_api_clients",
            return_value=_fake_async_clients(pools_api=pools),
        ):

            async def run():
                await pool.update(size=4)
                await pool.delete()
                await pool.refresh()

            asyncio.run(run())
        self.assertEqual(pools.updates[0][1].size, 4)
        self.assertEqual(pools.deleted, ["pool-1"])
        self.assertEqual(pool.ready_count, 9)


if __name__ == "__main__":
    unittest.main()
