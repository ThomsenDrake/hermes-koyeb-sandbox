"""Pins the executor-reply seam of the filesystem layer: every dict
reply is interpreted once, error strings classify into the SDK taxonomy,
and shell fallbacks escape their arguments."""

import asyncio
import base64
import os
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from koyeb.sandbox.exec import CommandResult, CommandStatus
from koyeb.sandbox.filesystem import (
    AsyncSandboxFilesystem,
    FileInfo,
    SandboxFilesystem,
    SandboxFilesystemError,
    SandboxFileExistsError,
    SandboxFileNotFoundError,
)


class _FakeSyncClient:
    """Executor-reply fake: per-method dict replies or exceptions."""

    def __init__(self, **replies):
        self.replies = replies
        self.calls = []

    def _reply(self, method, *args):
        self.calls.append((method, *args))
        reply = self.replies.get(method, {})
        if isinstance(reply, Exception):
            raise reply
        return reply

    def write_file(self, path, content):
        return self._reply("write_file", path, content)

    def read_file(self, path):
        return self._reply("read_file", path)

    def make_dir(self, path):
        return self._reply("make_dir", path)

    def list_dir(self, path):
        return self._reply("list_dir", path)

    def delete_file(self, path):
        return self._reply("delete_file", path)

    def delete_dir(self, path):
        return self._reply("delete_dir", path)


class _FakeAsyncClient(_FakeSyncClient):
    async def write_file(self, path, content):
        return _FakeSyncClient.write_file(self, path, content)

    async def read_file(self, path):
        return _FakeSyncClient.read_file(self, path)

    async def make_dir(self, path):
        return _FakeSyncClient.make_dir(self, path)

    async def list_dir(self, path):
        return _FakeSyncClient.list_dir(self, path)

    async def delete_file(self, path):
        return _FakeSyncClient.delete_file(self, path)

    async def delete_dir(self, path):
        return _FakeSyncClient.delete_dir(self, path)


@contextmanager
def _fs(client):
    """SandboxFilesystem with its executor client patched for the block."""
    fs = SandboxFilesystem(sandbox=None)
    with patch.object(SandboxFilesystem, "_get_client", return_value=client):
        yield fs


@contextmanager
def _async_fs(client):
    """AsyncSandboxFilesystem with its async client patched for the block."""
    fs = AsyncSandboxFilesystem(sandbox=None)
    with patch.object(
        AsyncSandboxFilesystem, "_get_async_client", return_value=client
    ):
        yield fs


