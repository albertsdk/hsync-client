# HSYNC Client — CLI, config & orchestration

How flags/env become the running `CONFIG` and how `run_sync` drives a session.

## CLI (`cli.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `main` (parent callback) | `cli.py:750` | global options `--version` / `--capabilities-json` / `--help-symbols` (typed before the subcommand); no implicit mode |
| `lan` | `cli.py` | LAN P2P subcommand: shared + LAN-only flags, including final `--no-peer-download` opt-out and dangerous `--allow-unauthenticated-writes`; validates and calls `run_sync` |
| `server` | `cli.py` `server` | central-server subcommand: bare `HOSTNAME` positional + `--port` + shared flags, resolves the server URL eagerly (bare `HOSTNAME` form, else `HSYNC_SERVER` from shell or `--env-file`) and fails fast with `typer.BadParameter` when no server can be resolved, then calls `run_sync` |
| `run` | `:1121` | console-script entry |
| `collect_subcommand_options` | `:89` | introspects each subcommand's long flags for the capabilities document |
| `capabilities_json_callback` | `:109` | `--capabilities-json` (per-subcommand introspection via `collect_subcommand_options`) |
| `_validate_bucket` | `:126` | bucket flag validation |
| `_build_server_url` | `:142` | bare-hostname form → `https://host:port` (implied scheme, `SERVER_DEFAULT_PORT` when `--port` is omitted, IPv6 auto-bracketing; rejects URL-like and `host:port` inputs, refuses `--port` without a hostname); `None` hostname → the `server` subcommand resolves the `HSYNC_SERVER` fallback (shell env or `--env-file`) and raises `typer.BadParameter` if still unset |
| `_validate_shared_options` | `:185` | numeric-range / bandwidth / direction / concurrency / mass-delete / delta checks shared by both subcommands |

Secrets are **not** CLI flags: `HSYNC_API_KEY` / `HSYNC_LAN_KEY`, or hidden
`--api-key-fd` (server subcommand) / `--lan-key-fd` (lan subcommand) for a
parent process. `--env-file` is the only dotenv path (`main_logic.py` →
`load_env_file`); `./.env` is not auto-loaded. `--env-file` only fills
`EnvSettings` fields; bucket/path/direction/the `lan`/`server` subcommand
selection/`--force-master`/the server `HOSTNAME`+`--port` stay CLI-only
(the full-URL alternative is the `HSYNC_SERVER` env var; the CLI host wins
when both are set). A server-mode run with neither a `HOSTNAME` nor a resolvable
`HSYNC_SERVER` (shell env or `--env-file`) is rejected at parse time with a
`typer.BadParameter` usage error, before the banner/logging or `run_sync`
starts — so the missing server never reaches the deep, late validation inside
`run_sync`.

Flags map to env keys where `EnvSettings` has a field; capabilities JSON is
built from `hsync_common.protocol.capabilities.build_capabilities` (schema v2:
`subcommands` + per-subcommand `cli_options`) so the desktop can gate UI (§8).

`--once` is the single one-shot selector: it maps to `continuous=False`,
which also feeds the LAN `ONE_SHOT` config key, so the semantics are uniform
across topologies. Continuous is the default and `--once` is the only run-mode knob.

## Orchestration (`main_logic.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `run_sync` | `main_logic.py:539` | validate → safety checks → branch mode → watcher loop; logs `hsync_common.runtime.runtime_summary()` (container, mem/cpu cgroup limits, TTY) at startup. Identity: LAN mode resolves the persistent device identity (`identity.load_or_create_device_identity`) and uses its device ID as `CID` (logged as `Device ID`); remote mode keeps a per-session random `get_cid()`. Before the mode branch it acquires the per-directory `InstanceLock` for `local_path` (released in `finally`; refusal exits 1) |
| `_check_deletion_risk` | `:285` | warn/prompt on risky delete settings (`--yes` bypass; fails fast with exit 1 when no interactive TTY) |
| `_check_lan_write_policy` | `main_logic.py` | default-deny every write-capable no-key LAN run, independent of TTY/`--yes`; only `--allow-unauthenticated-writes` or `HSYNC_LAN_ALLOW_UNAUTHENTICATED_WRITES=true` opts in |
| `_warn_if_lan_in_container` | `:424` | warn that mDNS discovery cannot cross container bridge/NAT networking (suggests `--network host`) |
| `_check_disk_space` | `:441` | min-free-disk guard |
| `_check_server_connectivity` | `:1142` | startup health gate; one-shot mode fails fast on an unreachable server, continuous mode (default) logs a warning and lets `remote_connect` retry forever (LAN parity) |
| `_get_stability_delay_value` / `_get_discovery_timeout_value` / `_get_resync_value` | `:116` / `:130` / `:147` | resolve CLI > env > default |
| `_start_hsyncignore_watcher` | `:474` | hot-reload `.hsyncignore` matcher |
| `_safe_config_for_log` | `:512` | builds the redacted verbose `Local configuration` dump: masks `API_KEY`/`LAN_KEY`/`Authorization` and appends a `PATHS` section (`ENV_FILE`, `DEVICE_DIR`, `STATE_DIR`, `STATE_DB`, `LOCKS_DIR`) resolved from `identity.default_device_dir`/`default_locks_dir` + `folder.default_state_dir`/`default_syncstate_db_path` |
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

