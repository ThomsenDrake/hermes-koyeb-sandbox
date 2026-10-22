# coding: utf-8

"""Control-plane client bundles, their (token, host) caches, and the
sandbox executor client factories."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from koyeb.api import ApiClient, Configuration
from koyeb.api.api import (
    AppsApi,
    CatalogInstancesApi,
    DeploymentsApi,
    InstancesApi,
    InstanceSnapshotsApi,
    PoolClaimsApi,
    SecretsApi,
    ServicePoolsApi,
    ServicesApi,
)
from koyeb.api_async import ApiClient as AsyncApiClient
from koyeb.api_async import Configuration as AsyncConfiguration
from koyeb.api_async.api import (
    AppsApi as AsyncAppsApi,
    CatalogInstancesApi as AsyncCatalogInstancesApi,
    DeploymentsApi as AsyncDeploymentsApi,
    InstancesApi as AsyncInstancesApi,
    InstanceSnapshotsApi as AsyncInstanceSnapshotsApi,
    PoolClaimsApi as AsyncPoolClaimsApi,
    SecretsApi as AsyncSecretsApi,
    ServicePoolsApi as AsyncServicePoolsApi,
    ServicesApi as AsyncServicesApi,
)

from .errors import MissingApiTokenError, SandboxError

if TYPE_CHECKING:
    from .executor_client import ConnectionInfo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiClients:
    """Bundle of Koyeb API clients sharing a single underlying ApiClient."""

    apps: AppsApi
    services: ServicesApi
    instances: InstancesApi
    catalog_instances: CatalogInstancesApi
    deployments: DeploymentsApi
    secrets: SecretsApi
    instance_snapshots: Any
    service_pools: ServicePoolsApi
    pool_claims: PoolClaimsApi


_api_clients_cache: Dict[Tuple[str, str], ApiClients] = {}


def get_api_clients(
    api_token: Optional[str] = None, host: Optional[str] = None
) -> ApiClients:
    """
    Get configured API clients for Koyeb operations.

    Caches clients by (token, host) to reuse the underlying HTTP connection pool.

    Args:
        api_token: Koyeb API token. If not provided, will try to get from KOYEB_API_TOKEN env var
        host: Koyeb API host URL. If not provided, will try to get from KOYEB_API_HOST env var (defaults to https://app.koyeb.com)

    Returns:
        ApiClients with apps, services, instances, catalog_instances, deployments, and secrets attributes

    Raises:
        ValueError: If API token is not provided
    """
    token = api_token or os.getenv("KOYEB_API_TOKEN")
    if not token:
        raise MissingApiTokenError()

    api_host = os.getenv("KOYEB_API_HOST", host)
    if not api_host:
        api_host = "https://app.koyeb.com"
    cache_key = (token, api_host)

    if cache_key in _api_clients_cache:
        return _api_clients_cache[cache_key]

    configuration = Configuration(host=api_host)
    configuration.api_key["Bearer"] = token
    configuration.api_key_prefix["Bearer"] = "Bearer"

    api_client = ApiClient(configuration)
    clients = ApiClients(
        apps=AppsApi(api_client),
        services=ServicesApi(api_client),
        instances=InstancesApi(api_client),
        catalog_instances=CatalogInstancesApi(api_client),
        deployments=DeploymentsApi(api_client),
        secrets=SecretsApi(api_client),
        instance_snapshots=InstanceSnapshotsApi(api_client),
        service_pools=ServicePoolsApi(api_client),
        pool_claims=PoolClaimsApi(api_client),
    )
    _api_clients_cache[cache_key] = clients
    return clients


# --- Async API clients ---


@dataclass(frozen=True)
class AsyncApiClients:
    """Bundle of async Koyeb API clients sharing a single underlying AsyncApiClient."""

    apps: AsyncAppsApi
    services: AsyncServicesApi
    instances: AsyncInstancesApi
    catalog_instances: AsyncCatalogInstancesApi
    deployments: AsyncDeploymentsApi
    secrets: AsyncSecretsApi
    instance_snapshots: Any
    service_pools: AsyncServicePoolsApi
    pool_claims: AsyncPoolClaimsApi


_async_api_clients_cache: Dict[Tuple[str, str], AsyncApiClients] = {}


def get_async_api_clients(
    api_token: Optional[str] = None, host: Optional[str] = None
) -> AsyncApiClients:
    """
    Get configured async API clients for Koyeb operations.

    Caches clients by (token, host) to reuse the underlying HTTP connection pool.

    Args:
        api_token: Koyeb API token. If not provided, will try to get from KOYEB_API_TOKEN env var
        host: Koyeb API host URL. If not provided, will try to get from KOYEB_API_HOST env var

    Returns:
        AsyncApiClients with async API client instances

    Raises:
        ValueError: If API token is not provided
    """
    token = api_token or os.getenv("KOYEB_API_TOKEN")
    if not token:
        raise MissingApiTokenError()

    api_host = os.getenv("KOYEB_API_HOST", host)
    if not api_host:
        api_host = "https://app.koyeb.com"
    cache_key = (token, api_host)

    if cache_key in _async_api_clients_cache:
        return _async_api_clients_cache[cache_key]

    configuration = AsyncConfiguration(host=api_host)
    configuration.api_key["Bearer"] = token
    configuration.api_key_prefix["Bearer"] = "Bearer"

    api_client = AsyncApiClient(configuration)
    clients = AsyncApiClients(
        apps=AsyncAppsApi(api_client),
        services=AsyncServicesApi(api_client),
        instances=AsyncInstancesApi(api_client),
        catalog_instances=AsyncCatalogInstancesApi(api_client),
        deployments=AsyncDeploymentsApi(api_client),
        secrets=AsyncSecretsApi(api_client),
        instance_snapshots=AsyncInstanceSnapshotsApi(api_client),
        service_pools=AsyncServicePoolsApi(api_client),
        pool_claims=AsyncPoolClaimsApi(api_client),
    )
    _async_api_clients_cache[cache_key] = clients
    return clients


def create_sandbox_client(
    conn_info: Optional["ConnectionInfo"],
    existing_client: Optional[Any] = None,
) -> Any:
    """
    Create or return existing SandboxClient instance with validation.

    Helper function to create SandboxClient instances with consistent validation.
    Used by Sandbox, SandboxExecutor, and SandboxFilesystem to avoid duplication.

    Args:
        conn_info: The information needed to connect to the sandbox executor API
        existing_client: Existing client instance to return if not None

    Returns:
        SandboxClient: Configured client instance

    Raises:
        SandboxError: If sandbox URL or secret is not available
    """
    if existing_client is not None:
        return existing_client

    try:
        conn_info.validate()
    except ValueError as e:
        raise SandboxError(str(e))

    from .executor_client import SandboxClient

    return SandboxClient(conn_info)


def create_async_sandbox_client(
    conn_info: Optional["ConnectionInfo"],
    existing_client: Optional[Any] = None,
) -> Any:
    """
    Create or return existing AsyncSandboxClient instance with validation.

    Helper function to create AsyncSandboxClient instances with consistent validation.
    Used by AsyncSandbox to avoid duplication.

    Args:
        conn_info: The information needed to connect to the sandbox executor API
        existing_client: Existing client instance to return if not None

    Returns:
        AsyncSandboxClient: Configured async client instance

    Raises:
        SandboxError: If sandbox URL or secret is not available
    """
    if existing_client is not None:
        return existing_client

    try:
        conn_info.validate()
    except ValueError as e:
        raise SandboxError(str(e))

    from .executor_client import AsyncSandboxClient

    return AsyncSandboxClient(conn_info)
