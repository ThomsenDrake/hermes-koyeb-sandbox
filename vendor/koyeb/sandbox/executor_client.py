"""
Sandbox Executor API Client

Sync and async Python clients for interacting with the Sandbox Executor API.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Iterator, Optional

import httpx

from .errors import (
    NoSandboxSecretError,
    SandboxError,
    SandboxRequestError,
    SandboxServiceError,
    SandboxTimeoutError,
)

DEFAULT_HTTP_TIMEOUT = 30  # seconds for HTTP requests

logger = logging.getLogger(__name__)


@dataclass
class ConnectionInfo:
    """Information needed to connect to a sandbox"""

    public_url: str
    routing_key: Optional[str]
    secret: Optional[str]

    def __str__(self) -> str:
        return f"ConnectionInfo(public_url={self.public_url}, routing_key={self.routing_key}, secret={'********' if self.secret else 'None'})"

    def validate(self) -> None:
        if not self.public_url:
            raise ValueError("Unable to get sandbox URL")
        if not self.secret:
            raise NoSandboxSecretError(
                "Sandbox secret not available — this handle is not "
                "executor-connected (e.g. it came from Sandbox.list()); use "
                "Sandbox.get_from_id(<id>) for a connected handle"
            )


def _build_headers(conn_info: ConnectionInfo) -> Dict[str, str]:
    """Build common headers for sandbox client."""
    headers = {
        "Authorization": f"Bearer {conn_info.secret}",
        "Content-Type": "application/json",
    }
    if conn_info.routing_key:
        headers["X-Routing-Key"] = conn_info.routing_key
    return headers


def _parse_sse_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse a Server-Sent Events line into an event dict."""
    if not line or not line.startswith("data:"):
        return None
    data = line[5:].strip()
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return {"error": f"Failed to parse event data: {data}"}


