# coding: utf-8

"""
Koyeb Sandbox Snapshot - Snapshot functionality for Koyeb sandboxes
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar, Dict, List, Optional, Union

from .sandbox import Sandbox
from .clients import get_api_clients
from .errors import MissingApiTokenError, SandboxError


class SnapshotType(Enum):
    """Types of sandbox snapshots."""

    FILESYSTEM = "filesystem"
    FULL = "full"


class SnapshotStatus(Enum):
    """Status of a sandbox snapshot."""

    INVALID = "invalid"
    CREATING = "creating"
    UPLOADING = "uploading"
    AVAILABLE = "available"
    DELETING = "deleting"
    DELETED = "deleted"
    FAILED = "failed"


@dataclass
class Snapshot:
    """
    Represents a sandbox snapshot resource.

    A snapshot captures the state of a sandbox at a specific point in time,
    including its filesystem and optionally running processes. Sandboxes can
    be spawned from snapshots to create pre-configured environments.
    """

    id: str
    name: str
    service_id: str
    snapshot_type: SnapshotType
    status: SnapshotStatus
    created_at: datetime
    deployment_id: Optional[str] = None
    available_at: Optional[datetime] = None
    size: Optional[int] = None
    region: str = ""
    organization_id: str = ""
    project_id: Optional[str] = None
    messages: List[str] = field(default_factory=list)
    api_token: Optional[str] = None
    host: Optional[str] = None
    sandbox_secret: Optional[str] = None
    operations: List[str] = field(default_factory=list)

    @classmethod
    def get(
        cls,
        snapshot_id: str,
        api_token: Optional[str] = None,
        host: Optional[str] = None,
    ) -> Snapshot:
        """
        Get a snapshot by ID.

        Uses the InstanceSnapshots API which is for sandbox/service instance snapshots.

        Args:
            snapshot_id: The ID of the snapshot to retrieve
            api_token: Koyeb API token (falls back to KOYEB_API_TOKEN env var)
            host: Koyeb API host

        Returns:
            Snapshot: The snapshot object

        Raises:
            SandboxError: If snapshot cannot be retrieved
        """
        import os

        if not api_token:
            api_token = os.getenv("KOYEB_API_TOKEN")
        if not api_token:
            raise MissingApiTokenError(
                "API token is required. Set KOYEB_API_TOKEN environment variable."
            )

        try:
            clients = get_api_clients(api_token, host)
            reply = clients.instance_snapshots.get_instance_snapshot(snapshot_id)

            if not reply.instance_snapshot:
                raise SandboxError(f"Snapshot {snapshot_id} not found")

            api_snapshot = reply.instance_snapshot
            return cls._from_instance_api_snapshot(api_snapshot, api_token, host)

        except Exception as e:
            raise SandboxError(f"Failed to get snapshot: {e}") from e

    @classmethod
    def list(
        cls,
        service_id: Optional[str] = None,
        snapshot_type: Optional[SnapshotType] = None,
        status: Optional[SnapshotStatus] = None,
        limit: int = 50,
        offset: int = 0,
        api_token: Optional[str] = None,
        host: Optional[str] = None,
    ) -> List[Snapshot]:
        """
        List snapshots with optional filters.

        Uses the InstanceSnapshots API which is for sandbox/service instance snapshots.

        Args:
            service_id: Filter by service ID
            snapshot_type: Filter by snapshot type
            status: Filter by snapshot status
            limit: Maximum number of snapshots to return
            offset: Offset for pagination
            api_token: Koyeb API token
            host: Koyeb API host

        Returns:
            List of Snapshot objects
        """
        import os

        if not api_token:
            api_token = os.getenv("KOYEB_API_TOKEN")
        if not api_token:
            raise MissingApiTokenError(
                "API token is required. Set KOYEB_API_TOKEN environment variable."
            )

        try:
            from koyeb.api.models.instance_snapshot_type import InstanceSnapshotType
            from koyeb.api.models.instance_snapshot_status import InstanceSnapshotStatus

            clients = get_api_clients(api_token, host)

            # Map our SnapshotType to InstanceSnapshotType
            type_param = None
            if snapshot_type:
                if snapshot_type == SnapshotType.FILESYSTEM:
                    type_param = InstanceSnapshotType.INSTANCE_SNAPSHOT_TYPE_FILESYSTEM
                elif snapshot_type == SnapshotType.FULL:
                    type_param = InstanceSnapshotType.INSTANCE_SNAPSHOT_TYPE_FULL

            # Map our SnapshotStatus to InstanceSnapshotStatus
            status_param = None
            if status:
                status_map = {
                    SnapshotStatus.CREATING: InstanceSnapshotStatus.INSTANCE_SNAPSHOT_STATUS_CREATING,
                    SnapshotStatus.UPLOADING: InstanceSnapshotStatus.INSTANCE_SNAPSHOT_STATUS_UPLOADING,
                    SnapshotStatus.AVAILABLE: InstanceSnapshotStatus.INSTANCE_SNAPSHOT_STATUS_AVAILABLE,
                    SnapshotStatus.FAILED: InstanceSnapshotStatus.INSTANCE_SNAPSHOT_STATUS_FAILED,
                }
                status_param = status_map.get(status)

            # Build kwargs, omitting None values
            list_kwargs = {
                "limit": str(limit),
                "offset": str(offset),
            }
            if service_id:
                list_kwargs["service_id"] = service_id
            if type_param:
                list_kwargs["type"] = type_param
            if status_param:
                list_kwargs["status"] = status_param

            reply = clients.instance_snapshots.list_instance_snapshots(**list_kwargs)

            snapshots = []
            if reply.instance_snapshots:
                for api_snapshot in reply.instance_snapshots:
                    snapshots.append(
                        cls._from_instance_api_snapshot(api_snapshot, api_token, host)
                    )

            return snapshots

        except Exception as e:
            raise SandboxError(f"Failed to list snapshots: {e}") from e

    @classmethod
    def _from_instance_api_snapshot(
        cls,
        api_snapshot: Any,
        api_token: Optional[str],
        host: Optional[str],
        sandbox_secret: Optional[str] = None,
    ) -> Snapshot:
        """Convert InstanceSnapshot API model to Snapshot object."""
        # Map InstanceSnapshotType to our SnapshotType
        snapshot_type = SnapshotType.FILESYSTEM
        if api_snapshot.type and api_snapshot.type.value:
            type_map = {
                "INSTANCE_SNAPSHOT_TYPE_FILESYSTEM": SnapshotType.FILESYSTEM,
                "INSTANCE_SNAPSHOT_TYPE_FULL": SnapshotType.FULL,
            }
            snapshot_type = type_map.get(
                api_snapshot.type.value, SnapshotType.FILESYSTEM
            )

        # Map InstanceSnapshotStatus to our SnapshotStatus
        status = SnapshotStatus.INVALID
        if api_snapshot.status and api_snapshot.status.value:
            status_map = {
                "INSTANCE_SNAPSHOT_STATUS_INVALID": SnapshotStatus.INVALID,
                "INSTANCE_SNAPSHOT_STATUS_CREATING": SnapshotStatus.CREATING,
                "INSTANCE_SNAPSHOT_STATUS_UPLOADING": SnapshotStatus.UPLOADING,
                "INSTANCE_SNAPSHOT_STATUS_AVAILABLE": SnapshotStatus.AVAILABLE,
                "INSTANCE_SNAPSHOT_STATUS_DELETING": SnapshotStatus.DELETING,
                "INSTANCE_SNAPSHOT_STATUS_DELETED": SnapshotStatus.DELETED,
                "INSTANCE_SNAPSHOT_STATUS_FAILED": SnapshotStatus.FAILED,
            }
            status = status_map.get(api_snapshot.status.value, SnapshotStatus.INVALID)

        # Get region from the snapshot
        # Try region field first, then regional_deployment_id if available
        region = ""
        if hasattr(api_snapshot, "region") and api_snapshot.region:
            region = api_snapshot.region
        elif (
            hasattr(api_snapshot, "regional_deployment_id")
            and api_snapshot.regional_deployment_id
        ):
            # regional_deployment_id typically contains region info, e.g., "region-service-id"
            # For now, we don't have a direct mapping, so leave as empty
            region = ""

        return cls(
            id=api_snapshot.id or "",
            name=api_snapshot.name or "",
            service_id=api_snapshot.service_id or "",
            snapshot_type=snapshot_type,
            status=status,
            created_at=api_snapshot.created_at or datetime.utcnow(),
            deployment_id=api_snapshot.deployment_id,
            available_at=api_snapshot.available_at,
            region=region,
            organization_id=api_snapshot.organization_id or "",
            project_id=api_snapshot.project_id,
            messages=api_snapshot.messages or [],
            api_token=api_token,
            host=host,
            sandbox_secret=sandbox_secret,
        )

    def refresh(self) -> None:
        """Refresh snapshot state from the API."""
        if not self.id or not self.api_token:
            raise SandboxError("Cannot refresh snapshot without ID and API token")

        updated = self.get(self.id, api_token=self.api_token, host=self.host)
        # Preserve sandbox_secret, api_token, host, and operations - don't overwrite from API
        for field in self.__dataclass_fields__:
            if field not in ("api_token", "host", "sandbox_secret", "operations"):
                setattr(self, field, getattr(updated, field))

    def wait_available(
        self,
        timeout: int = 600,
        poll_interval: float = 5.0,
    ) -> bool:
        """
        Wait for snapshot to become available.

        Args:
            timeout: Maximum time to wait in seconds
            poll_interval: Time between status checks in seconds

        Returns:
            True if snapshot became available, False if timeout
        """
        import time

        start_time = time.time()
        while time.time() - start_time < timeout:
            self.refresh()
            if self.status == SnapshotStatus.AVAILABLE:
                return True
            if self.status == SnapshotStatus.FAILED:
                return False
            time.sleep(poll_interval)
        return False

    def delete(self) -> bool:
        """
        Delete this snapshot.

        Uses the InstanceSnapshots API which is for sandbox/service instance snapshots.

        Returns:
            True if deletion was successful
        """
        if not self.id or not self.api_token:
            raise SandboxError("Cannot delete snapshot without ID and API token")

        try:
            clients = get_api_clients(self.api_token, self.host)
            reply = clients.instance_snapshots.delete_instance_snapshot(self.id)
            return True

        except Exception as e:
            raise SandboxError(f"Failed to delete snapshot: {e}") from e

    def spawn(
        self,
        name: Optional[str] = None,
        wait_ready: bool = True,
        timeout: int = 300,
        **create_kwargs,
    ) -> Sandbox:
        """
        Spawn a new sandbox from this snapshot.

        Args:
            name: Name for the new sandbox
            wait_ready: Whether to wait for sandbox to be ready
            timeout: Timeout for sandbox creation in seconds
            **create_kwargs: Additional arguments to pass to Sandbox.create()

        Returns:
            Sandbox: A new sandbox instance initialized from this snapshot
        """
        if not self.id:
            raise SandboxError("Cannot spawn from snapshot without ID")

        create_params = {
            "snapshot": self,
            "wait_ready": wait_ready,
            "timeout": timeout,
        }
        if name:
            create_params["name"] = name
        if self.api_token:
            create_params["api_token"] = self.api_token
        if self.host:
            create_params["host"] = self.host
        if self.sandbox_secret:
            create_params["sandbox_secret"] = self.sandbox_secret

        create_params.update(create_kwargs)
        return Sandbox.create(**create_params)


class DeclarativeSnapshot:
    """
    Fluent builder for creating sandbox snapshots declaratively.

    This builder allows you to define a sandbox environment by:
    - Writing files
    - Copying local files/directories
    - Running setup commands
    - Setting environment variables

    Then builds a snapshot that can be used to spawn pre-configured sandboxes.
    """

    def __init__(
        self,
        name: str,
        image: str,
        workdir: Optional[str] = None,
        api_token: Optional[str] = None,
        host: Optional[str] = None,
        delete_builder: bool = True,
    ):
        """
        Initialize the declarative snapshot builder.

        Args:
            name: Name for the template/builder
            image: Docker image to use for the sandbox
            workdir: Working directory in the sandbox
            api_token: Koyeb API token
            host: Koyeb API host
            delete_builder: Whether to delete the builder sandbox after creating the snapshot (default: True)
        """
        import os

        if not api_token:
            api_token = os.getenv("KOYEB_API_TOKEN")
        if not api_token:
            raise MissingApiTokenError(
                "API token is required. Set KOYEB_API_TOKEN environment variable."
            )

        self._name = name
        self._image = image
        self._workdir = workdir
        self._api_token = api_token
        self._host = host
        self._delete_builder = delete_builder
        self._files: Dict[str, str] = {}
        self._copy_ops: List[Tuple[str, str]] = []
        self._commands: List[str] = []
        self._operations: List[str] = []
        self._builder_sandbox: Optional[Sandbox] = None

    def file(self, path: str, content: str) -> DeclarativeSnapshot:
        """
        Write a file to the sandbox during build.

        Args:
            path: Path in the sandbox (e.g., "/workspace/requirements.txt")
            content: File content as string

        Returns:
            self for method chaining
        """
        self._files[path] = content
        return self

    def copy(self, src: str, dst: str) -> DeclarativeSnapshot:
        """
        Copy a local file or directory to the sandbox during build.

        Args:
            src: Local source path (file or directory)
            dst: Destination path in the sandbox

        Returns:
            self for method chaining
        """
        self._copy_ops.append((src, dst))
        return self

    def run(self, command: str, cwd: Optional[str] = None) -> DeclarativeSnapshot:
        """
        Run a command during build.

        Args:
            command: Command to execute
            cwd: Working directory for the command

        Returns:
            self for method chaining
        """
        if cwd:
            self._commands.append(f"cd {cwd} && {command}")
        else:
            self._commands.append(command)
        return self

    def build(
        self,
        snapshot_name: Optional[str] = None,
    ) -> Snapshot:
        """
        Build the snapshot by creating a temporary sandbox,
        applying all configurations, and creating a snapshot.

        The builder sandbox is automatically deleted after the snapshot is created.

        Args:
            snapshot_name: Name for the snapshot (defaults to builder name)

        Returns:
            Snapshot: The created snapshot
        """
        import os
        import shutil

        name = snapshot_name or self._name

        try:
            # Create builder sandbox
            self._builder_sandbox = Sandbox.create(
                image=self._image,
                name=f"builder-{self._name}",
                wait_ready=True,
                api_token=self._api_token,
                host=self._host,
            )

            # Apply working directory
            if self._workdir:
                self._operations.append(f"set_workdir: {self._workdir}")
                self._builder_sandbox.filesystem.mkdir(self._workdir)

            # Write files
            for path, content in self._files.items():
                self._operations.append(f"create_file: {path}")
                self._builder_sandbox.filesystem.write_file(path, content)

            # Copy local files/directories
            for src, dst in self._copy_ops:
                self._operations.append(f"copy: {src} -> {dst}")
                if os.path.isdir(src):
                    # Copy directory recursively
                    self._copy_directory(src, dst)
                else:
                    # Copy single file
                    with open(src, "r") as f:
                        content = f.read()
                    self._builder_sandbox.filesystem.write_file(dst, content)

            # Run commands
            for command in self._commands:
                self._operations.append(f"run: {command}")
                result = self._builder_sandbox.exec(command, timeout=300)
                if not result.success:
                    raise SandboxError(
                        f"Command failed: {command}\nstdout: {result.stdout}\nstderr: {result.stderr}"
                    )

            # Create snapshot
            snapshot = self._builder_sandbox.snapshot(
                name=name,
                snapshot_type=SnapshotType.FILESYSTEM,
                wait_available=True,
            )

            # Store operations in the snapshot
            snapshot.operations = self._operations

            return snapshot

        except Exception as e:
            raise SandboxError(f"Failed to build snapshot: {e}") from e

        finally:
            # Delete the builder sandbox if requested
            if self._delete_builder and self._builder_sandbox:
                try:
                    self._builder_sandbox.delete()
                except Exception:
                    pass

    def _copy_directory(self, src_dir: str, dst_dir: str) -> None:
        """Recursively copy a local directory to the sandbox."""
        if not self._builder_sandbox:
            return

        for root, dirs, files in os.walk(src_dir):
            # Create directories
            for d in dirs:
                src_path = os.path.join(root, d)
                rel_path = os.path.relpath(src_path, src_dir)
                dst_path = os.path.join(dst_dir, rel_path)
                self._builder_sandbox.filesystem.mkdir(dst_path)

            # Copy files
            for f in files:
                src_path = os.path.join(root, f)
                rel_path = os.path.relpath(src_path, src_dir)
                dst_path = os.path.join(dst_dir, rel_path)
                with open(src_path, "r") as f:
                    content = f.read()
                self._builder_sandbox.filesystem.write_file(dst_path, content)

    def get_operations(self) -> List[str]:
        """Get list of operations recorded during build."""
        return self._operations
