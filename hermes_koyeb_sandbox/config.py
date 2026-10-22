"""Koyeb sandbox configuration. No network or Hermes imports at module load."""
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

# The Koyeb API rejects app/service names over 64 chars; the SDK derives the
# app name from the sandbox name, so the sandbox name must fit that budget.
_SANDBOX_NAME_MAX_LENGTH = 64

_DEFAULT_IMAGE = "koyeb/sandbox"


def _read_env(name: str) -> str:
    """Scope-aware read when running inside Hermes, plain os.environ otherwise."""
    try:
        from tools.terminal_scope import terminal_env

        return terminal_env(name, "")
    except Exception:
        return os.getenv(name, "")


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


def as_bool(value: object, *, default: bool = False) -> bool:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, bool):
        return value
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
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class KoyebBackendConfig:
    api_token: str = field(repr=False)
    image: str = _DEFAULT_IMAGE
    region: str | None = None
    instance_type: str = "micro"
    name_prefix: str = "hermes"
    ttl_seconds: int = 0          # 0 = no auto-delete after creation
    idle_timeout: int = 0         # 0 = keep always-on (no scale-to-zero)
    wait_ready_timeout: int = 300
    registry_secret: str | None = None
    block_network: bool = False
    api_host: str | None = None

    def __post_init__(self) -> None:
        if not self.api_token:
            raise ValueError(
                "KOYEB_API_TOKEN is not set. Put your Koyeb API token in .env "
                "(KOYEB_API_TOKEN=...) or the Hermes credential store."
            )
        if not self.image or not isinstance(self.image, str):
            raise ValueError("KOYEB_SANDBOX_IMAGE must be a non-empty image reference")
        if self.instance_type not in {"micro", "small", "medium", "large", "xlarge"}:
            raise ValueError(
                "KOYEB_SANDBOX_INSTANCE_TYPE must be one of micro, small, medium, large, xlarge"
            )
        if not self.name_prefix or not self.name_prefix.replace("-", "").isalnum():
            raise ValueError("KOYEB_SANDBOX_NAME_PREFIX must be alphanumeric with dashes")
        if self.ttl_seconds < 0:
            raise ValueError("KOYEB_SANDBOX_TTL_SECONDS must be >= 0")
        if self.idle_timeout < 0:
            raise ValueError("KOYEB_SANDBOX_IDLE_TIMEOUT_SECONDS must be >= 0")
        if self.wait_ready_timeout < 1:
            raise ValueError("KOYEB_SANDBOX_WAIT_READY_TIMEOUT_SECONDS must be >= 1")

    @classmethod
    def from_env(cls, secret_reader: Callable[[str], str | None] | None = None) -> "KoyebBackendConfig":
        read_secret = secret_reader or _read_secret
        region = _read_env("KOYEB_SANDBOX_REGION")
        host = _read_env("KOYEB_API_HOST")
        return cls(
            api_token=read_secret("KOYEB_API_TOKEN")
            or read_secret("AGENTS_SDK_KOYEB_SANDBOX_API_TOKEN") or "",
            image=_read_env("KOYEB_SANDBOX_IMAGE") or _DEFAULT_IMAGE,
            region=region or None,
            instance_type=_read_env("KOYEB_SANDBOX_INSTANCE_TYPE") or "micro",
            name_prefix=_read_env("KOYEB_SANDBOX_NAME_PREFIX") or "hermes",
            ttl_seconds=_positive_int(_read_env("KOYEB_SANDBOX_TTL_SECONDS") or "0",
                                      "KOYEB_SANDBOX_TTL_SECONDS"),
            idle_timeout=_positive_int(_read_env("KOYEB_SANDBOX_IDLE_TIMEOUT_SECONDS") or "0",
                                       "KOYEB_SANDBOX_IDLE_TIMEOUT_SECONDS"),
            wait_ready_timeout=_positive_int(
                _read_env("KOYEB_SANDBOX_WAIT_READY_TIMEOUT_SECONDS") or "300",
                "KOYEB_SANDBOX_WAIT_READY_TIMEOUT_SECONDS", minimum=1),
            registry_secret=_read_env("KOYEB_SANDBOX_REGISTRY_SECRET") or None,
            block_network=as_bool(_read_env("KOYEB_SANDBOX_BLOCK_NETWORK")),
            api_host=host or None,
        )

    def sandbox_name(self, task_id: str) -> str:
        """Deterministic, bounded sandbox name for a task: <prefix>-<hash>.

        Hashed (not raw) so an arbitrary task_id can never blow past the Koyeb
        64-char name limit or leak a profile path into a service name.
        """
        digest = hashlib.sha256(str(task_id).encode()).hexdigest()[:24]
        name = f"{self.name_prefix}-{digest}"
        return name[:_SANDBOX_NAME_MAX_LENGTH]
