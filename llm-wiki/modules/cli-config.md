# HSYNC Client — CLI, config & orchestration

How flags/env become the running `CONFIG` and how `run_sync` drives a session.

## CLI (`cli.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `main` | `cli.py:101` | the Typer command: all flags, then calls `run_sync` |
| `run` | `:674` | console-script entry |
| `version_callback` | `:52` | `--version` |
| `capabilities_json_callback` | `:76` | `--capabilities-json` (introspects options via `collect_cli_options:59`) |
| `_validate_bucket` | `:88` | bucket flag validation |

Secrets are **not** CLI flags: `HSYNC_API_KEY` / `HSYNC_LAN_KEY`, or hidden
`--api-key-fd` / `--lan-key-fd` for a parent process. There is no `--api-key`
or `--lan-key`. `--env-file` is the only dotenv path (`main_logic.py` →
`load_env_file`); `./.env` is not auto-loaded. `--env-file` only fills
`EnvSettings` fields; bucket/path/watch/direction/`--lan-mode`/`--force-master`
stay CLI-only.

Flags map to env keys where `EnvSettings` has a field; capabilities JSON is
built from `hsync_common.protocol.capabilities.build_capabilities` so the
desktop can gate UI (§8).

## Orchestration (`main_logic.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `run_sync` | `main_logic.py:529` | validate → safety checks → branch mode → watcher loop; logs `hsync_common.runtime.runtime_summary()` (container, mem/cpu cgroup limits, TTY) at startup. Identity: LAN mode resolves the persistent device identity (`identity.load_or_create_device_identity`) and uses its device ID as `CID` (logged as `Device ID`); remote mode keeps a per-session random `get_cid()`. Before the mode branch it acquires the per-directory `InstanceLock` for `local_path` (released in `finally`; refusal exits 1) |
| `_check_deletion_risk` | `:304` | warn/prompt on risky delete settings (`--yes` bypass; fails fast with exit 1 when no interactive TTY) |
| `_check_lan_write_policy` | `:396` | enforce LAN key for unattended writes (`--yes` **or** no interactive TTY, via `is_tty_interactive()`) |
| `_warn_if_lan_in_container` | `:441` | warn that mDNS discovery cannot cross container bridge/NAT networking (suggests `--network host`) |
| `_check_disk_space` | `:458` | min-free-disk guard |
| `_check_server_connectivity` | `:1040` | fail fast on unreachable server |
| `_get_stability_delay_value` / `_get_discovery_timeout_value` / `_get_resync_value` | `:215` / `:232` / `:249` | resolve CLI > env > default |
| `_start_hsyncignore_watcher` | `:463` | hot-reload `.hsyncignore` matcher |
| `_run_preview` | `:87` | dry-run action preview |
| stdin runtime control | `:916`/`:1014`/`:1019` | live commands (e.g. change direction) via `stdin_commands.StdinCommandHandler` (`stdin_commands.py:35`) |

## Config (`config.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `EnvSettings` | `config.py:47` | pydantic-settings model for all `HSYNC_*` vars |
| `HsyncConfig` / `ApiEndpoints` | `:294` / `:275` | the merged runtime config TypedDicts |
| `CONFIG` (global) | `:511` | the live config SSOT |
| `set_config` | `:517` | write the global in-place; modules read `CONFIG` directly — **no** `get_config` |
| `set_debug` / `is_debug` | `:411` / `:432` | debug flag |
| `load_env_file` | `:33` | dotenv loader |
| typed getters | `:154`–`:265` | `get_server_url`, `get_api_key`, LAN getters, guard getters, delta getters (`get_delta_enabled`, `get_delta_min_file_size`, `get_delta_min_file_size_lan`), etc. |

## Constants & validators

- `constants.py` — bounds + defaults: stability (`:57`–`:73`), reschedule
  (`:79`/`:80`), concurrency (`:90`), retry (`:99`–`:101`), mass-delete (`:112`–`:114`),
  bandwidth (`:121`/`:122`), resync (`:130`–`:135`), max inbound file size
  (  `MAX_INBOUND_SIZE_MB_*`, default 10 GB — absolute anti-bomb cap applied as
  `min(declared, cap)` to downloads and received LAN uploads, wired into
  `CONFIG["MAX_INBOUND_SIZE"]` in bytes), inotify (`:176`/`:178`); visual
   `ActionLabel` enum (`:195`); delta transfer (`:185`–`:189`: `DELTA_ENABLED`,
   `DELTA_MIN_FILE_SIZE` 256 KB, `DELTA_MIN_FILE_SIZE_LAN` 1 MB, `DELTA_TOCTOU_REHASH_THRESHOLD` 10 MB).
- Range and bucket validation is inline in `cli.py` (Typer) — e.g.
  `resync`/`reconnect_delay` bounds raise `typer.BadParameter`; bucket naming
  via `_validate_bucket` (`cli.py:88`). There is no separate validators module.

## UI events (`events.py`)

`EventEmitter` (`events.py:33`) writes structured `UiEventName` events (from
`hsync_common.protocol.events`) to a desktop-provided fd. `init_emitter` (`:84`),
`get_emitter` (`:93`), `emit_event` (`:98`). Consumed by `hsync-desktop`.

> **Domain-critical**: any change to `HSYNC_*` flags / defaults requires the
> synchronized `packaging/wd-os5` update (see `CLAUDE.md`).
> WD `packaging/wd-os5/Apkg/start.sh` maps the web UI autotune fields to the
> binary: `--no-autotune` / `--timeout` / `--retry-delay`, and exports
> `HSYNC_TRANSFER_RETRY_MIN_WAIT` / `HSYNC_TRANSFER_RETRY_MAX_WAIT` (no CLI).
> Empty UI values are omitted so autotune/defaults apply. Secrets stay env-only.
> Human guides: `../../docs/configuration.md`, `../../docs/configuration-server.md`, `../../docs/configuration-lan.md`, `../../docs/usage.md`.
