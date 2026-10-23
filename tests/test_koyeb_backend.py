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
        self.uploads: list[tuple[str, str]] = []

    def write_file(self, path, content, encoding="utf-8"):
        self.writes.append((path, content if isinstance(content, bytes) else content.encode()))

    def upload_file(self, local_path, remote_path, encoding="utf-8"):
        self.uploads.append((local_path, remote_path))

    def download_file(self, remote_path, local_path, encoding="utf-8"):
        # Empty tar: sync_back extracts nothing and does not retry.
        import tarfile
        with tarfile.open(local_path, "w"):
            pass


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
        self.created: list = []
        self.events: list = []

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
                mod.events.append(("delete", self.name))

            def snapshot(self, name, wait_available=True, timeout=600, **kwargs):
                self.calls.append({"command": "__snapshot__", "name": name})
                mod.events.append(("snapshot", name))
                return type("Snap", (), {"id": f"snap-{name}"})()

            @classmethod
            def create(cls, **kwargs):
                mod.create_kwargs.append(kwargs)
                mod.events.append(("create", kwargs.get("name")))
                inst = cls(name=kwargs.get("name"))
                mod.created.append(inst)
                return inst

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
    # Don't upload the real Hermes tree during unit tests.
    monkeypatch.setattr(
        "tools.environments.file_sync.iter_sync_files", lambda *args, **kwargs: [],
    )
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
    token = "koyeb-test-token-do-not-log"
    monkeypatch.setenv("KOYEB_API_TOKEN", token)
    cfg = KoyebBackendConfig.from_env()
    assert cfg.api_token == token
    assert token not in repr(cfg)
    assert cfg.image == "koyeb/sandbox"
    assert cfg.region is None          # SDK default (reads KOYEB_REGION itself)
    assert cfg.instance_type is None   # derived
    assert cfg.resolved_instance_type() == "micro"
    assert cfg.name_prefix == "hermes"
    assert cfg.ttl_seconds == 0        # 0 = no delete_after_delay
    assert cfg.idle_timeout is None    # derived from lifetime
    assert cfg.resolved_idle_timeout() == max(600, 2 * cfg.lifetime_seconds)
    assert cfg.resolved_delete_after_inactivity() == 3600
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
        assert kwargs["instance_type"] == "micro"
        # Crash backstop: a sandbox Hermes never cleans up still stops billing.
        assert kwargs["idle_timeout"] == cfg.resolved_idle_timeout()
        assert kwargs["idle_timeout"] > 0
        assert kwargs["delete_after_inactivity_delay"] == cfg.resolved_delete_after_inactivity()
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
        self.tools = []

    def register_terminal_environment_provider(self, provider):
        self.registered["provider"] = provider

    def register_tool(self, *args, **kwargs):
        self.tools.append((args, kwargs))


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
    assert provider.session_isolated_when_nonpersistent is True
    assert provider.cache_path_base == "~/.hermes"
    assert ctx.tools == []  # no model-facing tool; backends are terminal.backend only


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
        assert env._persistent is False
    finally:
        env.cleanup()