class TestReplyInterpretation(unittest.TestCase):
    """Dict replies classify once: error strings map to the taxonomy."""

    def test_write_file_error(self):
        with _fs(_FakeSyncClient(write_file={"error": "disk full"})) as fs:
            with self.assertRaises(SandboxFilesystemError) as cm:
                fs.write_file("/a", "x")
        self.assertEqual(str(cm.exception), "Failed to write file: disk full")

    def test_write_file_base64_encodes_bytes(self):
        client = _FakeSyncClient()
        with _fs(client) as fs:
            fs.write_file("/b", b"\x00\x01", encoding="base64")
        method, path, content = client.calls[0]
        self.assertEqual((method, path), ("write_file", "/b"))
        self.assertEqual(content, base64.b64encode(b"\x00\x01").decode("ascii"))

    def test_read_file_returns_file_info(self):
        with _fs(_FakeSyncClient(read_file={"content": "hi"})) as fs:
            self.assertEqual(
                fs.read_file("/a"), FileInfo(content="hi", encoding="utf-8")
            )

    def test_read_file_base64_decodes_bytes(self):
        encoded = base64.b64encode(b"\x00\x01").decode("ascii")
        with _fs(_FakeSyncClient(read_file={"content": encoded})) as fs:
            info = fs.read_file("/a", encoding="base64")
        self.assertEqual(info.content, b"\x00\x01")
        self.assertEqual(info.encoding, "base64")

    def test_read_file_not_found(self):
        with _fs(
            _FakeSyncClient(read_file={"error": "no such file or directory"})
        ) as fs:
            with self.assertRaises(SandboxFileNotFoundError) as cm:
                fs.read_file("/a")
        self.assertEqual(str(cm.exception), "File not found: /a")

    def test_read_file_generic_error(self):
        with _fs(_FakeSyncClient(read_file={"error": "boom"})) as fs:
            with self.assertRaises(SandboxFilesystemError) as cm:
                fs.read_file("/a")
        self.assertEqual(str(cm.exception), "Failed to read file: boom")

    def test_exception_strings_classify_too(self):
        # Transport failures map through the same classifier, chained.
        with _fs(_FakeSyncClient(read_file=RuntimeError("... no such file ..."))) as fs:
            with self.assertRaises(SandboxFileNotFoundError) as cm:
                fs.read_file("/a")
        self.assertIsNotNone(cm.exception.__cause__)

    def test_mkdir_exists_error(self):
        with _fs(_FakeSyncClient(make_dir={"error": "file already exists"})) as fs:
            with self.assertRaises(SandboxFileExistsError) as cm:
                fs.mkdir("/d")
        self.assertEqual(str(cm.exception), "Directory already exists: /d")

    def test_mkdir_generic_error(self):
        with _fs(_FakeSyncClient(make_dir={"error": "boom"})) as fs:
            with self.assertRaises(SandboxFilesystemError) as cm:
                fs.mkdir("/d")
        self.assertEqual(str(cm.exception), "Failed to create directory: boom")

    def test_list_dir_entries_and_not_found(self):
        with _fs(_FakeSyncClient(list_dir={"entries": ["a", "b"]})) as fs:
            self.assertEqual(fs.list_dir("/d"), ["a", "b"])
        with _fs(_FakeSyncClient(list_dir={"error": "no such file"})) as fs:
            with self.assertRaises(SandboxFileNotFoundError) as cm:
                fs.list_dir("/d")
        self.assertEqual(str(cm.exception), "Directory not found: /d")

    def test_delete_file_not_found(self):
        with _fs(_FakeSyncClient(delete_file={"error": "no such file"})) as fs:
            with self.assertRaises(SandboxFileNotFoundError) as cm:
                fs.delete_file("/f")
        self.assertEqual(str(cm.exception), "File not found: /f")

    def test_delete_dir_not_empty(self):
        with _fs(_FakeSyncClient(delete_dir={"error": "directory not empty"})) as fs:
            with self.assertRaises(SandboxFilesystemError) as cm:
                fs.delete_dir("/d")
        self.assertEqual(str(cm.exception), "Directory not empty: /d")


class TestShellFallbacks(unittest.TestCase):
    """mv/test/rm run through the executor with escaped arguments."""

    @staticmethod
    def _result(exit_code=0, stderr=""):
        return CommandResult(
            stdout="",
            stderr=stderr,
            exit_code=exit_code,
            status=CommandStatus.FINISHED if exit_code == 0 else CommandStatus.FAILED,
        )

    @contextmanager
    def _fs(self, results):
        fs = SandboxFilesystem(sandbox=None)
        calls = []
        queue = list(results)

        def executor(cmd):
            calls.append(cmd)
            return queue.pop(0)

        with patch.object(SandboxFilesystem, "_get_executor", return_value=executor):
            yield fs, calls

    def test_rename_escapes_and_reports_not_found(self):
        with self._fs([self._result(), self._result(1, "mv: no such file")]) as (fs, calls):
            fs.rename_file("/a b", "/c d")
            self.assertEqual(calls[0], "mv '/a b' '/c d'")
            with self.assertRaises(SandboxFileNotFoundError):
                fs.rename_file("/gone", "/c")

    def test_exists_is_file_is_dir_use_test_builtins(self):
        with self._fs(
            [self._result(), self._result(1), self._result()]
        ) as (fs, calls):
            self.assertTrue(fs.exists("/x"))
            self.assertFalse(fs.is_file("/x"))
            self.assertTrue(fs.is_dir("/x"))
        self.assertEqual(
            [c.split()[:2] for c in calls],
            [["test", "-e"], ["test", "-f"], ["test", "-d"]],
        )

    def test_rm_recursive_flag_and_escaping(self):
        with self._fs([self._result(), self._result()]) as (fs, calls):
            fs.rm("/x")
            fs.rm("/x y", recursive=True)
        # shlex.quote leaves plain paths bare and quotes special ones
        self.assertEqual(calls, ["rm /x", "rm -rf '/x y'"])

    def test_rm_not_found(self):
        with self._fs([self._result(1, "no such file")]) as (fs, _):
            with self.assertRaises(SandboxFileNotFoundError):
                fs.rm("/gone")

    def test_move_file(self):
        with self._fs([self._result()]) as (fs, calls):
            fs.move_file("/a b", "/c")
        self.assertEqual(calls, ["mv '/a b' /c"])


