"""Live smoke test: run a command through the REAL Hermes backend factory with
the ``koyeb`` backend against the REAL Koyeb API.

Nothing is mocked here: the provider registers into the real Hermes registry,
the factory builds the environment, and KoyebSandboxEnvironment.create/exec/
delete hit https://app.koyeb.com.

Env: KOYEB_API_TOKEN must be set (profile .env is loaded by the shell wrapper).
Run: PYTHONPATH=/opt/hermes:<repo_root> python tests/test_live_koyeb.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, "/opt/hermes")
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, REPO_ROOT)


def main() -> int:
    assert os.environ.get("KOYEB_API_TOKEN"), "KOYEB_API_TOKEN not set"

    import agent.terminal_env_registry as registry
    from hermes_koyeb_sandbox.provider import KoyebTerminalProvider

    registry.register_provider(KoyebTerminalProvider())
    assert "koyeb" in registry.plugin_backend_names()

    from tools.terminal_tool_backends import _create_environment

    started = time.time()
    env = _create_environment(
        "koyeb", image="", cwd="/workspace", timeout=120,
        container_config={"container_persistent": False}, task_id=f"live-smoke-{int(time.time())}",
    )
    created = time.time() - started
    print(f"sandbox created in {created:.1f}s")
    print(f"  service_id: {env._sandbox.service_id}")
    print(f"  name:       {env.sandbox_name}")

    try:
        # 1. Plain command; read back real output.
        r1 = env.execute("echo hello-from-koyeb && uname -sm")
        print(f"\n[1] rc={r1['returncode']}")
        print(r1["output"].strip())

        # 2. Non-zero exit + stderr propagation.
        r2 = env.execute("echo oops >&2; exit 7")
        print(f"\n[2] rc={r2['returncode']} (expect 7)")
        print(r2["output"].strip())

        # 3. cwd tracking through the Hermes wrapper.
        r3 = env.execute("cd /tmp && pwd")
        print(f"\n[3] rc={r3['returncode']} cwd={r3.get('cwd', '(not observed)')}")

        ok = r1["returncode"] == 0 and "hello-from-koyeb" in r1["output"] \
            and r2["returncode"] == 7 and "oops" in r2["output"]
    finally:
        env.cleanup()
        print(f"\ncleanup done (sandbox deleted: {env._sandbox is not None})")

    print("LIVE SMOKE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
