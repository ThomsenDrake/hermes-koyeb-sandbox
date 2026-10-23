"""Live smoke test against the real Koyeb API.

Skipped unless KOYEB_API_TOKEN is set. Creates one non-persistent sandbox,
runs a command through the Hermes backend factory, deletes it, and checks
the name is gone. The token is never printed.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, "/opt/hermes")
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, REPO_ROOT)

pytestmark = pytest.mark.skipif(
    not os.environ.get("KOYEB_API_TOKEN"),
    reason="KOYEB_API_TOKEN not set",
)


def test_live_sandbox_is_created_and_deleted():
    import koyeb

    import agent.terminal_env_registry as registry
    from hermes_koyeb_sandbox.provider import KoyebTerminalProvider
    from tools.terminal_tool_backends import _create_environment

    registry.register_provider(KoyebTerminalProvider())
    task_id = f"live-smoke-{int(time.time())}"
    env = _create_environment(
        "koyeb", image="", cwd="/workspace", timeout=120,
        container_config={"container_persistent": False}, task_id=task_id,
    )
    name = env.sandbox_name
    try:
        result = env.execute("echo hello-from-koyeb")
        assert result["returncode"] == 0
        assert "hello-from-koyeb" in result["output"]
    finally:
        env.cleanup()

    token = os.environ["KOYEB_API_TOKEN"]
    left = [s for s in koyeb.Sandbox.list(api_token=token) if getattr(s, "name", None) == name]
    assert left == []


def main() -> int:
    """Script entry kept for `python tests/test_live_koyeb.py`."""
    if not os.environ.get("KOYEB_API_TOKEN"):
        print("SKIP: KOYEB_API_TOKEN not set")
        return 0
    try:
        test_live_sandbox_is_created_and_deleted()
    except Exception as exc:
        print(f"LIVE SMOKE: FAIL ({type(exc).__name__})")
        return 1
    print("LIVE SMOKE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
