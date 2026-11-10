# HSYNC Client — Remote (server) mode

Default mode: the client syncs a local dir to an `hsync-server` bucket over REST
(transfers) + WebSocket (event propagation). Server REST/WS contract:
[hsync-server wiki](https://github.com/albertsdk/hsync-server/blob/main/llm-wiki/README.md).

## Lifecycle (`remote_websocket.py`)

| Symbol | Location | Role |
| ------ | -------- | ---- |
| `remote_events` | `hsync_client/remote_websocket.py::remote_events` | WS connect + auth handshake + watchdog watcher, then the inbound processing loop. **Readiness threshold**: after `observer.start()` it logs `[READY] Client ready for sync — watching …` (`hsync_client/remote_websocket.py::remote_events`) — the observable marker that the watcher is live and the WS is authenticated |
| `remote_connect` | `hsync_client/remote_websocket.py::remote_connect` | top-level: initial full sync + optional `resync` task + `remote_events`; reconnect loop with `RECONNECT_DELAY` |
| `_process_remote_events` | `hsync_client/remote_websocket.py::_process_remote_events` | own the connection's `InboundEventQueue` (`inbound_queue.py`, see [sync-core.md](sync-core.md#inbound_queuepy--scheduling-inbound-mutations)): read inbound frames (`_read_remote_events`, `hsync_client/remote_websocket.py::_read_remote_events`), validate the frame type and bucket, and `submit` each accepted event — the loop never waits for the transfer an event triggers, so a large download leaves the events behind it (an instant delete included) free to apply. `drain` runs in its `finally`, so the reconnect's full sync starts on a settled tree. Per-message isolation: a malformed JSON frame is dropped (`JSONDecodeError` caught per frame) and a failing mutation is logged — neither kills the connection loop. Frame validation accepts `added`/`modified`/`renamed`/`deleted`/`symlink`: symlink events are propagated by the server (`handle_symlink_event` returns the Event and `ConnectionManager.send_message` broadcasts it to every peer except the sender), so remote-mode clients replicate symlinks in real time (parity with p2p mode) instead of waiting for the next full sync. Dispatch config: `REMOTE_DISPATCH_CTX` (`hsync_client/remote_websocket.py::REMOTE_DISPATCH_CTX`, `recover_on_missing_rename_src=True`) |
| `resync` | `hsync_client/remote_websocket.py::resync` | delegates to `sync.periodic_resync(p2p_mode=False)` |
| `RemoteFileEventHandler` | `hsync_client/remote_websocket.py::RemoteFileEventHandler` | watchdog → `upload_file` (threads `from_closed`); realtime uploads run on a process-wide bounded pool (`hsync_client/remote_websocket.py::_get_remote_upload_executor`, `REALTIME_UPLOAD_MAX_WORKERS`; lazy singleton from the shared `hsync_client.pools.get_singleton_executor`) rather than inline on the watchdog dispatch thread, so a large upload does not block the processing of subsequent events |

## WS handshake (shared SSOT)

The WS auth frame is built from `hsync_common.protocol.handshake`
(`build_ws_auth_request`): sends `protocol_version` (`PROTOCOL_VERSION_WS`),
`app_version`, `capabilities`, `token`. A `protocol_version` mismatch is a hard
fail (disconnect). Server side: [websocket.md](https://github.com/albertsdk/hsync-server/blob/main/llm-wiki/modules/websocket.md).

## REST transport (`utils/http.py`)

All REST calls go through a single pinned/configured `requests.Session`:

| Symbol | Location | Role |
| ------ | -------- | ---- |
| `get_http_session` | `hsync_client/utils/http.py::get_http_session` | shared session (keep-alive, TLS verify); honours the environment proxies — unlike the fingerprint-pinned p2p session, which sets `trust_env = False` |
| `http_get/post/delete/patch` | `hsync_client/utils/http.py::http_get`–`hsync_client/utils/http.py::http_patch` | thin verbs over the session |
| `get_ssl_verify` | `hsync_client/utils/http.py::get_ssl_verify` | TLS verification policy |
| `diagnose_connection_error` | `hsync_client/utils/http.py::diagnose_connection_error` | classify TLS/plaintext mismatches for clear errors |

REST requests carry `X-Protocol-Version` (`PROTOCOL_VERSION_REST`); the server
hard-fails 426 on mismatch (server `middleware.py`). Endpoints mirror the
server API prefix `/api/v1` and are exercised by `sync.py` (list/tombstones) and
`transfer.py` (upload/download).

## Server certificate trust (ADR-0036)

The sync server authenticates with built-in TLS (self-signed) or a public-CA
certificate behind a reverse proxy, so the standard chain is the default anchor
(`chain`); the SSH-style pinning policies (`ask`/`accept-new`/`strict`/`none`)
are opt-in. The policy SSOT is `hsync_common.protocol.tls_trust`
(`evaluate_tls_trust`, also `chain`/`none` → proceed); the store is
`server_trust.py` (`ServerTrustStore`, `server_known_hosts.json`,
`HSYNC_SERVER_TRUST_FILE`) over the shared `known_hosts.py` base; the probe is
`fetch_server_fingerprint` (raw TLS, never trusts). At connect time
`main_logic._evaluate_server_trust` resolves the pin (flags `--server-trust` /
`--server-fingerprint` / `--server-tofu` / `--insecure`) and sets
`CONFIG["SERVER_CERT_FINGERPRINT"]`; `utils.get_ssl_verify` returns `False` when
it is set and `utils.get_http_session` mounts the same `FingerprintHTTPAdapter`;
the WSS event stream pins the live connection via
`remote_websocket._verify_server_wss_fingerprint`. A refusal logs the SSH-style
`trust_messages.server_*` guidance and emits
`pairing_needed`/`fingerprint_changed` (`scope="server"`).

## Reconnect & resilience

- `remote_connect` (`hsync_client/remote_websocket.py::remote_connect`) is an infinite loop: each
  iteration re-measures RTT and re-seeds bandwidth (`_refresh_network_tuning()`,
  `hsync_client/remote_websocket.py::remote_connect`) and runs a full `sync()` before `remote_events`; on `ConnectionClosed`
  or unexpected error it waits a **full-jitter exponential backoff**
  (`_reconnect_delay`: `uniform(0, min(base*2**attempts, RECONNECT_DELAY_MAX))`,
  base = `RECONNECT_DELAY`) and loops again. The wait is **interruptible**: the
  loop holds a `hsync_client/remote_websocket.py::_ReconnectWaker`, which turns the shared
  network monitor's "link is back" notification (`hsync_client/net_watch.py::NetworkChangeMonitor`,
  see [net-watch.md](net-watch.md)) into an `asyncio.Event` the backoff waits on, so
  a restored link is retried immediately instead of after up to
  `RECONNECT_DELAY_MAX`; the waker clears a stale signal first and unsubscribes
  when the loop ends. The attempt counter resets both after a healthy loop and as
  soon as `sync()` succeeds (a completed round proves the link works), so an
  unrelated later failure does not inherit the outage's delay. `WatcherStartError`
  (local watcher refused even by the polling fallback — see
  [modules/sync-core.md](sync-core.md), "Watcher lifecycle") has its own branch:
  same exponential backoff, but a dedicated log line so the retry is not mistaken
  for a transport failure. Not attempt-capped: the daemon never exits on
  connectivity.
  Autotune may rewrite `RECONNECT_DELAY` (the backoff base) from RTT. The
  per-iteration refresh means the first sync after a cold start (startup
  probes failed while the server was still down) also runs on freshly
  measured RTT/bandwidth rather than the startup safe defaults.
  Any failure of the event channel — `ConnectionClosed` or an unexpected
  exception — reports `waiting` (`hsync_client/remote_websocket.py::remote_events`), so the
  front-end never keeps claiming a live link while the loop is retrying. The
  event channel owns the link state: `connected` is published only after the auth
  handshake and its protocol-version check succeed, and a refused handshake
  (certificate fingerprint, authentication or protocol version) publishes
  `waiting`, marks the channel not ready (`hsync_client/events.py::set_event_channel_ready`)
  and raises `hsync_client/exceptions.py::WebSocketHandshakeError` so the loop backs off on
  its own attempt counter. While the channel is not ready the REST round does
  not report `connected` (`hsync_client/sync.py::_collect_listings` consults
  `hsync_client/events.py::event_channel_ready`), so a refused channel keeps reading
  `waiting` instead of flipping on every cycle. The data movement a cycle
  performs is a different axis (`activity`, see
  [`cli-config.md`](cli-config.md#ui-events-eventspy)): a link stays `connected`
  while the cycle runs.
- **Initial connectivity check** (`main_logic._check_server_connectivity`
  `hsync_client/main_logic.py::_check_server_connectivity`) runs before the WS loop and uses exponential backoff
  with **7 attempts, 1–30 s** (default: `CONNECTIVITY_RETRY_*` constants,
  overridable via `HSYNC_CONNECTIVITY_RETRY_ATTEMPTS` /
  `HSYNC_CONNECTIVITY_RETRY_MIN_WAIT` / `HSYNC_CONNECTIVITY_RETRY_MAX_WAIT`
  environment variables). The backoff sequence is 1 s → 2 s → 4 s → 8 s → 16 s
  (~31 s cumulative), covering a typical server restart window. Each retry logs
  the attempt number (e.g. "attempt 2/5") so users can gauge progress. On
  failure **one-shot mode exits non-zero**; **continuous mode (the default)**
  logs a warning, keeps the client alive and delegates the infinite retry to
  `remote_connect` — a startup outage never terminates the daemon (p2p parity).
- Per-file transfers also use exponential backoff (`retry.py`);
  outbound WS events use `EventQueue` (see [sync-core.md](sync-core.md)).
- Transfers retry on transient errors (`hsync_client/retry.py::retry_on_transient_transfer_error`) and emit `transfer_failed`
  on exhaustion.
- Periodic `resync` (`hsync_client/remote_websocket.py::resync`) and inotify-overflow handling force a full
  reconcile while already connected (§13).
- The reconnect `sync()` reconciles local changes made while the WS was down
  (the watcher is stopped during the outage): creations/modifications are
  uploaded, and deletions are detected by the offline-deletion tombstone
  minting in `sync()` (a merge-base entry whose file is no longer on disk →
  tombstone → `delete_remote`, no resurrection). **Renames made while
  disconnected** are paired with the stale remote path by `(sha256, size)` and
  applied with `PATCH …/files` — the server moves its own copy, so the file is
  not re-uploaded (ADR-0044; the same pairing runs in p2p mode and in the
  preview).
- **Crash-safe merge base**: real-time transfers (watcher uploads / inbound
  downloads) persist the merge base to the state DB immediately
  (`_record_synced_base`, see [sync-core.md](sync-core.md)), so a process kill
  between a watcher upload and the next cycle flush cannot lose the base —
  otherwise a file deleted while the process was dead would be resurrected on
  restart (no base to mint a tombstone from). Full-sync transfers defer to the
  single end-of-cycle flush.
- **Per-path upload scheduling**: `RemoteFileEventHandler` submits uploads through
  `hsync_client/upload_scheduler.py::PerPathUploadScheduler` — one in-flight attempt per path,
  with watcher bursts coalesced to the newest request (Syncthing-style). A slow
  delta scan therefore cannot race a concurrent full upload for the same path
  (which would move the server base and force a 409 fallback). `hsync_client/transfer.py::upload_file`
  self-records the local edit (`note_local_edit` + flush) **after** capturing the
  delta context (which needs the old base for signatures) and before advertising,
  so the advertised causal clock descends from the stored base and the server's
  arbitration accepts the delta instead of returning `superseded`.

## Auth & versioning

- Bearer API key resolved via `config.get_api_key` (`hsync_client/config.py::get_api_key`); missing key
  in server mode fails fast in `run_sync` (`main_logic.py`).
- App + protocol versions come from `constants.py` and
  `hsync_common.protocol.handshake`.

> Human guides: `../../docs/getting-started-server.md`, `../../docs/usage.md`,
> `../../docs/configuration-server.md`, `../../docs/configuration.md`.
