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
| `run_lan_mode` | `lan_handler.py:165` | start discovery, elect, branch to role |
| `_run_as_master` | `:300` | start `LANServer` + `LANWebSocketServer` + watcher |
| `_run_as_slave` | `:434` | TOFU-pin master, refresh tuning, then connect + sync |
| `_evaluate_master_pin` | `:377` | apply pin decision before slave connect |
| `_refresh_lan_network_tuning` | `:107` | re-derive autotune-managed keys from real master RTT (slave, post-discovery) |
| `NO_MASTER_WARNING_TIMEOUT` | `:50` | warn when no master appears |

## Discovery & election (`lan.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `LANSync` | `lan.py:654` | top-level LAN coordinator |
| `PeerManager` | `:129` | tracks peers, runs election, heartbeats |
| `Peer` | `:90` | a discovered peer (cid, endpoint, cert_fp, sync_direction) |
| `HsyncServiceListener` | `:501` | Zeroconf callbacks (add/update/remove) |
| `get_free_port` | `:68` | bind an ephemeral serving port |
| `HSYNC_SERVICE_TYPE` | `:47` | `_hsync._tcp.local.` mDNS type |

Election is delegated to the common SSOT: `PeerManager._elect_master` builds
`ElectionCandidate`s and calls `hsync_common.protocol.lan.elect_master`
(bidirectional peers preferred; `--force-master`/`--never-master` honored). TXT
records advertise `protocol`, `cert_fp`, `sync_direction`; protocol-mismatched
peers are ignored.

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
| `LANServer` | `lan_server.py:1048` | HTTPS file server (REST mirror of hsync-server) |
| `BoundedThreadingHTTPServer` | `:66` | `ThreadingHTTPServer` subclass with a `BoundedSemaphore` cap on concurrent request threads (H5, default `LAN_SERVER_MAX_THREADS=32`) |
| `LANRequestHandler` | `:289` | per-request handler; endpoints `API_PREFIX` (`:55`); `POST /dirs` creates directories from slaves (`_handle_create_dir`) |
| `_StreamingMultipartParser` | `:125` | streaming upload parsing |
| `_CappedBytesWriter` | `:97` | abort uploads whose bytes exceed the effective cap — min(declared size, `MAX_INBOUND_SIZE`) — (decompression-bomb containment) |
| `LANWebSocketServer` | `lan_websocket.py:123` | WSS event hub; LAN-key auth (`hmac.compare_digest`) with CID validation (`is_valid_cid`); when no `LAN_KEY` is set, a lightweight `hello` handshake lets peers declare their CID for logging (qualified as `unauthenticated`) — CID format is validated in both paths |
| `LANMasterFileEventHandler` | `:363` | master-local FS changes → broadcast; logs every event with the originator CID (via `peek_inbound_origin`, which does NOT consume the entry) |

Peer-supplied paths on download/delete/rename/upload are validated with
`validate_remote_relpath` and confined with `resolve_within` (no `startswith`
prefix checks — sibling directories cannot bypass confinement). The `/files`
listing and `/files/download` handlers additionally skip/refuse file-symlinks
whose target escapes `LOCAL_BUCKET` (`is_safe_symlink_target`): a slave must
never be able to dereference `secret -> /etc/passwd` through the master and
learn its hash (listing) or content (download). In-bucket symlinks keep their
current semantic (listed/downloaded as the link entry). Uploads require
a matching `sha256sum` form field (format-validated with `is_sha256_hex`,
verified with `hmac.compare_digest` before the atomic rename), clamp
far-future mtimes (`MTIME_MAX_FUTURE_SKEW_SECONDS`), and cap the bytes landing
on disk at `min(declared size, MAX_INBOUND_SIZE)` — the declared size alone is
not a safe bound against a hostile peer, and uploads with no declared size are
capped at `MAX_INBOUND_SIZE` outright. The `.part` temp file is opened with
`O_NOFOLLOW` (`utils.open_partial_no_follow`), so a raced symlink cannot
redirect the streamed write. Multipart form fields (64 KiB) and part-header
lines (8 KiB) are memory-capped; only the file part streams. Tombstone gossip
POST bodies are size-capped (`MAX_TOMBSTONE_PAYLOAD_BYTES`). The
echo-suppression `cid` on upload/delete/rename is honoured only when it passes
`is_valid_cid` *and* the request is authenticated (`LAN_KEY` set).

