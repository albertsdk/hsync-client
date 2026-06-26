# hsync-client — CLI, config & orchestration

How flags/env become the running `CONFIG` and how `run_sync` drives a session.

## CLI (`cli.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `main` | `cli.py:101` | the Typer command: all flags, then calls `run_sync` |
| `run` | `:674` | console-script entry |
| `version_callback` | `:52` | `--version` |
| `capabilities_json_callback` | `:76` | `--capabilities-json` (introspects options via `collect_cli_options:59`) |
| `_validate_bucket` | `:88` | bucket flag validation |

Flags map to env keys; capabilities JSON is built from
`hsync_common.protocol.capabilities.build_capabilities` so the desktop can gate UI
(§8).

## Orchestration (`main_logic.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `run_sync` | `main_logic.py:459` | validate → safety checks → branch mode → watcher loop |
| `_check_deletion_risk` | `:262` | warn/prompt on risky delete settings (`--yes` bypass) |
| `_check_lan_write_policy` | `:344` | enforce LAN key for non-interactive writes |
| `_check_disk_space` | `:388` | min-free-disk guard |
| `_check_server_connectivity` | `:954` | fail fast on unreachable server |
| `_warn_on_clock_skew` | `:1004` | warn on large clock offset |
| `_get_stability_delay_value` / `_get_discovery_timeout_value` / `_get_resync_value` | `:210` / `:227` / `:244` | resolve CLI > env > default |
| `_start_hsyncignore_watcher` | `:421` | hot-reload `.hsyncignore` matcher |
| `_run_preview` | `:85` | dry-run action preview |
| stdin runtime control | `:830`/`:928`/`:933` | live commands (e.g. change direction) via `stdin_commands.StdinCommandHandler` (`stdin_commands.py:35`) |

## Config (`config.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `EnvSettings` | `config.py:47` | pydantic-settings model for all `HSYNC_*` vars |
| `HsyncConfig` / `ApiEndpoints` | `:294` / `:275` | the merged runtime config TypedDicts |
| `CONFIG` (global) | `:391` | the live config SSOT |
| `set_config` / `get_config` | `:397` / `:422` | write/read the global |
| `set_debug` / `is_debug` | `:411` / `:432` | debug flag |
| `load_env_file` | `:33` | dotenv loader |
| typed getters | `:154`–`:265` | `get_server_url`, `get_api_key`, LAN getters, guard getters, etc. |

## Constants & validators

- `constants.py` — bounds + defaults: stability (`:57`–`:73`), reschedule
  (`:79`/`:80`), concurrency (`:90`), retry (`:99`–`:101`), mass-delete (`:112`–`:114`),
  bandwidth (`:121`/`:122`), resync (`:130`–`:135`), inotify (`:163`/`:165`); visual
  `ActionLabel` enum (`:176`).
- `validators.py` — range validators via `create_int_range_validator` (`:33`):
  `valid_reconnect_delay` (`:72`), `valid_resync` (`:96`), `valid_bucket` (`:120`),
  `valid_stability_delay` (`:144`), `valid_http_timeout` (`:168`),
  `valid_max_wait_timeout` (`:192`), `valid_bucket_path` (`:216`).

## UI events (`events.py`)

`EventEmitter` (`events.py:33`) writes structured `UiEventName` events (from
`hsync_common.protocol.events`) to a desktop-provided fd. `init_emitter` (`:84`),
`get_emitter` (`:93`), `emit_event` (`:98`). Consumed by `hsync-desktop`.

> **Domain-critical**: any change to `HSYNC_*` flags / defaults requires the
> synchronized `packaging/wd-os5` update (see `CLAUDE.md`).
> Human guides: `../docs/configuration.md`, `../docs/usage.md`.
