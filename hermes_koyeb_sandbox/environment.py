"""Koyeb sandbox execution environment (Hermes BaseEnvironment backend).

One Koyeb sandbox per task, same lifecycle as Modal and Vercel Sandbox:

* **Persistent** (``terminal.container_persistent: true``, the Hermes default):
  resume a still-living sandbox by name (a crashed process may not have
  cleaned up yet). Otherwise restore the filesystem snapshot recorded for
  this task, or create a fresh sandbox. On cleanup, sync ``~/.hermes`` back,
  snapshot the filesystem, delete the live sandbox so it stops billing, and
  keep the snapshot id for the next session.
* **Non-persistent**: create on first use, delete on cleanup. A leftover
  sandbox with the same name (a crash before the last cleanup) is deleted
  before create so ephemeral runs never reuse each other's filesystem.

Both modes pass ``idle_timeout`` and ``delete_after_inactivity_delay`` so a
sandbox Hermes never gets to clean up scales to zero and is then deleted.

The Hermes command wrapper embeds its own ``builtin cd -- <cwd>`` and CWD
marker, so commands are dispatched WITHOUT a cwd argument: the image's
WORKDIR applies and the wrapper's cd positions the shell (a missing cwd then
surfaces as the Hermes-standard exit 126 instead of a Koyeb API 400).

Command output truncation is not done here. Hermes truncates terminal and
``execute_code`` output in ``tools/tool_output_truncate.py`` for every backend.
"""
from __future__ import annotations

import base64
import contextlib
import io
import logging
import os
import shlex
import tarfile
import threading
import time
from pathlib import Path

from tools.environments.base import BaseEnvironment, _load_json_store, _update_json_store

from .config import KoyebBackendConfig

logger = logging.getLogger(__name__)

_HOST_CWD_PREFIXES = ("/Users/", "/home/", "/opt/data/", "/mnt/", "/media/")
_SNAPSHOT_STORE_NAME = "koyeb_snapshots.json"
_FILESYSTEM_SNAPSHOT_TIMEOUT = 180


def decode_sync_stdout(stdout: str) -> bytes:
    """Turn a ``tar | base64`` stdout stream into archive bytes.

    Whitespace (GNU ``base64`` wraps at 76 columns) is stripped. ``validate``
    rejects a stream that has stderr, a shell marker, or a truncated chunk
    mixed in — those used to surface later as ``tar: invalid header``.
    """
    compact = "".join((stdout or "").split())
    if not compact:
        raise RuntimeError("Koyeb sync_back stdout was empty")
    try:
        return base64.b64decode(compact, validate=True)
    except Exception as exc:
        raise RuntimeError("Koyeb sync_back stdout was not a base64 tar") from exc


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


def _snapshot_store() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / _SNAPSHOT_STORE_NAME
    except Exception:
        return Path.home() / ".hermes" / _SNAPSHOT_STORE_NAME


def _load_snapshot_id(task_id: str) -> str | None:
    snapshot_id = _load_json_store(_snapshot_store()).get(task_id)
    return snapshot_id if isinstance(snapshot_id, str) and snapshot_id else None


def _store_snapshot_id(task_id: str, snapshot_id: str) -> None:
    def update(snapshots: dict) -> None:
        snapshots[task_id] = snapshot_id

    _update_json_store(_snapshot_store(), update)


def _drop_snapshot_id(task_id: str, snapshot_id: str | None = None) -> None:
    def update(snapshots: dict) -> None:
        current = snapshots.get(task_id)
        if current is not None and snapshot_id in (None, current):
            snapshots.pop(task_id, None)

    _update_json_store(_snapshot_store(), update)