def test_per_task_image_and_memory_select_instance(fake_koyeb, monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    monkeypatch.setenv("KOYEB_SANDBOX_IMAGE", "should-lose")
    root = _load_plugin_root()
    ctx = _FakeCtx()
    root.register(ctx)
    env = ctx.registered["provider"].create_environment(
        cwd="/workspace", timeout=60, task_id="img",
        image="python:3.12",
        container_config={"container_persistent": False, "container_memory": 1024},
    )
    try:
        kwargs = fake_koyeb.create_kwargs[-1]
        assert kwargs["image"] == "python:3.12"
        assert kwargs["instance_type"] == "small"  # 1 vCPU, 1 GiB covers 1024 MB
    finally:
        env.cleanup()


def test_config_yaml_image_beats_legacy_env(monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    monkeypatch.setenv("KOYEB_SANDBOX_IMAGE", "from-env:latest")
    monkeypatch.setattr(
        "hermes_koyeb_sandbox.config._terminal_section",
        lambda: {"koyeb_image": "from-config:latest"},
    )
    cfg = KoyebBackendConfig.from_env()
    assert cfg.image == "from-config:latest"


def test_explicit_instance_type_beats_resource_mapping(monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    cfg = KoyebBackendConfig.from_env(container_memory=8192, container_cpu=8)
    assert cfg.resolved_instance_type() == "xlarge"
    monkeypatch.setenv("KOYEB_SANDBOX_INSTANCE_TYPE", "nano")
    pinned = KoyebBackendConfig.from_env(container_memory=8192)
    assert pinned.instance_type == "nano"
    assert pinned.resolved_instance_type() == "nano"


def test_custom_disk_larger_than_instance_is_rejected(monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    with pytest.raises(ValueError, match="container_disk"):
        KoyebBackendConfig.from_env(container_disk=6000)  # micro disk is 5120 MB


def test_docker_network_false_blocks_egress(monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    cfg = KoyebBackendConfig.from_env(docker_network=False)
    assert cfg.block_network is True


def test_explicit_idle_zero_stays_always_on(monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    monkeypatch.setenv("KOYEB_SANDBOX_IDLE_TIMEOUT_SECONDS", "0")
    cfg = KoyebBackendConfig.from_env()
    assert cfg.resolved_idle_timeout() == 0


def test_ephemeral_deletes_leftover_before_create(fake_koyeb, monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    from hermes_koyeb_sandbox.environment import KoyebSandboxEnvironment
    cfg = KoyebBackendConfig.from_env()
    fake_koyeb.list_names = [cfg.sandbox_name("left")]
    env = KoyebSandboxEnvironment(cfg, cwd="~", timeout=30, task_id="left", persistent=False)
    try:
        kinds = [kind for kind, _name in fake_koyeb.events]
        assert kinds.index("delete") < kinds.index("create")
    finally:
        env.cleanup()


def test_persistent_snapshots_then_deletes(fake_koyeb, monkeypatch, tmp_path):
    monkeypatch.setenv("KOYEB_API_TOKEN", "secret-token")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_koyeb_sandbox.environment import KoyebSandboxEnvironment, _load_snapshot_id
    cfg = KoyebBackendConfig.from_env()
    env = KoyebSandboxEnvironment(cfg, cwd="~", timeout=30, task_id="keep", persistent=True)
    sandbox = env._sandbox
    env.cleanup()
    assert any(c.get("command") == "__snapshot__" for c in sandbox.calls)
    assert any(c.get("command") == "__delete__" for c in sandbox.calls)
    stored = _load_snapshot_id("keep")
    assert stored and stored.startswith("snap-")
    assert "secret-token" not in (tmp_path / "koyeb_snapshots.json").read_text()

    # Next session restores that snapshot instead of an empty sandbox.
    fake_koyeb.list_names = []
    fake_koyeb.create_kwargs.clear()
    restored = KoyebSandboxEnvironment(cfg, cwd="~", timeout=30, task_id="keep", persistent=True)
    try:
        assert fake_koyeb.create_kwargs[-1]["snapshot"] == stored
    finally:
        restored.cleanup()


def test_init_failure_deletes_sandbox(fake_koyeb, monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    from hermes_koyeb_sandbox.environment import KoyebSandboxEnvironment

    def boom(self):
        raise RuntimeError("bootstrap failed")

    monkeypatch.setattr(KoyebSandboxEnvironment, "init_session", boom)
    cfg = KoyebBackendConfig.from_env()
    with pytest.raises(RuntimeError, match="bootstrap failed"):
        KoyebSandboxEnvironment(cfg, cwd="~", timeout=30, task_id="boom", persistent=False)
    assert fake_koyeb.created, "sandbox was never created"
    assert any(c.get("command") == "__delete__" for c in fake_koyeb.created[-1].calls)


def test_cleanup_after_exec_exception_deletes(fake_koyeb, monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    from hermes_koyeb_sandbox.environment import KoyebSandboxEnvironment
    cfg = KoyebBackendConfig.from_env()
    env = KoyebSandboxEnvironment(cfg, cwd="~", timeout=30, task_id="err", persistent=False)
    sandbox = env._sandbox

    def explode(command, **kwargs):
        raise TimeoutError("exec timed out")

    sandbox.exec = explode
    try:
        env.execute("sleep 30")
    except Exception:
        pass
    # A failed command does not delete the sandbox by itself (same as Modal/Docker).
    assert not any(c.get("command") == "__delete__" for c in sandbox.calls)
    env.cleanup()
    assert any(c.get("command") == "__delete__" for c in sandbox.calls)


def test_environment_returns_full_output_for_hermes_truncation(fake_koyeb, monkeypatch):
    """Backends do not cap output. Hermes truncates in tool_output_truncate."""
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    from hermes_koyeb_sandbox.environment import KoyebSandboxEnvironment
    cfg = KoyebBackendConfig.from_env()
    env = KoyebSandboxEnvironment(cfg, cwd="~", timeout=30, task_id="big", persistent=False)
    blob = "x" * 20000
    try:
        env._sandbox.exec = lambda command, **kwargs: FakeCommandResult(
            stdout=blob, stderr="tail", exit_code=0,
        )
        result = env.execute("echo big")
        assert result["returncode"] == 0
        assert blob in result["output"]
        assert "tail" in result["output"]
    finally:
        env.cleanup()


def test_file_sync_uploads_before_execute(fake_koyeb, monkeypatch, tmp_path):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    host_file = tmp_path / "token.txt"
    host_file.write_text("secret")
    remote = "/root/.hermes/credentials/token.txt"
    monkeypatch.setattr(
        "tools.environments.file_sync.iter_sync_files",
        lambda *args, **kwargs: [(str(host_file), remote)],
    )
    from hermes_koyeb_sandbox.environment import KoyebSandboxEnvironment
    cfg = KoyebBackendConfig.from_env()
    env = KoyebSandboxEnvironment(cfg, cwd="~", timeout=30, task_id="sync", persistent=False)
    try:
        # FileSyncManager copies into a temp dir, then uploads that copy.
        # The remote path is the contract; the local path is the staging file.
        assert remote in [dest for _, dest in env._sandbox.filesystem.uploads]
    finally:
        env._sync_manager.sync_back = lambda *a, **k: None
        env.cleanup()


def test_probe_only_skips_file_sync(fake_koyeb, monkeypatch):
    monkeypatch.setenv("KOYEB_API_TOKEN", "t")
    root = _load_plugin_root()
    ctx = _FakeCtx()
    root.register(ctx)
    env = ctx.registered["provider"].create_environment(
        cwd="~", timeout=30, task_id="prompt-backend-probe",
        container_config={"container_persistent": True},
        probe_only=True,
    )
    try:
        assert env.probe_only is True
        assert env._persistent is False
        assert env._sync_manager is None
    finally:
        env.cleanup()
        assert any(c.get("command") == "__delete__" for c in env._sandbox.calls)
