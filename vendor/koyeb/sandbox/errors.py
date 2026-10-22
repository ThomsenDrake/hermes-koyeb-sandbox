# coding: utf-8

"""
The SDK error taxonomy: every failure the sandbox layer raises is a
SandboxError subclass, so callers catch one family.
"""

from __future__ import annotations

from typing import Any, Optional

MIN_PORT = 1
MAX_PORT = 65535


class SandboxError(Exception):
    """Base exception for sandbox operations"""


class MissingApiTokenError(SandboxError, ValueError):
    """Raised when no API token is provided and KOYEB_API_TOKEN is unset.

    Also inherits ValueError for back-compat with published 1.5.x callers
    that catch the old plain ValueError from the token gates.
    """

    DEFAULT_MESSAGE = (
        "API token is required. Set KOYEB_API_TOKEN environment variable "
        "or pass api_token parameter"
    )

    def __init__(self, message: Optional[str] = None):
        super().__init__(message or self.DEFAULT_MESSAGE)


class InvalidPortError(SandboxError, ValueError):
    """Raised when a port is not an integer in [MIN_PORT, MAX_PORT].

    Also inherits ValueError for back-compat with published 1.5.x callers
    that catch the old plain ValueError from validate_port.
    """

    def __init__(self, port: Any):
        super().__init__(
            f"Port must be an integer between {MIN_PORT} and {MAX_PORT}, got {port}"
        )


class NoSandboxSecretError(SandboxError):
    """Raised when a sandbox deployment carries no SANDBOX_SECRET, so the
    executor connection cannot be established."""


class SandboxTimeoutError(SandboxError):
    """Raised when a sandbox operation times out"""


class SandboxDeploymentError(SandboxError):
    """Raised when a sandbox deployment reaches an error state"""


class SandboxRequestError(SandboxError):
    """Raised when the sandbox executor returns a non-OK HTTP response.

    Carries the HTTP status code and response body. SandboxServiceError
    (HTTP 5xx) subclasses this, so `except SandboxRequestError` catches
    every executor HTTP failure — mirroring the JS SDK's SandboxRequestError.
    """

    def __init__(
        self,
        status_code: int,
        body: Any = None,
        message: Optional[str] = None,
    ):
        self.status_code = status_code
        self.body = body
        super().__init__(
            message or f"Sandbox executor request failed: {status_code} {body}"
        )


class SandboxServiceError(SandboxRequestError):
    """Raised when the sandbox executor returns an HTTP 5xx error"""

    def __init__(self, status_code: int, message: str):
        super().__init__(
            status_code,
            body=message,
            message=f"Sandbox service error ({status_code}): {message}",
        )


class EgressPolicyError(SandboxError):
    """Raised when egress policy arguments are invalid or conflicting"""


class PoolClaimError(SandboxError):
    """Raised when claiming a sandbox from a service pool fails"""


class ServicePoolError(SandboxError):
    """Raised when a service pool operation fails"""


class ServiceTerminalStateError(SandboxError):
    """Raised when a service reaches a state that will never become ready"""

    def __init__(self, service_id: str, status: Any):
        self.service_id = service_id
        self.status = status
        status_value = getattr(status, "value", status)
        super().__init__(
            f"Service '{service_id}' reached terminal state '{status_value}' "
            f"and will not become ready."
        )
