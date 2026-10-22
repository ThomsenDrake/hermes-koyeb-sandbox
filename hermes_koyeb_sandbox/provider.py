"""Koyeb terminal-environment provider (registers the ``koyeb`` backend)."""
from __future__ import annotations

from agent.terminal_env_provider import TerminalEnvironmentProvider

from .config import SECRET_KEYS, KoyebBackendConfig, as_bool


class KoyebTerminalProvider(TerminalEnvironmentProvider):
    is_remote = True
    is_container = True
    session_isolated_when_nonpersistent = False

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
    def default_cwd(self) -> str | None:
        return "/workspace"

    def _config(self) -> KoyebBackendConfig:
        return KoyebBackendConfig.from_env()

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
            self._config()
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
            "Optional: KOYEB_SANDBOX_IMAGE, KOYEB_SANDBOX_REGION, KOYEB_SANDBOX_INSTANCE_TYPE",
        ]

    def doctor_checks(self) -> list[tuple[bool, str, str]]:
        ok = self.is_available()
        return [(ok, "Koyeb backend",
                 "KOYEB_API_TOKEN configured" if ok else "KOYEB_API_TOKEN missing")]

    def create_environment(self, *, cwd: str, timeout: int, task_id: str = "default",
                           image: str | None = None,
                           container_config: dict | None = None, **kwargs):
        from .environment import KoyebSandboxEnvironment

        cfg = self._config()
        cc = container_config or {}
        persistent = as_bool(cc.get("container_persistent"), default=True)
        if kwargs.get("probe_only") or task_id == "prompt-backend-probe":
            persistent = False
        return KoyebSandboxEnvironment(
            cfg, cwd=cwd, timeout=timeout, task_id=str(task_id), persistent=persistent)
