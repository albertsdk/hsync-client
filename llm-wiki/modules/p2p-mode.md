# HSYNC Client — p2p (P2P) mode

Server-less mode: peers discover each other on the LAN (mDNS), **elect a master**,
and the master serves files over HTTPS/WSS while slaves sync to it. The master
is a **transport coordinator**, not an authority: every node applies the same
causal vector-clock arbitration (ADR-0022). Security and election rules are
SSOT in `hsync-common`.

> **Domain-critical**: before changing discovery/election/security, read
> [`hsync-common/docs/p2p-protocol.md`](https://github.com/albertsdk/hsync-common/blob/main/docs/p2p-protocol.md).

## Entry & roles (`p2p_handler.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `run_p2p_mode` | `p2p_handler.py:109` | start discovery, elect, branch to role; kicks off the background SNTP clock-offset probe (`timesync.start_ntp_clock_offset_probe`) so discovery never waits on an external time source |
| `_run_as_master` | `:273` | start `P2PServer` + `P2PWebSocketServer` + watcher; **readiness threshold**: after both servers are up and `update_server_ready()` ran, it logs `[READY] Master ready — serving …` (`:346`). On `WatcherStartError` (the watcher could not start even on the polling fallback, see [modules/sync-core.md](sync-core.md) "Watcher lifecycle") it tears the half-started WSS server down, does NOT advertise `server_ready`, and backs off `WATCHER_START_RETRY_DELAY_S` before the discovery loop retries |
| `_run_as_slave` | `:418` | TOFU-pin master, refresh tuning, then connect + sync; on `WatcherStartError` it backs off `WATCHER_START_RETRY_DELAY_S` before re-entering the discovery loop (a kernel limit never clears at the normal cadence) |
| `_evaluate_master_pin` | `:377` | apply pin decision before slave connect |
| `_refresh_p2p_network_tuning` | `:107` | re-derive autotune-managed keys from real master RTT (slave, post-discovery) |
| `NO_MASTER_WARNING_TIMEOUT` | `:50` | warn when no master appears |

## Discovery & election (`p2p_peers.py`, `p2p_discovery*.py`)

The former single `p2p_peers.py`/`p2p_discovery_mdns.py` is split by responsibility so peer state can be fed
by **two** discovery plans (mDNS and bootnode), with the sync core unaware of
which one is active:

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `DiscoveryPlan` | `p2p_discovery.py:36` | minimal interface both plans implement (`start`/`stop`/`wait_for_discovery`, `update_endpoint_ready`/`update_server_ready`/`update_server_not_ready`, `set_connected_clients*`, `port`, `peer_manager`) |
| `PeerManager` | `p2p_peers.py:123` | tracks peers, runs election, liveness (shared) |
| `Peer` | `p2p_peers.py:68` | a discovered peer (cid, endpoint, cert_fp, sync_direction) |
| `PeerSnapshot` | `p2p_peers.py:111` | immutable projection for the handler |
| `get_free_port` / `HSYNC_P2P_LISTEN_PORT` | `p2p_peers.py:51` / `:48` | bind an ephemeral serving port |
| `MdnsDiscovery` | `p2p_discovery_mdns.py:271` | mDNS/Zeroconf discovery plan (browse + advertise) |
| `HsyncServiceListener` | `p2p_discovery_mdns.py:79` | Zeroconf callbacks (add/update/remove; parses the ``connected`` TXT value) |
| `HSYNC_SERVICE_TYPE` | `p2p_discovery_mdns.py:51` | `_hsync._tcp.local.` mDNS type |

Election is delegated to the common SSOT: `PeerManager._elect_master` builds
`ElectionCandidate`s and calls `hsync_common.protocol.p2p.elect_master`
(bidirectional peers preferred; `--force-master`/`--never-master` honored). TXT
records advertise `protocol`, `cert_fp`, `sync_direction`, and `peer_download`.
`peer_download` means the node implements the contract; whether its HTTPS seeder
is reachable *now* is not a discovery fact at all — it is carried by the
authenticated WSS session and its capacity advertisement (ADR-0030).
Protocol-mismatched peers are ignored.

`MdnsDiscovery.wait_for_discovery` is the single owner of the display-ready presence
pair (`hsync_client/presence.py`): `total = 1 + alive mDNS peers`, `connected =
1 + WS clients connected` (as master, via the WS-server provider wired by
`p2p_handler.run_p2p_mode`; as slave, from the master's advertised
`connected` TXT value). The pair is emitted as the `peers` UI event (schema v2)
on change, and the operator-facing status log uses three states: `Discovered N
peer(s)`, `No other peers discovered - N client(s) connected`, or `No other
peers discovered - this client is alone`. The master re-registers its mDNS
record whenever the WS count changes (`MdnsDiscovery.set_connected_clients`), so
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
`DISCOVERY_TIMEOUT` window (default `0` = wait indefinitely, matching the
server-mode default of retrying an unreachable server forever). With no
change the wait still blocks for the full window, so the polling cadence is
unchanged in steady state.

### Failover & staleness sweep

Election is also re-run by a **periodic staleness sweep**, so a hard-crashed
master (no mDNS goodbye, no further announce) is replaced even in a two-node
group where no further peer event would otherwise arrive:

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `Peer.is_alive(timeout)` | `p2p_peers.py:90` | mDNS-staleness liveness check against a configurable threshold |
| `PeerManager._is_peer_alive` | `p2p_peers.py:461` | liveness = mDNS staleness **or** WSS-connected (`_connected_cids`) |
| `PeerManager.mark_peer_connected` / `mark_peer_disconnected` | `p2p_peers.py:439` / `:451` | feed the WebSocket-authoritative liveness signal (set by the master's WSS admission/release and by the slave around its WSS connection) |
| `PeerManager.is_wss_connected` | `p2p_peers.py:456` | lock-guarded query on `_connected_cids`; used by the listener's goodbye grace |
| `HsyncServiceListener.remove_service` | `p2p_discovery_mdns.py:250` | **goodbye grace** (`GOODBYE_GRACE_S`): delay `remove_peer` so an Android heartbeat re-register can cancel it (covers slave<->slave, not only WSS); a live WSS link is still an immediate ignore |
| `PeerManager.reconcile_stale_peers` | `p2p_peers.py:411` | expire peers whose liveness stopped and re-elect; invoked every discovery wake (`p2p_handler.run_p2p_mode`) and on a slave WebSocket drop (`_run_as_slave`) |
| `_validate_peer_timeout_relationship` | `config.py:224` | enforces `HSYNC_PEER_TIMEOUT ≥ 2× HSYNC_HEARTBEAT_INTERVAL` |
| timeout wiring | `p2p_handler.py:133` → `MdnsDiscovery` → `PeerManager` | `HSYNC_PEER_TIMEOUT`/`HSYNC_HEARTBEAT_INTERVAL` reach `is_alive`/the discovery cadence |
| `MdnsDiscovery._record_lock` | `p2p_discovery_mdns.py:379` | serializes every record publication (initial register, heartbeat re-announce, in-place refresh, teardown) across the heartbeat thread, the debounced presence timer and the main thread; `stop()` publishes `_running = False` under it so a queued refresh bails instead of racing the unregister |
| `MdnsDiscovery._refresh_service` | `p2p_discovery_mdns.py:507` | **in-place** TXT update (`zeroconf.update_service`), never unregister+register: a goodbye would make slaves see a spurious master `remove_service` |
| `MdnsDiscovery._heartbeat` / `_heartbeat_loop` | `p2p_discovery_mdns.py:589` / `:610` | liveness heartbeat: a background thread re-announces the TXT record every `heartbeat_interval` (env `HSYNC_HEARTBEAT_INTERVAL`, wired in `p2p_handler.py:158`) with an incremented opaque `heartbeat` counter |
| `MdnsDiscovery.update_server_not_ready` | `p2p_discovery_mdns.py:484` | leadership lease (ADR-0027): on yield, clear the local `server_ready` flag and update mDNS in place without `server_ready`/`connected` |
| yield path | `p2p_handler.py:217` | when `is_master()` flips false after serving, stop servers **and** revoke incumbency + log `[MASTER YIELD]` (mirrored in Android `LogAction.MASTER_YIELD`) |

**Liveness is two-layer.** The **WebSocket link is authoritative** for the
master↔slave channels: the master marks each admitted slave CID as connected
(`P2PWebSocketServer.set_peer_connection_callback`, wired in
`p2p_handler._run_as_master` **before** the WSS server starts accepting
connections so no admission is missed), and a slave marks the master connected
for the whole `p2p_websocket_client` lifetime (`_run_as_slave`). Disconnect
notifications are ownership-aware: the `SlaveCidRegistry.release` return value
gates the "disconnected" emission so a same-address reconnect never clears a
still-live CID's mark, and `P2PWebSocketServer.stop()` emits disconnects for
every admitted CID before clearing the registry. For the **slave↔slave**
channel (no WSS link) liveness is carried by the **heartbeat**: zeroconf only
re-delivers a TXT record when its *value* changes, so every peer re-announces
its record each `HEARTBEAT_INTERVAL` with an incremented opaque `heartbeat`
counter. mDNS staleness is therefore the **fallback** that detects a hard
crash: a dead peer stops heartbeating and goes stale within `PEER_TIMEOUT`,
without falsely demoting an idle-but-alive peer (the split-brain regression).
On the receiving side, `remove_service` applies the **goodbye grace**
(`GOODBYE_GRACE_S`): Android NSD heartbeats re-announce as unregister+register,
so every interval broadcasts a goodbye even for live slave<->slave peers.
The removal is delayed so a following add/update can cancel it; a live WSS
link is still ignored immediately. A true leave expires after the grace
window (or via the staleness sweep on a hard crash).

A slave whose WebSocket drops clears the mark, re-runs the sweep immediately but
only demotes a master whose record is already stale: a transient local-link blip
leaves the incumbent in place, so the surviving peers do not split-brain. A
master that *loses* the election yields immediately and revokes its incumbency
so it is not kept as a "zombie incumbent" (`server_ready=true` while no longer
serving).

## Master server (`p2p_server.py`, `p2p_websocket.py`)

The p2p master mirrors the central server's causal upload arbitration via
`hsync_client/arbitration.py` (parallel to `hsync_server.arbitration`, both
built on `hsync_common` primitives + the local sync-state store). The
`_handle_upload` endpoint arbitrate-then-commit: a stale upload is rejected
(`409 superseded`), a tie-break loser is preserved as a conflicted copy
(`409 conflict_lost`), and an accepted upload records the merged vclock
(convergence invariant, ADR-0022 §5). The `/files` listing and broadcast events attach the
effective causal version to each `FileDict` so slaves run the same causal
matrix on the wire data.

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `P2PServer` | `p2p_server.py:1627` | HTTPS file server (REST mirror of hsync-server) |
| `P2PServerMode` | `p2p_server.py` | explicit `master` or `seeder` route surface. Seeder mode allows health/time and exact-hash `GET /files/download` only; all mutating or unrelated routes are `405` |
| `P2PInventoryStore` | `p2p_inventory.py` | master-owned SQLite availability index keyed by `(bucket, normalized path, lowercase SHA-256, CID)` with per-session tokens; stale disconnect cleanup cannot delete a newer reconnect session |
| `P2PSourceService` | `p2p_sources.py` | validates count/byte-bounded wantlists, joins inventory holders with live/seed-ready peer snapshots, excludes requester/self by CID (distinct peers may share an IP), shuffles otherwise-equal candidates, and emits strict source responses. Each candidate carries the seeder's advertised `serve_slots`; a CID without a live capacity advertisement is not a candidate at all (ADR-0030) |
| `/files/sources` | `p2p_server.py` | authenticated-if-key, read-only bounded POST. Wrong buckets are `404`; no-key default-deny mutation policy does not block this read operation |
| `BoundedThreadingHTTPServer` | `:118` | `ThreadingHTTPServer` subclass with a `BoundedSemaphore` cap (H5, default `P2P_SERVER_MAX_THREADS=64`). With HTTP/1.1 keep-alive each **connection** — idle ones included — holds its thread+slot for its whole lifetime, so the cap is sized for the peers' connection pools, not just in-flight requests; idle connections are reaped after `P2P_HANDLER_IDLE_TIMEOUT_S` (300 s — above the worst-case inter-chunk silence of a maximally throttled 256 KiB upload at the 1 KB/s CLI floor, ~262 s, and the client HTTP timeout); `handle_error` logs routine peer connection resets/timeouts at DEBUG |
| `P2PRequestHandler` | `:215` | per-request handler; **HTTP/1.1 keep-alive** (`protocol_version`, every response framed with an exact `Content-Length` — `_send_json_response` always sets it — so connections persist and no TCP+TLS handshake is paid per request) with **TCP_NODELAY** on every connection (`disable_nagle_algorithm`: without it Nagle parks the small response body behind the peer's delayed ACK — a ~40 ms stall per response on persistent connections, benchmarked at 24 vs 5500 req/s by `scripts/benchmark_p2p_transfers.py`); endpoints `API_PREFIX`; `POST /dirs` creates directories from slaves (`_handle_create_dir`); `POST /symlinks` creates links STRUCTURALLY from slaves (`_handle_symlink` — mirror of the central server; an existing file/link is replaced, the target is validated with `is_safe_symlink_target`). **Idempotent**: a link already present with the SAME target is a converged echo and returns 200 without touching the filesystem (unlink+recreate on every echo was what kept the SYMLINK/DELETE oscillation alive: each mutation emitted a `deleted`+`created` pair, the single inbound-origin mark was consumed by the first, and the second was rebroadcast with the master CID) |
| `_handle_patch_permissions` | `p2p_server.py` (PATCH route) | metadata-only chmod from a slave: `PATCH /files/permissions?filename&permissions&cid` applies the sanitized mode to an existing file (owner rwx floor via `set_permissions_on_dir` for dirs; no file bytes cross the wire), attributes the resulting IN_ATTRIB to the slave (`mark_inbound_origin`) so the master's watcher re-broadcasts the change to the other slaves. Mirrors the central server's `PATCH …/files/permissions` |
| `_handle_list_dirs` | `p2p_server.py` (GET route) | `GET /dirs` returns `[{"path", "permissions"}]` for every non-hidden, non-symlink directory — mirrors the central server so the client's download-only directory-permission converge (`converge_directory_permissions`) works identically in both modes |
| `_create_dir_on_master` (permissions param) | `p2p_websocket.py:1364` | `POST /dirs` gains an optional `permissions` query param: the slave's directory-chmod propagate (`_propagate_dir_permission_change`, `_last_dir_mode` dedup map) reuses the endpoint to push the new mode; the master applies it and re-broadcasts |
| `_handle_create_dir` parent marking | `p2p_server.py:532` | `mkdir(parents=True)` also creates every missing ancestor of the requested path, and the master's recursive watcher fires a create event for each. `_handle_create_dir` marks **every missing ancestor inside the bucket** (plus the leaf, only when it does not already exist) with `mark_inbound_origin` before the mkdir, so those synthetic create events carry the slave's CID: without it they would be logged/broadcast as local creates and the slave could never echo-suppress them (`[REMOTE CREATE]` lines on the slave side). Ancestors above the bucket root are never candidates |
| `_StreamingMultipartParser` | `multipart.py:68` | streaming upload parsing (extracted to `multipart.py`) |
| `_handle_upload_status` | `p2p_server.py:1067` | `GET /files/upload-status` — reports `.part` size for resume (mirrors central server `get_upload_status`); returns `complete`/`resumable`/`new` + offset |
| `_handle_upload` (offset) | `:1153` | resumable upload: `offset > 0` appends to `.part` in `"ab"` mode (size validated); incomplete uploads return `202` with the new offset; compressed + offset rejected |
| safe download serving | `P2PRequestHandler._handle_download` / `_serve_open_file` | opens beneath the bucket with component-by-component `O_NOFOLLOW`, hashes and streams the same descriptor, checks identity/size during transfer, emits canonical `X-Checksum-SHA256`, and treats `Content-Length` as wire bytes. Zstd is selected only after explicit negotiation and is staged in a bounded temporary file; ranges are always identity. A bounded checksum cache (512 entries, keyed `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)`, invalidated via the same `_same_open_file` identity checks) collapses the 2–3 disk passes per download to one pass + send |
| `_CappedBytesWriter` | `multipart.py:39` | abort uploads whose bytes exceed the effective cap — min(declared size, `MAX_INBOUND_SIZE`) — (decompression-bomb containment) |
| `p2p_delta.py` | `p2p_delta.py` | the four delta endpoints of the embedded master (`GET/POST /files/delta/signatures`, `POST /files/delta/apply`, `GET /files/delta/data`) as thin shells over `hsync_common.delta` (wire shapes, paths and status codes identical to the FastAPI server, pinned by cross-mode fixtures); signature matching runs under its own sub-semaphore (4) so CPU-bound work cannot occupy the global thread cap. Literal payloads are parked in ONE process-scoped temp dir (`hsync-delta-lan-*`, outside `LOCAL_BUCKET` so the sync engine never sees them) and reached only through tokens (TTL 300 s, max 100 live, single use); the dir is removed at interpreter exit by the `atexit` hook `_discard_payload_dir`. **A new process-scoped temp dir must register the same cleanup** — an unregistered one leaks a directory per run, and `test_p2p_delta.py::TestTokenStore::test_process_scoped_temp_dirs_register_their_cleanup` fails as soon as one appears. `POST /files/delta/apply` resolves its target through the shared `_resolve_servable` gate, so an empty/absent `path` is 400 "Missing path parameter" like the other LAN file endpoints (not the old 403) |
| `P2PWebSocketServer` | `p2p_websocket.py:185` | WSS event hub; LAN-key auth (`hmac.compare_digest`) with CID validation (`is_valid_cid`); **`p2p/1` capability exchange (ADR-0024)** — greeting/hello/auth frames carry `protocol_version` + `capabilities` (master advertise, slave reply); a protocol mismatch on the auth path is a hard fail (`reason: protocol_incompatible`); when no `P2P_KEY` is set, the `hello` handshake exchanges capabilities for coherence only (logged `unauthenticated`) — CID format is validated in both paths |
| `P2PMasterFileEventHandler` | `:546` | master-local FS changes → broadcast; logs every event with the originator CID (via `peek_inbound_origin`, which does NOT consume the entry) |

Peer-supplied paths on download/delete/rename/upload are validated with
`validate_remote_relpath` and confined with `resolve_within` (no `startswith`
prefix checks — sibling directories cannot bypass confinement).

The reconcile action `transfer.delete_file_remote` speaks **both transports'
DELETE contracts**: form fields (`filename`, `cid`) against the central server
and query params (`path`, `is_dir`, `cid`) against the p2p master
(`p2p_server._handle_delete` reads only the query string). Sending the wrong
shape made the master answer 400 and left the stale remote copy in place (the
wire-shape half of the resurrection bug); the branch is driven by
`CONFIG["P2P_MODE"]` and covered by `tests/test_transfer_delete_remote.py`.
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
`is_valid_cid` *and* the request is authenticated (`P2P_KEY` set).

## Slave client (`p2p_websocket.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `p2p_websocket_client` | `p2p_websocket.py:736` | connect to master WSS, verify cert fp, **`p2p/1` handshake**: hard-fail on greeting protocol mismatch, auth/hello frames carry `protocol_version` + `capabilities`; `local_capabilities()` is announced, `adopt_remote_capabilities()` applies the auth reply as the authoritative capability source (compression/delta gates). **Readiness threshold**: the local watcher starts before the WS connect, but `[READY]` (`:875`) fires only after the handshake completed — a failed auth never reaches it. **Inbound file downloads are fanned out** (`event_dispatch._spawn_file_download`): the receive loop never awaits a download — events for the same path are chained (order preserved, plus `path_lock` inside `download_file`), distinct paths run concurrently under a semaphore sized by `MAX_CONCURRENT_TRANSFERS`; the bounded tasks are drained (`drain_file_downloads`) in the connection teardown before the discovery loop's full sync. Without the fan-out the serial `await update_file` per event capped a burst at ~60 files/s while the master serves 200+ req/s |
| `P2PInventoryPublisher` | `p2p_inventory.py` | continuous-slave WSS publisher: immutable snapshot + bounded pending journal, chunked snapshot/delta frames, one in-flight generation, ACK timeout/retry, and resnapshot fallback on overflow/reject/reconnect. **ACK timeouts never close the WSS**: publishing is disabled for the connection (the master-side session expires via its staging TTL) while the event channel on the same socket keeps flowing — closing it used to tear the channel down and force a full resync on both peers under burst backpressure |
| slave seeder lifecycle | `p2p_handler.py` | starts only for continuous non-dry-run slaves after the read-only HTTPS port binds; advertises its endpoint readiness (`https`/`cert_fp`) and its `serve_slots` capacity on the master WSS handshake only while its seeder is bound; stops accepting, withdraws that readiness, and drops role-owned publisher/resolver state before becoming master or exiting. Bind/readiness failure degrades to requester-only |
| `P2PSourceResolver` / `P2PSourceTelemetry` | `p2p_sources.py` | count- and byte-bounded master wantlists plus one fresh single-file lookup for realtime events (measured on the LAN burst: the whole peer-offload block costs ~4.3 ms CPU/file and ~47 % throughput when disabled — see `hsync-client/docs/internal/testing.md`. The per-file lookup is one POST, ~1.0-1.1 ms, because `candidates_for(refresh=True)` calls `resolve([file])` even though `resolve` batches up to `P2P_SOURCES_MAX_FILES` identities; micro-batching those lookups was implemented and measured at 3 ms collection window and did **not** produce a resolvable gain at the default 5-way concurrency, so it was reverted); immutable candidate maps are passed explicitly. Invalid/auth/pin-rotated CIDs stay disabled for the role session, while transient failures use deterministic cooldown. **Admission control (ADR-0030)**: `acquire_slot`/`release_slot` bound in-flight reads per CID to the candidate's advertised `serve_slots`, waiting up to `P2P_PEER_SLOT_WAIT_S` (2 s) before selecting the master — an expired wait is never a refused attempt. **Ranking (ADR-0030)**: `rank(sources, size=…)` orders by failures, then by `_SourceMetrics.predicted_cost_s` = `rtt_ema + size/throughput_ema` — a predicted transfer time, not a raw rate, so a peer that served a large file cannot outrank a lower-latency one for a small file (with no size the setup term decides). A `busy` (503 + `Retry-After`) is a scheduling signal: it defers the source for exactly that interval (`BUSY_DEFAULT_BACKOFF_S` fallback) without incrementing its failure count or dropping it from ranking; candidate URLs are bucket-bound before TOFU and pinned HTTPS uses fingerprint-only verification |
| `_handle_p2p_event` | `p2p_websocket.py:985` | apply inbound master events locally; the `p2p_websocket_client` loop builds **one `EventDispatchContext` per connection** (`_build_p2p_dispatch_ctx`, `p2p_websocket.py:963`) and passes it to every call so the inbound directory dedup cache survives across events (a per-event context would reset the cache and re-log every duplicate `added`/`modified` directory burst as a fresh `[REMOTE CREATE]`/`[REMOTE MODIFY]`) |
| `P2PSlaveFileEventHandler` | `:1082` | slave-local FS changes → upload to master. Realtime operations (uploads, inbound event downloads, deletes, renames) run on a process-wide bounded pool (`_get_realtime_executor:109`, `REALTIME_MAX_WORKERS=8`; lazy singleton from the shared `hsync_client.pools.get_singleton_executor`) instead of one thread per event: long-lived workers reuse their keep-alive HTTP sessions to the master (no TCP+TLS handshake per file) and the parallelism stays predictable; `_start_operation` tracks `Future`s so `wait_pending` can still wait them out gracefully |
| live deletion marker (slave) | `P2PSlaveFileEventHandler.on_deleted` (`:1771`) / `on_moved` visible→hidden | a slave-side file deletion records a **causal tombstone locally** via the shared `_record_deletion_marker` (`p2p_websocket.py:520`) before the best-effort HTTP DELETE reaches the master. Correctness never depends on the transport being up: the marker inherits the merge-base hash + clock (ticked by this peer) and drops the merge base, so the next reconciliation deletes the stale remote copy (`delete_remote`) instead of resurrecting it — the master-unreachable-deletion regression |
| `_p2p_periodic_resync` | `:1004` | thin wrapper around `sync.periodic_resync(p2p_mode=True)` |
| echo suppression | `:113`/`:137` | `mark_inbound_origin` / `consume_inbound_origin` (TTL `:110`) |
| rename redundancy (inbound + outbound) | `P2PSlaveFileEventHandler.record_inbound_rename` / `is_inbound_rename_echo` + `_record_outbound_dir_rename` / `is_outbound_dir_descendant` | exact `(src,dst)` pair cache (TTL `:703`); also matches **descendant** echoes of a directory rename in both directions: inbound (master renamed `ABC -> ABC-123`, slave's watcher echoes children) and outbound (slave renames `ABC-123 -> ABC-123-X`, its watcher emits child `on_moved` events). A single PATCH already renamed the whole tree on the master, so `on_moved` skips the redundant child PATCHes proactively and a 404 from the master is degraded to DEBUG when it matches, else stays WARNING (real divergence). Identifier-suffix guard (starts with `/`) avoids suppressing genuine renames that merely share a path prefix |
| inbound-delete echo | `event_dispatch._apply_delete` (dir `:457` / file `:485`) | a `deleted` event for a path already absent locally is a benign echo (originator removed it / peer delete converged / never existed here): logged at DEBUG, not WARNING. Echo suppression can miss when the master has no `P2P_KEY` (the `mark_inbound_origin` cid gate is a security measure) or when `rmtree` removes a directory's children without individually marking them |
| recursive inbound delete echo | `event_dispatch._apply_delete` dir branch + `P2PSlaveFileEventHandler.record_inbound_delete` / `is_inbound_delete_echo` | before `rmtree`, the confined absolute root path is passed to `EventDispatchContext.record_inbound_delete` (wired in `_handle_p2p_event`). The slave caches the root with a 10 s TTL and its `on_deleted` drops any later delete equal to or under that root (same `/`-suffix guard as the rename echo): those are its own watcher echoes of the rmtree it just applied, not new local deletions — without the cache each would be re-sent to the master where it 404s as a benign echo. Remote mode never installs the callback |
| create-upload deferral | `P2PSlaveFileEventHandler._schedule_create_upload` / `_cancel_create_upload` / `cancel_all_create_timers` | an `on_created` upload for a non-empty regular file is deferred (`CREATE_UPLOAD_DEFER_S` = `STABILITY_DELAY_ON_CLOSED`) so `on_closed` (`IN_CLOSE_WRITE`, the reliable fd-closed trigger) can cancel it and take precedence with the fast stability re-check; the timer is the fallback for files that arrive already complete with no writer fd (atomic rename / hard-link / `mv` in). `on_deleted` / `on_moved(src)` / shutdown cancel pending timers so stale uploads never fire. Eliminates the duplicate upload-intent/`[UPLOAD]` pair (the intent line was later removed entirely — ADR 0018) and avoids uploading half-written files. |
| sentinel never uploaded | `P2PSlaveFileEventHandler._handle_file_upload` | a `.hsynckeep` basename is skipped before building the `File` and starting the upload. The sentinel is a per-peer local artifact: the periodic reconcile already excludes it from sync actions in both directions (`sync.py`), each peer maintains its own (`ensure_hsynckeep_files` / `_ensure_sentinel*`), and the directory itself is propagated by the directory event — uploading it only produced a 0-byte `[UPLOAD]` line and a broadcast round-trip. This is the single chokepoint covering every slave upload path (`on_created` deferred, `on_closed`, symlink, hidden→visible). |
| escaping symlink refused (slave upload) | `P2PSlaveFileEventHandler.on_created` (symlink branch) | a file-symlink inside the bucket whose target escapes `LOCAL_BUCKET` is skipped (logged `[SECURITY]`) instead of being dereferenced and exfiltrated. An in-bucket symlink is uploaded STRUCTURALLY via `transfer.upload_symlink` (the pair over `POST /symlinks`; the absolute local target is relativized by `hsync_common.wire_symlink_target` before it reaches the wire) — the content-upload path (`_upload_to_master` → `upload_file`) is never used for links. Belt-and-braces: `transfer.upload_file` refuses any symlink whose target escapes the bucket, and the shared watcher (`file_event_handler.BaseFileEventHandler.on_created`) refuses to broadcast a `symlink` event whose target escapes. |

## Local capabilities (`capabilities.py`)

| Symbol | Purpose |
| ------ | ------- |
| `local_capabilities` | runtime-derived capability set advertised on every handshake (always `vector_clocks` + `tombstones`; `compression`/`delta` when enabled; `peer_download` only when the runtime feature is enabled) — shared by remote and LAN transports |
| `adopt_remote_capabilities` | adopts the authenticated peer's capabilities as the session transfer gates (`SERVER_COMPRESSION_ENABLED`/`SERVER_DELTA_ENABLED`) |

## Security: TOFU pinning + persistent identity

| Concern | Code | Notes |
| ------- | ---- | ----- |
| Pin store | `pinning.py` (`PinStore`) | disk-backed `0o600` JSON keyed by `(CID, bucket)`. Every read-modify-write holds a sibling `.lock` file with POSIX `flock` or Windows `msvcrt.locking`, reloads inside the lock, writes a unique same-directory temp, fsyncs file + parent directory, and atomically replaces. Master and each selected seeder use independent persistent TOFU entries; `--require-pairing` applies to the master only |
| Pin decision | common `protocol/pairing.py` | `evaluate_pin`; refuse on changed fingerprint, emit `fingerprint_changed`/`pairing_needed` |
| Device identity | `identity.py` (`load_or_create_device_identity`) | Syncthing-style: one identity per installation — stable device ID (`device_id` file, CID_PATTERN-valid) + keypair in `HSYNC_DEVICE_DIR` (`default_device_dir`); cached process-wide. The device ID **is** the LAN `cid` (resolved in `main_logic.run_sync`), so pins survive restarts and cert rotation (delete `*.pem`, keep `device_id`) is detected as `[FINGERPRINT CHANGED]`. Keypair itself: `ssl_utils.load_or_create_master_cert` (dir delegates to `identity.default_device_dir`) |
| Instance lock | `identity.py` (`InstanceLock`) | cross-process `flock` per synced dir (key = sha256 of realpath) in `HSYNC_LOCKS_DIR` / `$XDG_RUNTIME_DIR/hsync/locks`; kernel-released on death (never stale); second process on the same dir is refused with the holder PID in the message. Acquired for the whole run in `main_logic.run_sync`; same-process re-entry is a no-op (CliRunner tests) |
| mDNS name clash | `p2p_peers.py`/`p2p_discovery_mdns.py` (`_register_service`) | `NonUniqueNameException` → clear `RuntimeError`: same device identity + same bucket already announced on this host → use a separate `HSYNC_DEVICE_DIR` (different buckets on one host share the identity fine: service name is `{bucket}-{device_id}`) |
| Duplicate identity detection | `p2p_peers.py`/`p2p_discovery_mdns.py` (`PeerManager.add_peer` `_is_foreign_duplicate`/`_report_duplicate_identity` `p2p_peers.py:356`/`:378`) + `p2p_websocket.py` (`P2PWebSocketServer._admit_slave` `p2p_websocket.py:411` via `SlaveCidRegistry` `p2p_websocket.py:223`) + `p2p_server.py` (`P2PRequestHandler._reject_duplicate_cid` `p2p_server.py:449`) | A foreign host presenting our own CID (a cloned `HSYNC_DEVICE_DIR`) is told apart from a legitimate self-discovery via the advertised address + cert fingerprint and refused with a `[SECURITY]` error (dedup'd per signature, so mDNS heartbeats don't spam). The master's `SlaveCidRegistry` reserves every admitted slave CID against its source IP atomically (no check→reserve race), refuses conflicts on the WSS path (`auth_failed("cid_conflict")`, same-address reconnects replace their own binding) and on every authenticated HTTP write (`409 Duplicate device identity`) — a clone cannot corrupt the causal vector-clock/tombstone authorship on any channel. The slave surfaces the `cid_conflict` reason as a clear `[SECURITY]` error. Every detection point emits the `duplicate_identity` UI event (`hsync_common.protocol.events.UiEventName`), surfaced as a Desktop notification |

On the unauthenticated (no `P2P_KEY`) WSS `hello` path the declared CID is not
authenticated, so a hostile peer that observes mDNS could squat another
slave's CID and get the legit slave refused while it holds the connection.
No-key mode is read-only by default: discovery, WSS availability publication,
list/download/source lookup, and download-delta reads remain available, while
write-capable startup and every mutating HTTP route are denied unless the
explicit dangerous opt-in is set. CID squatting can therefore affect
availability/source hints but cannot authorize a filesystem mutation.
| Connection pinning | `ssl_utils.py` | `create_fingerprint_ssl_context`, `FingerprintHTTPAdapter`, `verify_peer_cert_fingerprint` (no TOCTOU) |
| Pinned session trust | `utils.py:get_http_session` | A fingerprint-pinned session sets `trust_env = False`: the pinned fingerprint is the whole trust anchor (TOFU), so `*_proxy`, `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE` and `.netrc` must not add a second one — an environment proxy would route the pinned peer connection through a third party. Also removes `requests`'s per-request environment-proxy resolution (5 calls/file, ~0.85 ms CPU/file on a burst). Remote-mode sessions keep the default (`trust_env = True`) |

HTTPS/WSS is mandatory in p2p mode (no plaintext fallback). Write-capable no-key
sync requires `--allow-unauthenticated-writes` or
`HSYNC_P2P_ALLOW_UNAUTHENTICATED_WRITES=true`; TTY state and `--yes` never
authorize it (`main_logic._check_p2p_write_policy`).

> Human guide: `../../docs/p2p-mode.md`. Election/security rationale:
> ADRs 0001/0002 + `p2p-protocol.md` (see [decisions.md](../decisions.md)).

## Role handover on the shared port (seeder ⇄ master)

The slave seeder and the master bind the **same** listen port (`p2p_sync.port`),
so a role change hands the port over: `P2PServer.stop()` sets `stopping`, releases
the listener and closes the live keep-alive connections
(`BoundedThreadingHTTPServer.close_active_connections`), and
`P2PRequestHandler.parse_request` refuses (503) any request arriving on a
`stopping` server. Without it, a pooled connection from the seeder era kept
being served by the seeder handler after promotion. See `decisions.md`
("retired server role") and the `TestRoleHandover` unit/BB regressions.

## Bootnode discovery plan (`boot_client.py`, `stun.py` — C2)

Cross-network discovery uses a `boot/1` bootstrap node (SSOT: `hsync-bootnode`).
The wire contract is mirrored in `hsync_common.protocol.bootnode` (frames +
strict validators); the client side is split so transport details stay out of
peer state:

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `BootnodeClient` | `boot_client.py:78` | synchronous `boot/1` client: one QUIC/TLS 1.3 connection (ALPN `hsync-boot/1`), one bidi stream per request, NDJSON frames; `register`/`refresh`/`leave`/`query` |
| `build_register_frame` / `build_refresh_frame` / `build_leave_frame` / `build_query_frame` | `boot_client.py:195`–`:269` | TypedDict frame builders; every request is re-validated against the mirror before it is sent (fail-fast, no network on an invalid frame) |
| `BootnodeError` / `BootnodeUnreachable` / `BootnodeProtocolError` | `boot_client.py:65`–`:75` | transport failure vs contract violation |
| `observed_address` | `stun.py:35` | server-reflexive public `ip:port` from a validated `ack` (STUN-echo) |

| `BootnodeDiscovery` | `p2p_discovery_bootnode.py:66` | the `DiscoveryPlan` implementation: lease loop (register → refresh), peer sample → `PeerManager`, `wait_for_discovery` |
| `candidate_endpoints` | `nat_traversal.py:43` | ordered, de-duplicated dialable endpoints for the `register` `endpoints[]` (observed → UPnP → local) |
| `UpnpMapper` / `discover_igd` | `upnp.py:275` / `:74` | best-effort TCP port mapping for the master; every failure degrades to "no UPnP" (node stays slave-only) |
| `get_local_ip` | `net_utils.py:27` | shared local-source-IP probe (mDNS advertiser, UPnP mapper, bootnode candidates) |

Readiness & banner: `run_p2p_mode` logs the active plan at startup
(`Discovery: mDNS on the local network` vs `Discovery: bootnode HOST:PORT
(cross-network; UPnP on|off)`). The **bootnode plan only** adds a readiness gate:
the master emits `[READY]` (and writes the ready file) only after
`endpoint_published()` — i.e. after the second `register` that carries the
`endpoints[]`. The mDNS plan returns `True` immediately, so its readiness
timing is unchanged. The wait is bounded by `READINESS_ENDPOINT_TIMEOUT_S`
(10 s) so a wedged lease loop degrades to the old timing instead of hanging.

The same startup also emits the **`discovery` UI event** (`UiEventName.discovery`,
added to `hsync-common/protocol/events.py`) with `plan` (`mdns` | `bootnode`) and,
with a bootnode, its host — so a front-end can surface the active plan instead of
silently degrading to mDNS. The desktop maps it to the `discovery_changed`
signal; the payload stays a flat optional-field object (no events-schema bump).

Lease semantics: the **first** `register` has no endpoint (public address not
known yet) and capacity 0; the `ack` carries the observed endpoint (STUN-echo);
once the handler signals `update_endpoint_ready(True)` the adapter maps the TCP
port (UPnP, if enabled) and sends a **second `register`** publishing
`endpoints[]` + the advisory `capacity` (0 unless peer-download is on). A
`refresh` renews TTL + `connected` only; if the node drops the lease, the loop
falls back to a full `register`. Relayed peers are fed to `PeerManager` only
when `reachable` is true and `proto == "tcp"`; the local CID is never re-added.

`leave` has **no response frame** (spec): the client writes it and closes the
stream without waiting for a reply. A black-box integration test
(`tests/test_bb_p2p_bootnode_client.py`, gated by `HSYNC_BB_P2P=1`, skipped when
the sibling bootnode is not built) spawns the real `hsync-bootnode` on loopback
and drives register → ack/STUN-echo → query relay → refresh → leave over the
public wire.

TLS trust: `--ca-cert` pins the bootnode certificate; `--insecure` disables
verification (tests/loopback only). `DEFAULT_QUIC_IDLE_TIMEOUT = 30.0`.
Config surface: `HSYNC_BOOTNODE` (bare host; `_validate_bootnode_host` rejects
scheme/path/`host:port`), `HSYNC_UPNP_ENABLED`, `HSYNC_P2P_LISTEN_PORT`
(`constants.BOOTNODE_DEFAULT_PORT = 443`).
