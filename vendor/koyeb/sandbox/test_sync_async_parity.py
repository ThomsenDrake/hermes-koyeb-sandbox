"""Pins sync/async signature parity across the sandbox layer.

The Python SDK is the reference implementation for the client family, so async
twins must not silently drift from their sync originals.
"""

import ast
import inspect
import os
import unittest

from koyeb.sandbox.exec import AsyncSandboxExecutor, SandboxExecutor
from koyeb.sandbox.executor_client import AsyncSandboxClient, SandboxClient
from koyeb.sandbox.filesystem import (
    AsyncSandboxFileIO,
    AsyncSandboxFilesystem,
    SandboxFileIO,
    SandboxFilesystem,
)
from koyeb.sandbox.pool import (
    AsyncServicePool,
    ServicePool,
    claim,
    claim_async,
    get_claim,
    get_claim_async,
    list_claims,
    list_claims_async,
    wait_claim_ready,
    wait_claim_ready_async,
)
from koyeb.sandbox.sandbox import (
    AsyncSandbox,
    Sandbox,
    _cleanup_after_failure,
    _cleanup_after_failure_async,
)
from koyeb.sandbox.clients import (
    create_async_sandbox_client,
    create_sandbox_client,
    get_api_clients,
    get_async_api_clients,
)

CLASS_PAIRS = [
    ("Sandbox", Sandbox, AsyncSandbox),
    ("ServicePool", ServicePool, AsyncServicePool),
    ("SandboxExecutor", SandboxExecutor, AsyncSandboxExecutor),
    ("SandboxFilesystem", SandboxFilesystem, AsyncSandboxFilesystem),
    ("SandboxFileIO", SandboxFileIO, AsyncSandboxFileIO),
    ("SandboxClient", SandboxClient, AsyncSandboxClient),
]

FUNCTION_PAIRS = [
    ("claim", claim, claim_async),
    ("get_claim", get_claim, get_claim_async),
    ("list_claims", list_claims, list_claims_async),
    ("wait_claim_ready", wait_claim_ready, wait_claim_ready_async),
    ("_cleanup_after_failure", _cleanup_after_failure, _cleanup_after_failure_async),
    ("get_api_clients", get_api_clients, get_async_api_clients),
    ("create_sandbox_client", create_sandbox_client, create_async_sandbox_client),
]

# AsyncSandbox.__init__ forwards to Sandbox.__init__ via *args/**kwargs.
STRUCTURAL_ALLOWLIST = {("Sandbox", "__init__")}

# Sync-only public surface per class; async either inlines these or lacks
# a twin. New entries are deliberate decisions, not accidents.
SYNC_ONLY_PUBLIC_ALLOWLIST = {
    "Sandbox": {"get_domain", "get_tcp_proxy_info", "template"},
    "ServicePool": set(),
    "SandboxExecutor": set(),
    "SandboxFilesystem": set(),
    "SandboxFileIO": set(),
    "SandboxClient": set(),
}


def _params(fn):
    """Parameter map with self/cls stripped so bound and unbound forms compare."""
    params = list(inspect.signature(fn).parameters.values())
    if params and params[0].name in ("self", "cls"):
        params = params[1:]
    return {p.name: p for p in params}


def _own_callable_names(cls):
    return {
        name
        for name in vars(cls)
        if callable(getattr(cls, name, None))
    }


class TestSyncAsyncSignatureParity(unittest.TestCase):
    def _assert_twins_match(self, label, sync_fn, async_fn, allow_structural=False):
        sync_params = _params(sync_fn)
        async_params = _params(async_fn)
        shared = set(sync_params)
        if not allow_structural:
            # Positional callers depend on order and kind, not just names.
            self.assertEqual(
                [(p.name, p.kind) for p in sync_params.values()],
                [(p.name, p.kind) for p in async_params.values()],
                f"{label}: parameter order or kind differs",
            )
        else:
            shared &= set(async_params)
        for name in sorted(shared):
            self.assertEqual(
                sync_params[name].default,
                async_params[name].default,
                f"{label}: default for '{name}' differs",
            )

    def test_async_create_matches_sync_defaults(self):
        # Explicit canary: keeps the flagship drift readable as the sweep grows.
        sync_params = _params(Sandbox.create)
        async_params = _params(AsyncSandbox.create)
        self.assertEqual(
            sync_params["idle_timeout"].default,
            async_params["idle_timeout"].default,
            "AsyncSandbox.create idle_timeout default drifted from sync",
        )
        self.assertEqual(
            sync_params["enable_mesh"].default,
            async_params["enable_mesh"].default,
            "AsyncSandbox.create enable_mesh default drifted from sync",
        )

    def test_class_method_twins_have_matching_signatures(self):
        for name, sync_cls, async_cls in CLASS_PAIRS:
            shared = _own_callable_names(sync_cls) & _own_callable_names(async_cls)
            for method in sorted(shared):
                self._assert_twins_match(
                    f"{name}.{method}",
                    getattr(sync_cls, method),
                    getattr(async_cls, method),
                    allow_structural=(name, method) in STRUCTURAL_ALLOWLIST,
                )

    def test_module_function_twins_have_matching_signatures(self):
        for name, sync_fn, async_fn in FUNCTION_PAIRS:
            self._assert_twins_match(name, sync_fn, async_fn)

    def test_sync_only_public_methods_are_pinned(self):
        # Methods without an async override are invisible to the twins
        # check; adding one requires an explicit decision recorded here.
        for name, sync_cls, async_cls in CLASS_PAIRS:
            sync_only = _own_callable_names(sync_cls) - _own_callable_names(async_cls)
            sync_only = {n for n in sync_only if not n.startswith("_")}
            self.assertEqual(
                SYNC_ONLY_PUBLIC_ALLOWLIST[name],
                sync_only,
                f"{name}: sync-only public methods",
            )


class TestNoneDefaultsAreOptional(unittest.TestCase):
    """A None default with a bare annotation lies to type checkers."""

    def test_no_bare_annotation_with_none_default(self):
        violations = self._find_violations()
        self.assertEqual(
            [], violations, "Non-Optional annotations with None default found"
        )

    @staticmethod
    def _find_violations():
        violations = []
        sandbox_dir = os.path.dirname(os.path.abspath(__file__))
        for fname in sorted(os.listdir(sandbox_dir)):
            if not fname.endswith(".py") or fname.startswith("test_"):
                continue
            with open(os.path.join(sandbox_dir, fname)) as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    violations.extend(
                        TestNoneDefaultsAreOptional._check_function(fname, node)
                    )
        return violations

    @staticmethod
    def _check_function(fname, fn):
        violations = []
        pos = fn.args.posonlyargs + fn.args.args
        for default, arg in zip(fn.args.defaults, pos[len(pos) - len(fn.args.defaults):]):
            violations.extend(
                TestNoneDefaultsAreOptional._check_arg(fname, fn, arg, default)
            )
        for arg, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
            if default is not None:
                violations.extend(
                    TestNoneDefaultsAreOptional._check_arg(fname, fn, arg, default)
                )
        return violations

    @staticmethod
    def _check_arg(fname, fn, arg, default):
        if not (isinstance(default, ast.Constant) and default.value is None):
            return []
        if arg.annotation is None:
            return []
        ann = ast.unparse(arg.annotation)
        if (
            "Optional" in ann
            or "Union" in ann
            or ann == "Any"
            or ann.endswith("| None")
        ):
            return []
        return [f"{fname}:{fn.lineno} {fn.name}({arg.arg}: {ann} = None)"]


if __name__ == "__main__":
    unittest.main()
