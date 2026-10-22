"""hermes-koyeb-sandbox plugin: Koyeb Sandbox terminal backend for Hermes Agent.

Install: clone (or `hermes plugins install`) into a profile's plugins/
directory. Enable: set ``terminal.backend: koyeb`` in config.yaml. Credentials
go in .env or the Hermes credential store — never in this repo.
"""

__version__ = "0.1.0"


def register(ctx):
    from .hermes_koyeb_sandbox.provider import KoyebTerminalProvider

    ctx.register_terminal_environment_provider(KoyebTerminalProvider())
