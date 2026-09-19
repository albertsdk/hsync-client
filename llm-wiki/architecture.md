# HSYNC Client — Architecture (agent view)

One process, two run modes over a shared watcher + transfer core. No daemon
framework; asyncio event loop + watchdog observer + a thread pool for transfers.

## Entry & orchestration

`main.main` (`main.py:25`) invokes the Typer app (`cli.py`): a parent callback
`cli.main` (`cli.py:750`) carrying the global options, plus two mutually
exclusive subcommands `cli.lan` (`cli.py:837`) and `cli.server` (`cli.py:985`).
Each subcommand validates its options and calls `run_sync` (`main_logic.py:539`),
which validates inputs, runs pre-sync safety checks, then branches by mode and
starts the watcher loop. Console script: `cli.run` (`cli.py:1121`).

## The two run modes

| Mode | Trigger | Network role | Core entry |
| ---- | ------- | ------------ | ---------- |
| **Remote (server)** | `hsync server` subcommand; continuous unless `--once` | client ↔ `hsync-server` (REST + WS) | `remote_websocket.remote_events` (`remote_websocket.py:98`) |
| **LAN (P2P)** | `hsync lan` subcommand | master (serves) or slave (syncs to master) via mDNS + HTTPS/WSS | `lan_handler.run_lan_mode` (`lan_handler.py:109`) |

LAN sub-roles: `_run_as_master` (`lan_handler.py:273`) starts `LANServer`
(`lan_server.py:1626`) + `LANWebSocketServer` (`lan_websocket.py:185`); `_run_as_slave`
(`lan_handler.py:418`) connects to the elected master. Role is chosen by election
(see [lan-mode.md](modules/lan-mode.md)).

## Config flow

CLI flags + `HSYNC_*` env (pydantic `EnvSettings` `config.py:47`) merge into the
global `CONFIG: HsyncConfig` TypedDict (`config.py:294`/`:391`) via `set_config`
(`config.py:397`); modules read it through `get_config` (`config.py:422`) and typed
getters (`config.py:154`–`:265`). Input validation is inline in `cli.py`
(Typer); defaults/bounds in `constants.py`.

## Pre-sync safety

`run_sync` runs guards before touching files:
- `_check_deletion_risk` (`main_logic.py:285`) — warns/prompts when settings could
  delete local data (gated by `--yes`).
- `_check_lan_write_policy` (`main_logic.py:379`) — requires a LAN key for
  write-capable LAN sync in non-interactive mode.
- `_check_disk_space` (`main_logic.py:441`), `_check_server_connectivity`
  (`main_logic.py:1142`).
- Runtime mass-deletion guard during sync: `_mass_deletion_blocked` (`sync.py:94`).

## Readiness threshold (`[READY]`, shared label SSOT)

Every run mode emits exactly one observable `[READY]` INFO line when the
node is truly ready to synchronize — watcher active **and** event channel
established:

| Mode | Emission point |
| ---- | -------------- |
| Remote (continuous, default) | `remote_websocket.py:210` — after WS auth + `observer.start()` |
| LAN master | `lan_handler.py:346` — after HTTP + WS servers up, mDNS `server_ready`, watcher running |
| LAN slave | `lan_websocket.py:875` — after watcher start **and** completed master handshake (a failed auth never reaches it) |

The label value is the SSOT member `hsync_common.labels.ActionLabel.READY`
(category `create`, green); Android mirrors it in
`hsync-android` `LogManager.kt` `LogAction.READY` (parity-tested by
`LogActionTest`). Failure paths (auth/protocol refusal) exit before the
threshold, so `[READY]` always means "reacting to events now".

## Watcher + transfer core (shared by both modes)

```
WatcherManager (watcher.py:105) ──> BaseFileEventHandler (file_event_handler.py:79)
        │ on_created/on_closed/on_modified/on_deleted/on_moved
        ▼
 stability check (transfer._wait_for_stable_file) ──> upload_file (transfer.py)
        │
 inbound WS event ──> event_dispatch.apply_inbound_event (event_dispatch.py:103)
        │                         └─> download/delete/rename/symlink locally
        ▼
 sync.sync (sync.py:132): list local/remote, diff (get_sync_actions:684),
                          execute in parallel (_execute_parallel_transfers:267)
```

