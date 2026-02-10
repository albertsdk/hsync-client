# HSYNC Client — LAN (P2P) mode

Server-less mode: peers discover each other on the LAN (mDNS), **elect a master**,
and the master serves files over HTTPS/WSS while slaves sync to it. The master
is a **transport coordinator**, not an authority: every node applies the same
causal vector-clock arbitration (ADR-0022). Security and election rules are
SSOT in `hsync-common`.

> **Domain-critical**: before changing discovery/election/security, read
> [`hsync-common/docs/lan-protocol.md`](https://github.com/albertobasso/hsync-common/blob/main/docs/lan-protocol.md).

## Entry & roles (`lan_handler.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `run_lan_mode` | `lan_handler.py:109` | start discovery, elect, branch to role; kicks off the background SNTP clock-offset probe (`timesync.start_ntp_clock_offset_probe`) so discovery never waits on an external time source |
| `_run_as_master` | `:273` | start `LANServer` + `LANWebSocketServer` + watcher; **readiness threshold**: after both servers are up and `update_server_ready()` ran, it logs `[READY] Master ready — serving …` (`:346`) |
| `_run_as_slave` | `:418` | TOFU-pin master, refresh tuning, then connect + sync |
| `_evaluate_master_pin` | `:377` | apply pin decision before slave connect |
| `_refresh_lan_network_tuning` | `:107` | re-derive autotune-managed keys from real master RTT (slave, post-discovery) |
| `NO_MASTER_WARNING_TIMEOUT` | `:50` | warn when no master appears |

## Discovery & election (`lan.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `LANSync` | `lan.py:654` | top-level LAN coordinator |
| `PeerManager` | `:129` | tracks peers, runs election, heartbeats |
| `Peer` | `:90` | a discovered peer (cid, endpoint, cert_fp, sync_direction) |
| `HsyncServiceListener` | `:501` | Zeroconf callbacks (add/update/remove; parses the ``connected`` TXT value) |
| `get_free_port` | `:68` | bind an ephemeral serving port |
| `HSYNC_SERVICE_TYPE` | `:47` | `_hsync._tcp.local.` mDNS type |

Election is delegated to the common SSOT: `PeerManager._elect_master` builds
`ElectionCandidate`s and calls `hsync_common.protocol.lan.elect_master`
(bidirectional peers preferred; `--force-master`/`--never-master` honored). TXT
records advertise `protocol`, `cert_fp`, `sync_direction`; protocol-mismatched
peers are ignored.

`LANSync.wait_for_discovery` is the single owner of the display-ready presence
pair (`hsync_client/presence.py`): `total = 1 + alive mDNS peers`, `connected =
1 + WS clients connected` (as master, via the WS-server provider wired by
`lan_handler.run_lan_mode`; as slave, from the master's advertised
`connected` TXT value). The pair is emitted as the `peers` UI event (schema v2)
on change, and the operator-facing status log uses three states: `Discovered N
peer(s)`, `No other peers discovered - N client(s) connected`, or `No other
peers discovered - this client is alone`. The master re-registers its mDNS
record whenever the WS count changes (`LANSync.set_connected_clients`), so
slaves observe presence without a server round-trip.

The wait is **event-driven**, not a fixed sleep: `PeerManager` keeps a
generation counter + `threading.Condition` (`_change_generation` /
`wait_for_change`) signalled on peer add/remove, local `server_ready` flip,
election-relevant field changes, and discovery-relevant metadata updates
(address/port, HTTPS/fingerprint, `connected`, capabilities). Heartbeat-only
`last_seen` refreshes do **not** signal. `wait_for_discovery` blocks on
`PeerManager.wait_for_change(timeout)` instead of `time.sleep(timeout)`. A
peer already advertising (or a master flipping to `server_ready`) therefore
wakes the loop in mDNS time (~1 s) rather than after the full
`DISCOVERY_TIMEOUT` window. With no change the wait still blocks for the full
window, so the polling cadence is unchanged in steady state.

## Master server (`lan_server.py`, `lan_websocket.py`)

The LAN master mirrors the central server's causal upload arbitration via
`hsync_client/arbitration.py` (parallel to `hsync_server.arbitration`, both
built on `hsync_common` primitives + the local sync-state store). The
`_handle_upload` endpoint arbitrate-then-commit: a stale upload is rejected
(`409 superseded`), a tie-break loser is preserved as a conflicted copy
(`409 conflict_lost`), and an accepted upload records the merged vclock
(convergence D11). The `/files` listing and broadcast events attach the
effective causal version to each `FileDict` so slaves run the same causal
matrix on the wire data.

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `LANServer` | `lan_server.py:1627` | HTTPS file server (REST mirror of hsync-server) |
| `BoundedThreadingHTTPServer` | `:118` | `ThreadingHTTPServer` subclass with a `BoundedSemaphore` cap (H5, default `LAN_SERVER_MAX_THREADS=64`). With HTTP/1.1 keep-alive each **connection** — idle ones included — holds its thread+slot for its whole lifetime, so the cap is sized for the peers' connection pools, not just in-flight requests; idle connections are reaped after `LAN_HANDLER_IDLE_TIMEOUT_S` (300 s — above the worst-case inter-chunk silence of a maximally throttled 256 KiB upload at the 1 KB/s CLI floor, ~262 s, and the client HTTP timeout); `handle_error` logs routine peer connection resets/timeouts at DEBUG |
| `LANRequestHandler` | `:215` | per-request handler; **HTTP/1.1 keep-alive** (`protocol_version`, every response framed with an exact `Content-Length` — `_send_json_response` always sets it — so connections persist and no TCP+TLS handshake is paid per request) with **TCP_NODELAY** on every connection (`disable_nagle_algorithm`: without it Nagle parks the small response body behind the peer's delayed ACK — a ~40 ms stall per response on persistent connections, benchmarked at 24 vs 5500 req/s by `scripts/benchmark_lan_transfers.py`); endpoints `API_PREFIX`; `POST /dirs` creates directories from slaves (`_handle_create_dir`); `POST /symlinks` creates links STRUCTURALLY from slaves (`_handle_symlink` — mirror of the central server; an existing file/link is replaced, the target is validated with `is_safe_symlink_target`). **Idempotent**: a link already present with the SAME target is a converged echo and returns 200 without touching the filesystem (unlink+recreate on every echo was what kept the SYMLINK/DELETE oscillation alive: each mutation emitted a `deleted`+`created` pair, the single inbound-origin mark was consumed by the first, and the second was rebroadcast with the master CID) |
| `_handle_patch_permissions` | `lan_server.py` (PATCH route) | metadata-only chmod from a slave: `PATCH /files/permissions?filename&permissions&cid` applies the sanitized mode to an existing file (owner rwx floor via `set_permissions_on_dir` for dirs; no file bytes cross the wire), attributes the resulting IN_ATTRIB to the slave (`mark_inbound_origin`) so the master's watcher re-broadcasts the change to the other slaves. Mirrors the central server's `PATCH …/files/permissions` |
| `_handle_list_dirs` | `lan_server.py` (GET route) | `GET /dirs` returns `[{"path", "permissions"}]` for every non-hidden, non-symlink directory — mirrors the central server so the client's download-only directory-permission converge (`converge_directory_permissions`) works identically in both modes |
| `_create_dir_on_master` (permissions param) | `lan_websocket.py:1364` | `POST /dirs` gains an optional `permissions` query param: the slave's directory-chmod propagate (`_propagate_dir_permission_change`, `_last_dir_mode` dedup map) reuses the endpoint to push the new mode; the master applies it and re-broadcasts |
| `_handle_create_dir` parent marking | `lan_server.py:532` | `mkdir(parents=True)` also creates every missing ancestor of the requested path, and the master's recursive watcher fires a create event for each. `_handle_create_dir` marks **every missing ancestor inside the bucket** (plus the leaf, only when it does not already exist) with `mark_inbound_origin` before the mkdir, so those synthetic create events carry the slave's CID: without it they would be logged/broadcast as local creates and the slave could never echo-suppress them (`[REMOTE CREATE]` lines on the slave side). Ancestors above the bucket root are never candidates |
| `_StreamingMultipartParser` | `multipart.py:68` | streaming upload parsing (extracted to `multipart.py`) |
| `_handle_upload_status` | `lan_server.py:1067` | `GET /files/upload-status` — reports `.part` size for resume (mirrors central server `get_upload_status`); returns `complete`/`resumable`/`new` + offset |
| `_handle_upload` (offset) | `:1153` | resumable upload: `offset > 0` appends to `.part` in `"ab"` mode (size validated); incomplete uploads return `202` with the new offset; compressed + offset rejected |
| compressed download cap | `LANRequestHandler._handle_download` | the compressed download branch buffers the whole file in RAM to build the zstd frame, so it is capped at `LAN_COMPRESSION_MAX_FILE_BYTES` (256 MB): larger compressible files fall back to the streaming UNCOMPRESSED path (no master RAM spike — the old bound was `MAX_INBOUND_SIZE`, 10 GB by default) and keep Range-resume, which a zstd response cannot offer |
| `_CappedBytesWriter` | `multipart.py:39` | abort uploads whose bytes exceed the effective cap — min(declared size, `MAX_INBOUND_SIZE`) — (decompression-bomb containment) |
| `LANWebSocketServer` | `lan_websocket.py:185` | WSS event hub; LAN-key auth (`hmac.compare_digest`) with CID validation (`is_valid_cid`); **`lan/1` capability exchange (ADR-0024)** — greeting/hello/auth frames carry `protocol_version` + `capabilities` (master advertise, slave reply); a protocol mismatch on the auth path is a hard fail (`reason: protocol_incompatible`); when no `LAN_KEY` is set, the `hello` handshake exchanges capabilities for coherence only (logged `unauthenticated`) — CID format is validated in both paths |
| `LANMasterFileEventHandler` | `:546` | master-local FS changes → broadcast; logs every event with the originator CID (via `peek_inbound_origin`, which does NOT consume the entry) |

Peer-supplied paths on download/delete/rename/upload are validated with
`validate_remote_relpath` and confined with `resolve_within` (no `startswith`
prefix checks — sibling directories cannot bypass confinement).

The reconcile action `transfer.delete_file_remote` speaks **both transports'
DELETE contracts**: form fields (`filename`, `cid`) against the central server
and query params (`path`, `is_dir`, `cid`) against the LAN master
(`lan_server._handle_delete` reads only the query string). Sending the wrong
shape made the master answer 400 and left the stale remote copy in place (the
wire-shape half of the resurrection bug); the branch is driven by
`CONFIG["LAN_MODE"]` and covered by `tests/test_transfer_delete_remote.py`.
**Delete/rename/symlink endpoints use `resolve_final=False`**: the final
component is never dereferenced, so deleting/renaming/replacing a link acts on
the LINK entry, never on its target (with the default full resolve, deleting a
link deleted the target file on the master and the broadcast propagated the
data loss to every peer). Presence checks are dangling-aware
(`is_symlink() or exists()`): a dangling link is a real directory entry. The
`/files` listing and `/files/download` handlers additionally skip/refuse file-symlinks
whose target escapes `LOCAL_BUCKET` (`is_safe_symlink_target`): a slave must
never be able to dereference `secret -> /etc/passwd` through the master and
learn its hash (listing) or content (download). In-bucket symlinks are listed
as STRUCTURAL entries (`type: "symlink"` with `(filepath, target)`, never a
dereferenced File), so a full-sync download re-creates the LINK on the slave
(`materialize_symlink_local`). Uploads require
a matching `sha256sum` form field (format-validated with `is_sha256_hex`,
verified with `hmac.compare_digest` before the atomic rename), clamp
far-future mtimes (`MTIME_MAX_FUTURE_SKEW_SECONDS`), and cap the bytes landing
on disk at `min(declared size, MAX_INBOUND_SIZE)` — the declared size alone is
not a safe bound against a hostile peer, and uploads with no declared size are
capped at `MAX_INBOUND_SIZE` outright. The `.part` temp file is opened with
`O_NOFOLLOW` (`utils.open_partial_no_follow`), so a raced symlink cannot
redirect the streamed write. Resumable uploads open the `.part` in `"ab"`
(append) mode when `offset > 0`; the existing size must match the declared
offset or the upload is rejected. Incomplete uploads (total bytes < declared
size) return `202 Accepted` with the new offset so the client can resume;
complete uploads proceed to SHA-256 verification and atomic rename. Multipart form fields (64 KiB) and part-header
lines (8 KiB) are memory-capped; only the file part streams. Tombstone gossip
POST bodies are size-capped (`MAX_TOMBSTONE_PAYLOAD_BYTES`). The
echo-suppression `cid` on upload/delete/rename is honoured only when it passes
`is_valid_cid` *and* the request is authenticated (`LAN_KEY` set).

## Slave client (`lan_websocket.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `lan_websocket_client` | `lan_websocket.py:736` | connect to master WSS, verify cert fp, **`lan/1` handshake**: hard-fail on greeting protocol mismatch, auth/hello frames carry `protocol_version` + `capabilities`; `local_capabilities()` is announced, `adopt_remote_capabilities()` applies the auth reply as the authoritative capability source (compression/delta gates). **Readiness threshold**: the local watcher starts before the WS connect, but `[READY]` (`:875`) fires only after the handshake completed — a failed auth never reaches it |
| `_handle_lan_event` | `lan_websocket.py:985` | apply inbound master events locally; the `lan_websocket_client` loop builds **one `EventDispatchContext` per connection** (`_build_lan_dispatch_ctx`, `lan_websocket.py:963`) and passes it to every call so the inbound directory dedup cache survives across events (a per-event context would reset the cache and re-log every duplicate `added`/`modified` directory burst as a fresh `[REMOTE CREATE]`/`[REMOTE MODIFY]`) |
| `LANSlaveFileEventHandler` | `:1082` | slave-local FS changes → upload to master. Realtime operations (uploads, inbound event downloads, deletes, renames) run on a process-wide bounded pool (`_get_realtime_executor:109`, `REALTIME_MAX_WORKERS=8`; lazy singleton from the shared `hsync_client.pools.get_singleton_executor`) instead of one thread per event: long-lived workers reuse their keep-alive HTTP sessions to the master (no TCP+TLS handshake per file) and the parallelism stays predictable; `_start_operation` tracks `Future`s so `wait_pending` can still wait them out gracefully |
| live deletion marker (slave) | `LANSlaveFileEventHandler.on_deleted` (`:1771`) / `on_moved` visible→hidden | a slave-side file deletion records a **causal tombstone locally** via the shared `_record_deletion_marker` (`lan_websocket.py:520`) before the best-effort HTTP DELETE reaches the master. Correctness never depends on the transport being up: the marker inherits the merge-base hash + clock (ticked by this peer) and drops the merge base, so the next reconciliation deletes the stale remote copy (`delete_remote`) instead of resurrecting it — the master-unreachable-deletion regression |
| `_lan_periodic_resync` | `:937` | periodic full reconcile (§13) |
| echo suppression | `:113`/`:137` | `mark_inbound_origin` / `consume_inbound_origin` (TTL `:110`) |
| rename redundancy (inbound + outbound) | `LANSlaveFileEventHandler.record_inbound_rename` / `is_inbound_rename_echo` + `_record_outbound_dir_rename` / `is_outbound_dir_descendant` | exact `(src,dst)` pair cache (TTL `:703`); also matches **descendant** echoes of a directory rename in both directions: inbound (master renamed `ABC -> ABC-123`, slave's watcher echoes children) and outbound (slave renames `ABC-123 -> ABC-123-X`, its watcher emits child `on_moved` events). A single PATCH already renamed the whole tree on the master, so `on_moved` skips the redundant child PATCHes proactively and a 404 from the master is degraded to DEBUG when it matches, else stays WARNING (real divergence). Identifier-suffix guard (starts with `/`) avoids suppressing genuine renames that merely share a path prefix |
| inbound-delete echo | `event_dispatch._apply_delete` (dir `:457` / file `:485`) | a `deleted` event for a path already absent locally is a benign echo (originator removed it / peer delete converged / never existed here): logged at DEBUG, not WARNING. Echo suppression can miss when the master has no `LAN_KEY` (the `mark_inbound_origin` cid gate is a security measure) or when `rmtree` removes a directory's children without individually marking them |
| recursive inbound delete echo | `event_dispatch._apply_delete` dir branch + `LANSlaveFileEventHandler.record_inbound_delete` / `is_inbound_delete_echo` | before `rmtree`, the confined absolute root path is passed to `EventDispatchContext.record_inbound_delete` (wired in `_handle_lan_event`). The slave caches the root with a 10 s TTL and its `on_deleted` drops any later delete equal to or under that root (same `/`-suffix guard as the rename echo): those are its own watcher echoes of the rmtree it just applied, not new local deletions — without the cache each would be re-sent to the master where it 404s as a benign echo. Remote mode never installs the callback |
| create-upload deferral | `LANSlaveFileEventHandler._schedule_create_upload` / `_cancel_create_upload` / `cancel_all_create_timers` | an `on_created` upload for a non-empty regular file is deferred (`CREATE_UPLOAD_DEFER_S` = `STABILITY_DELAY_ON_CLOSED`) so `on_closed` (`IN_CLOSE_WRITE`, the reliable fd-closed trigger) can cancel it and take precedence with the fast stability re-check; the timer is the fallback for files that arrive already complete with no writer fd (atomic rename / hard-link / `mv` in). `on_deleted` / `on_moved(src)` / shutdown cancel pending timers so stale uploads never fire. Eliminates the duplicate upload-intent/`[UPLOAD]` pair (the intent line was later removed entirely — ADR 0018) and avoids uploading half-written files. |
| sentinel never uploaded | `LANSlaveFileEventHandler._handle_file_upload` | a `.hsynckeep` basename is skipped before building the `File` and starting the upload. The sentinel is a per-peer local artifact: the periodic reconcile already excludes it from sync actions in both directions (`sync.py`), each peer maintains its own (`ensure_hsynckeep_files` / `_ensure_sentinel*`), and the directory itself is propagated by the directory event — uploading it only produced a 0-byte `[UPLOAD]` line and a broadcast round-trip. This is the single chokepoint covering every slave upload path (`on_created` deferred, `on_closed`, symlink, hidden→visible). |
| escaping symlink refused (slave upload) | `LANSlaveFileEventHandler.on_created` (symlink branch) | a file-symlink inside the bucket whose target escapes `LOCAL_BUCKET` is skipped (logged `[SECURITY]`) instead of being dereferenced and exfiltrated. An in-bucket symlink is uploaded STRUCTURALLY via `transfer.upload_symlink` (the pair over `POST /symlinks`; the absolute local target is relativized by `hsync_common.wire_symlink_target` before it reaches the wire) — the content-upload path (`_upload_to_master` → `upload_file`) is never used for links. Belt-and-braces: `transfer.upload_file` refuses any symlink whose target escapes the bucket, and the shared watcher (`file_event_handler.BaseFileEventHandler.on_created`) refuses to broadcast a `symlink` event whose target escapes. |

## Local capabilities (`capabilities.py`)

| Symbol | Purpose |
| ------ | ------- |
| `local_capabilities` | runtime-derived capability set advertised on every handshake (always `vector_clocks` + `tombstones`; `compression`/`delta` when enabled) — shared by remote and LAN transports (ADR-0024) |
| `adopt_remote_capabilities` | adopts the authenticated peer's capabilities as the session transfer gates (`SERVER_COMPRESSION_ENABLED`/`SERVER_DELTA_ENABLED`) |

## Security: TOFU pinning + persistent identity

| Concern | Code | Notes |
| ------- | ---- | ----- |
| Pin store | `pinning.py:56` (`PinStore`) | disk-backed, `0o600` (`_PIN_FILE_MODE:39`); key `_pin_key:51`; path `default_pin_store_path:42` / `PIN_FILE_ENV:35` |
| Pin decision | common `protocol/pairing.py` | `evaluate_pin`; refuse on changed fingerprint, emit `fingerprint_changed`/`pairing_needed` |
| Device identity | `identity.py` (`load_or_create_device_identity`) | Syncthing-style: one identity per installation — stable device ID (`device_id` file, CID_PATTERN-valid) + keypair in `HSYNC_DEVICE_DIR` (`default_device_dir`); cached process-wide. The device ID **is** the LAN `cid` (resolved in `main_logic.run_sync`), so pins survive restarts and cert rotation (delete `*.pem`, keep `device_id`) is detected as `[FINGERPRINT CHANGED]`. Keypair itself: `ssl_utils.load_or_create_master_cert` (dir delegates to `identity.default_device_dir`) |
| Instance lock | `identity.py` (`InstanceLock`) | cross-process `flock` per synced dir (key = sha256 of realpath) in `HSYNC_LOCKS_DIR` / `$XDG_RUNTIME_DIR/hsync/locks`; kernel-released on death (never stale); second process on the same dir is refused with the holder PID in the message. Acquired for the whole run in `main_logic.run_sync`; same-process re-entry is a no-op (CliRunner tests) |
| mDNS name clash | `lan.py` (`_register_service`) | `NonUniqueNameException` → clear `RuntimeError`: same device identity + same bucket already announced on this host → use a separate `HSYNC_DEVICE_DIR` (different buckets on one host share the identity fine: service name is `{bucket}-{device_id}`) |
| Connection pinning | `ssl_utils.py` | `create_fingerprint_ssl_context`, `FingerprintHTTPAdapter`, `verify_peer_cert_fingerprint` (no TOCTOU) |

HTTPS/WSS is mandatory in LAN mode (no plaintext fallback). Write-capable sync in
non-interactive mode requires a LAN key (`main_logic._check_lan_write_policy`).

> Human guide: `../../docs/lan-mode.md`. Election/security rationale:
> ADRs 0001/0002 + `lan-protocol.md` (see [decisions.md](../decisions.md)).
