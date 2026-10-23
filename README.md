# hermes-koyeb-sandbox

A Hermes Agent plugin that runs the terminal, file tools, and `execute_code` inside a [Koyeb Sandbox](https://www.koyeb.com/docs/sandboxes). It registers the `koyeb` terminal backend. It does not register a tool.

Hermes has one execution slot: `terminal.backend` (env: `TERMINAL_ENV`). Docker, Modal, Daytona, Vercel Sandbox, SSH, and plugin backends such as Singularity and this one are all selected there. `execute_code`, the file tools, and `delegate_task` children use that same backend. There is no second "sandbox provider" you can point at Koyeb while `terminal.backend` stays `local`.

## Install

Into the profile that should run commands in Koyeb (the default profile is `~/.hermes`):

```bash
hermes plugins install https://github.com/ThomsenDrake/hermes-koyeb-sandbox --enable
```

From a checkout of this repo:

```bash
hermes plugins install /path/to/hermes-koyeb-sandbox --enable
```

Or copy the tree into the profile and enable it:

```bash
git clone https://github.com/ThomsenDrake/hermes-koyeb-sandbox \
  ~/.hermes/plugins/hermes-koyeb-sandbox
hermes plugins enable hermes-koyeb-sandbox
```

The `koyeb` SDK is vendored under `vendor/` (1.5.5). An already-installed `koyeb-sdk>=1.5.2,<2` wins; otherwise the plugin uses the vendored copy. No write to the Hermes runtime venv is required.

## Enable

Stop the gateway for that profile first. A running gateway keeps the backend it started with.

```bash
hermes config set terminal.backend koyeb
```

Or in the profile's `config.yaml`:

```yaml
terminal:
  backend: koyeb          # local | docker | ssh | modal | daytona | vercel_sandbox | koyeb | ...
  timeout: 180            # per-command seconds (TERMINAL_TIMEOUT)
  lifetime_seconds: 300   # idle reaper; then cleanup()
  container_persistent: true
  # optional size / image — see below
  # koyeb_image: koyeb/sandbox
  # koyeb_instance_type: micro
```

Never set `terminal.backend: koyeb` in a profile whose gateway is already running and expect it to switch. Restart the gateway (or start a new CLI) after the change. Enabling the plugin does not change `terminal.backend`. Leaving it at `local` keeps every command on the local machine.

`TERMINAL_ENV=koyeb` overrides `terminal.backend` the same way it does for the built-in backends.

## Credentials

Never put the token in code or `config.yaml`. Put it in the profile's `.env` (or the Hermes credential store):

| Variable | Required | Meaning |
|---|---|---|
| `KOYEB_API_TOKEN` | yes | Koyeb API token. Create one at https://app.koyeb.com/account/api |
| `KOYEB_API_HOST` | no | API host override (default `https://app.koyeb.com`). `terminal.koyeb_api_host` wins |

The token is listed in `strip_env_keys` (`KOYEB_API_TOKEN`, `KOYEB_API_KEY`, `AGENTS_SDK_KOYEB_SANDBOX_API_TOKEN`), so Hermes strips it from every subprocess the agent spawns. It is never logged.

## Same knobs as the other backends

| Setting | Env override | What it does |
|---|---|---|
| `terminal.backend` | `TERMINAL_ENV` | `koyeb` selects this backend for terminal, files, and `execute_code`. |
| `terminal.timeout` | `TERMINAL_TIMEOUT` | Per-command timeout (default 180). A timed-out command does not delete the sandbox; Hermes' idle reaper and session teardown call `cleanup()`. |
| `terminal.lifetime_seconds` | `TERMINAL_LIFETIME_SECONDS` | Idle seconds before Hermes calls `cleanup()` (default 300). |
| `terminal.container_persistent` | `TERMINAL_CONTAINER_PERSISTENT` | `true` (default): snapshot the filesystem on cleanup, delete the live sandbox, restore the snapshot next session. `false`: delete on cleanup; each session gets its own sandbox (`session_isolated_when_nonpersistent`). |
| `terminal.container_cpu` | `TERMINAL_CONTAINER_CPU` | With `container_memory`, picks a Koyeb instance type when you move them off the Hermes defaults. |
| `terminal.container_memory` | `TERMINAL_CONTAINER_MEMORY` | Same. The shared defaults (1 vCPU, 5120 MB) stay on `micro`, because no Koyeb type is 1 vCPU / 5 GB. |
| `terminal.container_disk` | `TERMINAL_CONTAINER_DISK` | Not settable on its own. `0` or `51200` (the shared default) means "don't ask". Any other value must fit the instance's disk or backend creation fails. |
| `terminal.docker_network` | `TERMINAL_DOCKER_NETWORK` | `false` blocks sandbox egress, same air-gap switch Docker uses. |
| `terminal.koyeb_image` | `TERMINAL_KOYEB_IMAGE` | Image. Default `koyeb/sandbox`. A batch run's per-task `koyeb_image` override wins. Legacy: `KOYEB_SANDBOX_IMAGE`. |
| `terminal.koyeb_instance_type` | `TERMINAL_KOYEB_INSTANCE_TYPE` | `nano`, `micro`, `small`, `medium`, `large`, `xlarge`, `2xlarge`–`5xlarge`, or an `eco-*` type. Wins over the cpu/memory mapping. Legacy: `KOYEB_SANDBOX_INSTANCE_TYPE`. |
| `terminal.koyeb_region` | `TERMINAL_KOYEB_REGION` | Region slug (for example `fra`). Legacy: `KOYEB_SANDBOX_REGION`. |
| `terminal.cwd` | `TERMINAL_CWD` | Where commands start. Host paths are mapped onto the sandbox home. `default_cwd` is `~`. |

config.yaml wins over the env var, same as `terminal.singularity_image`.

## Koyeb-only knobs

| Setting | Default | Meaning |
|---|---|---|
| `terminal.koyeb_idle_timeout_seconds` | `max(600, 2 × lifetime_seconds)` | Scale-to-zero if Hermes dies before `cleanup()`. `0` keeps the instance always-on and it bills until something deletes it. |
| `terminal.koyeb_delete_after_inactivity_seconds` | `3600` | After scale-to-zero, Koyeb deletes the sandbox. `0` disables that cloud-side delete. |
| `terminal.koyeb_ttl_seconds` | `0` | `delete_after_delay` from creation. Leave at 0 so a long session is not killed on a clock. |
| `terminal.koyeb_wait_ready_timeout_seconds` | `300` | Create/readiness wait. |
| `terminal.koyeb_name_prefix` | `hermes` | Sandbox names are `<prefix>-<hash>` and stay within 64 characters. |
| `terminal.koyeb_registry_secret` | none | Koyeb secret for a private image. |
| `terminal.koyeb_block_network` | `false` | Block all sandbox egress. |

Legacy `KOYEB_SANDBOX_*` names still work when the `terminal.koyeb_*` key and the `TERMINAL_KOYEB_*` var are unset.

## Lifecycle

Matches Modal and Vercel Sandbox, adapted to the Koyeb API (named instance types, snapshots, no separate stop):

- **Persistent:** resume by name if a previous process left the sandbox up; otherwise restore `~/.hermes/koyeb_snapshots.json` or create fresh. `~/.hermes` (credentials, skills, cache) is synced in before commands and back on teardown. Cleanup snapshots the filesystem, deletes the live sandbox, and drops the previous snapshot object.
- **Non-persistent:** create, run, delete on cleanup. A leftover sandbox with the same name is deleted before create.
- **Crash:** `idle_timeout` scales the instance to zero, then `delete_after_inactivity_delay` deletes it. Nothing stays always-on unless you set the idle timeout to `0`.
- **Command timeout:** the command returns and the sandbox stays up, as on Docker and Modal. The idle reaper (`lifetime_seconds`) and session teardown call `cleanup()`, which deletes it.
- **Large output:** Hermes truncates terminal and `execute_code` results in `tools/tool_output_truncate.py` for every backend. This plugin returns the full merged stdout/stderr so that truncator sees one stream.

`delegate_task` children share the parent session's sandbox unless the task registered an isolation override (`koyeb_image` or `env_type`). An `env_type` override gives the task its own sandbox slot; it does not switch the backend. `_plan_execution` still reads `TERMINAL_ENV`.

## Tests

```bash
pip install pytest
PYTHONPATH=/opt/hermes python -m pytest tests/ -q
```

The SDK boundary is faked. Hermes `BaseEnvironment` (snapshot bootstrap, cwd markers, process handles, file sync) runs for real.

A live smoke test runs only when `KOYEB_API_TOKEN` is set. It creates a sandbox, runs a command, deletes it, and checks the name is gone:

```bash
KOYEB_API_TOKEN=... PYTHONPATH=/opt/hermes python -m pytest tests/test_live_koyeb.py -q
```

## License

MIT
