# coding: utf-8

"""
The control-plane seam: the narrow interface between the sandbox layer
and the Koyeb API.

Two adapters share the orchestration above them — the generated
koyeb.api (sync) and koyeb.api_async (async) clients — so the sandbox
twins never touch model flavors again: neutral info types come out,
payload dicts go in. Errors map to SandboxError exactly where the
orchestration contract demands it (service lookup); raw ApiException
propagates where callers clean up (create/delete/update).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from koyeb.api.exceptions import ApiException, NotFoundException
from koyeb.api.models.create_app import CreateApp
from koyeb.api.models.create_service import CreateService
from koyeb.api.models.update_service import UpdateService
from koyeb.api_async.exceptions import ApiException as AsyncApiException
from koyeb.api_async.exceptions import (
    NotFoundException as AsyncNotFoundException,
)
from koyeb.api_async.models.create_app import CreateApp as AsyncCreateApp
from koyeb.api_async.models.create_service import CreateService as AsyncCreateService
from koyeb.api_async.models.update_service import UpdateService as AsyncUpdateService

from .errors import SandboxError


@dataclass(frozen=True)
class AppInfo:
    """Neutral app summary."""

    id: str
    name: str
    domains: List[str]


@dataclass(frozen=True)
class ServiceInfo:
    """Neutral service summary."""

    id: str
    app_id: str
    name: str
    active_deployment_id: Optional[str]
    latest_deployment_id: Optional[str]


@dataclass(frozen=True)
class DeploymentInfo:
    """Neutral deployment summary.

    status stays raw (enum or string) for classify_deployment_status;
    env is flattened for SANDBOX_SECRET extraction. The definition dict
    is its own seam op (deployment_definition) so wait-polling paths
    never pay for serialization.
    """

    id: str
    status: Any
    env: Dict[str, Optional[str]]
    sandbox_url: Optional[str]
    routing_key: Optional[str]
    proxy_ports: List[Dict[str, Any]]


def _deployment_info(deployment: Any) -> DeploymentInfo:
    """Model-flavor-agnostic conversion shared by both adapters."""
    definition = deployment.definition
    env: Dict[str, Optional[str]] = {}
    if definition is not None and definition.env:
        for env_var in definition.env:
            env[env_var.key] = env_var.value
    sandbox_url = None
    routing_key = None
    proxy_ports: List[Dict[str, Any]] = []
    metadata = getattr(deployment, "metadata", None)
    if metadata is not None:
        if metadata.sandbox is not None:
            sandbox_url = metadata.sandbox.public_url
            routing_key = metadata.sandbox.routing_key
        for port in metadata.proxy_ports or []:
            proxy_ports.append(
                {"port": port.port, "host": port.host, "public_port": port.public_port}
            )
    return DeploymentInfo(
        id=deployment.id,
        status=deployment.status,
        env=env,
        sandbox_url=sandbox_url,
        routing_key=routing_key,
        proxy_ports=proxy_ports,
    )


def _not_found(service_id: str) -> SandboxError:
    return SandboxError(f"Sandbox not found with id: {service_id}")


class SyncControlPlane:
    """Control-plane adapter over the generated koyeb.api clients."""

    def __init__(self, clients: Any):
        self._clients = clients

    def create_app(self, payload: Dict[str, Any]) -> str:
        reply = self._clients.apps.create_app(app=CreateApp(**payload))
        return reply.app.id

    def delete_app(self, app_id: str) -> None:
        self._clients.apps.delete_app(app_id)

    def get_app(self, app_id: str) -> AppInfo:
        app = self._clients.apps.get_app(app_id).app
        domains = [d.name for d in getattr(app, "domains", None) or []]
        return AppInfo(id=app.id, name=app.name, domains=domains)

    def create_service(self, payload: Dict[str, Any]) -> str:
        reply = self._clients.services.create_service(service=CreateService(**payload))
        return reply.service.id

    def get_service(self, service_id: str) -> ServiceInfo:
        try:
            service = self._clients.services.get_service(id=service_id).service
        except NotFoundException as e:
            raise _not_found(service_id) from e
        except ApiException as e:
            raise SandboxError(
                f"Failed to retrieve sandbox with id: {service_id}: {e}"
            ) from e
        if service is None:
            raise _not_found(service_id)
        return ServiceInfo(
            id=service.id,
            app_id=service.app_id,
            name=service.name,
            active_deployment_id=service.active_deployment_id,
            latest_deployment_id=service.latest_deployment_id,
        )

    def update_service(self, service_id: str, definition: Dict[str, Any]) -> Optional[str]:
        reply = self._clients.services.update_service(
            id=service_id, service=UpdateService(definition=definition)
        )
        if reply is not None and reply.service is not None:
            return reply.service.latest_deployment_id
        return None

    def delete_service(self, service_id: str) -> None:
        self._clients.services.delete_service(id=service_id)

    def list_services(
        self,
        offset: int,
        limit: int,
        name: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> Tuple[List[ServiceInfo], int]:
        reply = self._clients.services.list_services(
            app_id=app_id,
            name=name,
            types=["SANDBOX"],
            limit=str(limit),
            offset=str(offset),
        )
        services = [
            ServiceInfo(
                id=service.id,
                app_id=service.app_id,
                name=service.name,
                active_deployment_id=service.active_deployment_id,
                latest_deployment_id=service.latest_deployment_id,
            )
            for service in reply.services or []
        ]
        return services, reply.count or 0

    def get_deployment(self, deployment_id: str) -> DeploymentInfo:
        deployment = self._clients.deployments.get_deployment(id=deployment_id).deployment
        return _deployment_info(deployment)

    def deployment_definition(self, deployment_id: str) -> Dict[str, Any]:
        deployment = self._clients.deployments.get_deployment(id=deployment_id).deployment
        return deployment.definition.to_dict()


class AsyncControlPlane:
    """Control-plane adapter over the generated koyeb.api_async clients."""

    def __init__(self, clients: Any):
        self._clients = clients

    async def create_app(self, payload: Dict[str, Any]) -> str:
        reply = await self._clients.apps.create_app(app=AsyncCreateApp(**payload))
        return reply.app.id

    async def delete_app(self, app_id: str) -> None:
        await self._clients.apps.delete_app(app_id)

    async def get_app(self, app_id: str) -> AppInfo:
        app = (await self._clients.apps.get_app(app_id)).app
        domains = [d.name for d in getattr(app, "domains", None) or []]
        return AppInfo(id=app.id, name=app.name, domains=domains)

    async def create_service(self, payload: Dict[str, Any]) -> str:
        reply = await self._clients.services.create_service(
            service=AsyncCreateService(**payload)
        )
        return reply.service.id

    async def get_service(self, service_id: str) -> ServiceInfo:
        try:
            service = (await self._clients.services.get_service(id=service_id)).service
        except AsyncNotFoundException as e:
            raise _not_found(service_id) from e
        except AsyncApiException as e:
            raise SandboxError(
                f"Failed to retrieve sandbox with id: {service_id}: {e}"
            ) from e
        if service is None:
            raise _not_found(service_id)
        return ServiceInfo(
            id=service.id,
            app_id=service.app_id,
            name=service.name,
            active_deployment_id=service.active_deployment_id,
            latest_deployment_id=service.latest_deployment_id,
        )

    async def update_service(
        self, service_id: str, definition: Dict[str, Any]
    ) -> Optional[str]:
        reply = await self._clients.services.update_service(
            id=service_id, service=AsyncUpdateService(definition=definition)
        )
        if reply is not None and reply.service is not None:
            return reply.service.latest_deployment_id
        return None

    async def delete_service(self, service_id: str) -> None:
        await self._clients.services.delete_service(id=service_id)

    async def list_services(
        self,
        offset: int,
        limit: int,
        name: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> Tuple[List[ServiceInfo], int]:
        reply = await self._clients.services.list_services(
            app_id=app_id,
            name=name,
            types=["SANDBOX"],
            limit=str(limit),
            offset=str(offset),
        )
        services = [
            ServiceInfo(
                id=service.id,
                app_id=service.app_id,
                name=service.name,
                active_deployment_id=service.active_deployment_id,
                latest_deployment_id=service.latest_deployment_id,
            )
            for service in reply.services or []
        ]
        return services, reply.count or 0

    async def get_deployment(self, deployment_id: str) -> DeploymentInfo:
        deployment = (
            await self._clients.deployments.get_deployment(id=deployment_id)
        ).deployment
        return _deployment_info(deployment)

    async def deployment_definition(self, deployment_id: str) -> Dict[str, Any]:
        deployment = (
            await self._clients.deployments.get_deployment(id=deployment_id)
        ).deployment
        return deployment.definition.to_dict()
