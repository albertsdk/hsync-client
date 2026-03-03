# HSYNC Client — Remote (server) mode

Default mode: the client syncs a local dir to an `hsync-server` bucket over REST
(transfers) + WebSocket (event propagation). Server REST/WS contract:
[hsync-server wiki](https://github.com/albertobasso/hsync-server/blob/main/llm-wiki/README.md).

## Lifecycle (`remote_websocket.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `remote_events` | `remote_websocket.py:97` | WS connect + auth handshake + watchdog watcher, then the inbound processing loop. **Readiness threshold**: after `observer.start()` it logs `[READY] Client ready for sync — watching …` (`:179`) — the observable marker that the watcher is live and the WS is authenticated |
| `remote_connect` | `:373` | top-level: initial full sync + optional `resync` task + `remote_events`; reconnect loop with `RECONNECT_DELAY` |
| `_process_remote_events` | `:253` | read inbound frames, dispatch via `REMOTE_DISPATCH_CTX` (`:216`); per-message isolation: a malformed JSON frame is dropped (`JSONDecodeError` caught per frame) and apply errors logged — neither kills the connection loop. Accepts `added`/`modified`/`renamed`/`deleted`/`symlink`: symlink events are propagated by the server (`handle_symlink_event` returns the Event and `ConnectionManager.send_message` broadcasts it to every peer except the sender), so remote-mode clients now replicate symlinks in real time (parity with LAN mode) instead of waiting for the next full sync |
| `resync` | `:329` | delegates to `sync.periodic_resync(lan_mode=False)` |
| `RemoteFileEventHandler` | `:526` | watchdog → `upload_file` (threads `from_closed`); realtime uploads run on a process-wide bounded pool (`_get_remote_upload_executor:70`, `REALTIME_UPLOAD_MAX_WORKERS=8`; lazy singleton from the shared `hsync_client.pools.get_singleton_executor`) instead of inline on the watchdog dispatch thread — a large upload no longer blocks the processing of every subsequent event |

## WS handshake (shared SSOT)

The WS auth frame is built from `hsync_common.protocol.handshake`
(`build_ws_auth_request`): sends `protocol_version` (`PROTOCOL_VERSION_WS`),
`app_version`, `capabilities`, `token`. A `protocol_version` mismatch is a hard
fail (disconnect). Server side: [websocket.md](https://github.com/albertobasso/hsync-server/blob/main/llm-wiki/modules/websocket.md).

## REST transport (`utils.py`)

All REST calls go through a single pinned/configured `requests.Session`:

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `get_http_session` | `utils.py:93` | shared session (keep-alive, TLS verify) |
| `http_get/post/delete/patch` | `utils.py:120`–`:176` | thin verbs over the session |
| `get_ssl_verify` | `utils.py:65` | TLS verification policy |
| `diagnose_connection_error` | `utils.py:146` | classify TLS/plaintext mismatches for clear errors |

REST requests carry `X-Protocol-Version` (`PROTOCOL_VERSION_REST`); the server
hard-fails 426 on mismatch (server `middleware.py:111`). Endpoints mirror the
server API prefix `/api/v1` and are exercised by `sync.py` (list/tombstones) and
`transfer.py` (upload/download).

## Reconnect & resilience

- `remote_connect` (`remote_websocket.py:438`) is an infinite loop: each
  iteration re-measures RTT and re-seeds bandwidth (`_refresh_network_tuning()`,
  `:355`) and runs a full `sync()` before `remote_events`; on `ConnectionClosed`
  or unexpected error it sleeps a **full-jitter exponential backoff**
  (`_reconnect_delay`: `uniform(0, min(base*2**attempts, RECONNECT_DELAY_MAX))`,
  base = `RECONNECT_DELAY`) and loops again; the attempt counter resets after
  a healthy loop. `WatcherStartError` (local watcher refused even by the
  polling fallback — see [modules/sync-core.md](sync-core.md), "Watcher
  lifecycle") has its own branch: same exponential backoff, but a dedicated
  log line so the retry is not mistaken for a transport failure. Not
  attempt-capped: the daemon never exits on connectivity.
  Autotune may rewrite `RECONNECT_DELAY` (the backoff base) from RTT. The
  per-iteration refresh means the first sync after a cold start (startup
  probes failed while the server was still down) also runs on freshly
  measured RTT/bandwidth rather than the startup safe defaults.
- **Initial connectivity check** (`main_logic._check_server_connectivity`
  `main_logic.py:1142`) runs before the WS loop and uses exponential backoff
  with **7 attempts, 1–30 s** (default: `CONNECTIVITY_RETRY_*` constants,
  overridable via `HSYNC_CONNECTIVITY_RETRY_ATTEMPTS` /
  `HSYNC_CONNECTIVITY_RETRY_MIN_WAIT` / `HSYNC_CONNECTIVITY_RETRY_MAX_WAIT`
  environment variables). The backoff sequence is 1 s → 2 s → 4 s → 8 s → 16 s
  (~31 s cumulative), covering a typical server restart window. Each retry logs
  the attempt number (e.g. "attempt 2/5") so users can gauge progress. On
  failure **one-shot mode exits non-zero**; **continuous mode (the default)**
  logs a warning, keeps the client alive and delegates the infinite retry to
  `remote_connect` — a startup outage never terminates the daemon (LAN parity).
- Per-file transfers also use exponential backoff (`retry.py`);
  outbound WS events use `EventQueue` (see [sync-core.md](sync-core.md)).
- Transfers retry on transient errors (`retry.py:105`) and emit `transfer_failed`
  on exhaustion.
- Periodic `resync` (`:296`) and inotify-overflow handling force a full
  reconcile while already connected (§13).
- The reconnect `sync()` reconciles local changes made while the WS was down
  (the watcher is stopped during the outage): creations/modifications are
  uploaded, and deletions are detected by the offline-deletion tombstone
  minting in `sync()` (a merge-base entry whose file is no longer on disk →
  tombstone → `delete_remote`, no resurrection).
- **Crash-safe merge base**: real-time transfers (watcher uploads / inbound
  downloads) persist the merge base to `syncstate.db` immediately
  (`_record_synced_base`, see [sync-core.md](sync-core.md)), so a process kill
  between a watcher upload and the next cycle flush cannot lose the base —
  otherwise a file deleted while the process was dead would be resurrected on
  restart (no base to mint a tombstone from). Full-sync transfers defer to the
  single end-of-cycle flush.

## Auth & versioning

- Bearer API key resolved via `config.get_api_key` (`config.py:159`); missing key
  in server mode fails fast in `run_sync` (`main_logic.py`).
- App + protocol versions come from `constants.py:32` and
  `hsync_common.protocol.handshake`.

> Human guides: `../../docs/getting-started-server.md`, `../../docs/usage.md`,
> `../../docs/configuration-server.md`, `../../docs/configuration.md`.
