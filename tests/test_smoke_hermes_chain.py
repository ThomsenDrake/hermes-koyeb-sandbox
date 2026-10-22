"""Smoke test: run a command through the REAL Hermes backend factory with the
``koyeb`` backend registered from this plugin, mocking only the Koyeb SDK's
network boundary. Proves the whole chain:

    plugin register() -> agent.terminal_env_registry -> tools.terminal_tool_backends
    ._create_environment("koyeb") -> provider.create_environment ->
    KoyebSandboxEnvironment.execute -> real output read back.

Run:  PYTHONPATH=/opt/hermes:<repo_root> python tests/test_smoke_hermes_chain.py
"""

from __future__ import annotations

import os
import re
import sys
import types
from dataclasses import dataclass

sys.path.insert(0, "/opt/hermes")
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, REPO_ROOT)


@dataclass
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration: float = 0.1
    command: str = ""


class _RecordingSandbox:
    def __init__(self, name="smoke"):
        self.name = name
        self.execs: list[str] = []

    def exec(self, command, cwd=None, env=None, timeout=30, **kwargs):
        self.execs.append(command)
        out = f"[koyeb-exec] {command}"
        marker = re.search(r"(__HERMES_CWD_[0-9a-f]+__)", command)
        if marker and "printf" in command:
            out += f"\n\n{marker.group(1)}{cwd}{marker.group(1)}\n"
        return CommandResult(stdout=out, exit_code=0)

    def wait_ready(self, timeout=300):
        return True

    def delete(self):
        self.execs.append("__delete__")

    @property
    def id(self):
        return "svc-smoke"


def install_fake_koyeb() -> types.ModuleType:
    mod = types.ModuleType("koyeb")

    class Sandbox(_RecordingSandbox):
        @classmethod
        def create(cls, **kwargs):
            return cls(name=kwargs.get("name") or "smoke")

        @classmethod
        def get_from_id(cls, id=None, **kwargs):
            return cls(name="resumed")

        @classmethod
        def list(cls, **kwargs):
            return []

    mod.Sandbox = Sandbox
    sys.modules["koyeb"] = mod
    return mod


def main() -> int:
    os.environ["KOYEB_API_TOKEN"] = "smoke-test-token"
    install_fake_koyeb()

    # 1. Register the plugin's provider into the REAL Hermes registry.
    import hermes_koyeb_sandbox as plugin_root

    import agent.terminal_env_registry as registry
    from hermes_koyeb_sandbox.provider import KoyebTerminalProvider

    registry.register_provider(KoyebTerminalProvider())
    assert "koyeb" in registry.plugin_backend_names(), "koyeb not registered"

    # 2. Ask the REAL factory to build the environment for backend "koyeb".
    from tools.terminal_tool_backends import _create_environment

    env = _create_environment(
        "koyeb", image="", cwd="/workspace", timeout=60,
        container_config={"container_persistent": False}, task_id="smoke",
    )
    assert type(env).__name__ == "KoyebSandboxEnvironment", type(env).__name__
    assert getattr(env, "_hermes_backend_name", "") == "koyeb"

    # 3. Execute a command and read back REAL output through the Hermes base.
    result = env.execute("echo hello-from-koyeb")
    print("returncode:", result["returncode"])
    print("output:", result["output"][:120], "...")

    ok = (
        result["returncode"] == 0
        and "[koyeb-exec]" in result["output"]
        and "echo hello-from-koyeb" in result["output"]
    )

    # 4. Cleanup deletes the sandbox (non-persistent).
    env.cleanup()
    sandbox = env._sandbox
    deleted = "__delete__" in sandbox.execs
    print("sandbox deleted on cleanup:", deleted)

    # 5. Requirements check through the real plugin path.
    from tools.terminal_tool_backends import _check_plugin_requirements

    os.environ["KOYEB_API_TOKEN"] = ""
    missing_token = _check_plugin_requirements({"env_type": "koyeb"})
    os.environ["KOYEB_API_TOKEN"] = "smoke-test-token"
    with_token = _check_plugin_requirements({"env_type": "koyeb"})
    print("requirements with token:", with_token, "| without:", missing_token)

    success = ok and deleted and with_token and not missing_token
    print("SMOKE:", "PASS" if success else "FAIL")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
