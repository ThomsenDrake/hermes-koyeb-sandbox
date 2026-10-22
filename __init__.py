"""hermes-koyeb-sandbox plugin: Koyeb Sandbox terminal backend for Hermes Agent.

Install: clone (or `hermes plugins install`) into a profile's plugins/
directory. Enable: set ``terminal.backend: koyeb`` in config.yaml. Credentials
go in .env or the Hermes credential store — never in this repo.

Ships the pure-Python ``koyeb`` SDK under ``vendor/`` so no site-packages
write is needed (Hermes runtime venvs are root-owned). The vendored path is
prepended to ``sys.path`` only when ``koyeb`` is not already importable, so a
normally-installed SDK (``pip install 'koyeb-sdk>=1.5.2,<2'``) always wins.
"""

__version__ = "0.1.1"

import sys
from pathlib import Path


def _ensure_koyeb_importable() -> None:
    """Prefer an installed koyeb SDK; fall back to the vendored copy."""
    try:
        import koyeb  # noqa: F401
        return
    except ImportError:
        pass
    vendor = Path(__file__).resolve().parent / "vendor"
    if str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))


_ensure_koyeb_importable()


def register(ctx):
    from .hermes_koyeb_sandbox.provider import KoyebTerminalProvider

    ctx.register_terminal_environment_provider(KoyebTerminalProvider())