class KoyebSandboxEnvironment(BaseEnvironment):
    """Run terminal commands inside a Koyeb Sandbox."""

    _stdin_mode = "payload"
    _snapshot_timeout = 120

    def __init__(self, config: KoyebBackendConfig, *, cwd: str = "~", timeout: int = 120,
                 task_id: str = "default", persistent: bool = False, probe_only: bool = False):
        self.config = config
        self.task_id = task_id
        # ``_persistent`` is what Hermes' ``is_persistent_env`` reads so the
        # agent loop keeps the sandbox across turns (Daytona/Modal do the same).
        self.probe_only = probe_only
        self.persistent = bool(persistent) and not probe_only
        self._persistent = self.persistent
        self.sandbox_name = config.sandbox_name(task_id)
        self._lock = threading.RLock()
        self._closed = False
        self._sandbox = None
        self._sync_manager = None
        self._home = "/tmp"
        try:
            self._sandbox = self._open_sandbox()
            self._home = self._resolve_home()
            super().__init__(cwd=_sanitize_cwd(cwd, self._home), timeout=timeout)
            if not self.probe_only:
                self._wire_sync()
                if self._sync_manager is not None:
                    self._sync_manager.sync(force=True)
            self.init_session()
        except Exception:
            self.cleanup()
            raise

    # ── Sandbox lifecycle ────────────────────────────────────────────────

    def _create_kwargs(self, *, snapshot: str | None = None) -> dict:
        cfg = self.config
        kwargs = dict(
            image=cfg.image,
            name=self.sandbox_name,
            wait_ready=True,
            api_token=cfg.api_token,
            timeout=cfg.wait_ready_timeout,
            instance_type=cfg.resolved_instance_type(),
            # Always set, including 0 (explicit always-on). A positive value
            # is the crash backstop: the instance sleeps if Hermes dies.
            idle_timeout=cfg.resolved_idle_timeout(),
        )
        inactivity = cfg.resolved_delete_after_inactivity()
        if inactivity > 0:
            kwargs["delete_after_inactivity_delay"] = inactivity
        if cfg.ttl_seconds > 0:
            kwargs["delete_after_delay"] = cfg.ttl_seconds
        if cfg.region:
            kwargs["region"] = cfg.region
        if cfg.registry_secret:
            kwargs["registry_secret"] = cfg.registry_secret
        if cfg.block_network:
            kwargs["block_network"] = True
        if cfg.api_host:
            kwargs["host"] = cfg.api_host
        if snapshot:
            kwargs["snapshot"] = snapshot
        return kwargs

    def _find_existing(self):
        import koyeb

        matches = [
            s for s in koyeb.Sandbox.list(api_token=self.config.api_token, host=self.config.api_host)
            if getattr(s, "name", None) == self.sandbox_name
        ]
        if not matches:
            return None
        return koyeb.Sandbox.get_from_id(
            id=matches[0].id, api_token=self.config.api_token, host=self.config.api_host,
        )

    def _delete_quiet(self, sandbox) -> None:
        try:
            sandbox.delete()
        except Exception as exc:
            if not _sandbox_error_text(exc):
                logger.warning("Koyeb: failed to delete sandbox %s: %s", self.sandbox_name, exc)
            return
        logger.info("Koyeb: deleted sandbox %s", self.sandbox_name)

    def _open_sandbox(self):
        import koyeb  # lazy: only required when this backend is selected

        if self.persistent:
            existing = self._find_existing()
            if existing is not None:
                logger.info("Koyeb: resumed sandbox %s for task %s",
                            getattr(existing, "service_id", "?"), self.task_id)
                return existing
            snapshot_id = _load_snapshot_id(self.task_id)
            if snapshot_id:
                try:
                    sandbox = koyeb.Sandbox.create(**self._create_kwargs(snapshot=snapshot_id))
                    logger.info("Koyeb: restored snapshot %s for task %s", snapshot_id, self.task_id)
                    return sandbox
                except Exception as exc:
                    logger.warning(
                        "Koyeb: failed to restore snapshot %s for task %s; creating a fresh sandbox: %s",
                        snapshot_id, self.task_id, exc,
                    )
                    _drop_snapshot_id(self.task_id, snapshot_id)
        else:
            # Ephemeral runs must not attach to a sandbox a crashed process left behind.
            existing = self._find_existing()
            if existing is not None:
                logger.info("Koyeb: deleting leftover sandbox %s before a fresh ephemeral create",
                            self.sandbox_name)
                self._delete_quiet(existing)

        sandbox = koyeb.Sandbox.create(**self._create_kwargs())
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

    def _wire_sync(self) -> None:
        """Sync ``~/.hermes`` the way Modal, Daytona, and Vercel Sandbox do.

        Skills, credential files, and cache land in the sandbox before each
        command; cleanup pulls changes back. A probe (``uname`` for the system
        prompt) skips this — uploading the tree just to print the kernel is
        what the SSH probe_only path refuses to do.
        """
        try:
            from tools.environments.file_sync import (
                FileSyncManager, iter_sync_files, quoted_rm_command,
            )
        except Exception:
            logger.debug("Koyeb: file sync unavailable", exc_info=True)
            return
        self._quoted_rm = quoted_rm_command
        remote_base = f"{self._home.rstrip('/')}/.hermes"
        self._sync_manager = FileSyncManager(
            get_files_fn=lambda: iter_sync_files(remote_base),
            upload_fn=self._upload_one,
            delete_fn=self._delete_remote,
            bulk_upload_fn=self._bulk_upload,
            bulk_download_fn=self._bulk_download,
        )

    def _filesystem(self):
        return self._sandbox.filesystem

    def _upload_one(self, host_path: str, remote_path: str) -> None:
        self._bulk_upload([(host_path, remote_path)])

    def _exec_checked(self, command: str, *, timeout: int, label: str):
        result = self._sandbox.exec(command, timeout=timeout)
        if getattr(result, "exit_code", 1) != 0:
            detail = (getattr(result, "stderr", None) or getattr(result, "stdout", None) or "").strip()
            raise RuntimeError(f"Koyeb {label} failed (exit {result.exit_code}): {detail}")
        return result

    def _bulk_upload(self, files: list[tuple[str, str]]) -> None:
        """Install host files as real bytes, the way Modal pipes ``base64 -d | tar``.

        The Koyeb file API stores the JSON string verbatim. The Python SDK's
        ``encoding="base64"`` only base64-encodes that string on the client, so
        the sandbox would see base64 text and a later sync_back would copy it
        back over ``~/.hermes``. Write the archive as text, then decode it
        inside the sandbox.
        """
        if not files:
            return
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for host_path, remote_path in files:
                tar.add(host_path, arcname=remote_path.lstrip("/"))
        remote_b64 = f"/tmp/.hermes_push.{os.getpid()}.b64"
        # Text, not encoding="base64": the executor must store the ASCII
        # payload so ``base64 -d`` inside the sandbox recovers the tar.
        self._filesystem().write_file(remote_b64, base64.b64encode(buf.getvalue()).decode("ascii"))
        script = (
            "set -o pipefail; "
            f"base64 -d {shlex.quote(remote_b64)} | tar xf - -C /; "
            "status=$?; "
            f"rm -f {shlex.quote(remote_b64)}; "
            "exit $status"
        )
        self._exec_checked(f"bash -c {shlex.quote(script)}", timeout=120, label="file sync upload")

    def _delete_remote(self, remote_paths: list[str]) -> None:
        if remote_paths:
            self._sandbox.exec(self._quoted_rm(remote_paths), timeout=30)

    def _bulk_download(self, dest: Path) -> None:
        """Download ``~/.hermes`` the way Modal does: ``tar cf -`` as bytes.

        Koyeb exec stdout is text, so the archive is base64 on stdout and
        stderr stays on stderr. ``download_file(encoding="base64")`` cannot
        do this: that flag client-decodes a file the executor returned as a
        raw string, and ``tarfile`` then raises ``invalid header`` (retried
        three times by FileSyncManager).
        """
        rel = f"{self._home.rstrip('/')}/.hermes".lstrip("/")
        script = (
            "set -o pipefail; "
            f"tar cf - --exclude='*.sock' -C / {shlex.quote(rel)} | base64"
        )
        result = self._exec_checked(
            f"bash -c {shlex.quote(script)}", timeout=120, label="sync_back",
        )
        dest.write_bytes(decode_sync_stdout(result.stdout or ""))

    def _before_execute(self) -> None:
        if self._sync_manager is not None:
            self._sync_manager.sync()

    def _snapshot_filesystem(self, sandbox) -> str | None:
        name = f"{self.sandbox_name}-{int(time.time())}"[:64]
        try:
            snapshot = sandbox.snapshot(name, wait_available=True, timeout=_FILESYSTEM_SNAPSHOT_TIMEOUT)
        except Exception as exc:
            logger.warning("Koyeb: filesystem snapshot failed for %s: %s", self.sandbox_name, exc)
            return None
        snapshot_id = getattr(snapshot, "id", None)
        if not isinstance(snapshot_id, str) or not snapshot_id:
            logger.warning("Koyeb: snapshot for %s returned no id", self.sandbox_name)
            return None
        return snapshot_id

    def _delete_snapshot_object(self, snapshot_id: str) -> None:
        try:
            from koyeb.sandbox.snapshot import Snapshot
        except Exception:
            return
        try:
            Snapshot.get(
                snapshot_id, api_token=self.config.api_token, host=self.config.api_host,
            ).delete()
        except Exception as exc:
            logger.debug("Koyeb: could not delete previous snapshot %s: %s", snapshot_id, exc)

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

    def cleanup(self, force_remove: bool | None = None):
        """Release the sandbox.

        Persistent sessions snapshot then delete (Modal/Vercel: keep the
        filesystem, stop the bill). ``force_remove`` skips the snapshot, which
        is what the prompt-time probe asks for. Non-persistent sessions delete.
        Idempotent. Failures are logged, not raised — cleanup runs from
        ``finally`` blocks and from the idle reaper.
        """
        if not hasattr(self, "_lock"):
            return
        with self._lock:
            if self._closed or self._sandbox is None:
                return
            sandbox = self._sandbox
            self._closed = True
            if self._sync_manager is not None:
                try:
                    self._sync_manager.sync_back()
                except Exception as exc:
                    logger.warning("Koyeb: sync_back failed: %s", exc)
            keep_filesystem = self.persistent and not force_remove
            if keep_filesystem:
                previous = _load_snapshot_id(self.task_id)
                snapshot_id = self._snapshot_filesystem(sandbox)
                if snapshot_id:
                    try:
                        _store_snapshot_id(self.task_id, snapshot_id)
                        logger.info("Koyeb: saved filesystem snapshot %s for task %s",
                                    snapshot_id, self.task_id)
                    except Exception as exc:
                        logger.warning("Koyeb: could not record snapshot id: %s", exc)
                    if previous and previous != snapshot_id:
                        self._delete_snapshot_object(previous)
                else:
                    logger.warning(
                        "Koyeb: persistent sandbox %s will be deleted without a new snapshot",
                        self.sandbox_name,
                    )
            self._delete_quiet(sandbox)
