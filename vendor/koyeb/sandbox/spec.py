# coding: utf-8

"""
SandboxSpec: the create vocabulary for Koyeb sandboxes.

One definition of a sandbox deployment — env/secret injection, mesh
tri-state, scale-to-zero sleep, ports/routes, snapshot branches. Every
create flow (sync and async, Sandbox and ServicePool) builds one spec and
consumes its payloads, so the decision logic lives here exactly once.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from koyeb.api.models.config_file import ConfigFile
from koyeb.api.models.deployment_definition import DeploymentDefinition
from koyeb.api.models.deployment_definition_type import DeploymentDefinitionType
from koyeb.api.models.deployment_env import DeploymentEnv
from koyeb.api.models.deployment_instance_type import DeploymentInstanceType
from koyeb.api.models.deployment_mesh import DeploymentMesh
from koyeb.api.models.deployment_port import DeploymentPort
from koyeb.api.models.deployment_proxy_port import DeploymentProxyPort
from koyeb.api.models.deployment_route import DeploymentRoute
from koyeb.api.models.deployment_scaling import DeploymentScaling
from koyeb.api.models.deployment_scaling_target import DeploymentScalingTarget
from koyeb.api.models.deployment_scaling_target_sleep_idle_delay import (
    DeploymentScalingTargetSleepIdleDelay,
)
from koyeb.api.models.docker_source import DockerSource
from koyeb.api.models.network_policy import NetworkPolicy
from koyeb.api.models.proxy_port_protocol import ProxyPortProtocol
from koyeb.api.models.secret import Secret

from .egress import build_network_policy

if TYPE_CHECKING:
    from .snapshot import SnapshotType

# Valid protocols for DeploymentPort (from OpenAPI spec: http, http2, tcp)
# For sandboxes, we only support http and http2
VALID_DEPLOYMENT_PORT_PROTOCOLS = ("http", "http2")


def _validate_port_protocol(protocol: str) -> str:
    """
    Validate port protocol using API model structure.

    Args:
        protocol: Protocol string to validate

    Returns:
        Validated protocol string

    Raises:
        ValueError: If protocol is invalid
    """
    # Validate by attempting to create a DeploymentPort instance
    # This ensures we're using the API model's validation structure
    try:
        port = DeploymentPort(port=3030, protocol=protocol)
        # Additional validation: check if protocol is in allowed values
        if protocol not in VALID_DEPLOYMENT_PORT_PROTOCOLS:
            raise ValueError(
                f"Invalid protocol '{protocol}'. Must be one of {VALID_DEPLOYMENT_PORT_PROTOCOLS}"
            )
        return port.protocol or "http"
    except Exception as e:
        if isinstance(e, ValueError):
            raise
        raise ValueError(
            f"Invalid protocol '{protocol}'. Must be one of {VALID_DEPLOYMENT_PORT_PROTOCOLS}"
        ) from e


def build_env_vars(env: Optional[Dict[str, Any]]) -> List[DeploymentEnv]:
    """
    Build environment variables list from dictionary.

    Args:
        env: Dictionary of environment variables. Values can be plain strings
            or ``Secret`` instances. A ``Secret`` value is rendered as
            ``"{{ secret.<name> }}"`` so the Koyeb API substitutes the secret
            value at deploy time.

    Returns:
        List of DeploymentEnv objects
    """
    env_vars = []
    if env:
        for key, value in env.items():
            if isinstance(value, Secret):
                value = "{{ secret." + value.name + " }}"
            env_vars.append(DeploymentEnv(key=key, value=value))
    return env_vars


DEFAULT_CONFIG_FILE_PERMISSIONS = "0644"


def build_config_files(
    config_files: Optional[Dict[str, Any]],
) -> List[ConfigFile]:
    """
    Build config files list from dictionary.

    Args:
        config_files: Dictionary mapping file paths to file contents.
            Values can be plain strings (default permissions 0644) or
            ``ConfigFile`` instances (custom permissions). The dict key is
            always used as the file path.

    Returns:
        List of ConfigFile objects
    """
    result = []
    if config_files:
        for path, value in config_files.items():
            if isinstance(value, ConfigFile):
                result.append(
                    ConfigFile(
                        path=path,
                        content=value.content,
                        permissions=value.permissions
                        or DEFAULT_CONFIG_FILE_PERMISSIONS,
                    )
                )
            else:
                result.append(
                    ConfigFile(
                        path=path,
                        content=value,
                        permissions=DEFAULT_CONFIG_FILE_PERMISSIONS,
                    )
                )
    return result


def create_docker_source(
    image: str,
    privileged: Optional[bool] = None,
    image_registry_secret: Optional[str] = None,
    entrypoint: Optional[List[str]] = None,
    command: Optional[str] = None,
    args: Optional[List[str]] = None,
) -> DockerSource:
    """
    Create Docker source configuration.

    Args:
        image: Docker image name
        privileged: If True, run the container in privileged mode (default: None/False)
        image_registry_secret: Name of the secret containing registry credentials
            for pulling private images
        entrypoint: Override the default entrypoint of the Docker image
        command: Override the default command of the Docker image
        args: Arguments to pass to the command

    Returns:
        DockerSource object
    """
    return DockerSource(
        image=image,
        command=command,
        args=args,
        privileged=privileged,
        image_registry_secret=image_registry_secret,
        entrypoint=entrypoint,
    )


def create_koyeb_sandbox_ports(protocol: str = "http") -> List[DeploymentPort]:
    """
    Create port configuration for koyeb/sandbox image.

    Creates two ports:
    - Port 3030 exposed on HTTP, mounted on /koyeb-sandbox/
    - Port 3031 exposed with the specified protocol, mounted on /

    Args:
        protocol: Protocol to use for port 3031 ("http" or "http2"), defaults to "http"

    Returns:
        List of DeploymentPort objects configured for koyeb/sandbox
    """
    return [
        DeploymentPort(
            port=3030,
            protocol="http",
        ),
        DeploymentPort(
            port=3031,
            protocol=protocol,
        ),
    ]


def create_koyeb_sandbox_proxy_ports() -> List[DeploymentProxyPort]:
    """
    Create TCP proxy port configuration for koyeb/sandbox image.

    Creates proxy port for direct TCP access:
    - Port 3031 exposed via TCP proxy

    Returns:
        List of DeploymentProxyPort objects configured for TCP proxy access
    """
    return [
        DeploymentProxyPort(
            port=3031,
            protocol=ProxyPortProtocol.TCP,
        ),
    ]


def create_koyeb_sandbox_routes() -> List[DeploymentRoute]:
    """
    Create route configuration for koyeb/sandbox image to make it publicly accessible.

    Creates two routes:
    - Port 3030 accessible at /koyeb-sandbox/
    - Port 3031 accessible at /

    Returns:
        List of DeploymentRoute objects configured for koyeb/sandbox
    """
    return [
        DeploymentRoute(port=3030, path="/koyeb-sandbox/"),
        DeploymentRoute(port=3031, path="/"),
    ]


def create_deployment_definition(
    name: str,
    docker_source: DockerSource,
    env_vars: List[DeploymentEnv],
    instance_type: str,
    definition_type: DeploymentDefinitionType = DeploymentDefinitionType.SANDBOX,
    ports: Optional[List[DeploymentPort]] = None,
    exposed_port_protocol: Optional[str] = None,
    region: Optional[str] = None,
    routes: Optional[List[DeploymentRoute]] = None,
    idle_timeout: int = 300,
    enable_tcp_proxy: bool = False,
    _experimental_enable_light_sleep: bool = False,
    _experimental_deep_sleep_value: int = 3900,
    enable_mesh: Optional[bool] = None,
    config_files: Optional[List[ConfigFile]] = None,
    network_policy: Optional[NetworkPolicy] = None,
) -> DeploymentDefinition:
    """
    Create deployment definition for a sandbox service.

    Args:
        name: Service name
        docker_source: Docker configuration
        env_vars: Environment variables
        instance_type: Instance type
        definition_type: Deployment definition type. SANDBOX (the default) keeps
            the sandbox auto-wiring (ports 3030/3031 and the sandbox routes);
            every other type carries only caller-supplied ports/routes.
        ports: Caller-supplied ports (non-SANDBOX definitions); SANDBOX always
            wires its own 3030/3031 pair.
        exposed_port_protocol: Protocol to expose ports with ("http" or "http2").
            If None, defaults to "http".
            If provided, must be one of "http" or "http2".
        region: Region to deploy to. Defaults to KOYEB_REGION env var, or "na" if not set.
        routes: List of routes for public access
        idle_timeout: Number of seconds to wait before sleeping the instance if it receives no traffic
        enable_tcp_proxy: If True, enables TCP proxy for direct TCP access to port 3031
        _experimental_enable_light_sleep: If True, uses light sleep when reaching idle_timeout.
            Light Sleep reduces cold starts to ~200ms. After scaling to zero, the service stays in Light Sleep for idle_timeout seconds before going into Deep Sleep.
        _experimental_deep_sleep_value: Number of seconds for deep sleep when light sleep is enabled (default: 3900).
            Only used if _experimental_enable_light_sleep is True. Ignored otherwise.
        enable_mesh: Mesh tri-state: None (default) = auto, True = enabled, False = disabled
        network_policy: Optional network policy restricting egress traffic

    Returns:
        DeploymentDefinition object
    """
    if region is None:
        region = os.getenv("KOYEB_REGION", "na")

    # Convert single region string to list for API
    regions_list = [region]

    if definition_type == DeploymentDefinitionType.SANDBOX:
        # The sandbox wiring owns its ports (3030/3031) and routes.
        protocol = (
            exposed_port_protocol if exposed_port_protocol is not None else "http"
        )
        protocol = _validate_port_protocol(protocol)
        ports = create_koyeb_sandbox_ports(protocol)
        routes = create_koyeb_sandbox_routes()

    # Create TCP proxy ports if enabled
    proxy_ports = None
    if enable_tcp_proxy:
        proxy_ports = create_koyeb_sandbox_proxy_ports()

    # Process idle_timeout
    if idle_timeout == 0:
        sleep_idle_delay = None
    elif _experimental_enable_light_sleep:
        # Experimental mode: idle_timeout sets light_sleep value, deep_sleep uses _experimental_deep_sleep_value
        sleep_idle_delay = DeploymentScalingTargetSleepIdleDelay(
            light_sleep_value=idle_timeout,
            deep_sleep_value=_experimental_deep_sleep_value,
        )
    else:
        # Normal mode: only use deep_sleep
        sleep_idle_delay = DeploymentScalingTargetSleepIdleDelay(
            deep_sleep_value=idle_timeout,
        )

    # Create scaling configuration
    # If idle_timeout is 0, explicitly disable scale-to-zero (min=1, always-on)
    # Otherwise (int > 0), enable scale-to-zero (min=0)
    min_scale = 1 if idle_timeout == 0 else 0
    targets = None
    if sleep_idle_delay is not None:
        scaling_target = DeploymentScalingTarget(sleep_idle_delay=sleep_idle_delay)
        targets = [scaling_target]

    scalings = [DeploymentScaling(min=min_scale, max=1, targets=targets)]

    # Set mesh configuration
    if enable_mesh is None:
        mesh = DeploymentMesh.DEPLOYMENT_MESH_AUTO
    elif enable_mesh:
        mesh = DeploymentMesh.DEPLOYMENT_MESH_ENABLED
    else:
        mesh = DeploymentMesh.DEPLOYMENT_MESH_DISABLED

    return DeploymentDefinition(
        name=name,
        type=definition_type,
        docker=docker_source,
        env=env_vars,
        ports=ports,
        proxy_ports=proxy_ports,
        routes=routes,
        instance_types=[DeploymentInstanceType(type=instance_type)],
        scalings=scalings,
        regions=regions_list,
        mesh=mesh,
        config_files=config_files if config_files else None,
        network_policy=network_policy,
    )


@dataclass
class SandboxSpec:
    """The single definition of a sandbox deployment.

    ``definition_type`` defaults to SANDBOX, which keeps the sandbox
    auto-wiring; pool flows set WEB/WORKER and carry their own ports and
    routes. Invalid egress or port protocol fails at construction, before
    any API call. Sandbox flows call apply_sandbox_secret() before
    deployment_definition(): the secret rides the env. Pool flows never
    inject one — the platform mints the executor secret.
    """

    name: str
    image: str = "koyeb/sandbox"
    instance_type: str = "micro"
    definition_type: DeploymentDefinitionType = DeploymentDefinitionType.SANDBOX
    exposed_port_protocol: Optional[str] = None
    env: Optional[Dict[str, Any]] = None
    config_files: Optional[Dict[str, Any]] = None
    region: Optional[str] = None
    idle_timeout: int = 300
    enable_tcp_proxy: bool = False
    privileged: bool = False
    registry_secret: Optional[str] = None
    enable_light_sleep: bool = False
    deep_sleep_value: int = 3900
    delete_after_delay: int = 0
    delete_after_inactivity_delay: int = 0
    enable_mesh: Optional[bool] = None
    entrypoint: Optional[List[str]] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    ports: Optional[List[DeploymentPort]] = None
    routes: Optional[List[DeploymentRoute]] = None
    block_network: bool = False
    outbound_allowlist: Optional[List[str]] = None
    snapshot_id: Optional[str] = None
    snapshot_type: Optional["SnapshotType"] = None

    network_policy: Optional[NetworkPolicy] = field(default=None, init=False)

    def __post_init__(self):
        self.network_policy = build_network_policy(
            self.block_network, self.outbound_allowlist
        )
        if self.exposed_port_protocol is not None:
            _validate_port_protocol(self.exposed_port_protocol)

    def apply_sandbox_secret(self, sandbox_secret: Optional[str] = None) -> str:
        """Generate when missing, inject into env, return the secret."""
        if sandbox_secret is None:
            sandbox_secret = secrets.token_urlsafe(32)
        env = dict(self.env) if self.env else {}
        env["SANDBOX_SECRET"] = sandbox_secret
        self.env = env
        return sandbox_secret

    def app_payload(self) -> Dict[str, Any]:
        """CreateApp payload for the app a create call owns."""
        return {
            "name": f"sandbox-app-{self.name}-{int(time.time())}",
            "life_cycle": {"delete_when_empty": True},
        }

    def deployment_definition(self) -> DeploymentDefinition:
        """The deployment definition; the sync model is the wire truth."""
        return create_deployment_definition(
            name=self.name,
            docker_source=create_docker_source(
                self.image,
                privileged=self.privileged,
                image_registry_secret=self.registry_secret,
                entrypoint=self.entrypoint,
                command=self.command,
                args=self.args,
            ),
            env_vars=build_env_vars(self.env),
            instance_type=self.instance_type,
            definition_type=self.definition_type,
            ports=self.ports,
            exposed_port_protocol=self.exposed_port_protocol,
            region=self.region,
            routes=self.routes,
            idle_timeout=self.idle_timeout,
            enable_tcp_proxy=self.enable_tcp_proxy,
            _experimental_enable_light_sleep=self.enable_light_sleep,
            _experimental_deep_sleep_value=self.deep_sleep_value,
            enable_mesh=self.enable_mesh,
            config_files=build_config_files(self.config_files) or None,
            network_policy=self.network_policy,
        )

    def deployment_definition_dict(self) -> Dict[str, Any]:
        """Wire-format definition; both model flavors coerce this dict."""
        return self.deployment_definition().to_dict()

    def service_life_cycle(self) -> Dict[str, Any]:
        """ServiceLifeCycle payload."""
        return {
            "delete_after_create": self.delete_after_delay,
            "delete_after_sleep": self.delete_after_inactivity_delay,
        }

    def create_service_payload(self, app_id: str) -> Dict[str, Any]:
        """CreateService payload. FULL snapshots omit the definition (the
        API infers it); every other shape pins one."""
        payload: Dict[str, Any] = {
            "app_id": app_id,
            "life_cycle": self.service_life_cycle(),
            "name": self.name,
        }
        if self.snapshot_id:
            payload["instance_snapshot_id"] = self.snapshot_id
            from .snapshot import SnapshotType  # deferred: avoids import cycle

            if self.snapshot_type is not SnapshotType.FULL:
                payload["definition"] = self.deployment_definition_dict()
        else:
            payload["definition"] = self.deployment_definition_dict()
        return payload