class TestBatchAndTransfers(unittest.TestCase):
    def test_write_files_batches(self):
        client = _FakeSyncClient()
        with _fs(client) as fs:
            fs.write_files(
                [
                    {"path": "/a", "content": "1"},
                    {"path": "/b", "content": "2", "encoding": "base64"},
                ]
            )
        self.assertEqual([c[1] for c in client.calls], ["/a", "/b"])

    def test_upload_download_roundtrip(self):
        client = _FakeSyncClient(
            read_file={"content": base64.b64encode(b"payload").decode("ascii")}
        )
        with _fs(client) as fs:
            with tempfile.TemporaryDirectory() as tmp:
                local = os.path.join(tmp, "f.bin")
                with open(local, "wb") as f:
                    f.write(b"payload")
                fs.upload_file(local, "/remote", encoding="base64")
                self.assertEqual(client.calls[0][1], "/remote")
                target = os.path.join(tmp, "out.bin")
                fs.download_file("/remote", target, encoding="base64")
                with open(target, "rb") as f:
                    self.assertEqual(f.read(), b"payload")

    def test_upload_missing_local_file(self):
        with _fs(_FakeSyncClient()) as fs:
            with self.assertRaises(SandboxFileNotFoundError):
                fs.upload_file("/no/such/local", "/remote")


class TestSandboxFileIO(unittest.TestCase):
    def test_read_write_modes(self):
        with _fs(_FakeSyncClient(read_file={"content": "abc"})) as fs:
            with fs.open("/a", "r") as handle:
                self.assertEqual(handle.read(), "abc")
        client = _FakeSyncClient()
        with _fs(client) as fs:
            with fs.open("/b", "w") as handle:
                handle.write("data")
        self.assertEqual(client.calls[0][0], "write_file")

    def test_wrong_mode_raises(self):
        with _fs(_FakeSyncClient()) as fs:
            with self.assertRaises(ValueError):
                fs.open("/a", "r").write("x")
        with _fs(_FakeSyncClient()) as fs:
            with self.assertRaises(ValueError):
                fs.open("/a", "w").read()

    def test_closed_handle_raises(self):
        with _fs(_FakeSyncClient()) as fs:
            handle = fs.open("/a", "r")
            handle.close()
            with self.assertRaises(ValueError):
                handle.read()


class TestAsyncReplyInterpretation(unittest.TestCase):
    """Async twin classifies through the same taxonomy."""

    def test_read_file_not_found(self):
        with _async_fs(_FakeAsyncClient(read_file={"error": "no such file"})) as fs:
            with self.assertRaises(SandboxFileNotFoundError) as cm:
                asyncio.run(fs.read_file("/a"))
        self.assertEqual(str(cm.exception), "File not found: /a")

    def test_mkdir_exists_error(self):
        with _async_fs(_FakeAsyncClient(make_dir={"error": "already exists"})) as fs:
            with self.assertRaises(SandboxFileExistsError) as cm:
                asyncio.run(fs.mkdir("/d"))
        self.assertEqual(str(cm.exception), "Directory already exists: /d")

    def test_list_dir_entries(self):
        with _async_fs(_FakeAsyncClient(list_dir={"entries": ["x"]})) as fs:
            self.assertEqual(asyncio.run(fs.list_dir("/d")), ["x"])

    def test_delete_dir_not_empty(self):
        with _async_fs(
            _FakeAsyncClient(delete_dir={"error": "directory not empty"})
        ) as fs:
            with self.assertRaises(SandboxFilesystemError) as cm:
                asyncio.run(fs.delete_dir("/d"))
        self.assertEqual(str(cm.exception), "Directory not empty: /d")


if __name__ == "__main__":
    unittest.main()
