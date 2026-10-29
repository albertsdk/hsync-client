# HSYNC Client — Architecture (agent view)

One process, two run modes over a shared watcher + transfer core. No daemon
framework; asyncio event loop + watchdog observer + a thread pool for transfers.

## Entry & orchestration

`main.main` (`main.py::main`) invokes the Typer app (`cli.py`): a parent callback
`cli.main` (`cli.py::main`) carrying the global options, plus two mutually
exclusive subcommands `cli.p2p` (`cli.py::p2p`) and `cli.server` (`cli.py::server`).
Each subcommand validates its options and calls `run_sync` (`main_logic.py::run_sync`),
which validates inputs, runs pre-sync safety checks, then branches by mode and
starts the watcher loop. Console script: `cli.run` (`cli.py::run`).

## The two run modes

| Mode | Trigger | Network role | Core entry |
| ---- | ------- | ------------ | ---------- |
| **Remote (server)** | `hsync server` subcommand; continuous unless `--once` | client ↔ `hsync-server` (REST + WS) | `remote_websocket.remote_events` (`remote_websocket.py::remote_events`) |
| **p2p** | `hsync p2p` subcommand | master (serves) or slave (syncs to master) via mDNS or rendezvous rendezvous + HTTPS/WSS | `p2p_handler.run_p2p_mode` (`p2p_handler.py::run_p2p_mode`) |

p2p sub-roles: `_run_as_master` (`p2p_handler.py::_run_as_master`) starts the **single**
`P2PServer` (`p2p_server.py::P2PServer`) — one TLS listener serving the REST data plane and
the WSS realtime control plane (`P2PWebSocketServer`, `p2p_websocket.py::P2PWebSocketServer`) on the
same port — plus the master watcher; `_run_as_slave`
(`p2p_handler.py::_run_as_slave`) connects to the elected master. Role is chosen by election
(see [p2p-mode.md](modules/p2p-mode.md)).

## Config flow

CLI flags + `HSYNC_*` env (pydantic `EnvSettings` `config.py::EnvSettings`) merge into the
global `CONFIG: HsyncConfig` TypedDict (`config.py::HsyncConfig`) via `set_config`
(`config.py::set_config`); modules read it directly and through the typed
getters (`config.py::get_server_url`–`config.py::get_connectivity_retry_max_wait`). Input validation is inline in `cli.py`
(Typer); defaults/bounds in `constants.py`.

## Pre-sync safety

`run_sync` runs guards before touching files:
- `_check_deletion_risk` (`main_logic.py::_check_deletion_risk`) — warns/prompts when settings could
  delete local data (gated by `--allow-deletions`).
- `_check_p2p_write_policy` (`main_logic.py::_check_p2p_write_policy`) — requires a p2p key for
  write-capable p2p sync in non-interactive mode.
- `_check_disk_space` (`main_logic.py::_check_disk_space`), `_check_server_connectivity`
  (`main_logic.py::_check_server_connectivity`).
- Runtime mass-deletion guard during sync: `_mass_deletion_blocked` (`sync.py::_mass_deletion_blocked`).

## Readiness threshold (`[READY]`, shared label SSOT)

Every run mode emits exactly one observable `[READY]` INFO line when the
node is truly ready to synchronize — watcher active **and** event channel
established:

| Mode | Emission point |
| ---- | -------------- |
| Remote (continuous, default) | `remote_websocket.py::remote_events` — after WS auth + `observer.start()` |
| p2p master | `p2p_handler.py::_run_as_master` — after HTTP + WS servers up, mDNS `server_ready`, watcher running |
| p2p slave | `p2p_websocket.py::p2p_websocket_client` — after watcher start **and** completed master handshake (a failed auth never reaches it) |

The label value is the SSOT member `hsync_common.labels.ActionLabel.READY`
(category `create`, green); Android mirrors it in
`hsync-android` `LogManager.kt` `LogAction.READY` (parity-tested by
`LogActionTest`). Failure paths (auth/protocol refusal) exit before the
threshold, so `[READY]` always means "reacting to events now".

## Watcher + transfer core (shared by both modes)

```
WatcherManager (watcher.py::WatcherManager) ──> BaseFileEventHandler (file_event_handler.py::BaseFileEventHandler)
        │ on_created/on_closed/on_modified/on_deleted/on_moved
        ▼
 stability check (transfer._wait_for_stable_file) ──> upload_file (transfer.py::upload_file)
         │    via PerPathUploadScheduler (upload_scheduler.py::PerPathUploadScheduler):
         │    one in-flight upload per path, watcher bursts coalesced to the newest
         │    request; upload_file self-records the local edit before advertising
        │
 inbound WS event ──> event_dispatch.apply_inbound_event (event_dispatch.py::apply_inbound_event)
        │                         └─> download/delete/rename/symlink locally
        ▼
 sync.sync (sync.py::sync): list local/remote, diff (sync.py::get_sync_actions),
                          execute in parallel (sync.py::_execute_parallel_transfers)
```

Mode-specific handlers: `RemoteFileEventHandler` (`remote_websocket.py::RemoteFileEventHandler`) and
`P2PMasterFileEventHandler` (`p2p_websocket.py::P2PMasterFileEventHandler`) subclass `BaseFileEventHandler`;
`P2PSlaveFileEventHandler` (`p2p_websocket.py::P2PSlaveFileEventHandler`) is a separate watchdog
`FileSystemEventHandler` for the slave→master upload path. The realtime upload hooks
submit through `PerPathUploadScheduler` (`upload_scheduler.py::PerPathUploadScheduler`):
one in-flight attempt per path, with bursty watcher events coalesced to the newest
request, so a slow delta scan never races a concurrent full upload for the same file.

