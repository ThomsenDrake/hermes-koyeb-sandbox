# hermes-koyeb-sandbox

A Hermes Agent plugin that runs the `terminal` tool inside a [Koyeb Sandbox](https://www.koyeb.com) container. Registers the `koyeb` terminal backend.

## What it does

- One Koyeb Sandbox per task. Commands run in the container; output and exit codes come back to Hermes.
- **Non-persistent** (`terminal.container_persistent: false`): the sandbox is created on first command and deleted on cleanup.
- **Persistent** (`terminal.container_persistent: true`, default): a deterministically named sandbox is reused across sessions (resumed by name), and kept on cleanup.
- Stdout and stderr are merged into the terminal tool's `output`, as with other Hermes backends.

## Install

```bash
# from the repo
git clone https://github.com/ThomsenDrake/hermes-koyeb-sandbox \
  ~/.hermes/plugins/hermes-koyeb-sandbox   # or your profile's plugins/ dir
```

The `koyeb` SDK is required at runtime:

```bash
pip install 'koyeb-sdk>=1.5.2,<2'
```

## Enable

In the profile's `config.yaml`:

```yaml
terminal:
  backend: koyeb
  # optional:
  # container_persistent: true
  # timeout: 180
```

Or via env: `TERMINAL_ENV=koyeb`.

## Credentials

Never put tokens in code or config.yaml. Put them in the profile's `.env` (or the Hermes credential store):

| Variable | Required | Meaning |
|---|---|---|
| `KOYEB_API_TOKEN` | yes | Koyeb API token. Create one at https://app.koyeb.com/account/api |
| `KOYEB_API_HOST` | no | API host override (default `https://app.koyeb.com`) |

## Sandbox knobs (optional, via .env)

| Variable | Default | Meaning |
|---|---|---|
| `KOYEB_SANDBOX_IMAGE` | `koyeb/sandbox` | Docker image for the sandbox |
| `KOYEB_SANDBOX_REGION` | account default | Koyeb region (e.g. `fra`) |
| `KOYEB_SANDBOX_INSTANCE_TYPE` | `micro` | `micro`, `small`, `medium`, `large`, `xlarge` |
| `KOYEB_SANDBOX_NAME_PREFIX` | `hermes` | Sandbox name prefix; names are `<prefix>-<hash>` |
| `KOYEB_SANDBOX_TTL_SECONDS` | `0` (never) | Auto-delete the sandbox N seconds after creation |
| `KOYEB_SANDBOX_IDLE_TIMEOUT_SECONDS` | `0` (always-on) | Scale-to-zero idle timeout |
| `KOYEB_SANDBOX_WAIT_READY_TIMEOUT_SECONDS` | `300` | Create/readiness wait |
| `KOYEB_SANDBOX_REGISTRY_SECRET` | none | Koyeb secret name for private-image pulls |
| `KOYEB_SANDBOX_BLOCK_NETWORK` | `false` | `true` blocks all sandbox egress |

## Notes

- Commands execute via the Koyeb Sandbox exec API (`Sandbox.exec`), each wrapped by Hermes's shell machinery (env snapshots, cwd tracking, stdin staging).
- Host-looking cwds (`/home/...`, `/Users/...`, `/opt/data/...`) are mapped to `/workspace` inside the sandbox.
- The backend declares `KOYEB_API_TOKEN` / `KOYEB_API_KEY` in `strip_env_keys`, so the token is stripped from every subprocess the agent spawns and cannot leak into model-authored commands.
- `KOYEB_SANDBOX_TTL_SECONDS` and `KOYEB_SANDBOX_IDLE_TIMEOUT_SECONDS` map to Koyeb's `delete_after_delay` and `idle_timeout` create parameters.

## Tests

```bash
pip install pytest
PYTHONPATH=/opt/hermes python -m pytest tests/ -q
```

The Koyeb SDK boundary is faked; the Hermes `BaseEnvironment` machinery (snapshot bootstrap, cwd markers, process handles) runs for real.

## License

MIT
