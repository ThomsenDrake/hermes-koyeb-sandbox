"""Koyeb sandbox configuration. No network or Hermes imports at module load.

Settings follow the same shape as the built-in terminal backends and the
official Singularity plugin:

* ``terminal.koyeb_*`` in config.yaml wins over ``TERMINAL_KOYEB_*``, which
  wins over the legacy ``KOYEB_SANDBOX_*`` env vars.
* CPU, memory, disk, and ``container_persistent`` come from the
  ``container_config`` Hermes passes into ``create_environment`` (the shared
  ``terminal.container_*`` keys). Koyeb only sells named instance types, so
  those numbers select one — see :meth:`KoyebBackendConfig.resolved_instance_type`.
* The API token is a credential (``KOYEB_API_TOKEN``), like ``DAYTONA_API_KEY``.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Callable

SECRET_KEYS = frozenset({
    "KOYEB_API_TOKEN",
    "KOYEB_API_KEY",
    "AGENTS_SDK_KOYEB_SANDBOX_API_TOKEN",
})

# The Koyeb API rejects app/service names over 64 chars. The SDK derives the
# app name as ``sandbox-app-{name}-{unix_seconds}`` (vendor spec.app_payload),
# so the service name has to leave room for that prefix and a 10-digit stamp.
_SANDBOX_NAME_MAX_LENGTH = 64 - len("sandbox-app-") - 1 - 10

_DEFAULT_IMAGE = "koyeb/sandbox"

# Hermes shared defaults (tools/terminal_tool.py). They describe a Docker
# sandbox (1 vCPU, 5 GiB RAM, 50 GiB disk). No Koyeb instance is that shape,
# so leaving the shared keys alone keeps the SDK default (``micro``) instead
# of jumping to an 8-vCPU box. A non-default cpu or memory selects a real size.
_HERMES_DEFAULT_CPU = 1.0
_HERMES_DEFAULT_MEMORY_MB = 5120
_HERMES_DEFAULT_DISK_MB = 51200

# Standard instance types: (name, vCPU, RAM MiB, disk MiB).
# https://www.koyeb.com/docs/reference/instances
_STANDARD_INSTANCES: tuple[tuple[str, float, int, int], ...] = (
    ("nano", 0.25, 256, 2560),
    ("micro", 0.5, 512, 5120),
    ("small", 1.0, 1024, 10240),
    ("medium", 2.0, 2048, 20480),
    ("large", 4.0, 4096, 40960),
    ("xlarge", 8.0, 8192, 81920),
    ("2xlarge", 16.0, 16384, 163840),
    ("3xlarge", 24.0, 32768, 245760),
    ("4xlarge", 32.0, 65536, 327680),
    ("5xlarge", 40.0, 131072, 409600),
)

# Region-limited; accepted when the user names one, never auto-selected.
_ECO_INSTANCES: tuple[tuple[str, float, int, int], ...] = (
    ("eco-nano", 0.1, 256, 2048),
    ("eco-micro", 0.25, 512, 4096),
    ("eco-small", 0.5, 1024, 8192),
    ("eco-medium", 1.0, 2048, 16384),
    ("eco-large", 2.0, 4096, 20480),
    ("eco-xlarge", 4.0, 8192, 20480),
    ("eco-2xlarge", 8.0, 16384, 20480),
)

_INSTANCE_BY_NAME = {row[0]: row for row in (*_STANDARD_INSTANCES, *_ECO_INSTANCES)}

# Crash backstop. Hermes' idle reaper (``terminal.lifetime_seconds``, default
# 300) calls cleanup() while the process is alive. These only matter when that
# process dies first: the instance sleeps, then Koyeb deletes it, so a leaked
# sandbox cannot bill forever. idle_timeout stays above the reaper window so a
# live session is not put to sleep out from under Hermes.
_DEFAULT_DELETE_AFTER_INACTIVITY = 3600


def _read_env(name: str) -> str:
    """Scope-aware read for ``TERMINAL_*``. Other names use the credential-aware env reader.

    ``terminal_env`` returns the scope default for any key that is not part of
    the bridged terminal policy, so it must not be used for ``KOYEB_*``.
    """
    if name.startswith("TERMINAL_"):
        try:
            from tools.terminal_scope import terminal_env

            return terminal_env(name, "")
        except Exception:
            return os.getenv(name, "") or ""
    try:
        from hermes_cli.config import get_env_value

        return get_env_value(name) or ""
    except Exception:
        return os.getenv(name, "") or ""


def _read_secret(name: str) -> str | None:
    """Hermes credential store first (scope-aware), then plain environ."""
    try:
        from agent.secret_scope import get_secret

        value = get_secret(name)
        if value:
            return value
    except Exception:
        pass
    return os.getenv(name) or None


def _terminal_section() -> dict:
    """``terminal:`` from config.yaml, or ``{}`` when Hermes config can't be read."""
    try:
        from hermes_cli.config import load_config_readonly

        terminal = load_config_readonly().get("terminal")
        return terminal if isinstance(terminal, dict) else {}
    except Exception:
        return {}


