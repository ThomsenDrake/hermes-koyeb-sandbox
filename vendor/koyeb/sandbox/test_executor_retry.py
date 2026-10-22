import asyncio
import unittest
from unittest.mock import patch

import httpx

from koyeb.sandbox.executor_client import (
    AsyncSandboxClient,
    ConnectionInfo,
    SandboxClient,
)
from koyeb.sandbox.errors import (
    SandboxError,
    SandboxRequestError,
    SandboxServiceError,
    SandboxTimeoutError,
)


class _ScriptedHandler:
    """MockTransport handler playing a scripted sequence of responses/errors."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(request)

    @staticmethod
    def response(status_code, **kwargs):
        return lambda request: httpx.Response(status_code, request=request, **kwargs)


_CONN = ConnectionInfo(public_url="https://sb.example", routing_key=None, secret="s")


def _make_sync_client(handler):
    client = SandboxClient(_CONN)
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client, handler


def _make_async_client(handler):
    client = AsyncSandboxClient(_CONN)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, handler


class TestErrorHierarchy(unittest.TestCase):
    def test_service_error_is_request_error(self):
        self.assertTrue(issubclass(SandboxServiceError, SandboxRequestError))
        self.assertTrue(issubclass(SandboxRequestError, SandboxError))

    def test_service_error_message_unchanged(self):
        err = SandboxServiceError(status_code=503, message="boom")
        self.assertEqual(str(err), "Sandbox service error (503): boom")
        self.assertEqual(err.status_code, 503)


class TestSyncRetryScope(unittest.TestCase):
    """Executor requests retry all 5xx (not just 503) and network errors,
    with exponential backoff; 4xx wrap as SandboxRequestError."""

    def test_503_then_200(self):
        handler = _ScriptedHandler(
            [_ScriptedHandler.response(503), _ScriptedHandler.response(200)]
        )
        client, handler = _make_sync_client(handler)
        with patch("koyeb.sandbox.executor_client.time.sleep") as sleep:
            response = client._request_with_retry("GET", "https://sb.example/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(handler.calls, 2)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0])

    def test_all_5xx_retried(self):
        handler = _ScriptedHandler(
            [
                _ScriptedHandler.response(500),
                _ScriptedHandler.response(502),
                _ScriptedHandler.response(504),
                _ScriptedHandler.response(200),
            ]
        )
        client, handler = _make_sync_client(handler)
        with patch("koyeb.sandbox.executor_client.time.sleep") as sleep:
            response = client._request_with_retry("GET", "https://sb.example/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(handler.calls, 4)
        self.assertEqual(
            [c.args[0] for c in sleep.call_args_list], [1.0, 2.0, 4.0]
        )

    def test_5xx_exhausted_raises_service_error(self):
        handler = _ScriptedHandler([_ScriptedHandler.response(500)] * 4)
        client, handler = _make_sync_client(handler)
        with patch("koyeb.sandbox.executor_client.time.sleep"):
            with self.assertRaises(SandboxServiceError) as cm:
                client._request_with_retry("GET", "https://sb.example/health")
        self.assertEqual(cm.exception.status_code, 500)
        self.assertIsInstance(cm.exception, SandboxRequestError)
        self.assertEqual(handler.calls, 4)

    def test_4xx_wrapped_as_request_error(self):
        handler = _ScriptedHandler(
            [_ScriptedHandler.response(404, json={"error": "nope"})]
        )
        client, _ = _make_sync_client(handler)
        with self.assertRaises(SandboxRequestError) as cm:
            client._request_with_retry("GET", "https://sb.example/health")
        self.assertEqual(cm.exception.status_code, 404)
        self.assertIn("nope", str(cm.exception))
        self.assertNotIsInstance(cm.exception, httpx.HTTPStatusError)
        self.assertIsInstance(cm.exception, SandboxError)

    def test_network_error_retried_then_wrapped(self):
        handler = _ScriptedHandler(
            [
                lambda request: (_ for _ in ()).throw(
                    httpx.ConnectError("refused", request=request)
                )
            ]
            * 4
        )
        client, handler = _make_sync_client(handler)
        with patch("koyeb.sandbox.executor_client.time.sleep") as sleep:
            with self.assertRaises(SandboxError) as cm:
                client._request_with_retry("GET", "https://sb.example/health")
        self.assertNotIsInstance(cm.exception, SandboxRequestError)
        self.assertEqual(handler.calls, 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 2.0, 4.0])

    def test_network_error_recovers(self):
        def connect_error(request):
            raise httpx.ConnectError("refused", request=request)

        handler = _ScriptedHandler(
            [connect_error, _ScriptedHandler.response(200)]
        )
        client, handler = _make_sync_client(handler)
        with patch("koyeb.sandbox.executor_client.time.sleep"):
            response = client._request_with_retry("GET", "https://sb.example/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(handler.calls, 2)

    def test_timeout_still_sandbox_timeout(self):
        def read_timeout(request):
            raise httpx.ReadTimeout("too slow", request=request)

        handler = _ScriptedHandler([read_timeout])
        client, _ = _make_sync_client(handler)
        with self.assertRaises(SandboxTimeoutError):
            client._request_with_retry("GET", "https://sb.example/health")


class TestAsyncRetryScope(unittest.TestCase):
    def test_all_5xx_retried(self):
        handler = _ScriptedHandler(
            [
                _ScriptedHandler.response(500),
                _ScriptedHandler.response(503),
                _ScriptedHandler.response(200),
            ]
        )
        client, handler = _make_async_client(handler)

        async def run():
            with patch("koyeb.sandbox.executor_client.asyncio.sleep") as sleep:
                response = await client._request_with_retry(
                    "GET", "https://sb.example/health"
                )
                return response, sleep

        response, sleep = asyncio.run(run())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(handler.calls, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 2.0])

    def test_4xx_wrapped_as_request_error(self):
        handler = _ScriptedHandler(
            [_ScriptedHandler.response(401, json={"error": "bad token"})]
        )
        client, _ = _make_async_client(handler)

        async def run():
            return await client._request_with_retry(
                "GET", "https://sb.example/health"
            )

        with self.assertRaises(SandboxRequestError) as cm:
            asyncio.run(run())
        self.assertEqual(cm.exception.status_code, 401)

    def test_network_error_retried_then_wrapped(self):
        def connect_error(request):
            raise httpx.ConnectError("refused", request=request)

        handler = _ScriptedHandler([connect_error] * 4)
        client, handler = _make_async_client(handler)

        async def run():
            with patch("koyeb.sandbox.executor_client.asyncio.sleep"):
                return await client._request_with_retry(
                    "GET", "https://sb.example/health"
                )

        with self.assertRaises(SandboxError) as cm:
            asyncio.run(run())
        self.assertNotIsInstance(cm.exception, SandboxRequestError)
        self.assertEqual(handler.calls, 4)


if __name__ == "__main__":
    unittest.main()
