"""hermes-koyeb-sandbox plugin: Koyeb Sandbox terminal backend for Hermes Agent.

Hermes selects every sandbox through ``terminal.backend``. This plugin registers
the ``koyeb`` backend the same way Docker, Modal, Daytona, Vercel Sandbox, and
Singularity do. It does not register a model-facing tool: with the plugin
enabled and ``terminal.backend`` left at ``local``, commands stay on the local
machine. Set ``terminal.backend: koyeb`` to move the terminal, file tools, and
``execute_code`` into a Koyeb Sandbox together.

Install: clone (or `hermes plugins install`) into a profile's plugins/
directory. Enable the plugin, then set ``terminal.backend: koyeb`` in
config.yaml and restart the gateway. Credentials go in .env or the Hermes
credential store — never in this repo.

Ships the pure-Python ``koyeb`` SDK under ``vendor/`` so no site-packages
write is needed (Hermes runtime venvs are root-owned). The vendored path is
prepended to ``sys.path`` only when ``koyeb`` is not already importable, so a
normally-installed SDK (``pip install 'koyeb-sdk>=1.5.2,<2'``) always wins.
"""

__version__ = "0.2.0"

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