Mode-specific handlers: `RemoteFileEventHandler` (`remote_websocket.py:487`) and
`LANMasterFileEventHandler` (`lan_websocket.py:546`) subclass `BaseFileEventHandler`;
`LANSlaveFileEventHandler` (`lan_websocket.py:1003`) is a separate watchdog
`FileSystemEventHandler` for the slave→master upload path.

## Layering

```
constants / exceptions / logger / compression                  (leaf)
        │
utils (http session + pinning, path_lock, perms, bandwidth) / retry / ssl_utils / pools
        │
config (CONFIG SSOT) / events (UI event emitter)
        │
sync / transfer / event_dispatch / event_queue / file_event_handler   (core)
        │
remote_websocket | lan.py + lan_server + lan_websocket + lan_handler   (modes)
        │
main_logic (orchestration) ← cli ← main
```

**Logger sinks are asynchronous** (`logger.py:setup_logging`, `enqueue=True`
on both the `--log-file` and stdout sinks): formatting and the write syscall
run on loguru's dedicated thread, so per-event lines (one `[LOCAL CREATE]`
per file during a burst) never throttle the emitting engine thread — a
synchronous file sink measured ~2.5× slower event drain on the master's
watcher thread. Readers that need the file content immediately after a log
call wait via `logger.complete()`.

**The stdlib root level is the resolved level, not loguru's `level=0`
recipe** (`logging.basicConfig(..., level=level)`): a NOTSET root answers
`True` to `isEnabledFor(DEBUG)`, so every DEBUG line a library emits would
build a `LogRecord` and walk the stack in `findCaller` before the interceptor
forwarded it to loguru, whose sink then dropped it — ~26 dropped records per
transferred file on a LAN burst, 6.5 µs each against 0.08 µs for the level
check that now avoids them. Output is unchanged (those records were discarded
either way); `--debug` raises the root to DEBUG and lets them through.

**Exit paths flush the sinks** (`logger.py:flush_logs`, blocking `complete()`
wrapper, best-effort): an asynchronous sink writes from another thread, so a
path that logs its reason and terminates lost that record — observed
end-to-end as a bare non-zero exit with no explanation, because `CliRunner`
(the CLI is invoked in-process by the black-box server tests) tears the
captured stream down before the writer thread flushes
(`ValueError: I/O operation on closed file`). Every fatal exit therefore
drains the queue first: `main_logic._acquire_instance_lock` (the lock refusal
message *is* the error), the `finally` of the one-shot run (twice: once before
the shutdown steps so the diagnostic cannot be lost if they fail, once after
them), and `cli.run` as the process-level net — the latter importing `loguru`
**lazily and only when `hsync_client.logger` is already in `sys.modules`**, so
the `--version` / `--help` fast paths keep the minimal import surface pinned by
`tests/test_startup_imports.py`.

## State persistence (external SQLite, ADR-0025)

| Store | Module | File | Purpose |
| ----- | ------ | ---- | ------- |
| Sync base versions | `sync_state.py` | `$HSYNC_STATE_DIR/syncstate.db` (folder UUID) | three-way merge base (§7) |
| Tombstones | `tombstones.py` | same DB, `tombstones` table | deletion propagation |
| Folder identity | `state_backend.py` | `.hsyncfolder` at bucket root | UUIDv4 re-anchor |
| TOFU pins | `pinning.py:56` | pin store (0o600) | LAN master fingerprint trust |
| LAN master identity | `ssl_utils.py:98` | persistent cert/key (0o600) | stable fingerprint across restarts |

`resolve_bucket_folder` (`state_backend.py:39`) memoizes the
`(folder_id, db_path)` resolution keyed by the raw `bucket_dir` string:
`sync_state`/`tombstones` call it on every per-file operation, so the steady
state pays a bare dict lookup, not a `.hsyncfolder` read + two SQLite statements
+ the `Path.resolve()`/env-read key computation (L1
`get_sync_state_store(cached)`: ~0.055 → ~0.0003 ms). Re-anchor semantics
(move/copy/delete-marker) are unchanged; `reset_bucket_folder_cache()` is wired
into the test `reset_sync_state_stores` autouse fixture.

> Human diagrams (modes, event processing, stability, bandwidth): `../docs/internal/architecture.md`.
> Decision rationale: [decisions.md](decisions.md) → `https://github.com/albertsdk/hsync-common/blob/main/docs/adr/`.
