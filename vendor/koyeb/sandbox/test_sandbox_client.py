import asyncio
import unittest
from unittest.mock import patch

import httpx

from koyeb.sandbox.executor_client import (
    AsyncSandboxClient,
    ConnectionInfo,
    SandboxClient,
)
from koyeb.sandbox.sandbox import AsyncSandbox, Sandbox
from koyeb.sandbox.errors import SandboxError


class TestGetClientWhenUrlUnavailable(unittest.TestCase):
    """A gone sandbox makes _get_sandbox_url() return None (the metadata/domain
    lookups swallow NotFound and return None). _get_client/_get_async_client must
    raise SandboxError in that case, as their docstring promises, rather than
    letting a raw ``TypeError: cannot unpack non-iterable NoneType object`` escape.
    """

    def test_get_client_raises_sandbox_error(self):
        sb = Sandbox.__new__(Sandbox)
        sb._client = None
        sb.sandbox_secret = None
        with patch.object(Sandbox, "_get_sandbox_url", return_value=None):
            with self.assertRaises(SandboxError):
                sb._get_client()

    def test_get_async_client_raises_sandbox_error(self):
        sb = AsyncSandbox.__new__(AsyncSandbox)
        sb._async_client = None
        sb.sandbox_secret = None
        with patch.object(AsyncSandbox, "_get_sandbox_url", return_value=None):
            with self.assertRaises(SandboxError):
                sb._get_async_client()


class TestRunStreamingConnectionLoss(unittest.TestCase):
    """A dropped connection mid-stream (e.g. the instance is torn down during a
    redeployment) must surface as SandboxError, not a raw httpx transport error.
    """

    _CONN = ConnectionInfo(
        public_url="https://sb.example", routing_key=None, secret="s"
    )

    def test_sync_run_streaming_wraps_transport_error(self):
        client = SandboxClient(self._CONN)
        with patch.object(
            client._client,
            "stream",
            side_effect=httpx.RemoteProtocolError("peer closed connection"),
        ):
            with self.assertRaises(SandboxError):
                list(client.run_streaming("echo hi"))

    def test_async_run_streaming_wraps_transport_error(self):
        client = AsyncSandboxClient(self._CONN)

        async def consume():
            async for _ in client.run_streaming("echo hi"):
                pass

        with patch.object(
            client._client,
            "stream",
            side_effect=httpx.RemoteProtocolError("peer closed connection"),
        ):
            with self.assertRaises(SandboxError):
                asyncio.run(consume())


if __name__ == "__main__":
    unittest.main()
