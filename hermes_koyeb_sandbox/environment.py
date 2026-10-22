"""Koyeb sandbox execution environment (Hermes BaseEnvironment backend).

One Koyeb Sandbox per task. Non-persistent: created on construction, deleted
on cleanup. Persistent: named sandbox reused across sessions (resume by name;
falls back to create when not found), left in place on cleanup.

The Hermes command wrapper embeds its own ``builtin cd -- <cwd>`` and CWD
marker, so commands are dispatched WITHOUT a cwd argument: the image's
WORKDIR applies and the wrapper's cd positions the shell (a missing cwd then
surfaces as the Hermes-standard exit 126 instead of a Koyeb API 400).
"""
from __future__ import annotations

import contextlib
import logging
import shlex
import threading

from tools.environments.base import BaseEnvironment

from .config import KoyebBackendConfig

logger = logging.getLogger(__name__)

_HOST_CWD_PREFIXES = ("/Users/", "/home/", "/opt/data/", "/mnt/", "/media/")


def _sanitize_cwd(cwd: str | None, home: str) -> str:
    """Map ``~``, host-looking and relative cwds onto the sandbox home.

    An absolute path that does not look like a host mount is kept; the
    wrapper's ``cd`` surfaces a bad one as Hermes-standard exit 126.
    """
    if not cwd or cwd == "~" or not cwd.startswith("/") or cwd.startswith(_HOST_CWD_PREFIXES):
        return home
    return cwd


def _sandbox_error_text(exc: Exception) -> bool:
    """True when Koyeb says the sandbox is gone (404 / not found)."""
    text = str(exc).lower()
    return "not found" in text or "no longer exist" in text