## Layering

```
constants / exceptions / logger / compression                  (leaf)
        │
utils (http session + pinning, path_lock, perms, bandwidth) / retry / ssl_utils / pools
        │
config (CONFIG SSOT) / events (UI event emitter)
        │
sync / transfer / event_dispatch / event_queue / file_event_handler / upload_scheduler   (core)
        │
remote_websocket | p2p_peers.py / p2p_rendezvous_mdns.py + p2p_server + p2p_websocket + p2p_handler   (modes)
        │
main_logic (orchestration) ← cli ← main
```

**Logger sinks are asynchronous** (`logger.py::setup_logging`, `enqueue=True`
on both the `--log-file` and stdout sinks): formatting and the write syscall
run on loguru's dedicated thread, so per-event lines (one `[LOCAL CREATE]`
per file during a burst) never throttle the emitting engine thread — a
synchronous file sink measured ~2.5× slower event drain on the master's
watcher thread. Readers that need the file content immediately after a log
call wait via `logger.complete()`. The stdout sink is always installed: a
`--log-file` sink is added next to it, never in place of it, because the
parent that drives the client (`hsync-desktop`) reads those lines and has no
other view of the log; `-q` is the only suppressor.

**The stdlib root level is the resolved level, not loguru's `level=0`
recipe** (`logging.basicConfig(..., level=level)`): a NOTSET root answers
`True` to `isEnabledFor(DEBUG)`, so every DEBUG line a library emits would
build a `LogRecord` and walk the stack in `findCaller` before the interceptor
forwards it to loguru, whose sink drops it — ~26 wasted records per
transferred file on a LAN burst, 6.5 µs each against 0.08 µs for the level
check that filters them first. `--debug` raises the root to DEBUG and lets
them through.

**Exit paths flush the sinks** (`logger.py::flush_logs`, blocking `complete()`
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

**The sinks are closed on exit** (`logger.py::shutdown_logging`, drain then
`logger.remove()`): `enqueue=True` hands every sink a multiprocessing queue whose
semaphores belong to the stdlib resource tracker, and a single `--log-file` sink
accounts for eight of them. A process that ended with the handlers installed made
the tracker warn `There appear to be 8 leaked semaphore objects` at every stop;
closing them unregisters the semaphores. `cli.run` calls it in its `finally`, so
it covers normal exits, `SystemExit`, unhandled errors and signals.

**A stop is a `SIGTERM`, and it unwinds like Ctrl+C**
(`cli.py::_install_sigterm_handler`): the default action kills the process before
any `finally` runs, which is why a service stop without the handler skips the
server/P2P teardown and leaves the sinks open. `cli.run` installs a handler
that raises `KeyboardInterrupt` in the main thread, so `systemctl stop`,
`docker stop` and the
desktop's Stop button (SIGTERM, then SIGKILL after 5 s) follow the same path as
`SIGINT`: `run_p2p_mode` catches it, stops its servers, the sinks flush and close,
and the process exits normally. A **stop is not a failure**, which takes two
catches in `cli.run`: the plain `KeyboardInterrupt`, and the
`SystemExit(_INTERRUPTED_EXIT_CODE)` value that Typer produces on its own
(`typer/core.py` turns an interrupt into `Exit(130)` before any handler can see
it) — a server mode would otherwise exit with status 130. Any other exit code is
re-raised untouched.

## State persistence (external SQLite, ADR-0025)

| Store | Module | File | Purpose |
| ----- | ------ | ---- | ------- |
| Sync base versions | `sync_state.py` | `<device_dir>/syncstate/<uuid>.db` (folder UUID) | three-way merge base (§7) |
| Tombstones | `tombstones.py` | same DB, `tombstones` table | deletion propagation |
| Folder identity | `state_backend.py` | `.hsyncfolder` at bucket root | UUIDv4 re-anchor |
| TOFU pins | `pinning.py::PinStore` | pin store (0o600) | p2p master fingerprint trust |
| Rendezvous trust | `rendezvous_trust.py::RendezvousTrustStore` | `$HSYNC_RENDEZVOUS_SERVER_TRUST_FILE` (0o600) | rendezvous fingerprint pin/TOFU (ADR-0034) |
| p2p master identity | `ssl_utils.py::load_or_create_master_cert` | persistent cert/key (0o600) | stable fingerprint across restarts |

`resolve_bucket_folder` (`state_backend.py::resolve_bucket_folder`) memoizes the
`(folder_id, db_path)` resolution keyed by the raw `bucket_dir` string:
`sync_state`/`tombstones` call it on every per-file operation, so the steady
state pays a bare dict lookup, not a `.hsyncfolder` read + two SQLite statements
+ the `Path.resolve()`/env-read key computation (L1
`get_sync_state_store(cached)`: ~0.055 → ~0.0003 ms). Re-anchor semantics
(move/copy/delete-marker) are unchanged; `reset_bucket_folder_cache()` is wired
into the test `reset_sync_state_stores` autouse fixture.

> Human diagrams (modes, event processing, stability, bandwidth): `../docs/internal/architecture.md`.
> Decision rationale: [decisions.md](decisions.md) → `https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/`.
