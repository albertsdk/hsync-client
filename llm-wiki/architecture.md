# HSYNC Client — Architecture (agent view)

One process, two run modes over a shared watcher + transfer core. No daemon
framework; asyncio event loop + watchdog observer + a thread pool for transfers.

## Entry & orchestration

`main.main` (`main.py:25`) invokes the Typer app `cli.main` (`cli.py:101`), which
parses flags, sets up logging, builds `CONFIG`, and calls `run_sync`
(`main_logic.py:501`). `run_sync` validates inputs, runs pre-sync safety checks,
then branches by mode and starts the watcher loop. Console script: `cli.run`
(`cli.py:674`).

## The two run modes

| Mode | Trigger | Network role | Core entry |
| ---- | ------- | ------------ | ---------- |
| **Remote (server)** | default; `--server-url` set | client ↔ `hsync-server` (REST + WS) | `remote_websocket.remote_events` (`remote_websocket.py:59`) |
| **LAN (P2P)** | `--lan-mode` | master (serves) or slave (syncs to master) via mDNS + HTTPS/WSS | `lan_handler.run_lan_mode` (`lan_handler.py:109`) |

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
- `_check_deletion_risk` (`main_logic.py:262`) — warns/prompts when settings could
  delete local data (gated by `--yes`).
- `_check_lan_write_policy` (`main_logic.py:344`) — requires a LAN key for
  write-capable LAN sync in non-interactive mode.
- `_check_disk_space` (`main_logic.py:388`), `_check_server_connectivity`
  (`main_logic.py:954`).
- Runtime mass-deletion guard during sync: `_mass_deletion_blocked` (`sync.py:94`).

## Readiness threshold (`[READY]`, shared label SSOT)

Every run mode emits exactly one observable `[READY]` INFO line when the
node is truly ready to synchronize — watcher active **and** event channel
established:

| Mode | Emission point |
| ---- | -------------- |
| Remote (watch) | `remote_websocket.py:179` — after WS auth + `observer.start()` |
| LAN master | `lan_handler.py:346` — after HTTP + WS servers up, mDNS `server_ready`, watcher running |
| LAN slave | `lan_websocket.py:875` — after watcher start **and** completed master handshake (a failed auth never reaches it) |

The label value is the SSOT member `hsync_common.labels.ActionLabel.READY`
(category `create`, green); Android mirrors it in
`hsync-android` `LogManager.kt` `LogAction.READY` (parity-tested by
`LogActionTest`). Failure paths (auth/protocol refusal) exit before the
threshold, so `[READY]` always means "reacting to events now".

## Watcher + transfer core (shared by both modes)

```
watchdog Observer ──> BaseFileEventHandler (file_event_handler.py:49)
        │ on_created/on_closed/on_modified/on_deleted/on_moved
        ▼
 stability check (transfer._wait_for_stable_file:591) ──> upload_file (transfer.py:665)
        │
 inbound WS event ──> event_dispatch.apply_inbound_event (event_dispatch.py:103)
        │                         └─> download/delete/rename/symlink locally
        ▼
 sync.sync (sync.py:132): list local/remote, diff (get_sync_actions:684),
                          execute in parallel (_execute_parallel_transfers:267)
```

Mode-specific handlers: `RemoteFileEventHandler` (`remote_websocket.py:425`) and
`LANMasterFileEventHandler` (`lan_websocket.py:546`) subclass `BaseFileEventHandler`;
`LANSlaveFileEventHandler` (`lan_websocket.py:1003`) is a separate watchdog
`FileSystemEventHandler` for the slave→master upload path.

## Layering

```
constants / exceptions / logger / compression                  (leaf)
        │
utils (http session + pinning, path_lock, perms, bandwidth) / retry / ssl_utils
        │
config (CONFIG SSOT) / events (UI event emitter)
        │
sync / transfer / event_dispatch / event_queue / file_event_handler   (core)
        │
remote_websocket | lan.py + lan_server + lan_websocket + lan_handler   (modes)
        │
main_logic (orchestration) ← cli ← main
```

## State persistence (external SQLite, ADR-0025)

| Store | Module | File | Purpose |
| ----- | ------ | ---- | ------- |
| Sync base versions | `sync_state.py` | `$HSYNC_STATE_DIR/syncstate.db` (folder UUID) | three-way merge base (§7) |
| Tombstones | `tombstones.py` | same DB, `tombstones` table | deletion propagation |
| Folder identity | `state_backend.py` | `.hsyncfolder` at bucket root | UUIDv4 re-anchor |
| TOFU pins | `pinning.py:56` | pin store (0o600) | LAN master fingerprint trust |
| LAN master identity | `ssl_utils.py:98` | persistent cert/key (0o600) | stable fingerprint across restarts |

> Human diagrams (modes, event processing, stability, bandwidth): `../docs/architecture.md`.
> Decision rationale: [decisions.md](decisions.md) → `https://github.com/albertobasso/hsync-common/blob/main/docs/adr/`.
