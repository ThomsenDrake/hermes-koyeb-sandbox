import asyncio
import unittest
from unittest.mock import patch

from koyeb.sandbox.exec import (
    AsyncSandboxExecutor,
    CommandResult,
    CommandStatus,
    SandboxCommandError,
    SandboxExecutor,
)


class FakeSyncClient:
    def __init__(self, events=None, run_response=None):
        self._events = events or []
        self._run_response = run_response or {}

    def run_streaming(self, cmd=None, cwd=None, env=None, timeout=None):
        yield from self._events

    def run(self, cmd=None, cwd=None, env=None, timeout=None):
        return self._run_response


class FakeAsyncClient:
    def __init__(self, events=None, run_response=None):
        self._events = events or []
        self._run_response = run_response or {}

    async def run_streaming(self, cmd=None, cwd=None, env=None, timeout=None):
        for event in self._events:
            yield event

    async def run(self, cmd=None, cwd=None, env=None, timeout=None):
        return self._run_response


def _exec(client, **kwargs):
    executor = SandboxExecutor(None)
    with patch.object(SandboxExecutor, "_get_client", return_value=client):
        return executor("cmd", **kwargs)


def _exec_async(client, **kwargs):
    executor = AsyncSandboxExecutor(None)
    with patch.object(AsyncSandboxExecutor, "_get_async_client", return_value=client):

        async def run():
            return await executor("cmd", **kwargs)

        return asyncio.run(run())


class TestRaiseOnErrorOptIn(unittest.TestCase):
    """Default behavior unchanged: a failed command returns a failed
    CommandResult. With raise_on_error=True, a failed command raises
    SandboxCommandError carrying the result — so the exported class is no
    longer dead code."""

    def test_default_returns_failed_result(self):
        client = FakeSyncClient(
            events=[{"stream": "stdout", "data": "out"}, {"code": 1}]
        )
        result = _exec(client)
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 1)

    def test_raise_on_error_streaming_failure(self):
        client = FakeSyncClient(
            events=[{"stream": "stdout", "data": "out"}, {"code": 1}]
        )
        with self.assertRaises(SandboxCommandError) as cm:
            _exec(client, raise_on_error=True)
        self.assertIsInstance(cm.exception.result, CommandResult)
        self.assertEqual(cm.exception.result.exit_code, 1)
        self.assertIn("exit code 1", str(cm.exception))

    def test_raise_on_error_error_event(self):
        client = FakeSyncClient(events=[{"error": "failed to start"}])
        with self.assertRaises(SandboxCommandError):
            _exec(client, raise_on_error=True)

    def test_raise_on_error_success_does_not_raise(self):
        client = FakeSyncClient(
            events=[{"stream": "stdout", "data": "hi"}, {"code": 0}]
        )
        result = _exec(client, raise_on_error=True)
        self.assertTrue(result.success)

    def test_raise_on_error_non_streaming(self):
        client = FakeSyncClient(run_response={"stdout": "", "stderr": "boom", "code": 2})
        with self.assertRaises(SandboxCommandError) as cm:
            _exec(client, stream=False, raise_on_error=True)
        self.assertEqual(cm.exception.result.stderr, "boom")
        # default still returns the failed result
        result = _exec(FakeSyncClient(run_response={"stdout": "", "stderr": "boom", "code": 2}), stream=False)
        self.assertEqual(result.status, CommandStatus.FAILED)


class TestRaiseOnErrorAsyncMirror(unittest.TestCase):
    def test_default_returns_failed_result(self):
        client = FakeAsyncClient(
            events=[{"stream": "stdout", "data": "out"}, {"code": 1}]
        )
        result = _exec_async(client)
        self.assertFalse(result.success)

    def test_raise_on_error_streaming_failure(self):
        client = FakeAsyncClient(
            events=[{"stream": "stderr", "data": "bad"}, {"code": 1}]
        )
        with self.assertRaises(SandboxCommandError) as cm:
            _exec_async(client, raise_on_error=True)
        self.assertEqual(cm.exception.result.exit_code, 1)
        self.assertIn("exit code 1", str(cm.exception))

    def test_raise_on_error_non_streaming(self):
        client = FakeAsyncClient(run_response={"stdout": "", "stderr": "x", "code": 3})
        with self.assertRaises(SandboxCommandError):
            _exec_async(client, stream=False, raise_on_error=True)

    def test_default_non_streaming_returns_failed_result(self):
        client = FakeAsyncClient(run_response={"stdout": "", "stderr": "boom", "code": 2})
        result = _exec_async(client, stream=False)
        self.assertEqual(result.status, CommandStatus.FAILED)

    def test_raise_on_error_error_event(self):
        client = FakeAsyncClient(events=[{"error": "failed to start"}])
        with self.assertRaises(SandboxCommandError):
            _exec_async(client, raise_on_error=True)

    def test_raise_on_error_success_does_not_raise(self):
        client = FakeAsyncClient(
            events=[{"stream": "stdout", "data": "hi"}, {"code": 0}]
        )
        result = _exec_async(client, raise_on_error=True)
        self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main()
