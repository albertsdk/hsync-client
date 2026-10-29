# HSYNC Client — CLI, config & orchestration

How flags/env become the running `CONFIG` and how `run_sync` drives a session.

## CLI (`cli.py`)

| Symbol | Location | Role |
| ------ | -------- | ---- |
| `main` (parent callback) | `cli.py::main` | global options `--version` / `--capabilities-json` / `--help-symbols` (typed before the subcommand); no implicit mode |
| `p2p` | `cli.py::p2p` | p2p subcommand: shared + p2p-only flags, including final `--no-peer-download` opt-out and dangerous `--allow-unauthenticated-writes`; validates and calls `run_sync` |
| `server` | `cli.py::server` | central-server subcommand: `HOSTNAME[:PORT]` positional + `--port` + shared flags, resolves the server URL eagerly (`HOSTNAME`/`HOSTNAME:PORT` form, else `HSYNC_SERVER` from shell or `--env-file`) and fails fast with `typer.BadParameter` when no server can be resolved, then calls `run_sync` |
| `run` | `cli.py::run` | console-script entry: installs the `SIGTERM`→`KeyboardInterrupt` handler (`cli.py::_install_sigterm_handler`, so a service/desktop stop unwinds like Ctrl+C), maps an operator stop to a clean exit (plain `KeyboardInterrupt`, plus Typer's own `Exit(130)` kept in `cli.py::_INTERRUPTED_EXIT_CODE)`), and in its `finally` drains and closes the log sinks (`logger.py::shutdown_logging`) — the close is what releases the semaphores `resource_tracker` would otherwise report as leaked |
| `collect_subcommand_options` | `cli.py::collect_subcommand_options` | introspects each subcommand's long flags for the capabilities document |
| `capabilities_json_callback` | `cli.py::capabilities_json_callback` | `--capabilities-json` (per-subcommand introspection via `collect_subcommand_options`) |
| `_validate_bucket` | `cli.py::_validate_bucket` | bucket flag validation |
| `_build_server_url` | `cli.py` | `HOSTNAME[:PORT]` form → `https://host:port` (implied scheme, `SERVER_DEFAULT_PORT` when no port, IPv6 auto-bracketing via the shared `hsync_common.endpoint` parser; rejects URL-like input, refuses `--port` without a hostname, and a port in the host is mutually exclusive with `--port`); `None` hostname → the `server` subcommand resolves the `HSYNC_SERVER` fallback (shell env or `--env-file`) and raises `typer.BadParameter` if still unset |
| `_validate_shared_options` | `cli.py::_validate_shared_options` | numeric-range / bandwidth / direction / concurrency / mass-delete / delta checks shared by both subcommands. Accepted names are declared once in `_SharedValidationValues` (`cli.py::_SharedValidationValues`), whose field names drive the frame extraction and type every read (a typo is a mypy error); a command frame missing one fails fast naming the option. Shared/mode-specific classification pinned by `tests/test_cli_option_parity.py` |
| `state` namespace + `state_reset` | `cli.py::state_reset` | `hsync state reset --folder DIR [--json] [--quarantine]`: forgets one folder's versions + tombstones; the CLI is a thin shell and the logic lives in [state_reset.py](../data-models.md#state_resetpy-sync-state-reset) |

Startup note: `build_capabilities` (`hsync_common.protocol.capabilities`) and
`legend_lines` (`hsync_client.log_labels`) are imported **inside** their eager
callbacks (`capabilities_json_callback` / `legend_callback`), not at module
level, so the fast paths `--version` / `--help` / `--help-symbols` do not pay the
`hashing`→`asyncio` and `models` import chain that only `--capabilities-json`
needs. Pinned by `tests/test_startup_imports.py`.

Secrets are **not** CLI flags: `HSYNC_API_KEY` / `HSYNC_P2P_PASSPHRASE` /
`HSYNC_P2P_KEY`, or hidden `--api-key-fd` (server subcommand) /
`--p2p-passphrase-fd` / `--p2p-key-fd` (p2p subcommand) for a parent process. A
passphrase (at least 12 chars) is derived to the group key by
`derive_group_key`; never set both p2p forms. `--env-file` is the only dotenv
path (`main_logic.py` →
`load_env_file`); `./.env` is not auto-loaded. `--env-file` only fills
`EnvSettings` fields; bucket/path/direction/the `p2p`/`server` subcommand
selection/`--prefer-master`/the server `HOSTNAME[:PORT]`+`--port` stay CLI-only
(the `HSYNC_SERVER` env var accepts a bare `HOST[:PORT]`/`[IPv6]` or a full
`https://…[/path]` URL; the CLI host wins when both are set). A server-mode run
with neither a `HOSTNAME` nor a resolvable
`HSYNC_SERVER` (shell env or `--env-file`) is rejected at parse time with a
`typer.BadParameter` usage error, before the banner/logging or `run_sync`
starts — so the missing server never reaches the deep, late validation inside
`run_sync`.

`--deletion-policy {propagate,keep}` (`HSYNC_DELETION_POLICY`) is the only deletion control: it names the outcome instead of negating one, and both subcommands validate it before `run_sync` (see [sync-core.md](sync-core.md)).

Flags map to env keys where `EnvSettings` has a field; capabilities JSON is
built from `hsync_common.protocol.capabilities.build_capabilities` (schema v2:
`subcommands` + per-subcommand `cli_options`) so the desktop can gate UI (§8).

`--once` is the single one-shot selector: it maps to `continuous=False`,
which also feeds the p2p `ONE_SHOT` config key, so the semantics are uniform
across topologies. Continuous is the default and `--once` is the only run-mode knob.

## Orchestration (`main_logic.py`)

| Symbol | Location | Role |
| ------ | -------- | ---- |
| `run_sync` | `main_logic.py::run_sync` | validate → safety checks → branch mode → watcher loop; logs `hsync_common.runtime.runtime_summary()` (container, mem/cpu cgroup limits, TTY) at startup. Identity: p2p mode resolves the persistent device identity (`identity.load_or_create_device_identity`) and uses its device ID as `CID` (logged as `Device ID`); remote mode keeps a per-session random `get_cid()`. Before the mode branch it acquires the per-directory `InstanceLock` for `local_path` (released in `finally`); a refusal is logged, reported as `error(reason="startup")` and exits `1`, and the `ready` event is emitted only *after* the lock — the Desktop treats an exit before `ready` as a startup failure |
| `_check_deletion_risk` | `main_logic.py::_check_deletion_risk` | warn/prompt on risky delete settings (`--allow-deletions` bypass; fails fast with exit 1 when no interactive TTY) |
| `_check_p2p_write_policy` | `main_logic.py` | default-deny every write-capable no-key p2p run, independent of TTY; only `--allow-unauthenticated-writes` or `HSYNC_P2P_ALLOW_UNAUTHENTICATED_WRITES=true` opts in |
| `_check_p2p_keyless_policy` | `main_logic.py` | confirm keyless p2p (no shared secret); `--allow-unauthenticated-p2p` or the writes opt-in acknowledges; fails closed without a TTY |
| `_warn_if_p2p_in_container` | `main_logic.py::_warn_if_p2p_in_container` | warn that mDNS discovery cannot cross container bridge/NAT networking (suggests `--network host`) |
| `_check_disk_space` | `main_logic.py::_check_disk_space` | min-free-disk guard |
| `_check_server_connectivity` | `main_logic.py::_check_server_connectivity` | startup health gate; one-shot mode fails fast on an unreachable server, continuous mode (default) logs a warning and lets `remote_connect` retry forever (p2p parity) |
| `_get_stability_delay_value` / `_get_discovery_timeout_value` / `_get_resync_value` | `main_logic.py::_get_stability_delay_value` / `main_logic.py::_get_discovery_timeout_value` / `main_logic.py::_get_resync_value` | resolve CLI > env > default |
| `_start_hsyncignore_watcher` | `main_logic.py::_start_hsyncignore_watcher` | hot-reload `.hsyncignore` matcher |
| `_safe_config_for_log` | `main_logic.py::_safe_config_for_log` | builds the redacted verbose `Local configuration` dump: masks `API_KEY`/`P2P_KEY`/`Authorization` and appends a `PATHS` section (`ENV_FILE`, `DEVICE_DIR`, `STATE_DIR`, `STATE_DB`, `LOCKS_DIR`) resolved from `identity.default_device_dir`/`default_locks_dir` + `folder.default_state_dir`/`default_syncstate_db_path` |
| `_run_preview` | `preview.py::_run_preview` | dry-run action preview |
| stdin runtime control | `main_logic.py::_run_with_stdin_commands` / `stdin_commands.py::StdinCommandHandler` | live commands (e.g. change direction) via `stdin_commands.StdinCommandHandler` (`stdin_commands.py::StdinCommandHandler`) |

## Config (`config.py`)

| Symbol | Location | Role |
| ------ | -------- | ---- |
| `EnvSettings` | `config.py::EnvSettings` | pydantic-settings model for all `HSYNC_*` vars |
| `HsyncConfig` / `ApiEndpoints` | `config.py::HsyncConfig` / `config.py::ApiEndpoints` | the merged runtime config TypedDicts |
| `CONFIG` (global) | `config.py::CONFIG` | the live config SSOT |
| `set_config` | `config.py::set_config` | write the global in-place; modules read `CONFIG` directly — **no** `get_config` |
| `set_debug` / `is_debug` | `config.py::set_debug` / `config.py::is_debug` | debug flag |
| `load_env_file` | `config.py::load_env_file` | dotenv loader |
| typed getters | `config.py::get_server_url`–`config.py::get_connectivity_retry_max_wait` | `get_server_url`, `get_api_key`, p2p getters (`get_p2p_key`/`get_p2p_group`/`get_p2p_listen_port`), discovery getters, the bootnode getters (`get_bootnode`, `get_bootnode_tofu`, `get_bootnode_ports`), guard getters, delta getters, etc. |

The `p2p` subcommand takes an optional positional `BOOTNODE` (`HOST[:PORT]`) plus
`--port` (bootnode UDP, default 443) and `--upnp` (opt-in; off by default);
`_build_bootnode`
(`cli.py`) validates them, refuses `--port` without a host (never a silently
ignored flag), and a port in `BOOTNODE` is mutually exclusive with `--port`.
The source(s) are selected in `p2p_discovery.build_discovery_plan`
(`config["BOOTNODE"]` unset → mDNS only, set → `HybridDiscovery` running mDNS
**and** bootnode over one shared `PeerManager`), so `p2p_handler.run_p2p_mode`
only sees the `DiscoveryPlan` surface.

Multi-port dial fallback: `HSYNC_BOOTNODE_PORTS`
(`config.py::get_bootnode_ports`, a thin lenient wrapper over the shared
`hsync_common.endpoint.parse_port_list` SSOT) adds extra UDP ports to try for the
SAME bootnode; `BootnodeClient` (`boot_client.py::_with_connection`) dials the last
working port first (then the primary and extras) and falls back only on a
**handshake** failure — the blocked-port signal — so a client whose egress
firewall blocks UDP 443 reaches the node on another port (e.g. 53) and every
later exchange skips the blocked one. Once the handshake succeeds the port is
reachable, so a reply timeout or a trust/protocol error propagates without
re-dialling. The wire is unchanged and trust stays a single pin (shared fleet
identity), so the fallback never re-prompts.

The bootnode endpoint is `HSYNC_BOOTNODE` (the `_validate_bootnode_host`
validator accepts `host`, `host:port`, `[IPv6]`, `[IPv6]:port` and normalizes
through the shared `hsync_common.endpoint.normalize_bare_endpoint` — the same
rule the desktop GUI applies to its discovery-server field; no scheme or
path), and UPnP is `HSYNC_UPNP_ENABLED` (default `false`; opt-in). The p2p
command resolves the bootnode CLI > env (`_build_bootnode`,
`use_env=True`): a CLI `BOOTNODE[:PORT]` positional replaces the whole env
endpoint, and an explicit `--port` overrides a port embedded in the env.
`HSYNC_P2P_LISTEN_PORT` sets the master TCP listen port (env for the
`p2p_listen_port` field; `BOOTNODE_DEFAULT_PORT = 443` in `constants.py`).

Peer-download settings are `HSYNC_P2P_PEER_DOWNLOAD=true` and
`HSYNC_P2P_PEER_READ_CAP=2`. `--no-peer-download` is applied as
`CLI-enabled AND environment-enabled`, so the CLI opt-out is final. It gates
**block serving** (the seeder role and its `serve_slots` capacity); the node
still advertises `index` and publishes its read-only inventory with it disabled,
so peers can classify its share. The no-key
write opt-in is `CLI-opt-in OR environment-opt-in` and defaults false.

## Constants & validators

- `constants.py` — application info: re-exports `HSYNC_VERSION` +
  `HEADER_CLIENT_NAME`/`HEADER_CLIENT_VERSION` from `hsync-common`, and
  defines `CLIENT_NAME = "hsync-client"` (the `X-Client-Name` REST header
  value built in `main_logic.py`); re-exports the wire content types
  `CONTENT_TYPE_OCTET_STREAM`/`CONTENT_TYPE_JSON` used by
  `p2p_server.py`/`transfer.py`/`delta_transfer.py`/`p2p_delta.py`.
  Bounds + defaults (`constants.py`): stability, reschedule, concurrency, retry,
  mass-delete, bandwidth, resync, and max inbound file size
  (`MAX_INBOUND_SIZE_MB_*`, default 10 GB — absolute anti-bomb cap applied as
  `min(declared, cap)` to downloads and received p2p uploads, wired into
  `CONFIG["MAX_INBOUND_SIZE"]` in bytes), inotify. The free-space reserve is
  the same shape: `MIN_FREE_DISK_SPACE_DEFAULT/_MIN/_MAX` are **bytes**
  (500 MiB / 0 / 100 GiB) and feed `CONFIG["MIN_FREE_DISK_SPACE"]`; the
  `--min-free-disk-space` option takes a size string (`500MB`, `2GB`) that
  `cli.py::_parse_size_option` converts with `hsync_common.format.parse_size`
  (`require_unit=True`, so a bare number is rejected; `0` = disabled). The visual
   `ActionLabel` enum comes from `hsync_common.labels`; delta transfer (`constants.py`): `DELTA_ENABLED`,
   `DELTA_MIN_FILE_SIZE` 256 KB, `DELTA_MIN_FILE_SIZE_P2P` 1 MB, `DELTA_TOCTOU_REHASH_THRESHOLD` 10 MB.
   `WORKER_MEMORY_BUDGET_BYTES` (`constants.py::WORKER_MEMORY_BUDGET_BYTES`, 8 MiB) — per-worker memory budget for the
   parallel local-file scan/hash pool (`_get_optimal_workers`), combined with the
   cgroup memory limit to cap parallelism inside containers (no effect on bare
   metal, where the memory probe returns `None`).
- `constants.py::SERVER_DEFAULT_PORT` — a re-export of
  `hsync_common/constants.py::SERVER_DEFAULT_PORT`: the port `hsync server
  HOSTNAME` uses when `--port` is omitted (the same default the server binds),
  so the omitted-port form targets a default server install directly;
  reverse-proxy deployments pass `--port 443`.
- Shared CLI/GUI defaults and their accepted bounds (HTTP timeout, retry/stability
  delays, sync direction, bandwidth bounds, mass-delete guard, free-space/inbound
  limits, max-wait timeout, peer-download caps) live in `hsync_common/defaults.py`;
  `hsync_client.constants` re-exports them under its own names, and the desktop
  reads the same values and bounds, so the GUI cannot drift from the engine.
- Range and bucket validation is inline in `cli.py` (Typer) — e.g.
  `resync`/`reconnect_delay` bounds raise `typer.BadParameter` from
  `_validate_shared_options` (`cli.py::_validate_shared_options`); bucket naming
  via `_validate_bucket` (`cli.py::_validate_bucket`); p2p-only checks (discovery timeout,
  `--prefer-master` exclusivity) live in the `p2p` subcommand. There is no
  separate validators module.
- Docs drift guard: `tests/test_docs_option_parity.py` walks every command
  (including the `trust`/`state` groups) and fails when a visible option is
  missing from the man page or from `docs/*.md`, and when a man-page option
  entry names a flag the CLI does not accept.

## UI events (`events.py`)

`EventEmitter` (`events.py::EventEmitter`) writes structured `UiEventName` events (from
`hsync_common.protocol.events`) to a desktop-provided fd. `init_emitter` (`events.py::init_emitter`),
`get_emitter` (`events.py::get_emitter`), `emit_event` (`events.py::emit_event`). Consumed by `hsync-desktop`.

> **Domain-critical**: any change to `HSYNC_*` flags / defaults requires a
> synchronized update of the WD OS5 package (`hsync-wd-os5`,
> `packaging/Apkg/start.sh`) — see `AGENTS.md`.
> That launcher maps the web UI autotune fields to the
> binary: `--no-autotune` / `--http-timeout` / `--retry-delay`, and exports
> `HSYNC_TRANSFER_RETRY_MIN_WAIT` / `HSYNC_TRANSFER_RETRY_MAX_WAIT` (no CLI).
> Empty UI values are omitted so autotune/defaults apply. Secrets stay env-only.
> Human guides: `../../docs/configuration.md`, `../../docs/configuration-server.md`, `../../docs/configuration-p2p.md`, `../../docs/usage.md`.