class SandboxClient:
    """Client for the Sandbox Executor API."""

    def __init__(
        self, conn_info: ConnectionInfo, timeout: float = DEFAULT_HTTP_TIMEOUT
    ):
        """
        Initialize the Sandbox Client.

        Args:
            conn_info: The parameters needed to connect to the sandbox
            timeout: Request timeout in seconds (default: 30)
        """
        self.base_url = conn_info.public_url.rstrip("/")
        self.secret = conn_info.secret
        self.timeout = timeout
        self.headers = _build_headers(conn_info)
        self._client = httpx.Client(headers=self.headers, trust_env=True)
        self._closed = False

    def close(self) -> None:
        """Close the HTTP client and release resources."""
        if not getattr(self, "_closed", True) and hasattr(self, "_client"):
            self._client.close()
            self._closed = True

    def __enter__(self):
        """Context manager entry - returns self."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - automatically closes the client."""
        self.close()

    def __del__(self):
        """Clean up client on deletion (fallback, not guaranteed to run)."""
        if not getattr(self, "_closed", True):
            self.close()

    def _request_with_retry(
        self,
        method: str,
        url: str,
        max_retries: int = 3,
        initial_backoff: float = 1.0,
        **kwargs,
    ) -> httpx.Response:
        """
        Make an HTTP request with retry logic for transient failures.

        Retries all 5xx responses and network errors (httpx.RequestError,
        excluding timeouts) with exponential backoff. 4xx responses are
        wrapped as SandboxRequestError; 5xx that persist after retries are
        wrapped as SandboxServiceError.

        Args:
            method: HTTP method (e.g., 'GET', 'POST')
            url: The URL to request
            max_retries: Maximum number of retry attempts
            initial_backoff: Initial backoff time in seconds (doubles each retry)
            **kwargs: Additional arguments to pass to httpx

        Returns:
            Response object

        Raises:
            SandboxRequestError: On a non-retryable HTTP error (4xx)
            SandboxServiceError: On an HTTP 5xx error after retries are exhausted
            SandboxTimeoutError: On request timeout
            SandboxError: On network errors after retries are exhausted
        """
        backoff = initial_backoff

        # Set default timeout if not provided
        if "timeout" not in kwargs:
            kwargs["timeout"] = self.timeout

        for attempt in range(max_retries + 1):
            try:
                response = self._client.request(method, url, **kwargs)

                # Retry transient server errors (5xx) with backoff
                if response.status_code >= 500 and attempt < max_retries:
                    logger.debug(
                        f"Received {response.status_code} error, retrying... "
                        f"(attempt {attempt + 1}/{max_retries + 1})"
                    )
                    time.sleep(backoff)
                    backoff *= 2  # Exponential backoff
                    continue

                response.raise_for_status()
                return response

            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500 and attempt < max_retries:
                    logger.debug(
                        f"Received {e.response.status_code} error, retrying... "
                        f"(attempt {attempt + 1}/{max_retries + 1})"
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                if e.response.status_code >= 500:
                    raise SandboxServiceError(
                        status_code=e.response.status_code,
                        message=e.response.text,
                    ) from e
                raise SandboxRequestError(
                    status_code=e.response.status_code,
                    body=e.response.text,
                ) from e
            except httpx.TimeoutException as e:
                raise SandboxTimeoutError(
                    f"Request timed out after {kwargs.get('timeout', self.timeout)}s"
                ) from e
            except httpx.RequestError as e:
                # Transient network errors (connection refused, reset, ...)
                # are retried with the same backoff as server errors.
                if attempt < max_retries:
                    logger.debug(
                        f"Network error, retrying... "
                        f"(attempt {attempt + 1}/{max_retries + 1}): {e}"
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                logger.warning(f"Request failed after {max_retries} retries: {e}")
                raise SandboxError(f"Request to sandbox executor failed: {e}") from e

        # Unreachable in practice: every terminal path returns or raises.
        raise SandboxError(f"Request to {url} failed after {max_retries} retries")

    def health(self) -> Dict[str, str]:
        """
        Check the health status of the server.

        Uses a short timeout and no retries since callers (wait_ready)
        already handle polling with backoff.

        Returns:
            Dict with status information

        Raises:
            httpx.HTTPStatusError: If the health check fails
            httpx.TimeoutException: If the health check times out
        """
        response = self._client.get(f"{self.base_url}/health", timeout=5)
        response.raise_for_status()
        return response.json()

    def run(
        self,
        cmd: str,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Execute a shell command in the sandbox.

        Args:
            cmd: The shell command to execute
            cwd: Optional working directory for command execution
            env: Optional environment variables to set/override
            timeout: Optional timeout in seconds for the request

        Returns:
            Dict containing stdout, stderr, error (if any), and exit code
        """
        payload: Dict[str, Any] = {"cmd": cmd}
        if cwd is not None:
            payload["cwd"] = cwd
        if env is not None:
            payload["env"] = env

        request_timeout = timeout if timeout is not None else self.timeout
        response = self._request_with_retry(
            "POST",
            f"{self.base_url}/run",
            json=payload,
            timeout=request_timeout,
        )
        return response.json()

    def run_streaming(
        self,
        cmd: str,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Iterator[Dict[str, Any]]:
        """
        Execute a shell command in the sandbox and stream the output in real-time.

        This method uses Server-Sent Events (SSE) to stream command output line-by-line
        as it's produced. Use this for long-running commands where you want real-time
        output. For simple commands where buffered output is acceptable, use run() instead.

        Args:
            cmd: The shell command to execute
            cwd: Optional working directory for command execution
            env: Optional environment variables to set/override
            timeout: Optional timeout in seconds for the streaming request

        Yields:
            Dict events with the following types:

            - output events (as command produces output):
              {"stream": "stdout"|"stderr", "data": "line of output"}

            - complete event (when command finishes):
              {"code": <exit_code>, "error": false}

            - error event (if command fails to start):
              {"error": "error message"}

        Example:
            >>> client = SandboxClient("http://localhost:8080", "secret")
            >>> for event in client.run_streaming("echo 'Hello'; sleep 1; echo 'World'"):
            ...     if "stream" in event:
            ...         print(f"{event['stream']}: {event['data']}")
            ...     elif "code" in event:
            ...         print(f"Exit code: {event['code']}")
        """
        payload: Dict[str, Any] = {"cmd": cmd}
        if cwd is not None:
            payload["cwd"] = cwd
        if env is not None:
            payload["env"] = env

        request_timeout = timeout if timeout is not None else self.timeout
        try:
            with self._client.stream(
                "POST",
                f"{self.base_url}/run_streaming",
                json=payload,
                timeout=request_timeout,
            ) as response:
                if response.status_code >= 500:
                    response.read()
                    raise SandboxServiceError(
                        status_code=response.status_code,
                        message=response.text,
                    )
                response.raise_for_status()
                for line in response.iter_lines():
                    event = _parse_sse_line(line)
                    if event is not None:
                        yield event
        except httpx.TimeoutException as e:
            raise SandboxTimeoutError(
                f"Request timed out after {request_timeout}s"
            ) from e
        except httpx.RequestError as e:
            # E.g. the instance is torn down mid-stream during a redeployment.
            raise SandboxError(f"Connection to sandbox lost: {e}") from e

    def write_file(self, path: str, content: str) -> Dict[str, Any]:
        """
        Write content to a file.

        Args:
            path: The file path to write to
            content: The content to write

        Returns:
            Dict with success status and error if any
        """
        payload = {"path": path, "content": content}
        response = self._request_with_retry(
            "POST", f"{self.base_url}/write_file", json=payload
        )
        return response.json()

    def read_file(self, path: str) -> Dict[str, Any]:
        """
        Read content from a file.

        Args:
            path: The file path to read from

        Returns:
            Dict with file content and error if any
        """
        payload = {"path": path}
        response = self._request_with_retry(
            "POST", f"{self.base_url}/read_file", json=payload
        )
        return response.json()

    def delete_file(self, path: str) -> Dict[str, Any]:
        """
        Delete a file.

        Args:
            path: The file path to delete

        Returns:
            Dict with success status and error if any
        """
        payload = {"path": path}
        response = self._request_with_retry(
            "POST", f"{self.base_url}/delete_file", json=payload
        )
        return response.json()

    def make_dir(self, path: str) -> Dict[str, Any]:
        """
        Create a directory (including parent directories).

        Args:
            path: The directory path to create

        Returns:
            Dict with success status and error if any
        """
        payload = {"path": path}
        response = self._request_with_retry(
            "POST", f"{self.base_url}/make_dir", json=payload
        )
        return response.json()

    def delete_dir(self, path: str) -> Dict[str, Any]:
        """
        Recursively delete a directory and all its contents.

        Args:
            path: The directory path to delete

        Returns:
            Dict with success status and error if any
        """
        payload = {"path": path}
        response = self._request_with_retry(
            "POST", f"{self.base_url}/delete_dir", json=payload
        )
        return response.json()

    def list_dir(self, path: str) -> Dict[str, Any]:
        """
        List the contents of a directory.

        Args:
            path: The directory path to list

        Returns:
            Dict with entries list and error if any
        """
        payload = {"path": path}
        response = self._request_with_retry(
            "POST", f"{self.base_url}/list_dir", json=payload
        )
        return response.json()

    def bind_port(self, port: int) -> Dict[str, Any]:
        """
        Bind a port to the TCP proxy for external access.

        Configures the TCP proxy to forward traffic to the specified port inside the sandbox.
        This allows you to expose services running inside the sandbox to external connections.

        Args:
            port: The port number to bind to (must be a valid port number)

        Returns:
            Dict with success status, message, and port information

        Notes:
            - Only one port can be bound at a time
            - Binding a new port will override the previous binding
            - The port must be available and accessible within the sandbox environment
        """
        payload = {"port": str(port)}
        response = self._request_with_retry(
            "POST", f"{self.base_url}/bind_port", json=payload
        )
        return response.json()

    def unbind_port(self, port: Optional[int] = None) -> Dict[str, Any]:
        """
        Unbind a port from the TCP proxy.

        Removes the TCP proxy port binding, stopping traffic forwarding to the previously bound port.

        Args:
            port: Optional port number to unbind. If provided, it must match the currently bound port.
                If not provided, any existing binding will be removed.

        Returns:
            Dict with success status and message

        Notes:
            - If a port is specified and doesn't match the currently bound port, the request will fail
            - After unbinding, the TCP proxy will no longer forward traffic
        """
        payload: Dict[str, Any] = {}
        if port is not None:
            payload["port"] = str(port)
        response = self._request_with_retry(
            "POST", f"{self.base_url}/unbind_port", json=payload
        )
        return response.json()

    def start_process(
        self, cmd: str, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Start a background process in the sandbox.

        Starts a long-running background process that continues executing even after
        the API call completes. Use this for servers, workers, or other long-running tasks.

        Args:
            cmd: The shell command to execute as a background process
            cwd: Optional working directory for the process
            env: Optional environment variables to set/override for the process

        Returns:
            Dict with process id and success status:
                - id: The unique process ID (UUID string)
                - success: True if the process was started successfully

        Example:
            >>> client = SandboxClient("http://localhost:8080", "secret")
            >>> result = client.start_process("python -u server.py")
            >>> process_id = result["id"]
            >>> print(f"Started process: {process_id}")
        """
        payload: Dict[str, Any] = {"cmd": cmd}
        if cwd is not None:
            payload["cwd"] = cwd
        if env is not None:
            payload["env"] = env
        response = self._request_with_retry(
            "POST", f"{self.base_url}/start_process", json=payload
        )
        return response.json()

    def kill_process(self, process_id: str) -> Dict[str, Any]:
        """
        Kill a background process by its ID.

        Terminates a running background process. This sends a SIGTERM signal to the process,
        allowing it to clean up gracefully. If the process doesn't terminate within a timeout,
        it will be forcefully killed with SIGKILL.

        Args:
            process_id: The unique process ID (UUID string) to kill

        Returns:
            Dict with success status and error message if any

        Example:
            >>> client = SandboxClient("http://localhost:8080", "secret")
            >>> result = client.kill_process("550e8400-e29b-41d4-a716-446655440000")
            >>> if result.get("success"):
            ...     print("Process killed successfully")
        """
        payload = {"id": process_id}
        response = self._request_with_retry(
            "POST", f"{self.base_url}/kill_process", json=payload
        )
        return response.json()

    def list_processes(self) -> Dict[str, Any]:
        """
        List all background processes.

        Returns information about all currently running and recently completed background
        processes. This includes both active processes and processes that have completed
        (which remain in memory until server restart).

        Returns:
            Dict with a list of processes:
                - processes: List of process objects, each containing:
                    - id: Process ID (UUID string)
                    - command: The command that was executed
                    - status: Process status (e.g., "running", "completed")
                    - pid: OS process ID (if running)
                    - exit_code: Exit code (if completed)
                    - started_at: ISO 8601 timestamp when process started
                    - completed_at: ISO 8601 timestamp when process completed (if applicable)

        Example:
            >>> client = SandboxClient("http://localhost:8080", "secret")
            >>> result = client.list_processes()
            >>> for process in result.get("processes", []):
            ...     print(f"{process['id']}: {process['command']} - {process['status']}")
        """
        response = self._request_with_retry("GET", f"{self.base_url}/list_processes")
        return response.json()


class AsyncSandboxClient:
    """Async client for the Sandbox Executor API using httpx."""

    def __init__(
        self, conn_info: ConnectionInfo, timeout: float = DEFAULT_HTTP_TIMEOUT
    ):
        """
        Initialize the Async Sandbox Client.

        Args:
            conn_info: The parameters needed to connect to the sandbox
            timeout: Request timeout in seconds (default: 30)
        """
        self.base_url = conn_info.public_url.rstrip("/")
        self.secret = conn_info.secret
        self.timeout = timeout
        self.headers = _build_headers(conn_info)
        self._client = httpx.AsyncClient(headers=self.headers, trust_env=True)
        self._closed = False

    async def close(self) -> None:
        """Close the HTTP client and release resources."""
        if not getattr(self, "_closed", True) and hasattr(self, "_client"):
            await self._client.aclose()
            self._closed = True

    async def __aenter__(self):
        """Async context manager entry - returns self."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit - automatically closes the client."""
        await self.close()

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        max_retries: int = 3,
        initial_backoff: float = 1.0,
        **kwargs,
    ) -> httpx.Response:
        """
        Make an HTTP request with retry logic for transient failures.

        Retries all 5xx responses and network errors (httpx.RequestError,
        excluding timeouts) with exponential backoff. 4xx responses are
        wrapped as SandboxRequestError; 5xx that persist after retries are
        wrapped as SandboxServiceError.

        Args:
            method: HTTP method (e.g., 'GET', 'POST')
            url: The URL to request
            max_retries: Maximum number of retry attempts
            initial_backoff: Initial backoff time in seconds (doubles each retry)
            **kwargs: Additional arguments to pass to httpx

        Returns:
            Response object

        Raises:
            SandboxRequestError: On a non-retryable HTTP error (4xx)
            SandboxServiceError: On an HTTP 5xx error after retries are exhausted
            SandboxTimeoutError: On request timeout
            SandboxError: On network errors after retries are exhausted
        """
        backoff = initial_backoff

        # Set default timeout if not provided
        if "timeout" not in kwargs:
            kwargs["timeout"] = self.timeout

        for attempt in range(max_retries + 1):
            try:
                response = await self._client.request(method, url, **kwargs)

                # Retry transient server errors (5xx) with backoff
                if response.status_code >= 500 and attempt < max_retries:
                    logger.debug(
                        f"Received {response.status_code} error, retrying... "
                        f"(attempt {attempt + 1}/{max_retries + 1})"
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2  # Exponential backoff
                    continue

                response.raise_for_status()
                return response

            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500 and attempt < max_retries:
                    logger.debug(
                        f"Received {e.response.status_code} error, retrying... "
                        f"(attempt {attempt + 1}/{max_retries + 1})"
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                if e.response.status_code >= 500:
                    raise SandboxServiceError(
                        status_code=e.response.status_code,
                        message=e.response.text,
                    ) from e
                raise SandboxRequestError(
                    status_code=e.response.status_code,
                    body=e.response.text,
                ) from e
            except httpx.TimeoutException as e:
                raise SandboxTimeoutError(
                    f"Request timed out after {kwargs.get('timeout', self.timeout)}s"
                ) from e
            except httpx.RequestError as e:
                # Transient network errors (connection refused, reset, ...)
                # are retried with the same backoff as server errors.
                if attempt < max_retries:
                    logger.debug(
                        f"Network error, retrying... "
                        f"(attempt {attempt + 1}/{max_retries + 1}): {e}"
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                logger.warning(f"Request failed after {max_retries} retries: {e}")
                raise SandboxError(f"Request to sandbox executor failed: {e}") from e

        # Unreachable in practice: every terminal path returns or raises.
        raise SandboxError(f"Request to {url} failed after {max_retries} retries")

    async def health(self) -> Dict[str, str]:
        """
        Check the health status of the server.

        Uses a short timeout and no retries since callers (wait_ready)
        already handle polling with backoff.

        Returns:
            Dict with status information

        Raises:
            httpx.HTTPStatusError: If the health check fails
            httpx.TimeoutException: If the health check times out
        """
        response = await self._client.get(f"{self.base_url}/health", timeout=5)
        response.raise_for_status()
        return response.json()

    async def run(
        self,
        cmd: str,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Execute a shell command in the sandbox.

        Args:
            cmd: The shell command to execute
            cwd: Optional working directory for command execution
            env: Optional environment variables to set/override
            timeout: Optional timeout in seconds for the request

        Returns:
            Dict containing stdout, stderr, error (if any), and exit code
        """
        payload: Dict[str, Any] = {"cmd": cmd}
        if cwd is not None:
            payload["cwd"] = cwd
        if env is not None:
            payload["env"] = env

        request_timeout = timeout if timeout is not None else self.timeout
        response = await self._request_with_retry(
            "POST",
            f"{self.base_url}/run",
            json=payload,
            timeout=request_timeout,
        )
        return response.json()

    async def run_streaming(
        self,
        cmd: str,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Execute a shell command in the sandbox and stream the output in real-time.

        This method uses Server-Sent Events (SSE) to stream command output line-by-line
        as it's produced. Use this for long-running commands where you want real-time
        output. For simple commands where buffered output is acceptable, use run() instead.

        Args:
            cmd: The shell command to execute
            cwd: Optional working directory for command execution
            env: Optional environment variables to set/override
            timeout: Optional timeout in seconds for the streaming request

        Yields:
            Dict events with the following types:

            - output events (as command produces output):
              {"stream": "stdout"|"stderr", "data": "line of output"}

            - complete event (when command finishes):
              {"code": <exit_code>, "error": false}

            - error event (if command fails to start):
              {"error": "error message"}

        Example:
            >>> client = AsyncSandboxClient("http://localhost:8080", "secret")
            >>> async for event in client.run_streaming("echo 'Hello'; sleep 1; echo 'World'"):
            ...     if "stream" in event:
            ...         print(f"{event['stream']}: {event['data']}")
            ...     elif "code" in event:
            ...         print(f"Exit code: {event['code']}")
        """
        payload: Dict[str, Any] = {"cmd": cmd}
        if cwd is not None:
            payload["cwd"] = cwd
        if env is not None:
            payload["env"] = env

        request_timeout = timeout if timeout is not None else self.timeout
        try:
            async with self._client.stream(
                "POST",
                f"{self.base_url}/run_streaming",
                json=payload,
                timeout=request_timeout,
            ) as response:
                if response.status_code >= 500:
                    await response.aread()
                    raise SandboxServiceError(
                        status_code=response.status_code,
                        message=response.text,
                    )
                response.raise_for_status()
                async for line in response.aiter_lines():
                    event = _parse_sse_line(line)
                    if event is not None:
                        yield event
        except httpx.TimeoutException as e:
            raise SandboxTimeoutError(
                f"Request timed out after {request_timeout}s"
            ) from e
        except httpx.RequestError as e:
            # E.g. the instance is torn down mid-stream during a redeployment.
            raise SandboxError(f"Connection to sandbox lost: {e}") from e

    async def write_file(self, path: str, content: str) -> Dict[str, Any]:
        """
        Write content to a file.

        Args:
            path: The file path to write to
            content: The content to write

        Returns:
            Dict with success status and error if any
        """
        payload = {"path": path, "content": content}
        response = await self._request_with_retry(
            "POST", f"{self.base_url}/write_file", json=payload
        )
        return response.json()

    async def read_file(self, path: str) -> Dict[str, Any]:
        """
        Read content from a file.

        Args:
            path: The file path to read from

        Returns:
            Dict with file content and error if any
        """
        payload = {"path": path}
        response = await self._request_with_retry(
            "POST", f"{self.base_url}/read_file", json=payload
        )
        return response.json()

    async def delete_file(self, path: str) -> Dict[str, Any]:
        """
        Delete a file.

        Args:
            path: The file path to delete

        Returns:
            Dict with success status and error if any
        """
        payload = {"path": path}
        response = await self._request_with_retry(
            "POST", f"{self.base_url}/delete_file", json=payload
        )
        return response.json()

    async def make_dir(self, path: str) -> Dict[str, Any]:
        """
        Create a directory (including parent directories).

        Args:
            path: The directory path to create

        Returns:
            Dict with success status and error if any
        """
        payload = {"path": path}
        response = await self._request_with_retry(
            "POST", f"{self.base_url}/make_dir", json=payload
        )
        return response.json()

    async def delete_dir(self, path: str) -> Dict[str, Any]:
        """
        Recursively delete a directory and all its contents.

        Args:
            path: The directory path to delete

        Returns:
            Dict with success status and error if any
        """
        payload = {"path": path}
        response = await self._request_with_retry(
            "POST", f"{self.base_url}/delete_dir", json=payload
        )
        return response.json()

    async def list_dir(self, path: str) -> Dict[str, Any]:
        """
        List the contents of a directory.

        Args:
            path: The directory path to list

        Returns:
            Dict with entries list and error if any
        """
        payload = {"path": path}
        response = await self._request_with_retry(
            "POST", f"{self.base_url}/list_dir", json=payload
        )
        return response.json()

    async def bind_port(self, port: int) -> Dict[str, Any]:
        """
        Bind a port to the TCP proxy for external access.

        Configures the TCP proxy to forward traffic to the specified port inside the sandbox.
        This allows you to expose services running inside the sandbox to external connections.

        Args:
            port: The port number to bind to (must be a valid port number)

        Returns:
            Dict with success status, message, and port information

        Notes:
            - Only one port can be bound at a time
            - Binding a new port will override the previous binding
            - The port must be available and accessible within the sandbox environment
        """
        payload = {"port": str(port)}
        response = await self._request_with_retry(
            "POST", f"{self.base_url}/bind_port", json=payload
        )
        return response.json()

    async def unbind_port(self, port: Optional[int] = None) -> Dict[str, Any]:
        """
        Unbind a port from the TCP proxy.

        Removes the TCP proxy port binding, stopping traffic forwarding to the previously bound port.

        Args:
            port: Optional port number to unbind. If provided, it must match the currently bound port.
                If not provided, any existing binding will be removed.

        Returns:
            Dict with success status and message

        Notes:
            - If a port is specified and doesn't match the currently bound port, the request will fail
            - After unbinding, the TCP proxy will no longer forward traffic
        """
        payload: Dict[str, Any] = {}
        if port is not None:
            payload["port"] = str(port)
        response = await self._request_with_retry(
            "POST", f"{self.base_url}/unbind_port", json=payload
        )
        return response.json()

    async def start_process(
        self, cmd: str, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Start a background process in the sandbox.

        Starts a long-running background process that continues executing even after
        the API call completes. Use this for servers, workers, or other long-running tasks.

        Args:
            cmd: The shell command to execute as a background process
            cwd: Optional working directory for the process
            env: Optional environment variables to set/override for the process

        Returns:
            Dict with process id and success status:
                - id: The unique process ID (UUID string)
                - success: True if the process was started successfully

        Example:
            >>> client = AsyncSandboxClient("http://localhost:8080", "secret")
            >>> result = await client.start_process("python -u server.py")
            >>> process_id = result["id"]
            >>> print(f"Started process: {process_id}")
        """
        payload: Dict[str, Any] = {"cmd": cmd}
        if cwd is not None:
            payload["cwd"] = cwd
        if env is not None:
            payload["env"] = env
        response = await self._request_with_retry(
            "POST", f"{self.base_url}/start_process", json=payload
        )
        return response.json()

    async def kill_process(self, process_id: str) -> Dict[str, Any]:
        """
        Kill a background process by its ID.

        Terminates a running background process. This sends a SIGTERM signal to the process,
        allowing it to clean up gracefully. If the process doesn't terminate within a timeout,
        it will be forcefully killed with SIGKILL.

        Args:
            process_id: The unique process ID (UUID string) to kill

        Returns:
            Dict with success status and error message if any

        Example:
            >>> client = AsyncSandboxClient("http://localhost:8080", "secret")
            >>> result = await client.kill_process("550e8400-e29b-41d4-a716-446655440000")
            >>> if result.get("success"):
            ...     print("Process killed successfully")
        """
        payload = {"id": process_id}
        response = await self._request_with_retry(
            "POST", f"{self.base_url}/kill_process", json=payload
        )
        return response.json()

    async def list_processes(self) -> Dict[str, Any]:
        """
        List all background processes.

        Returns information about all currently running and recently completed background
        processes. This includes both active processes and processes that have completed
        (which remain in memory until server restart).

        Returns:
            Dict with a list of processes:
                - processes: List of process objects, each containing:
                    - id: Process ID (UUID string)
                    - command: The command that was executed
                    - status: Process status (e.g., "running", "completed")
                    - pid: OS process ID (if running)
                    - exit_code: Exit code (if completed)
                    - started_at: ISO 8601 timestamp when process started
                    - completed_at: ISO 8601 timestamp when process completed (if applicable)

        Example:
            >>> client = AsyncSandboxClient("http://localhost:8080", "secret")
            >>> result = await client.list_processes()
            >>> for process in result.get("processes", []):
            ...     print(f"{process['id']}: {process['command']} - {process['status']}")
        """
        response = await self._request_with_retry(
            "GET", f"{self.base_url}/list_processes"
        )
        return response.json()