def _config_then_env(config_key: str, env_names: tuple[str, ...]) -> str | None:
    """config.yaml ``terminal.<config_key>`` wins, then the env names in order."""
    value = _terminal_section().get(config_key)
    if value is not None and str(value).strip():
        return str(value).strip()
    for name in env_names:
        raw = _read_env(name).strip()
        if raw:
            return raw
    return None


def as_bool(value: object, *, default: bool = False) -> bool:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    raise ValueError("Expected a boolean value")


def _positive_int(raw: str, name: str, *, minimum: int = 0) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _optional_int(raw: str | None, name: str, *, minimum: int = 0) -> int | None:
    if raw is None:
        return None
    return _positive_int(raw, name, minimum=minimum)


@dataclass(frozen=True)
class KoyebBackendConfig:
    api_token: str = field(repr=False)
    image: str = _DEFAULT_IMAGE
    region: str | None = None
    # None: derive from container_cpu / container_memory. A string is explicit.
    instance_type: str | None = None
    name_prefix: str = "hermes"
    ttl_seconds: int = 0          # delete_after_delay; 0 = don't delete on a creation clock
    idle_timeout: int | None = None  # None = derive from lifetime_seconds
    delete_after_inactivity: int | None = None  # None = crash-backstop default
    wait_ready_timeout: int = 300
    registry_secret: str | None = None
    block_network: bool = False
    api_host: str | None = None
    container_cpu: float = _HERMES_DEFAULT_CPU
    container_memory: int = _HERMES_DEFAULT_MEMORY_MB
    container_disk: int = _HERMES_DEFAULT_DISK_MB
    lifetime_seconds: int = 300

    def __post_init__(self) -> None:
        if not self.api_token:
            raise ValueError(
                "KOYEB_API_TOKEN is not set. Put your Koyeb API token in .env "
                "(KOYEB_API_TOKEN=...) or the Hermes credential store."
            )
        if not self.image or not isinstance(self.image, str):
            raise ValueError("terminal.koyeb_image must be a non-empty image reference")
        if self.instance_type is not None and self.instance_type not in _INSTANCE_BY_NAME:
            known = ", ".join(sorted(_INSTANCE_BY_NAME))
            raise ValueError(
                f"terminal.koyeb_instance_type must be one of {known}, got {self.instance_type!r}"
            )
        if not self.name_prefix or not self.name_prefix.replace("-", "").isalnum():
            raise ValueError("terminal.koyeb_name_prefix must be alphanumeric with dashes")
        if self.ttl_seconds < 0:
            raise ValueError("terminal.koyeb_ttl_seconds must be >= 0")
        if self.idle_timeout is not None and self.idle_timeout < 0:
            raise ValueError("terminal.koyeb_idle_timeout_seconds must be >= 0")
        if self.delete_after_inactivity is not None and self.delete_after_inactivity < 0:
            raise ValueError("terminal.koyeb_delete_after_inactivity_seconds must be >= 0")
        if self.wait_ready_timeout < 1:
            raise ValueError("terminal.koyeb_wait_ready_timeout_seconds must be >= 1")
        if self.container_cpu < 0:
            raise ValueError("terminal.container_cpu must be >= 0")
        if self.container_memory < 0:
            raise ValueError("terminal.container_memory must be >= 0")
        if self.container_disk < 0:
            raise ValueError("terminal.container_disk must be >= 0")
        # Fail at config time when cpu/memory name no Koyeb instance.
        self.resolved_instance_type()
        self._reject_unsupported_disk()

    def _reject_unsupported_disk(self) -> None:
        """Koyeb disk is fixed per instance type. The shared 51200 default means
        "don't ask" (same sentinel Vercel accepts). Any other value must fit
        the instance that will actually be created."""
        if self.container_disk in (0, _HERMES_DEFAULT_DISK_MB):
            return
        spec = _INSTANCE_BY_NAME[self.resolved_instance_type()]
        cap = spec[3]
        if self.container_disk > cap:
            raise ValueError(
                "Koyeb Sandbox does not take terminal.container_disk on its own; disk comes with "
                f"the instance type. {self.resolved_instance_type()} includes {cap} MB and "
                f"terminal.container_disk is {self.container_disk}. Set terminal.koyeb_instance_type "
                "to a larger type, or leave container_disk at the shared default (51200)."
            )

    def resolved_instance_type(self) -> str:
        """Named Koyeb instance for this config.

        An explicit ``terminal.koyeb_instance_type`` wins. Otherwise the Hermes
        shared defaults (1 vCPU, 5120 MB) stay on ``micro`` — that pair is the
        Docker-sized default Hermes forwards for every container backend, and
        no Koyeb type matches it. Any other cpu or memory picks the smallest
        standard instance that covers the dimension that was actually changed.
        """
        if self.instance_type:
            return self.instance_type
        cpu_specified = self.container_cpu not in (0, _HERMES_DEFAULT_CPU)
        memory_specified = self.container_memory not in (0, _HERMES_DEFAULT_MEMORY_MB)
        if not cpu_specified and not memory_specified:
            return "micro"
        need_cpu = float(self.container_cpu) if cpu_specified else 0.0
        need_mem = int(self.container_memory) if memory_specified else 0
        for name, cpu, memory, _disk in _STANDARD_INSTANCES:
            if cpu + 1e-9 >= need_cpu and memory >= need_mem:
                return name
        raise ValueError(
            "No Koyeb instance type covers "
            f"container_cpu={self.container_cpu}, container_memory={self.container_memory}. "
            "Set terminal.koyeb_instance_type explicitly."
        )

    def resolved_idle_timeout(self) -> int:
        """Seconds until a leaked sandbox scales to zero.

        Explicit ``0`` keeps the instance always-on (it bills until something
        deletes it). Unset: twice ``terminal.lifetime_seconds``, and at least
        600, so the Hermes idle reaper runs cleanup() before Koyeb sleeps the
        sandbox, and a dead process still stops billing.
        """
        if self.idle_timeout is not None:
            return self.idle_timeout
        return max(600, 2 * max(1, self.lifetime_seconds))

    def resolved_delete_after_inactivity(self) -> int:
        """Seconds after scale-to-zero before Koyeb deletes the sandbox.

        Explicit ``0`` disables the cloud-side delete. Unset uses one hour,
        so a sandbox Hermes never got to clean up cannot bill indefinitely.
        """
        if self.delete_after_inactivity is not None:
            return self.delete_after_inactivity
        return _DEFAULT_DELETE_AFTER_INACTIVITY

    @classmethod
    def from_env(
        cls,
        secret_reader: Callable[[str], str | None] | None = None,
        *,
        image: str | None = None,
        container_cpu: float | None = None,
        container_memory: int | None = None,
        container_disk: int | None = None,
        docker_network: bool | None = None,
    ) -> "KoyebBackendConfig":
        read_secret = secret_reader or _read_secret
        section = _terminal_section()
        lifetime_raw = section.get("lifetime_seconds")
        if lifetime_raw is None or str(lifetime_raw).strip() == "":
            lifetime_raw = _read_env("TERMINAL_LIFETIME_SECONDS") or "300"
        chosen_image = (image or "").strip() or _config_then_env(
            "koyeb_image", ("TERMINAL_KOYEB_IMAGE", "KOYEB_SANDBOX_IMAGE"),
        ) or _DEFAULT_IMAGE
        block = as_bool(_config_then_env(
            "koyeb_block_network",
            ("TERMINAL_KOYEB_BLOCK_NETWORK", "KOYEB_SANDBOX_BLOCK_NETWORK"),
        ))
        # ``terminal.docker_network: false`` is the shared air-gap switch Hermes
        # forwards on container_config for every container backend.
        if docker_network is False:
            block = True
        return cls(
            api_token=read_secret("KOYEB_API_TOKEN")
            or read_secret("AGENTS_SDK_KOYEB_SANDBOX_API_TOKEN") or "",
            image=chosen_image,
            region=_config_then_env(
                "koyeb_region", ("TERMINAL_KOYEB_REGION", "KOYEB_SANDBOX_REGION"),
            ),
            instance_type=_config_then_env(
                "koyeb_instance_type",
                ("TERMINAL_KOYEB_INSTANCE_TYPE", "KOYEB_SANDBOX_INSTANCE_TYPE"),
            ),
            name_prefix=_config_then_env(
                "koyeb_name_prefix",
                ("TERMINAL_KOYEB_NAME_PREFIX", "KOYEB_SANDBOX_NAME_PREFIX"),
            ) or "hermes",
            ttl_seconds=_positive_int(
                _config_then_env(
                    "koyeb_ttl_seconds",
                    ("TERMINAL_KOYEB_TTL_SECONDS", "KOYEB_SANDBOX_TTL_SECONDS"),
                ) or "0",
                "terminal.koyeb_ttl_seconds",
            ),
            idle_timeout=_optional_int(
                _config_then_env(
                    "koyeb_idle_timeout_seconds",
                    ("TERMINAL_KOYEB_IDLE_TIMEOUT_SECONDS", "KOYEB_SANDBOX_IDLE_TIMEOUT_SECONDS"),
                ),
                "terminal.koyeb_idle_timeout_seconds",
            ),
            delete_after_inactivity=_optional_int(
                _config_then_env(
                    "koyeb_delete_after_inactivity_seconds",
                    (
                        "TERMINAL_KOYEB_DELETE_AFTER_INACTIVITY_SECONDS",
                        "KOYEB_SANDBOX_DELETE_AFTER_INACTIVITY_SECONDS",
                    ),
                ),
                "terminal.koyeb_delete_after_inactivity_seconds",
            ),
            wait_ready_timeout=_positive_int(
                _config_then_env(
                    "koyeb_wait_ready_timeout_seconds",
                    (
                        "TERMINAL_KOYEB_WAIT_READY_TIMEOUT_SECONDS",
                        "KOYEB_SANDBOX_WAIT_READY_TIMEOUT_SECONDS",
                    ),
                ) or "300",
                "terminal.koyeb_wait_ready_timeout_seconds",
                minimum=1,
            ),
            registry_secret=_config_then_env(
                "koyeb_registry_secret",
                ("TERMINAL_KOYEB_REGISTRY_SECRET", "KOYEB_SANDBOX_REGISTRY_SECRET"),
            ),
            block_network=block,
            api_host=_config_then_env(
                "koyeb_api_host", ("TERMINAL_KOYEB_API_HOST", "KOYEB_API_HOST"),
            ) or None,
            container_cpu=float(container_cpu if container_cpu is not None else _HERMES_DEFAULT_CPU),
            container_memory=int(
                container_memory if container_memory is not None else _HERMES_DEFAULT_MEMORY_MB
            ),
            container_disk=int(container_disk if container_disk is not None else _HERMES_DEFAULT_DISK_MB),
            lifetime_seconds=_positive_int(str(lifetime_raw), "terminal.lifetime_seconds", minimum=1),
        )

    def sandbox_name(self, task_id: str) -> str:
        """Deterministic, bounded sandbox name for a task: <prefix>-<hash>.

        Hashed (not raw) so an arbitrary task_id can never blow past the Koyeb
        64-char name limit or leak a profile path into a service name. The
        prefix is only ``name_prefix`` (default ``hermes``). A missing task
        id, or one that is the placeholder ``unknown``, still hashes under
        that prefix — it does not produce an ``unknown-`` service name.
        """
        digest = hashlib.sha256(str(task_id).encode()).hexdigest()[:24]
        name = f"{self.name_prefix}-{digest}"
        return name[:_SANDBOX_NAME_MAX_LENGTH]