Peer-download settings are `HSYNC_LAN_PEER_DOWNLOAD=true`,
`HSYNC_LAN_PEER_DOWNLOAD_MIN_SIZE=1048576`, and
`HSYNC_LAN_PEER_READ_CAP=2`. `--no-peer-download` is applied as
`CLI-enabled AND environment-enabled`, so the CLI opt-out is final. The no-key
write opt-in is `CLI-opt-in OR environment-opt-in` and defaults false.

## Constants & validators

- `constants.py` — application info: re-exports `HSYNC_VERSION` +
  `HEADER_CLIENT_NAME`/`HEADER_CLIENT_VERSION` from `hsync-common`, and
  defines `CLIENT_NAME = "hsync-client"` (the `X-Client-Name` REST header
  value built in `main_logic.py`); re-exports the wire content types
  `CONTENT_TYPE_OCTET_STREAM`/`CONTENT_TYPE_JSON` used by
  `lan_server.py`/`transfer.py`/`delta_transfer.py`/`lan_delta.py`.
  Bounds + defaults: stability (`:57`–`:73`), reschedule
  (`:79`/`:80`), concurrency (`:90`), retry (`:99`–`:101`), mass-delete (`:112`–`:114`),
  bandwidth (`:121`/`:122`), resync (`:130`–`:135`), max inbound file size
  (  `MAX_INBOUND_SIZE_MB_*`, default 10 GB — absolute anti-bomb cap applied as
  `min(declared, cap)` to downloads and received LAN uploads, wired into
  `CONFIG["MAX_INBOUND_SIZE"]` in bytes), inotify (`:176`/`:178`); visual
   `ActionLabel` enum (`:195`); delta transfer (`:185`–`:189`: `DELTA_ENABLED`,
   `DELTA_MIN_FILE_SIZE` 256 KB, `DELTA_MIN_FILE_SIZE_LAN` 1 MB, `DELTA_TOCTOU_REHASH_THRESHOLD` 10 MB).
   `WORKER_MEMORY_BUDGET_BYTES` (`:213`, 8 MiB) — per-worker memory budget for the
   parallel local-file scan/hash pool (`_get_optimal_workers`), combined with the
   cgroup memory limit to cap parallelism inside containers (no effect on bare
   metal, where the memory probe returns `None`).
- `constants.py:173` — `SERVER_DEFAULT_PORT = 9753`: the port `hsync server
  HOSTNAME` uses when `--port` is omitted. It matches the hsync-server
  default (`HSYNC_PORT`), so the omitted-port form targets a default server
  install directly; reverse-proxy deployments pass `--port 443`.
- Range and bucket validation is inline in `cli.py` (Typer) — e.g.
  `resync`/`reconnect_delay` bounds raise `typer.BadParameter` from
  `_validate_shared_options` (`cli.py:185`); bucket naming
  via `_validate_bucket` (`cli.py:126`); LAN-only checks (discovery timeout,
  `--force-master` exclusivity) live in the `lan` subcommand. There is no
  separate validators module.

## UI events (`events.py`)

`EventEmitter` (`events.py:33`) writes structured `UiEventName` events (from
`hsync_common.protocol.events`) to a desktop-provided fd. `init_emitter` (`:84`),
`get_emitter` (`:93`), `emit_event` (`:98`). Consumed by `hsync-desktop`.

> **Domain-critical**: any change to `HSYNC_*` flags / defaults requires the
> synchronized `packaging/wd-os5` update (see `AGENTS.md`).
> WD `packaging/wd-os5/Apkg/start.sh` maps the web UI autotune fields to the
> binary: `--no-autotune` / `--http-timeout` / `--retry-delay`, and exports
> `HSYNC_TRANSFER_RETRY_MIN_WAIT` / `HSYNC_TRANSFER_RETRY_MAX_WAIT` (no CLI).
> Empty UI values are omitted so autotune/defaults apply. Secrets stay env-only.
> Human guides: `../../docs/configuration.md`, `../../docs/configuration-server.md`, `../../docs/configuration-lan.md`, `../../docs/usage.md`.
