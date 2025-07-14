# HSYNC Client — Remote (server) mode

Default mode: the client syncs a local dir to an `hsync-server` bucket over REST
(transfers) + WebSocket (event propagation). Server REST/WS contract:
[hsync-server wiki](https://github.com/albertobasso/hsync-server/blob/main/llm-wiki/README.md).

## Lifecycle (`remote_websocket.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `remote_events` | `remote_websocket.py:58` | top-level: initial sync + WS connect + watcher |
| `remote_connect` | `:275` | open WS, run auth handshake, then process loop |
| `_process_remote_events` | `:212` | read inbound events, dispatch via `REMOTE_DISPATCH_CTX` (`:205`); per-message error isolation (one malformed event cannot kill the loop) |
| `resync` | `:257` | periodic/triggered full re-reconcile |
| `RemoteFileEventHandler` | `:324` | watchdog → `upload_file` (threads `from_closed`) |

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

- Outbound events buffered + retried by `EventQueue` (see
  [sync-core.md](sync-core.md)).
- Transfers retry on transient errors (`retry.py:105`) and emit `transfer_failed`
  on exhaustion.
- Periodic `resync` and inotify-overflow handling force a full reconcile (§13).

## Auth & versioning

- Bearer API key resolved via `config.get_api_key` (`config.py:159`); missing key
  in server mode fails fast in `run_sync` (`main_logic.py`).
- App + protocol versions come from `constants.py:32` and
  `hsync_common.protocol.handshake`.

> Human guides: `../../docs/getting-started-server.md`, `../../docs/usage.md`,
> `../../docs/configuration-server.md`, `../../docs/configuration.md`.
