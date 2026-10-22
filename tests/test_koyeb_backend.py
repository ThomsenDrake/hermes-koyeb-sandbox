"""Tests for the Koyeb sandbox terminal backend.

The Koyeb SDK boundary (koyeb.Sandbox) is faked at the class level: the
environment module must call Sandbox.get_from_id/Sandbox.create and read
CommandResult fields off the object those return. Everything else is the
real Hermes BaseEnvironment machinery.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import types
from dataclasses import dataclass, field

import pytest

sys.path.insert(0, "/opt/hermes")

from hermes_koyeb_sandbox.config import KoyebBackendConfig, as_bool  # noqa: E402


# ── Fake koyeb SDK surface (verified against koyeb-sdk 1.5.5) ──────────────

@dataclass
class FakeCommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration: float = 0.1
    command: str = ""

@dataclass
class FakeSandbox:
    sandbox_id: str = "sbx-123"
    app_id: str = "app-123"
    service_id: str = "svc-123"
    name: str = "test-sandbox"
    calls: list = field(default_factory=list)

    def exec(self, command, cwd=None, env=None, timeout=30, **kwargs):
        self.calls.append({"command": command, "cwd": cwd, "env": env, "timeout": timeout})
        return FakeCommandResult(stdout=f"ran: {command}", exit_code=0)

    def delete(self):
        self.calls.append({"command": "__delete__"})


class _FakeFilesystem:
    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.writes: list[tuple[str, bytes]] = []

    def write_file(self, path, content, encoding="utf-8"):
        self.writes.append((path, content if isinstance(content, bytes) else content.encode()))


class FakeSandboxModule(types.ModuleType):
    """koyeb module double: records constructor kwargs, returns FakeSandbox."""

    def __init__(self):
        super().__init__("koyeb")
        self._mod = types.ModuleType("koyeb")
        self._mod.Sandbox = self._sandbox_cls()
        self._mod.AsyncSandbox = self._mod.Sandbox
        self.Sandbox = self._mod.Sandbox
        self.AsyncSandbox = self._mod.Sandbox
        self.create_kwargs: list = []
        self.get_ids: list = []
        self.list_names: list[str] = []   # names Sandbox.list() reports as existing

    def _sandbox_cls(self):
        mod = self

        class Sandbox:
            def __init__(self, sandbox_id="sbx-123", app_id="app-123", service_id="svc-123",
                         name=None, api_token=None, sandbox_secret=None, host=None, **kw):
                self.sandbox_id = sandbox_id
                self.app_id = app_id
                self.service_id = service_id
                self.name = name
                self.api_token = api_token
                self.calls = []
                self.filesystem = _FakeFilesystem(self)

            def exec(self, command, cwd=None, env=None, timeout=30, **kwargs):
                self.calls.append({"command": command, "cwd": cwd, "env": env, "timeout": timeout})
                # Home probe: $HOME in the default koyeb/sandbox image (root).
                if "${HOME:-/}" in command:
                    return FakeCommandResult(stdout="/root\n", exit_code=0)
                if command.startswith("test -d "):
                    return FakeCommandResult(stdout="", exit_code=0)
                # Behave like a shell: run the command, then the base's CWD-marker
                # printf (the wrapper appends it to every command) with $(pwd -P)
                # resolved to the exec cwd.
                out = f"ran: {command}"
                marker = re.search(r"(__HERMES_CWD_[0-9a-f]+__)", command)
                if marker and "printf" in command:
                    out += f"\n\n{marker.group(1)}{cwd or '/root'}{marker.group(1)}\n"
                return FakeCommandResult(stdout=out, exit_code=0)

            def wait_ready(self, timeout=300):
                return True

            @property
            def id(self):
                # Mirrors koyeb-sdk: Sandbox.id is the service id.
                return self.service_id

            def delete(self):
                self.calls.append({"command": "__delete__"})

            @classmethod
            def create(cls, **kwargs):
                mod.create_kwargs.append(kwargs)
                return cls(name=kwargs.get("name"))

            @classmethod
            def get_from_id(cls, id=None, api_token=None, host=None, **kwargs):
                mod.get_ids.append(id)
                return cls(name="resumed")

            @classmethod
            def list(cls, api_token=None, host=None, app_id=None, name=None):
                return [cls(service_id=f"svc-{n}", name=n) for n in mod.list_names]

        return Sandbox


@pytest.fixture()
def fake_koyeb(monkeypatch):
    mod = FakeSandboxModule()
    monkeypatch.setitem(sys.modules, "koyeb", mod)
    return mod


def _make_config(monkeypatch, **env):
    monkeypatch.setenv("KOYEB_API_TOKEN", "test-token-123")
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    return KoyebBackendConfig.from_env()


# ── Config tests ────────────────────────────────────────────────────────────

def test_config_requires_token(monkeypatch):
    monkeypatch.delenv("KOYEB_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="KOYEB_API_TOKEN"):
        KoyebBackendConfig.from_env()


def test_config_defaults(monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    cfg = KoyebBackendConfig.from_env()
    assert cfg.api_token == "t"
    assert cfg.image == "koyeb/sandbox"
    assert cfg.region is None          # SDK default (reads KOYEB_REGION itself)
    assert cfg.instance_type == "micro"
    assert cfg.name_prefix == "hermes"
    assert cfg.ttl_seconds == 0        # 0 = never auto-delete on TTL
    assert cfg.idle_timeout == 0      # 0 = keep always-on
    assert cfg.wait_ready_timeout == 300
    assert cfg.registry_secret is None
    assert cfg.block_network is False


def test_config_reads_env_overrides(monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    monkeypatch.setenv("KOYEB_SANDBOX_IMAGE", "python:3.13-slim")
    monkeypatch.setenv("KOYEB_SANDBOX_REGION", "fra")
    monkeypatch.setenv("KOYEB_SANDBOX_INSTANCE_TYPE", "medium")
    monkeypatch.setenv("KOYEB_SANDBOX_NAME_PREFIX", "drake")
    monkeypatch.setenv("KOYEB_SANDBOX_TTL_SECONDS", "600")
    monkeypatch.setenv("KOYEB_SANDBOX_IDLE_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("KOYEB_SANDBOX_WAIT_READY_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("KOYEB_SANDBOX_REGISTRY_SECRET", "my-registry")
    monkeypatch.setenv("KOYEB_SANDBOX_BLOCK_NETWORK", "true")
    cfg = KoyebBackendConfig.from_env()
    assert cfg.image == "python:3.13-slim"
    assert cfg.region == "fra"
    assert cfg.instance_type == "medium"
    assert cfg.name_prefix == "drake"
    assert cfg.ttl_seconds == 600
    assert cfg.idle_timeout == 300
    assert cfg.wait_ready_timeout == 120
    assert cfg.registry_secret == "my-registry"
    assert cfg.block_network is True


def test_sandbox_name_short_and_stable(monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    cfg = KoyebBackendConfig.from_env()
    n1 = cfg.sandbox_name("my-task-id")
    n2 = cfg.sandbox_name("my-task-id")
    assert n1 == n2
    assert n1.startswith("hermes-")
    assert len(n1) <= 64
    assert cfg.sandbox_name("other") != n1


# ── Environment execute path ────────────────────────────────────────────────

def test_execute_runs_command_through_koyeb(fake_koyeb, monkeypatch, tmp_path):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    from hermes_koyeb_sandbox.environment import KoyebSandboxEnvironment
    cfg = KoyebBackendConfig.from_env()
    env = KoyebSandboxEnvironment(cfg, cwd="/workspace", timeout=60, task_id="default",
                                  persistent=False)
    try:
        result = env.execute("echo hello")
        assert result["returncode"] == 0
        assert "ran: " in result["output"]
        # The wrapped command went through the koyeb sandbox handle
        assert len(fake_koyeb.create_kwargs) == 1
        kwargs = fake_koyeb.create_kwargs[0]
        assert kwargs["api_token"] == "t"
        assert kwargs["name"].startswith("hermes-")
        assert kwargs["image"] == "koyeb/sandbox"
    finally:
        env.cleanup()


def test_execute_nonzero_exit(fake_koyeb, monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    from hermes_koyeb_sandbox.environment import KoyebSandboxEnvironment

    # Make the fake exec fail
    mod = sys.modules["koyeb"]

    class Failing(mod.Sandbox):
        def exec(self, command, **kwargs):
            return FakeCommandResult(stdout="", stderr="boom", exit_code=3)

    mod.get_ids.clear()
    mod.create_kwargs.clear()

    cfg = KoyebBackendConfig.from_env()
    env = KoyebSandboxEnvironment(cfg, cwd="/workspace", timeout=60, task_id="default",
                                  persistent=False)
    try:
        # Patch the live sandbox's exec to fail
        env._sandbox.exec = lambda command, **kw: FakeCommandResult(stdout="", stderr="boom", exit_code=3)
        result = env.execute("false")
        assert result["returncode"] == 3
        assert "boom" in result["output"]
    finally:
        env.cleanup()


def test_cleanup_deletes_nonpersistent_sandbox(fake_koyeb, monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    from hermes_koyeb_sandbox.environment import KoyebSandboxEnvironment
    cfg = KoyebBackendConfig.from_env()
    env = KoyebSandboxEnvironment(cfg, cwd="/workspace", timeout=60, task_id="t1",
                                  persistent=False)
    sandbox = env._sandbox
    env.cleanup()
    assert any(c.get("command") == "__delete__" for c in sandbox.calls)
    # Second cleanup is a no-op
    env.cleanup()


def test_persistent_reuses_named_sandbox(fake_koyeb, monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    from hermes_koyeb_sandbox.environment import KoyebSandboxEnvironment
    cfg = KoyebBackendConfig.from_env()
    # Report the expected sandbox name as already existing on Koyeb.
    fake_koyeb.list_names = [cfg.sandbox_name("ptask")]
    env = KoyebSandboxEnvironment(cfg, cwd="/workspace", timeout=60, task_id="ptask",
                                  persistent=True)
    try:
        # persistent mode looked the sandbox up instead of creating
        assert len(fake_koyeb.create_kwargs) == 0
        assert len(fake_koyeb.get_ids) == 1
        assert fake_koyeb.get_ids[0].startswith("svc-hermes-")
    finally:
        env.cleanup()


def test_unusable_cwd_falls_back_to_home(fake_koyeb, monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    from hermes_koyeb_sandbox.environment import KoyebSandboxEnvironment
    cfg = KoyebBackendConfig.from_env()
    # host-style cwd is not usable inside the remote container -> sandbox home
    env = KoyebSandboxEnvironment(cfg, cwd="/home/drake/project", timeout=60, task_id="t2",
                                  persistent=False)
    try:
        assert env.cwd == "/root"          # home probed from the fake sandbox
        assert env._home == "/root"
    finally:
        env.cleanup()


def test_exec_dispatched_without_cwd_kwarg(fake_koyeb, monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    from hermes_koyeb_sandbox.environment import KoyebSandboxEnvironment
    cfg = KoyebBackendConfig.from_env()
    env = KoyebSandboxEnvironment(cfg, cwd="~", timeout=60, task_id="t4", persistent=False)
    try:
        env.execute("echo hi")
        sandbox = env._sandbox
        # every exec call carries cwd=None: the wrapper owns `cd`
        assert all(c["cwd"] is None for c in sandbox.calls)
    finally:
        env.cleanup()


def test_execute_with_stdin_payload(fake_koyeb, monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    from hermes_koyeb_sandbox.environment import KoyebSandboxEnvironment
    cfg = KoyebBackendConfig.from_env()
    env = KoyebSandboxEnvironment(cfg, cwd="~", timeout=60, task_id="t5", persistent=False)
    try:
        result = env.execute("cat", stdin_data="payload-line-1\npayload-line-2\n")
        # stdin was staged through sandbox.filesystem.write_file (SDK transport)
        assert env._sandbox.filesystem.writes, "stdin was not staged"
        path, data = env._sandbox.filesystem.writes[0]
        assert path.startswith("/tmp/.hermes-stdin-")
        assert data == b"payload-line-1\npayload-line-2\n"
        # the dispatched command redirects stdin from the staged file
        assert any("exec 0<" in c["command"] for c in env._sandbox.calls)
        assert result["returncode"] == 0
    finally:
        env.cleanup()


# ── Provider registration ────────────────────────────────────────────────────

def _load_plugin_root():
    """Import the plugin's top-level ``__init__.py`` the way the Hermes plugin
    loader does (module-from-spec with the plugin dir as the package root), so
    ``register(ctx)`` is exercised exactly as the runtime sees it."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    name = "hermes_koyeb_sandbox_root"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(root, "__init__.py"), submodule_search_locations=[root])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeCtx:
    def __init__(self):
        self.registered = {}

    def register_terminal_environment_provider(self, provider):
        self.registered["provider"] = provider