## Slave client (`lan_websocket.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `lan_websocket_client` | `lan_websocket.py:468` | connect to master WSS, verify cert fp, auth (includes CID), sync |
| `_handle_lan_event` | `:636` | apply inbound master events locally |
| `LANSlaveFileEventHandler` | `:667` | slave-local FS changes → upload to master |
| `_lan_periodic_resync` | `:616` | periodic full reconcile (§13) |
| echo suppression | `:85`/`:98` | `mark_inbound_origin` / `consume_inbound_origin` (TTL `:82`) |
| rename redundancy (inbound + outbound) | `LANSlaveFileEventHandler.record_inbound_rename` / `is_inbound_rename_echo` + `_record_outbound_dir_rename` / `is_outbound_dir_descendant` | exact `(src,dst)` pair cache (TTL `:703`); also matches **descendant** echoes of a directory rename in both directions: inbound (master renamed `ABC -> ABC-123`, slave's watcher echoes children) and outbound (slave renames `ABC-123 -> ABC-123-X`, its watcher emits child `on_moved` events). A single PATCH already renamed the whole tree on the master, so `on_moved` skips the redundant child PATCHes proactively and a 404 from the master is degraded to DEBUG when it matches, else stays WARNING (real divergence). Identifier-suffix guard (starts with `/`) avoids suppressing genuine renames that merely share a path prefix |
| inbound-delete echo | `event_dispatch._apply_delete` (dir `:293` / file `:312`) | a `deleted` event for a path already absent locally is a benign echo (originator removed it / peer delete converged / never existed here): logged at DEBUG, not WARNING. Echo suppression can miss when the master has no `LAN_KEY` (the `mark_inbound_origin` cid gate is a security measure) or when `rmtree` removes a directory's children without individually marking them |
| create-upload deferral | `LANSlaveFileEventHandler._schedule_create_upload` / `_cancel_create_upload` / `cancel_all_create_timers` | an `on_created` upload for a non-empty regular file is deferred (`CREATE_UPLOAD_DEFER_S` = `STABILITY_DELAY_ON_CLOSED`) so `on_closed` (`IN_CLOSE_WRITE`, the reliable fd-closed trigger) can cancel it and take precedence with the fast stability re-check; the timer is the fallback for files that arrive already complete with no writer fd (atomic rename / hard-link / `mv` in). `on_deleted` / `on_moved(src)` / shutdown cancel pending timers so stale uploads never fire. Eliminates the duplicate upload-intent/`[UPLOAD]` pair (the intent line was later removed entirely — ADR 0018) and avoids uploading half-written files. |
| sentinel never uploaded | `LANSlaveFileEventHandler._handle_file_upload` | a `.hsynckeep` basename is skipped before building the `File` and starting the upload. The sentinel is a per-peer local artifact: the periodic reconcile already excludes it from sync actions in both directions (`sync.py`), each peer maintains its own (`ensure_hsynckeep_files` / `_ensure_sentinel*`), and the directory itself is propagated by the directory event — uploading it only produced a 0-byte `[UPLOAD]` line and a broadcast round-trip. This is the single chokepoint covering every slave upload path (`on_created` deferred, `on_closed`, symlink, hidden→visible). |
| escaping symlink refused (slave upload) | `LANSlaveFileEventHandler.on_created` (symlink branch) | a file-symlink inside the bucket whose target escapes `LOCAL_BUCKET` is skipped (logged `[SECURITY]`) instead of being dereferenced and exfiltrated. In-bucket symlinks keep their current semantic (uploaded as the link entry). Belt-and-braces: `transfer.upload_file` applies the same guard to every upload path (`_upload_to_master` → `upload_file`), and the shared watcher (`file_event_handler.BaseFileEventHandler.on_created`) refuses to broadcast a `symlink` event whose target escapes the bucket. |

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
