# HSYNC Client — p2p (P2P) mode

Server-less mode: peers discover each other — via mDNS on the LAN (default) or
through a `boot/1` bootnode across networks — **elect a master**,
and the master serves files over HTTPS/WSS while slaves sync to it. The master
is a **transport coordinator**, not an authority: every node applies the same
causal vector-clock arbitration (ADR-0022). Security and election rules are
SSOT in `hsync-common`.

> **Domain-critical**: before changing discovery/election/security, read
> [`hsync-common/docs/p2p-protocol.md`](https://github.com/albertsdk/hsync-common/blob/main/docs/p2p-protocol.md).

## Entry & roles (`p2p_handler.py`)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `run_p2p_mode` | `p2p_handler.py::run_p2p_mode` | start discovery, elect, branch to role; kicks off the background SNTP clock-offset probe (`timesync.start_ntp_clock_offset_probe`) so discovery never waits on an external time source |
| `_run_as_master` | `p2p_handler.py::_run_as_master` | start the `P2PServer` (HTTPS REST + WSS on **one** port, with `P2PWebSocketServer` attached as the canonical `/api/v1/buckets/{bucket}/events/{cid}` route) + watcher; **readiness threshold**: after the listener is bound, the watcher is running and `update_server_ready()` ran, it logs `[READY] Master ready — serving …`. It re-checks `peer_manager.is_master()` first and yields instead of advertising readiness when a peer won during the startup window (a loser that advertised `server_ready` would out-rank the winner and re-serve the same bucket — see [decisions.md](../decisions.md)). On `WatcherStartError` (the watcher could not start even on the polling fallback, see [modules/sync-core.md](sync-core.md) "Watcher lifecycle") it does NOT advertise `server_ready` and backs off `WATCHER_START_RETRY_DELAY_S` before the discovery loop retries |
| `_run_as_slave` | `p2p_handler.py::_run_as_slave` | TOFU-pin master, select the reachable data-plane endpoint (`_select_master_endpoint`: a multi-candidate bootnode peer is probed host/LAN-first with a bounded per-candidate timeout, a single-candidate peer keeps its endpoint), refresh tuning, then connect + sync; on `WatcherStartError` it backs off `WATCHER_START_RETRY_DELAY_S` before re-entering the discovery loop (a kernel limit never clears at the normal cadence) |
| `_evaluate_master_pin` | `p2p_handler.py::_evaluate_master_pin` | apply pin decision before slave connect |
| `_refresh_p2p_network_tuning` | `p2p_handler.py::_refresh_p2p_network_tuning` | re-derive autotune-managed keys from real master RTT (slave, post-discovery) |
| `NO_MASTER_WARNING_TIMEOUT` | `p2p_handler.py::NO_MASTER_WARNING_TIMEOUT` | warn when no master appears |

The outer discovery loop also owns the **one-shot window**: with `ONE_SHOT` and
a finite `DISCOVERY_TIMEOUT` it arms `discovery_deadline` (`p2p_handler.py::run_p2p_mode`)
and, once that deadline has passed with no live peer, logs `No master elected -
this client is alone, nothing to sync` and returns `ExitCode.P2P_NO_PEER` (5)
(`p2p_handler.py::run_p2p_mode`). The gate exists because a lone eligible node is already
MASTER at discovery start (see the election section below), so without it the
continuous master role would turn `--once` into a run that never ends. A master
that has live peers — and any continuous run, which has no deadline — keeps the
continuous role; `DISCOVERY_TIMEOUT=0` explicitly waits forever
(`docs/scheduling.md` is the user-facing SSOT for the code table).

## Discovery & election (`p2p_peers.py`, `p2p_discovery*.py`)

The former single `p2p_peers.py`/`p2p_discovery_mdns.py` is split by responsibility so peer state can be fed
by **two** discovery sources (mDNS and bootnode), with the sync core unaware of
which is active. With no bootnode the mDNS plan runs alone; with a bootnode
configured `HybridDiscovery` runs **both** over one shared `PeerManager`:

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `DiscoveryPlan` | `p2p_discovery.py::DiscoveryPlan` | minimal interface every plan implements (`start`/`stop`/`wait_for_discovery`, `update_endpoint_ready`/`update_server_ready`/`update_server_not_ready`, `set_connected_clients*`, `port`, `peer_manager`) |
| `DiscoveryPlanState` | `p2p_discovery.py::DiscoveryPlanState` | shared trivial state and the single `create_peer_manager()` builder (`port`, `peer_manager`, `set_connected_clients_provider`), plus `_init_common_fields()` — the election-advertisement fields both plans set identically |
| `PeerManager` | `p2p_peers.py::PeerManager` | tracks peers, runs election, liveness (shared) |
| `Peer` | `p2p_peers.py::Peer` | a discovered peer (cid, endpoint, cert_fp, sync_direction) |
| `PeerSnapshot` | `p2p_peers.py::PeerSnapshot` | immutable projection for the handler |
| `get_free_port` / `HSYNC_P2P_LISTEN_PORT` | `p2p_peers.py::get_free_port` / `p2p_peers.py::HSYNC_P2P_LISTEN_PORT` | bind an ephemeral serving port |
| `MdnsDiscovery` | `p2p_discovery_mdns.py::MdnsDiscovery` | mDNS/Zeroconf plan (browse + advertise); always active |
| `HybridDiscovery` | `p2p_discovery_hybrid.py::HybridDiscovery` | runs mDNS + bootnode over one shared `PeerManager` when a bootnode is configured |
| `HsyncServiceListener` | `p2p_discovery_mdns.py::HsyncServiceListener` | Zeroconf callbacks (add/update/remove; parses the ``connected`` TXT value) |
| `HSYNC_SERVICE_TYPE` | `p2p_discovery_mdns.py::HSYNC_SERVICE_TYPE` | `_hsync._tcp.local.` mDNS type |

Election is delegated to the common SSOT: `PeerManager._elect_master` builds
`ElectionCandidate`s and calls `hsync_common.protocol.p2p.elect_master`
(bidirectional peers preferred; `--prefer-master`/`--never-master` honored). Each
discovery plan calls `PeerManager.elect()` once at start, so a node with no peers
yet still resolves its role (a lone `--prefer-master` node serves immediately,
which is what lets a joining slave connect) — the election otherwise runs only on
peer add/remove, a local readiness change and the stale sweep. TXT
records advertise `protocol`, `cert_fp`, `sync_direction`, and `peer_download`.
`peer_download` means the node implements the contract; whether its HTTPS seeder
is reachable *now* is not a discovery fact at all — it is carried by the
authenticated WSS session and its capacity advertisement (ADR-0030).

The local node's candidacy can also be dropped at runtime
(`p2p_peers.py::PeerManager.set_master_candidacy`): a client parked in
continuous dry-run, waiting for the desktop's `PREVIEW`/`SYNC` commands, cannot
serve, so it is excluded from the election — a peer that elected it would wait
forever for its `server_ready`. `p2p_handler.py::_run_as_slave` calls
`p2p_discovery.py::DiscoveryPlanState.set_master_eligibility(False)` around the
`_run_stdin_wait` call and restores it when the dry run ends. The withdrawal
must reach the **wire**, not only the local election: the plan re-announces
`never_master` and no `prefer_master`
(`MdnsDiscovery.refresh_election_advertisement` re-registers the TXT,
`BootnodeDiscovery` bumps its advert generation, `HybridDiscovery` delegates to
both), because peers only rank the advertisement they receive.

The automatic dry-run preview first probes the master's data endpoint
(`p2p_handler.py::_master_endpoint_reachable`, a short TCP connect). A master
that just exited can still look "fresh" to `reconcile_stale_peers`, and
previewing against its dead endpoint would burn the whole retry budget and
surface a spurious error. When the probe fails, the slave demotes that peer
(`PeerManager.remove_peer`) and returns to the discovery loop, so the election
picks a reachable node.

Protocol-mismatched peers are ignored.

**Combined mDNS + bootnode (`HybridDiscovery`).** With a bootnode configured
both sources run against one `PeerManager` (built once via
`DiscoveryPlanState.create_peer_manager` and injected into both plans at
`start`). Consequences handled by that shared state:

- **Presence once**: `wait_for_discovery` blocks on the shared condition; the
  mDNS child owns the presence pair (`emit_discovery_status`), the bootnode child
  contributes the discovery log (`log_peer_count`) and the fatal check
  (`check_fatal`, which re-raises a trust failure so the run aborts).
- **Readiness is the AND**: `endpoint_published()` requires the bootnode's
  endpoint register (mDNS has no separate step), bounded by
  `READINESS_ENDPOINT_TIMEOUT_S`.
- **One peer entry per CID**: `add_peer` merges a peer seen by both sources —
  the non-empty candidate list wins (`endpoints`, richer than mDNS's single
  address) and `Peer.capabilities_known` stops the bootnode relay, which carries
  no capability flags, from clearing the TXT capabilities (`compression`,
  `delta`, `peer_download`) that mDNS advertised. Both sources relay the same
  election advertisement, so the election sees one candidate set.

`MdnsDiscovery.wait_for_discovery` is the single owner of the display-ready presence
pair (`hsync_client/presence.py`): `total = 1 + alive mDNS peers`, `connected =
1 + WS clients connected` (as master, via the WS-server provider wired by
`p2p_handler.run_p2p_mode`; as slave, from the master's advertised
`connected` TXT value). The pair is emitted as the `peers` UI event (schema v2)
on change, and the operator-facing status log uses three states: `Discovered N
peer(s)`, `No other peers discovered - N client(s) connected`, or `No other
peers discovered - this client is alone`. The master re-registers its mDNS
record whenever the WS count changes (`MdnsDiscovery.set_connected_clients`), so
slaves observe presence without a server round-trip. The WS-server provider and
the `set_clients_changed` callback are wired by `p2p_handler.run_p2p_mode`
**before** `_run_as_master` starts serving: wiring them only afterwards would
let the master advertise `connected=0` and miss a client that connects while it
is serving, leaving slaves reading a stale `0` (a `1/N` count with both peers
connected). Because the discovery loop that owns the `peers` event is blocked
inside `_run_as_master` while serving, the master re-emits its own pair on every
join/leave through `p2p_handler._emit_master_presence(count, peer_manager)`
(called from the `set_clients_changed` callback): `total = 1 + max(alive peers,
connected clients)`, `connected = min(1 + clients, total)`, so a two-node group
reads `2/2` even before the mDNS peer entry catches up with the WS join.
Symmetrically, the discovery loop is also blocked for the whole WebSocket run
while a slave is connected (`p2p_handler._run_as_slave`), so the slave re-emits
its own pair once the link is live through
`p2p_handler._emit_slave_presence(peer_manager)`: `total = 1 + alive peers`,
`connected = 1 + max(master advertised count, 1)` (a live WSS link proves the
master is reachable). Without it a connected slave kept the `1/N` pair it
emitted before connecting, since the master's TXT count lags the join.

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
| `Peer.is_alive(timeout)` | `p2p_peers.py::Peer.is_alive` | mDNS-staleness liveness check against a configurable threshold |
| `PeerManager._is_peer_alive` | `p2p_peers.py::PeerManager._is_peer_alive` | liveness = mDNS staleness **or** WSS-connected (`_connected_cids`) |
| `PeerManager.mark_peer_connected` / `mark_peer_disconnected` | `p2p_peers.py::PeerManager.mark_peer_connected` / `p2p_peers.py::PeerManager.mark_peer_disconnected` | feed the WebSocket-authoritative liveness signal (set by the master's WSS admission/release and by the slave around its WSS connection) |
| `PeerManager.is_wss_connected` | `p2p_peers.py::PeerManager.is_wss_connected` | lock-guarded query on `_connected_cids`; used by the listener's goodbye grace |
| `HsyncServiceListener.remove_service` | `p2p_discovery_mdns.py::HsyncServiceListener.remove_service` | **goodbye grace** (`GOODBYE_GRACE_S`): delay `remove_peer` so an Android heartbeat re-register can cancel it (covers slave<->slave, not only WSS); a live WSS link is still an immediate ignore |
| `PeerManager.reconcile_stale_peers` | `p2p_peers.py::PeerManager.reconcile_stale_peers` | expire peers whose liveness stopped and re-elect; invoked every discovery wake (`p2p_handler.run_p2p_mode`) and on a slave WebSocket drop (`_run_as_slave`) |
| `_validate_peer_timeout_relationship` | `config.py::_validate_peer_timeout_relationship` | enforces `HSYNC_PEER_TIMEOUT ≥ 2× HSYNC_HEARTBEAT_INTERVAL` |
| timeout wiring | `p2p_handler.py::run_p2p_mode` → `MdnsDiscovery` / `build_discovery_plan` → `PeerManager` | `HSYNC_PEER_TIMEOUT`/`HSYNC_HEARTBEAT_INTERVAL` reach `is_alive`/the discovery cadence; with a bootnode configured `build_discovery_plan` (`p2p_discovery.py`) floors it to `≥ 2 × HSYNC_BOOTNODE_REFRESH_INTERVAL` (default 60 s) so a live relayed peer is not expired between two random samples (`reconcile_stale_peers` would otherwise drop it and churn the election) |
| `MdnsDiscovery._record_lock` | `p2p_discovery_mdns.py::MdnsDiscovery` | serializes every record publication (initial register, heartbeat re-announce, in-place refresh, teardown) across the heartbeat thread, the debounced presence timer and the main thread; `stop()` publishes `_running = False` under it so a queued refresh bails instead of racing the unregister |
| `MdnsDiscovery._refresh_service` | `p2p_discovery_mdns.py::MdnsDiscovery._refresh_service` | **in-place** TXT update (`zeroconf.update_service`), never unregister+register: a goodbye would make slaves see a spurious master `remove_service` |
| `MdnsDiscovery._heartbeat` / `_heartbeat_loop` | `p2p_discovery_mdns.py::MdnsDiscovery._heartbeat_loop` / `p2p_discovery_mdns.py::MdnsDiscovery._heartbeat` | liveness heartbeat: a background thread re-announces the TXT record every `heartbeat_interval` (env `HSYNC_HEARTBEAT_INTERVAL`, wired in `p2p_handler.py`) with an incremented opaque `heartbeat` counter |
| `MdnsDiscovery.update_server_not_ready` | `p2p_discovery_mdns.py::MdnsDiscovery.update_server_not_ready` | leadership lease (ADR-0027): on yield, clear the local `server_ready` flag and update mDNS in place without `server_ready`/`connected` |
| yield path | `p2p_handler.py::run_p2p_mode` | when `is_master()` flips false after serving, stop servers **and** revoke incumbency + log `[MASTER YIELD]` (mirrored in Android `LogAction.MASTER_YIELD`) |

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
`hsync_client/arbitration.py`: the decision is the shared reference
implementation `hsync_common/models/arbitration.py::decide_arbitration`, and the
client module only binds it to the local sync-state store and the master CID
(`hsync_server/arbitration.py` binds the same decision to its per-bucket store).
The
`_handle_upload` endpoint arbitrate-then-commit: a stale upload is rejected
(`409 superseded`), a tie-break loser is preserved as a conflicted copy
(`409 conflict_lost`), and an accepted upload records the merged vclock
(convergence invariant, ADR-0022 §5). The `/files` listing and broadcast events attach the
effective causal version to each `FileDict` so slaves run the same causal
matrix on the wire data.

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `P2PServer` | `p2p_server.py::P2PServer` | HTTPS file server (REST mirror of hsync-server) |
| `P2PServerMode` | `p2p_server.py` | explicit `master` or `seeder` route surface. Seeder mode allows health/time and exact-hash `GET /files/download` only; all mutating or unrelated routes are `405` |
| `P2PInventoryStore` | `p2p_inventory.py` | master-owned SQLite availability index keyed by `(bucket, normalized path, lowercase SHA-256, CID)` with per-session tokens; stale disconnect cleanup cannot delete a newer reconnect session |
| `P2PSourceService` | `p2p_sources.py` | validates count/byte-bounded wantlists, joins inventory holders with live/seed-ready peer snapshots, excludes requester/self by CID (distinct peers may share an IP), shuffles otherwise-equal candidates, and emits strict source responses. Each candidate carries the seeder's advertised `serve_slots`; a CID without a live capacity advertisement is not a candidate at all (ADR-0030) |
| `/files/sources` | `p2p_server.py` | authenticated-if-key, read-only bounded POST. Wrong buckets are `404`; no-key default-deny mutation policy does not block this read operation |
| `P2PServer` (single aiohttp listener) | `p2p_server.py` | ONE TLS listener for the whole data plane. An `aiohttp` app runs the endpoint mixin on a bounded `ThreadPoolExecutor` (`P2P_SERVER_MAX_THREADS=64`) so the blocking data plane (file IO, sha256, zstd) never blocks the event loop; an `asyncio.Semaphore` of the same size caps in-flight requests so a peer flood cannot grow the executor's unbounded queue. `web.SockSite` + `ssl_context` terminate TLS once and expose one `port` (`port=0` → the real bound port, read from `getsockname()`). HTTP/1.1 keep-alive is aiohttp's; idle connections are reaped after `keepalive_timeout` = `P2P_HANDLER_IDLE_TIMEOUT_S` (300 s). DoS guards: per-IP admission is rate-limited (`TokenBucketLimiter` from `hsync_common.ratelimit`, `P2P_REQ_RATE_*`), a peer holds at most `_per_ip_inflight_cap(active_ips)` in-flight requests (fair-share over the 64 slots: floor `P2P_MIN_INFLIGHT_PER_IP`, ceiling `P2P_MAX_INFLIGHT_PER_IP`), and `_AiohttpTransport._check_progress` aborts a **request body** whose sustained read throughput stays below `P2P_REQUEST_MIN_RATE_BPS` after `P2P_REQUEST_GRACE_S` (response writes are excluded so a compute-bound handler is never mistaken for a trickle) — the per-I/O timeout alone resets on any byte, so this is what stops a trickle from pinning a slot. `client_max_size` is bounded by `MAX_INBOUND_SIZE`, the listen `backlog` is `P2P_SERVER_BACKLOG`, and `_P2PSite` bounds the TLS handshake (`P2P_TLS_HANDSHAKE_TIMEOUT_S`). `configure_role(...)` installs the serving role and `stop_serving()` retires it (503 while not serving) — the WS control plane is refused unless `master` |
| `P2PEndpointMixin` / `_AiohttpTransport` | `p2p_server.py` | the transport-agnostic endpoint logic (every `do_*` dispatcher and `_handle_*`, plus `_check_auth`/`_check_mutation_auth`) lives in `P2PEndpointMixin` and depends only on a low-level HTTP surface (`send_response`/`send_header`/`end_headers`/`wfile`/`rfile`/`headers`/`client_address`/`close_connection`). `_AiohttpTransport` supplies that surface on the listener: `end_headers` prepares a `web.StreamResponse`, `wfile.write` streams the body and `rfile.read` pulls the request body, each bridging the worker thread to the loop via `run_coroutine_threadsafe`. Endpoints keep their exact `Content-Length` framing so keep-alive persists. Every response also emits one DEBUG request line (`<addr> - "METHOD /path?query HTTP/1.1" <status> -`): `_AiohttpTransport.send_response` calls the mixin's stdlib-parity `log_request` explicitly, since aiohttp never invokes the `BaseHTTPRequestHandler` hook — this is what keeps the incoming request (and its query string, e.g. the seeder's `filename=…&sha256=…`) observable at `-v` |
| `P2PWebSocketServer` (control plane route) | `p2p_websocket.py` | WSS event hub served **on the single listener** (no own socket/thread): `handle(request)` is the `GET /api/v1/buckets/{bucket}/events/{cid}` route on `P2PServer`, using `web.WebSocketResponse(heartbeat=30, max_msg_size=WS_MAX_MESSAGE_BYTES)`. p2p-key auth (`hmac.compare_digest`) with CID validation (`is_valid_cid`); **`p2p/1` capability exchange (ADR-0024)** — greeting/hello/auth frames carry `protocol_version` + `capabilities`; a protocol mismatch on the auth path is a hard fail (`reason: protocol_incompatible`); when no `P2P_KEY` is set the `hello` handshake exchanges capabilities for coherence only (logged `unauthenticated`); the watcher (`start`) broadcasts through the listener loop and `stop` closes every client and clears the registry. The route is the `hsync_common.protocol.p2p.P2P_WS_EVENTS_PATH` SSOT (the slave builds the URI with `p2p_ws_events_path`), and the SQLite inventory calls (`begin_session`/`apply_frame`/`end_session`) are dispatched with `asyncio.to_thread` so they never stall the listener loop shared with the HTTP worker pool. A publish gate (`set_publishing`) is closed by `P2PServer.stop_serving()` **before** the role is retired and bumps a publish epoch that an in-flight broadcast (checked before every send) and a handshake completing after the yield both observe, so a yielded master pushes no event and admits no new client; `stop_serving()` returns the client-close future so `_stop_p2p_serving` waits (bounded) before detaching the shared inventory store |
| `_handle_patch_permissions` | `p2p_server.py` (PATCH route) | metadata-only chmod from a slave: `PATCH /files/permissions?filename&permissions&cid` applies the sanitized mode to an existing file (owner rwx floor via `set_permissions_on_dir` for dirs; no file bytes cross the wire), attributes the resulting IN_ATTRIB to the slave (`mark_inbound_origin`) so the master's watcher re-broadcasts the change to the other slaves. Mirrors the central server's `PATCH …/files/permissions` |
| `_handle_list_dirs` | `p2p_server.py` (GET route) | `GET /dirs` returns `[{"path", "permissions"}]` for every non-hidden, non-symlink directory — mirrors the central server so the client's download-only directory-permission converge (`converge_directory_permissions`) works identically in both modes |
| `_create_dir_on_master` (permissions param) | `p2p_websocket.py::_create_dir_on_master` | `POST /dirs` gains an optional `permissions` query param: the slave's directory-chmod propagate (`_propagate_dir_permission_change`, `_last_dir_mode` dedup map) reuses the endpoint to push the new mode; the master applies it and re-broadcasts |
| `_handle_create_dir` parent marking | `p2p_server.py::_handle_create_dir` | `mkdir(parents=True)` also creates every missing ancestor of the requested path, and the master's recursive watcher fires a create event for each. `_handle_create_dir` marks **every missing ancestor inside the bucket** (plus the leaf, only when it does not already exist) with `mark_inbound_origin` before the mkdir, so those synthetic create events carry the slave's CID: without it they would be logged/broadcast as local creates and the slave could never echo-suppress them (`[REMOTE CREATE]` lines on the slave side). Ancestors above the bucket root are never candidates |
| `_StreamingMultipartParser` | `multipart.py::_StreamingMultipartParser` | streaming upload parsing (extracted to `multipart.py`) |
| `_handle_upload_status` | `p2p_server.py::_handle_upload_status` | `GET /files/upload-status` — reports `.part` size for resume (mirrors central server `get_upload_status`); returns `complete`/`resumable`/`new` + offset |
| `_handle_upload` (offset) | `p2p_server.py::_handle_upload` | resumable upload: `offset > 0` appends to `.part` in `"ab"` mode (size validated); incomplete uploads return `202` with the new offset; compressed + offset rejected |
| safe download serving | `P2PEndpointMixin._handle_download` / `_serve_open_file` | opens beneath the bucket with component-by-component `O_NOFOLLOW`, hashes and streams the same descriptor, checks identity/size during transfer, emits canonical `X-Checksum-SHA256`, and treats `Content-Length` as wire bytes. Zstd is selected only after explicit negotiation and is staged in a bounded temporary file; ranges are always identity. A bounded checksum cache (512 entries, keyed `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)`, invalidated via the same `_same_open_file` identity checks) collapses the 2–3 disk passes per download to one pass + send |
| `_CappedBytesWriter` | `multipart.py::_CappedBytesWriter` | abort uploads whose bytes exceed the effective cap — min(declared size, `MAX_INBOUND_SIZE`) — (decompression-bomb containment) |
| `p2p_delta.py` | `p2p_delta.py` | the four delta endpoints of the embedded master (`GET/POST /files/delta/signatures`, `POST /files/delta/apply`, `GET /files/delta/data`) as thin shells over `hsync_common.delta` (wire shapes, paths and status codes identical to the FastAPI server, pinned by cross-mode fixtures); signature matching runs under its own sub-semaphore (4) so CPU-bound work cannot occupy the global thread cap. Literal payloads are parked in ONE process-scoped temp dir (`hsync-delta-lan-*`, outside `LOCAL_BUCKET` so the sync engine never sees them) and reached only through tokens (single use; shared registry `hsync_common/delta/token_store.py::DeltaTokenStore`, TTL `hsync_common/constants.py::DELTA_TOKEN_TTL`, max 100 live); the dir is removed at interpreter exit by the `atexit` hook `_discard_payload_dir`. **A new process-scoped temp dir must register the same cleanup** — an unregistered one leaks a directory per run, and `test_p2p_delta.py::TestTokenStore::test_process_scoped_temp_dirs_register_their_cleanup` fails as soon as one appears. `POST /files/delta/apply` resolves its target through the shared `_resolve_servable` gate, so an empty/absent `path` is 400 "Missing path parameter" like the other LAN file endpoints (not the old 403) |
| `P2PMasterFileEventHandler` | `p2p_websocket.py::P2PMasterFileEventHandler` | master-local FS changes → broadcast; logs every event with the originator CID (via `peek_inbound_origin`, which does NOT consume the entry) |

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
learn its hash (listing) or content (download). The listing itself is **streamed**
as a chunked JSON array (`P2PEndpointMixin._handle_list_files`, path order): the
master holds O(fanout + one entry) instead of the whole listing in RAM, and the
slave (`sync.py::get_files_remote`) sorts the collected entries by `mtime` to keep
the oldest-first priority. The transport helpers
(`P2PEndpointMixin._begin_chunked_json` / `_write_chunk` / `_end_chunked`) frame
the body for the stdlib transport; the aiohttp bridge frames it natively. In-bucket symlinks are listed
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
| `p2p_websocket_client` | `p2p_websocket.py::p2p_websocket_client` | connect to master WSS, verify cert fp, **`p2p/1` handshake**: hard-fail on greeting protocol mismatch, auth/hello frames carry `protocol_version` + `capabilities`; `local_capabilities()` is announced, `adopt_remote_capabilities()` applies the auth reply as the authoritative capability source (compression/delta gates). **Readiness threshold**: the local watcher starts before the WS connect, but `[READY]` (`p2p_websocket.py::p2p_websocket_client`) fires only after the handshake completed — a failed auth never reaches it. **Inbound file downloads are fanned out** (`event_dispatch._spawn_file_download`): the receive loop never awaits a download — events for the same path are chained (order preserved, plus `path_lock` inside `download_file`), distinct paths run concurrently under a semaphore sized by `MAX_CONCURRENT_TRANSFERS`; the bounded tasks are drained (`drain_file_downloads`) in the connection teardown before the discovery loop's full sync. The fan-out exists so a burst is not capped by a serial `await update_file` per event (~60 files/s) while the master serves 200+ req/s |
| `P2PInventoryPublisher` | `p2p_inventory.py` | continuous-slave WSS publisher: immutable snapshot + bounded pending journal, chunked snapshot/delta frames, one in-flight generation, ACK timeout/retry, and resnapshot fallback on overflow/reject/reconnect. **ACK timeouts never close the WSS**: publishing is disabled for the connection (the master-side session expires via its staging TTL) while the event channel on the same socket keeps flowing — closing it would tear the channel down and force a full resync on both peers under burst backpressure |
| slave seeder lifecycle | `p2p_handler.py` | starts only for continuous non-dry-run slaves after the read-only HTTPS port binds; advertises its endpoint readiness (`https`/`cert_fp`) and its `serve_slots` capacity on the master WSS handshake only while its seeder is bound; stops accepting, withdraws that readiness, and drops role-owned publisher/resolver state before becoming master or exiting. Bind/readiness failure degrades to requester-only |
| `P2PSourceResolver` / `P2PSourceTelemetry` | `p2p_sources.py` | count- and byte-bounded master wantlists plus one fresh single-file lookup for realtime events (measured on the LAN burst: the whole peer-offload block costs ~4.3 ms CPU/file and ~47 % throughput when disabled — see `hsync-client/docs/internal/testing.md`. The per-file lookup is one POST, ~1.0-1.1 ms, because `candidates_for(refresh=True)` calls `resolve([file])` even though `resolve` batches up to `P2P_SOURCES_MAX_FILES` identities; micro-batching those lookups was implemented and measured at 3 ms collection window and did **not** produce a resolvable gain at the default 5-way concurrency, so it was reverted); immutable candidate maps are passed explicitly. Invalid/auth/pin-rotated CIDs stay disabled for the role session, while transient failures use deterministic cooldown. **Admission control (ADR-0030)**: `acquire_slot`/`release_slot` bound in-flight reads per CID to the candidate's advertised `serve_slots`, waiting up to `P2P_PEER_SLOT_WAIT_S` (2 s) before selecting the master — an expired wait is never a refused attempt. **Ranking (ADR-0030)**: `rank(sources, size=…)` orders by failures, then by `_SourceMetrics.predicted_cost_s` = `rtt_ema + size/throughput_ema` — a predicted transfer time, not a raw rate, so a peer that served a large file cannot outrank a lower-latency one for a small file (with no size the setup term decides). A `busy` (503 + `Retry-After`) is a scheduling signal: it defers the source for exactly that interval (`BUSY_DEFAULT_BACKOFF_S` fallback) without incrementing its failure count or dropping it from ranking; candidate URLs are bucket-bound before TOFU and pinned HTTPS uses fingerprint-only verification |
| `_handle_p2p_event` | `p2p_websocket.py::_handle_p2p_event` | apply inbound master events locally; the `p2p_websocket_client` loop builds **one `EventDispatchContext` per connection** (`_build_p2p_dispatch_ctx`, `p2p_websocket.py::_handle_p2p_event`) and passes it to every call so the inbound directory dedup cache survives across events (a per-event context would reset the cache and re-log every duplicate `added`/`modified` directory burst as a fresh `[REMOTE CREATE]`/`[REMOTE MODIFY]`) |
| `P2PSlaveFileEventHandler` | `p2p_websocket.py::P2PSlaveFileEventHandler` | slave-local FS changes → upload to master. Realtime operations (uploads, inbound event downloads, deletes, renames) run on a process-wide bounded pool (`_get_realtime_executor:109`, `REALTIME_MAX_WORKERS=8`; lazy singleton from the shared `hsync_client.pools.get_singleton_executor`) instead of one thread per event: long-lived workers reuse their keep-alive HTTP sessions to the master (no TCP+TLS handshake per file) and the parallelism stays predictable; `_start_operation` tracks `Future`s so `wait_pending` can still wait them out gracefully |
| live deletion marker (slave) | `P2PSlaveFileEventHandler.on_deleted` (`p2p_websocket.py::P2PSlaveFileEventHandler`) / `on_moved` visible→hidden | a slave-side file deletion records a **causal tombstone locally** via the shared `_record_deletion_marker` (`p2p_websocket.py::_record_deletion_marker`) before the best-effort HTTP DELETE reaches the master. Correctness never depends on the transport being up: the marker inherits the merge-base hash + clock (ticked by this peer) and drops the merge base, so the next reconciliation deletes the stale remote copy (`delete_remote`) instead of resurrecting it — the master-unreachable-deletion regression |
| `_p2p_periodic_resync` | `p2p_websocket.py::_p2p_periodic_resync` | thin wrapper around `sync.periodic_resync(p2p_mode=True)` |
| echo suppression | `p2p_websocket.py::mark_inbound_origin`/`p2p_websocket.py::consume_inbound_origin` | `mark_inbound_origin` / `consume_inbound_origin` (TTL `p2p_websocket.py::INBOUND_ORIGIN_TTL`) |
| rename redundancy (inbound + outbound) | `P2PSlaveFileEventHandler.record_inbound_rename` / `is_inbound_rename_echo` + `_record_outbound_dir_rename` / `is_outbound_dir_descendant` | exact `(src,dst)` pair cache (TTL `p2p_websocket.py::is_outbound_dir_descendant`); also matches **descendant** echoes of a directory rename in both directions: inbound (master renamed `ABC -> ABC-123`, slave's watcher echoes children) and outbound (slave renames `ABC-123 -> ABC-123-X`, its watcher emits child `on_moved` events). A single PATCH already renamed the whole tree on the master, so `on_moved` skips the redundant child PATCHes proactively and a 404 from the master is degraded to DEBUG when it matches, else stays WARNING (real divergence). Identifier-suffix guard (starts with `/`) avoids suppressing genuine renames that merely share a path prefix |
| inbound-delete echo | `event_dispatch._apply_delete` (dir / file branches, `event_dispatch.py::_apply_delete`) | a `deleted` event for a path already absent locally is a benign echo (originator removed it / peer delete converged / never existed here): logged at DEBUG, not WARNING. Echo suppression can miss when the master has no `P2P_KEY` (the `mark_inbound_origin` cid gate is a security measure) or when `rmtree` removes a directory's children without individually marking them |
| recursive inbound delete echo | `event_dispatch._apply_delete` dir branch + `P2PSlaveFileEventHandler.record_inbound_delete` / `is_inbound_delete_echo` | before `rmtree`, the confined absolute root path is passed to `EventDispatchContext.record_inbound_delete` (wired in `_handle_p2p_event`). The slave caches the root with a 10 s TTL and its `on_deleted` drops any later delete equal to or under that root (same `/`-suffix guard as the rename echo): those are its own watcher echoes of the rmtree it just applied, not new local deletions — without the cache each would be re-sent to the master where it 404s as a benign echo. Remote mode never installs the callback |
| create-upload deferral | `P2PSlaveFileEventHandler._schedule_create_upload` / `_cancel_create_upload` / `cancel_all_create_timers` | an `on_created` upload for a non-empty regular file is deferred (`CREATE_UPLOAD_DEFER_S` = `STABILITY_DELAY_ON_CLOSED`) so `on_closed` (`IN_CLOSE_WRITE`, the reliable fd-closed trigger) can cancel it and take precedence with the fast stability re-check; the timer is the fallback for files that arrive already complete with no writer fd (atomic rename / hard-link / `mv` in). `on_deleted` / `on_moved(src)` / shutdown cancel pending timers so stale uploads never fire. Eliminates the duplicate upload-intent/`[UPLOAD]` pair (the intent line was later removed entirely — ADR 0018) and avoids uploading half-written files. |
| sentinel never uploaded | `P2PSlaveFileEventHandler._handle_file_upload` | a `.hsynckeep` basename is skipped before building the `File` and starting the upload. The sentinel is a per-peer local artifact: the periodic reconcile already excludes it from sync actions in both directions (`sync.py`), each peer maintains its own (`ensure_hsynckeep_files` / `_ensure_sentinel*`), and the directory itself is propagated by the directory event — uploading it only produced a 0-byte `[UPLOAD]` line and a broadcast round-trip. This is the single chokepoint covering every slave upload path (`on_created` deferred, `on_closed`, symlink, hidden→visible). |
| escaping symlink refused (slave upload) | `P2PSlaveFileEventHandler.on_created` (symlink branch) | a file-symlink inside the bucket whose target escapes `LOCAL_BUCKET` is skipped (logged `[SECURITY]`) instead of being dereferenced and exfiltrated. An in-bucket symlink is uploaded STRUCTURALLY via `transfer.upload_symlink` (the pair over `POST /symlinks`; the absolute local target is relativized by `hsync_common.wire_symlink_target` before it reaches the wire) — the content-upload path (`_upload_to_master` → `upload_file`) is never used for links. Belt-and-braces: `transfer.upload_file` refuses any symlink whose target escapes the bucket, and the shared watcher (`file_event_handler.BaseFileEventHandler.on_created`) refuses to broadcast a `symlink` event whose target escapes. |

## Local capabilities (`capabilities.py`)

| Symbol | Purpose |
| ------ | ------- |
| `local_capabilities` | runtime-derived capability set advertised on every handshake (always `vector_clocks` + `tombstones`; `compression`/`delta` when enabled; `peer_download` only when the runtime feature is enabled) — shared by remote and p2p transports |
| `adopt_remote_capabilities` | adopts the authenticated peer's capabilities as the session transfer gates (`SERVER_COMPRESSION_ENABLED`/`SERVER_DELTA_ENABLED`) |

## Security: TOFU pinning + persistent identity

| Concern | Code | Notes |
| ------- | ---- | ----- |
| Pin store | `pinning.py` (`PinStore`) | disk-backed `0o600` JSON keyed by `(CID, bucket)`, each value a record `{fingerprint, first_seen}` (the pinned fingerprint and the time the peer was first trusted, so the store is auditable like the known-hosts stores; a repin preserves `first_seen`). Every read-modify-write holds a sibling `.lock` file with POSIX `flock` or Windows `msvcrt.locking`, reloads inside the lock, writes a unique same-directory temp, fsyncs file + parent directory, and atomically replaces. `get`/`set`/`check_and_record`/`remove`/`entries` (sorted `(cid, bucket)`, with `first_seen`, for the CLI). Master and each selected seeder use independent persistent TOFU entries; `--require-pairing` applies to the master only |
| Pin decision | common `protocol/pairing.py` | `evaluate_pin`; refuse on changed fingerprint, emit `fingerprint_changed`/`pairing_needed` |
| Trust messages | `trust_messages.py` | shared SSH-style prose for all stores: `known_hosts_added_message`/`mismatch_guidance` (bootnode), `known_server_added_message`/`server_mismatch_guidance`/`server_pairing_needed_guidance` (server) and `known_peer_added_message`/`peer_mismatch_guidance`/`peer_pairing_needed_guidance`/`peer_source_changed_line` (mDNS). The peer/host blocks carry the normalized fingerprint and the exact `hsync trust …` commands |
| Trust CLI | `cli.py` (`hsync trust {peer,bootnode,server} {list,show,add,remove,fingerprint}` + `list --json`) | one namespace for the three pinning transports; `list --json` uses `entries()` |
| Device identity | `identity.py` (`load_or_create_device_identity`) | Syncthing-style: one identity per installation — stable device ID (`device_id` file, CID_PATTERN-valid) + keypair under `HSYNC_DEVICE_DIR` (`default_device_dir`, default `<HSYNC_STATE_DIR>/device`); cached process-wide. The device ID **is** the p2p `cid` (resolved in `main_logic.run_sync`), so pins survive restarts and cert rotation (delete `*.pem`, keep `device_id`) is detected as `[FINGERPRINT CHANGED]`. Keypair itself: `ssl_utils.load_or_create_master_cert` (dir delegates to `identity.default_device_dir`) |
| Instance lock | `identity.py` (`InstanceLock`) | cross-process advisory lock per synced dir (key = sha256 of realpath) in `$HSYNC_LOCKS_DIR`, else `$XDG_RUNTIME_DIR/hsync/locks`, else `<device-dir>/locks`; the primitive is shared with the JSON stores in `_filelock.py` (`flock` on POSIX, one-byte `msvcrt.locking` on Windows, loaded lazily so the module imports on every platform); OS-released on death (never stale); a second process on the same dir is refused with the holder PID in the message. Acquired for the whole run in `main_logic.run_sync`; same-process re-entry is a no-op (CliRunner tests) |
| mDNS name clash | `p2p_peers.py`/`p2p_discovery_mdns.py` (`_register_service`) | `NonUniqueNameException` → clear `RuntimeError`: same device identity + same bucket already announced on this host → use a separate `HSYNC_DEVICE_DIR` (different buckets on one host share the identity fine: service name is `{bucket}-{device_id}`) |
| Duplicate identity detection | `p2p_peers.py`/`p2p_discovery_mdns.py` (`PeerManager.add_peer` `_is_foreign_duplicate`/`_report_duplicate_identity` `p2p_peers.py::_report_duplicate_identity`/`p2p_peers.py::_report_duplicate_identity`) + `p2p_websocket.py` (`P2PWebSocketServer._admit_slave` `p2p_websocket.py::P2PWebSocketServer._admit_slave` via `SlaveCidRegistry` `p2p_websocket.py::SlaveCidRegistry`) + `p2p_server.py` (`P2PEndpointMixin._reject_duplicate_cid`) | A foreign host presenting our own CID (a cloned `HSYNC_DEVICE_DIR`) is told apart from a legitimate self-discovery via the advertised address + cert fingerprint and refused with a `[SECURITY]` error (dedup'd per signature, so mDNS heartbeats don't spam). The master's `SlaveCidRegistry` reserves every admitted slave CID against its source IP atomically (no check→reserve race), refuses conflicts on the WSS path (`auth_failed("cid_conflict")`, same-address reconnects replace their own binding) and on every authenticated HTTP write (`409 Duplicate device identity`) — a clone cannot corrupt the causal vector-clock/tombstone authorship on any channel. The slave surfaces the `cid_conflict` reason as a clear `[SECURITY]` error. Every detection point emits the `duplicate_identity` UI event (`hsync_common.protocol.events.UiEventName`), surfaced as a Desktop notification |

On the unauthenticated (no `P2P_KEY`) WSS `hello` path the declared CID is not
authenticated, so a hostile peer that observes mDNS could squat another
slave's CID and get the legit slave refused while it holds the connection.
No-key mode is read-only by default: discovery, WSS availability publication,
list/download/source lookup, and download-delta reads remain available, while
write-capable startup and every mutating HTTP route are denied unless the
explicit dangerous opt-in is set. CID squatting can therefore affect
availability/source hints but cannot authorize a filesystem mutation.
| Connection pinning | `ssl_utils.py` | `create_fingerprint_ssl_context`, `FingerprintHTTPAdapter`, `verify_peer_cert_fingerprint` (no TOCTOU) |
| Pinned session trust | `utils/http.py:get_http_session` | A fingerprint-pinned session sets `trust_env = False`: the pinned fingerprint is the whole trust anchor (TOFU), so `*_proxy`, `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE` and `.netrc` must not add a second one — an environment proxy would route the pinned peer connection through a third party. Also removes `requests`'s per-request environment-proxy resolution (5 calls/file, ~0.85 ms CPU/file on a burst). Remote-mode sessions keep the default (`trust_env = True`) |

HTTPS/WSS is mandatory in p2p mode (no plaintext fallback). Write-capable no-key
sync requires `--allow-unauthenticated-writes` or
`HSYNC_P2P_ALLOW_UNAUTHENTICATED_WRITES=true`; TTY state never authorizes it
(`main_logic._check_p2p_write_policy`). A keyless (no `HSYNC_P2P_KEY`) run,
read-only included, additionally requires `--allow-unauthenticated-p2p`
(`HSYNC_P2P_ALLOW_UNAUTHENTICATED=true`) or an interactive confirmation
(`main_logic._check_p2p_keyless_policy`).

The mDNS trust refusal is **not fatal** (unlike the bootnode lease loop): the
discovery loop retries continuously, so `_evaluate_master_pin` dedupes both the
log block and the structured event through `_mark_trust_notice` /
`_clear_trust_notice` (`p2p_handler.py`) over a `notified` map keyed
`(kind, cid)` → last fingerprint (created in `run_p2p_mode`, threaded through
`_run_as_slave`). The entry is cleared once the connection proceeds, so a later
flip re-notifies. TOFU logs
`trust_messages.known_peer_added_message`; a changed pin logs
`peer_mismatch_guidance` (`[FINGERPRINT CHANGED]`); strict pairing logs
`peer_pairing_needed_guidance` (`[PAIRING NEEDED]`); a changed seeder source logs
`peer_source_changed_line` in `p2p_sources.authorize` (per-session CID disabling
already dedupes that one).

> Human guide: `../../docs/p2p-mode.md`. Election/security rationale:
> ADRs 0001/0002 + `p2p-protocol.md` (see [decisions.md](../decisions.md)).

## Role handover on the single port (seeder ⇄ master)

The slave seeder and the master serve on the **same** listener
(`P2PServer`: one TLS port for HTTPS REST + WSS), so a role change hands it
over: `stop_serving()` retires the role (503 for any request while no role is
served, publish gate closed) and `P2PWebSocketServer.stop()` closes the live
control-plane clients. A pooled connection from the seeder era therefore cannot
be served by the retired role after promotion. See `decisions.md`
("retired server role") and the `TestRoleHandover` unit/BB regressions.

## Bootnode discovery plan (`boot_client.py`, `stun.py` — C2)

Cross-network discovery uses a `boot/1` bootstrap node (SSOT: `hsync-bootnode`).
The wire contract is mirrored in `hsync_common.protocol.bootnode` (frames +
strict validators); the client side is split so transport details stay out of
peer state:

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `BootnodeClient` | `boot_client.py::BootnodeClient` | synchronous `boot/1` client: one QUIC/TLS 1.3 connection (ALPN `hsync-boot/1`), one bidi stream per request, NDJSON frames; `register`/`refresh`/`leave`/`query`. ADR-0034 trust fields: `fingerprints` (pin, up to two) / `tofu` (trust on first use) |
| `build_register_frame` / `build_refresh_frame` / `build_leave_frame` / `build_query_frame` | `boot_client.py::build_register_frame`–`boot_client.py::build_query_frame` | TypedDict frame builders; every request is re-validated against the mirror before it is sent (fail-fast, no network on an invalid frame). `build_register_frame` always emits the election advertisement |
| `BootnodeError` / `BootnodeUnreachable` / `BootnodeProtocolError` / `BootnodeRefused` / `BootnodeFingerprintMismatch` | `boot_client.py::BootnodeError`–`boot_client.py::BootnodeFingerprintMismatch` | transport failure vs contract violation vs **uniform refusal** (`token_required`/`rate_limited`/`overloaded` answered to `register`/`refresh`; `overloaded` is node capacity, not client abuse) vs pin/TOFU mismatch (fatal) |
| `observed_address` | `stun.py::observed_address` | server-reflexive public `ip:port` from a validated `ack` (STUN-echo) |

| `BootnodeDiscovery` | `p2p_discovery_bootnode.py::BootnodeDiscovery` | the `DiscoveryPlan` implementation (extends `DiscoveryPlanState`): lease loop (register → refresh → query) at the `boot/1` cadence `BOOTNODE_REFRESH_INTERVAL` (default 30 s, spec `boot-1.md`; env `HSYNC_BOOTNODE_REFRESH_INTERVAL`, range 1–300), peer sample → `PeerManager`, plus an **event-based** `wait_for_discovery` (`PeerManager.wait_for_change`, no network) so the wait never polls `query` — the node rate-limits queries to 5/min per (IP, bucket) and a refusal arms a `retry_after` backoff. A `BootnodeFingerprintMismatch` stops the lease loop (never retried), is recorded as a fatal error and re-raised by `wait_for_discovery` as `DiscoveryFatalError` so `run_p2p_mode` exits with `ExitCode.FATAL`. Registration republishes the election advertisement on every role change (`_republish_advert`, `p2p_discovery_bootnode.py::BootnodeDiscovery`) and `update_server_ready`/`update_server_not_ready` mirror the incumbency into the shared `PeerManager`; relayed peers map the advertisement verbatim (`_peer_from_wire`, `p2p_discovery_bootnode.py::BootnodeDiscovery`) and keep the full `endpoints[]` candidate list, promoting a same-subnet candidate to the peer's primary address so a same-NAT peer dials the local path (no NAT hairpin) |
| `candidate_endpoints` | `nat_traversal.py::candidate_endpoints` | ordered, de-duplicated dialable endpoints for the `register` `endpoints[]` (observed → UPnP → local) |
| `UpnpMapper` / `discover_igd` | `hsync_client/upnp.py` / `hsync_common/upnp.py` | best-effort TCP port mapping for the master (opt-in, off by default); the SSDP/SOAP core (SSRF-hardened) is the shared `hsync_common.upnp` SSOT, the client adds only the lifecycle; every failure degrades to "no UPnP" (node stays slave-only) |
| `get_local_ip` | `net_utils.py::get_local_ip` | shared local-source-IP probe (mDNS advertiser, bootnode candidates, p2p handler) |
| `same_subnet` | `net_utils.py::same_subnet` | LAN-adjacency test (IPv4 `/24`, IPv6 `/64`) shared by the peer-source ranking and `dial_candidates` |
| `dial_candidates` / `Peer.ordered_endpoints` | `p2p_peers.py::dial_candidates` / `Peer.ordered_endpoints` | order a peer's dial candidates host/LAN-first (loopback + same-subnet before the advertised ones), degrading to the primary endpoint when no candidate list is known |
| `_probe_master_endpoint` / `_select_master_endpoint` | `p2p_handler.py::_probe_master_endpoint` / `p2p_handler.py::_select_master_endpoint` | pick the first reachable candidate by a fingerprint-pinned TLS handshake with a bounded per-candidate timeout (`MASTER_CANDIDATE_PROBE_TIMEOUT_S`, 3 s); single-candidate peers skip the probe |

Readiness & banner: `run_p2p_mode` logs the active plan at startup
(`Discovery: mDNS on the local network` vs `Discovery: bootnode HOST:PORT
(cross-network; UPnP on|off)`). The **bootnode plan only** adds a readiness gate:
the master emits `[READY]` (and writes the ready file) only after
`endpoint_published()` — i.e. after the second `register` that carries the
`endpoints[]`. The mDNS plan returns `True` immediately, so its readiness
timing is unchanged. The wait is bounded by `READINESS_ENDPOINT_TIMEOUT_S`
(10 s) so a wedged lease loop degrades to the old timing instead of hanging.

The same startup also emits the **`discovery` UI event** (`UiEventName.discovery`,
in `hsync-common/protocol/events.py`) with `plan` (`mdns` when no bootnode,
`mdns+bootnode` when one is configured) and, with a bootnode, its host — so a
front-end can surface the active source(s) instead of silently degrading. The
desktop maps it to the `discovery_changed` signal; the payload stays a flat
optional-field object (no events-schema bump).

Lease semantics: the **first** `register` has no endpoint (public address not
known yet) and capacity 0; the `ack` carries the observed endpoint (STUN-echo);
once the handler signals `update_endpoint_ready(True)` the adapter maps the TCP
port (UPnP, if enabled) and sends a **second `register`** publishing
`endpoints[]` + the advisory `capacity` (0 unless peer-download is on). A
`refresh` renews TTL + `connected` only; if the node drops the lease, the loop
falls back to a full `register`. Relayed peers are fed to `PeerManager` only
when `reachable` is true and `proto == "tcp"`; the local CID is never re-added.

Election advertisement: every `register` carries the local
`server_ready`/`prefer_master`/`never_master`/`startup_time`/`sync_direction`
(decision #31 in `hsync-bootnode`), the node echoes them verbatim on every
`query` peer, and `_peer_from_wire` maps **those** values into the shared
election — the old fabrication (`server_ready=True`, `startup_time=0.0`,
`never_master=False` for every reachable peer) made a read-only seeder look like
a serving master, so a `--prefer-master` node demoted itself, a `--never-master`
seeder became electable and the roles flapped. Roles now reach the bootnode two
ways: `update_server_ready`/`update_server_not_ready` mirror the incumbency into
the shared `PeerManager` (`set_local_server_ready`, exactly like
`MdnsDiscovery._refresh_service`, otherwise a freshly relayed peer demotes the
local master) *and* bump an advertisement generation; the lease loop notices the
unsent generation and re-`register`s (`_republish_advert`) until the node
acknowledged the new value, so a promotion/yield is visible immediately and a
failed re-register retries instead of stranding a stale role. `refresh` never
carries or mutates the advertisement (a change requires a new `register`, like
`capacity`/`endpoints`). A **uniform refusal** (`{error: "token_required"|
"rate_limited"|"overloaded"}` in a `response`) is a legitimate `boot/1` answer to
`register`/`refresh`: `BootnodeClient` raises `BootnodeRefused` for it (a
`response` without a valid `error` stays a `BootnodeProtocolError`), and the
lease loop keeps its state instead of resetting to a full register — the first
refusal per error is logged at WARNING, repeats at DEBUG.

Known limitation (pre-existing, unchanged by the advertisement work):
`update_endpoint_ready(False)` (the seeder role stopping) does not re-`register`
without `endpoints[]`/`capacity`, so the relayed endpoint can linger on the
bootnode record until the next full `register` or the 60 s TTL. The advertised
`server_ready=false` still keeps it from being ranked as an incumbent master,
and the lane is a candidate-only advertisement; a peer that dials it gets a
refusal from the unified listener and retries.

`leave` has **no response frame** (spec): the client writes it and closes the
stream without waiting for a reply. A black-box integration test
(`tests/test_bb_p2p_bootnode_client.py`, gated by `HSYNC_BB_P2P=1`, skipped when
the sibling bootnode is not built) spawns the real `hsync-bootnode` on loopback
and drives register → ack/STUN-echo → query relay → refresh → leave over the
public wire.

TLS trust (ADR-0034/0035): the bootnode is authenticated **before any frame is
written** — after the QUIC handshake the client reads the peer certificate
(aioquic's private `tls._peer_certificate`, isolated in `_peer_certificate`
`boot_client.py`) and checks it against the accepted set: a terminal-confirmed
`ask` prompt (the default policy), explicit `--bootnode-fingerprint` pins (up to
two for a rotation window) or the opt-in `--bootnode-tofu` store
(`bootnode_trust.py`, a `{host:port} → current` map under
`HSYNC_BOOTNODE_TRUST_FILE`). A first pin (prompt accepted or TOFU) logs the
SSH-style `Permanently added 'host:port' (SHA256) to the list of known hosts
(<path>)` (`trust_messages.known_hosts_added_message`) so the operator learns
where the pin lives. A refusal raises `BootnodeFingerprintMismatch`, whose `str`
is the concise case-specific reason (changed pin, node not pinned under `strict`,
no match against `--bootnode-fingerprint`) and whose `guidance` carries the
SSH-style recovery block — presented fingerprint, store path and the exact
`hsync trust bootnode add|remove` commands — logged once by `_lease_loop`
(`p2p_discovery_bootnode.py`), while the fatal propagation line
(`p2p_handler.py`) repeats only the concise reason. The same refusal emits a
structured event: a strict-unknown node emits `pairing_needed`
(`scope="bootnode"`), a changed pin / explicit-pin mismatch emits
`fingerprint_changed`. The connection closes with
zero bytes sent; the lease loop stops instead of retrying.
`--insecure` disables verification (tests/loopback only, mutually exclusive with
pin/TOFU); `--ca-cert` is **refused** in `hsync p2p` (it is a server-mode knob,
and a self-signed bootnode has no usable CA/hostname). With no pin/TOFU the
client keeps `verify_mode=CERT_REQUIRED` (fail-closed against a self-signed
node). `DEFAULT_QUIC_IDLE_TIMEOUT = 30.0`. Config surface: `HSYNC_BOOTNODE`
(a `HOST[:PORT]` endpoint: `host`, `host:port`, `[IPv6]`, `[IPv6]:port` via
`hsync_common.endpoint.parse_host_port`; no scheme/path),
`HSYNC_BOOTNODE_FINGERPRINT` (comma-separated pins), `HSYNC_BOOTNODE_TOFU`,
`HSYNC_UPNP_ENABLED`, `HSYNC_P2P_LISTEN_PORT`
(`constants.BOOTNODE_DEFAULT_PORT = 443`).

Token authentication: a **protected bucket** or a **private node** gates
`register`/`refresh`/`query` (and `leave` in private mode) on a `bucket_token`
the client derives from the **p2p shared key** — `derive_bucket_token(P2P_KEY,
REMOTE_BUCKET)` (SSOT `hsync_common.protocol.bootnode`), fed from
`config["P2P_KEY"]` (`main_logic.py`, env `HSYNC_P2P_KEY` or the hidden
`--p2p-key-fd` pipe) into `p2p_discovery.py::build_discovery_plan` → `BootnodeDiscovery`
(`p2p_discovery_bootnode.py::BootnodeDiscovery`), so a token exists only when a key is set. The
same key is the p2p data-plane credential (`Authorization: Bearer`): one key per
client, which authenticates to any protected bucket whose group key equals it.
Refusals stay uniform (`token_required` → `rate_limited`), so the client cannot
detect which buckets exist.