def test_provider_registers_koyeb_backend(fake_koyeb, monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    from agent.terminal_env_provider import TerminalEnvironmentProvider

    root = _load_plugin_root()
    ctx = _FakeCtx()
    root.register(ctx)
    provider = ctx.registered["provider"]
    assert isinstance(provider, TerminalEnvironmentProvider)
    assert provider.name == "koyeb"
    assert provider.is_remote is True
    assert provider.is_container is True
    assert "KOYEB_API_TOKEN" in provider.strip_env_keys


def test_provider_is_available_with_token(monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    root = _load_plugin_root()
    ctx = _FakeCtx()
    root.register(ctx)
    assert ctx.registered["provider"].is_available() is True


def test_provider_unavailable_without_token(monkeypatch):
    monkeypatch.delenv("KOYEB_API_TOKEN", raising=False)
    root = _load_plugin_root()
    ctx = _FakeCtx()
    root.register(ctx)
    assert ctx.registered["provider"].is_available() is False


def test_provider_create_environment_builds_env(fake_koyeb, monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    root = _load_plugin_root()
    ctx = _FakeCtx()
    root.register(ctx)
    provider = ctx.registered["provider"]
    env = provider.create_environment(cwd="/workspace", timeout=60, task_id="t3",
                                     container_config={"container_persistent": False})
    try:
        assert type(env).__name__ == "KoyebSandboxEnvironment"
        assert env.task_id == "t3"
    finally:
        env.cleanup()