class KoyebSandboxEnvironment(BaseEnvironment):
    """Run terminal commands inside a Koyeb Sandbox."""

    _stdin_mode = "payload"
    _snapshot_timeout = 120

    def __init__(self, config: KoyebBackendConfig, *, cwd: str = "~", timeout: int = 120,
                 task_id: str = "default", persistent: bool = False):
        self.config = config
        self.task_id = task_id
        self.persistent = persistent
        self.sandbox_name = config.sandbox_name(task_id)
        self._lock = threading.RLock()
        self._closed = False
        self._sandbox = self._open_sandbox()
        self._home = self._resolve_home()
        super().__init__(cwd=_sanitize_cwd(cwd, self._home), timeout=timeout)
        try:
            self.init_session()
        except Exception:
            self.cleanup()
            raise

    # ── Sandbox lifecycle ────────────────────────────────────────────────

    def _open_sandbox(self):
        import koyeb  # lazy: only required when this backend is selected

        create_kwargs = dict(
            image=self.config.image,
            name=self.sandbox_name,
            wait_ready=True,
            api_token=self.config.api_token,
            timeout=self.config.wait_ready_timeout,
            instance_type=self.config.instance_type,
        )
        if self.config.region:
            create_kwargs["region"] = self.config.region
        if self.config.ttl_seconds > 0:
            create_kwargs["delete_after_delay"] = self.config.ttl_seconds
        if self.config.idle_timeout > 0:
            create_kwargs["idle_timeout"] = self.config.idle_timeout
        if self.config.registry_secret:
            create_kwargs["registry_secret"] = self.config.registry_secret
        if self.config.block_network:
            create_kwargs["block_network"] = True
        if self.config.api_host:
            create_kwargs["host"] = self.config.api_host

        if self.persistent:
            # Resume a persistent sandbox by service-name lookup; create on miss.
            existing = [s for s in koyeb.Sandbox.list(api_token=self.config.api_token,
                                                      host=self.config.api_host)
                        if s.name == self.sandbox_name]
            if existing:
                sandbox = koyeb.Sandbox.get_from_id(id=existing[0].id,
                                                    api_token=self.config.api_token,
                                                    host=self.config.api_host)
                logger.info("Koyeb: resumed sandbox %s for task %s", sandbox.service_id, self.task_id)
                return sandbox
        sandbox = koyeb.Sandbox.create(**create_kwargs)
        logger.info("Koyeb: created sandbox %s (name=%s, task=%s)",
                    getattr(sandbox, "service_id", "?"), self.sandbox_name, self.task_id)
        return sandbox

    def _resolve_home(self) -> str:
        """The sandbox's own $HOME, probed once at open (image-dependent)."""
        try:
            result = self._sandbox.exec('echo "${HOME:-/}"', timeout=30)
            home = (result.stdout or "").strip()
            if result.exit_code == 0 and home.startswith("/") and home != "/":
                probe = self._sandbox.exec(f"test -d {shlex.quote(home)}", timeout=30)
                if probe.exit_code == 0:
                    return home
        except Exception as exc:
            logger.warning("Koyeb: home probe failed: %s", exc)
        return "/tmp"

    # ── BaseEnvironment contract ──────────────────────────────────────────

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: str | None = None):
        if self._closed:
            raise RuntimeError("Koyeb environment is closed")
        sandbox, lock = self._sandbox, self._lock
        state = {"cancelled": False, "staged": None, "dispatched": False}

        def cancel():
            with lock:
                state["cancelled"] = True

        def exec_fn() -> tuple[str, int]:
            staged = None
            with lock:
                if state["cancelled"]:
                    return ("", 130)
            command = cmd_string
            if stdin_data:
                # Payload-mode stdin: stage the bytes through the SDK's file
                # write (like Daytona), then let the wrapper's redirect own it.
                staged = self._staged_stdin_path()
                try:
                    sandbox.filesystem.write_file(
                        staged, stdin_data.encode("utf-8", "surrogateescape"))
                except Exception:
                    with contextlib.suppress(Exception):
                        sandbox.exec(f"rm -f {shlex.quote(staged)}", timeout=30)
                    raise
                with lock:
                    if state["cancelled"]:  # cancel() raced the upload
                        with contextlib.suppress(Exception):
                            sandbox.exec(f"rm -f {shlex.quote(staged)}", timeout=30)
                        return ("", 130)
                    state["staged"] = staged
                command = self._redirect_stdin_from_file(cmd_string, staged)
            # The Koyeb executor runs a command string under `sh` (dash), but the
            # Hermes wrapper is bash (``builtin cd``, ``export -p``, ``declare``).
            # Dispatch an explicit bash shell, as the Daytona backend does.
            shell_cmd = f"bash {'-l ' if login else ''}-c {shlex.quote(command)}"
            try:
                result = sandbox.exec(shell_cmd, timeout=timeout)
            except Exception as exc:
                if staged and not state["dispatched"]:
                    with contextlib.suppress(Exception):
                        sandbox.exec(f"rm -f {shlex.quote(staged)}", timeout=30)
                if _sandbox_error_text(exc):
                    raise RuntimeError(
                        f"Koyeb sandbox {self.sandbox_name} is gone; recreate the environment"
                    ) from exc
                raise
            with lock:
                state["dispatched"] = True
            # The base's ThreadedProcessHandle carries one stdout stream; the
            # Hermes terminal result is merged "output", so fold stderr in.
            out = result.stdout or ""
            err = result.stderr or ""
            return (out + (f"\n{err}" if err and out else err), result.exit_code)

        from tools.environments.base_output import _ThreadedProcessHandle
        return _ThreadedProcessHandle(exec_fn, cancel_fn=cancel)

    def cleanup(self):
        if not hasattr(self, "_lock"):
            return
        with self._lock:
            if self._closed or self._sandbox is None:
                return
            sandbox = self._sandbox
            self._closed = True
            if self.persistent:
                logger.info("Koyeb: keeping sandbox %s (persistent)", self.sandbox_name)
                return
            try:
                sandbox.delete()
                logger.info("Koyeb: deleted sandbox %s", self.sandbox_name)
            except Exception as exc:
                if not _sandbox_error_text(exc):
                    logger.warning("Koyeb: cleanup failed: %s", exc)
