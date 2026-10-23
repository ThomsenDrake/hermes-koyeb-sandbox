"""Koyeb terminal-environment provider (registers the ``koyeb`` backend).

Hermes has no sandbox slot beside ``terminal.backend``. This provider is
selected the same way as Docker, Modal, Daytona, Vercel Sandbox, SSH, and
the Singularity plugin: ``terminal.backend: koyeb`` / ``TERMINAL_ENV=koyeb``.
The terminal tool, file tools, ``execute_code``, and ``delegate_task``
children all share that one backend. Nothing here registers a model-facing
tool, because none of those backends do either.
"""
from __future__ import annotations

from typing import Any

from agent.terminal_env_provider import TerminalEnvironmentProvider

from .config import SECRET_KEYS, KoyebBackendConfig, as_bool


class KoyebTerminalProvider(TerminalEnvironmentProvider):
    is_remote = True
    is_container = True
    # Names are deterministic per task. Two ephemeral sessions that collapsed
    # onto one task id would create, then delete, the same sandbox.
    session_isolated_when_nonpersistent = True

    @property
    def name(self) -> str:
        return "koyeb"

    @property
    def display_name(self) -> str:
        return "Koyeb Sandbox"

    @property
    def description(self) -> str:
        return "Run commands in a Koyeb Sandbox container."

    @property
    def env_description(self) -> str:
        return "a Koyeb Sandbox container (Linux, remote)"

    @property
    def strip_env_keys(self) -> frozenset:
        return SECRET_KEYS

    @property
    def cache_path_base(self) -> str | None:
        # File sync lands ~/.hermes under the sandbox home, like the other
        # remote backends. "~" is resolved inside the sandbox, not on the host.
        return "~/.hermes"

    @property
    def default_cwd(self) -> str | None:
        return "~"

    def _config(self, **kwargs: Any) -> KoyebBackendConfig:
        return KoyebBackendConfig.from_env(**kwargs)

    def is_available(self) -> bool:
        try:
            self._config()
            return True
        except Exception:
            return False

    def check_requirements(self, config: dict) -> bool:
        import logging

        log = logging.getLogger("tools.terminal_tool")
        try:
            self._config(
                container_cpu=config.get("container_cpu", 1),
                container_memory=config.get("container_memory", 5120),
                container_disk=config.get("container_disk", 51200),
                docker_network=config.get("docker_network"),
            )
            return True
        except ValueError as exc:
            log.error("Koyeb backend: %s", exc)
            return False
        except Exception as exc:
            log.error("Koyeb backend not usable: %s", exc)
            return False

    def probe(self) -> tuple[str, str]:
        if self.is_available():
            return ("ready", "KOYEB_API_TOKEN found")
        return ("needs_setup", "Set KOYEB_API_TOKEN in .env or the credential store.")

    def setup_instructions(self) -> list[str]:
        return [
            "Create a Koyeb API token at https://app.koyeb.com/account/api",
            "Add it to your profile's .env: KOYEB_API_TOKEN=<token>",
            "Select this backend with `hermes config set terminal.backend koyeb`.",
            "That switches the terminal, file tools, and execute_code together. "
            "There is no way to keep terminal.backend on local and send only some commands here.",
            "Restart the gateway after changing terminal.backend. A running gateway keeps the backend it started with.",
            "Optional: terminal.koyeb_image, terminal.koyeb_instance_type, terminal.koyeb_region.",
        ]

    def doctor_checks(self) -> list[tuple[bool, str, str]]:
        ok = self.is_available()
        return [(ok, "Koyeb backend",
                 "KOYEB_API_TOKEN configured" if ok else "KOYEB_API_TOKEN missing")]

    def create_environment(self, *, cwd: str, timeout: int, task_id: str = "default",
                           image: str | None = None,
                           container_config: dict | None = None, **kwargs):
        from .environment import KoyebSandboxEnvironment

        cc = container_config or {}
        cfg = self._config(
            image=(image or "").strip() or None,
            container_cpu=cc.get("container_cpu", 1),
            container_memory=cc.get("container_memory", 5120),
            container_disk=cc.get("container_disk", 51200),
            docker_network=cc.get("docker_network"),
        )
        persistent = as_bool(cc.get("container_persistent"), default=True)
        probe_only = bool(kwargs.get("probe_only") or task_id == "prompt-backend-probe")
        if probe_only:
            persistent = False
        return KoyebSandboxEnvironment(
            cfg, cwd=cwd, timeout=timeout, task_id=str(task_id),
            persistent=persistent, probe_only=probe_only,
        )
