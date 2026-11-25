# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A sync trash: a deletion applied by the cycle moves the file into a
  device-local store (`<state dir>/trash`, `--trash-dir`) instead of unlinking
  it, kept for `--trash-retention` days (30 by default) under a
  `--trash-max-size` budget. `hsync trash list|add|restore|empty` lists, parks a
  file chosen by the user, restores and empties it, `--no-trash` turns it off,
  and a store pointed inside the synced folder is refused.
- `hsync trash prune [--json]`: applies the retention window and the size budget
  on demand, so a `--once` run or a stopped client does not keep the bytes past
  the window. `restore` and `empty` answer in JSON too, and the `list` entries
  carry `payload_present`.
- `hsync trash restore --force` replaces an existing file through the trash
  instead of refusing the path, and `hsync trash empty --all` empties every
  share of the device in one call.
- `--peer-role full|leech|seed|off` plus `--no-serve-peer-blocks`,
  `--no-fetch-from-peers` and `--no-publish-inventory`: the three peer-mesh capabilities
  as independent keys.
- A degraded host recovers a coarse clock offset from the rendezvous `ack` echo
  (TLS-pinned, crosses the NAT), and the log reports `[CLOCK SYNC]` / `[CLOCK
  DEGRADED]`.
- Cross-platform synchronisation parity on Windows and macOS: a lossy host
  advertises `readonly_permissions`, so permission-only changes are compared on
  the owner-write bit instead of re-applying POSIX modes, and filesystem
  operations a platform refuses are skipped with a warning — names that collide
  when case is folded, paths over the Windows `MAX_PATH` limit, and symlinks
  created without the required privilege.
- `--server-trust`/`--rendezvous-server-trust`/`--p2p-trust` (and the matching
  `HSYNC_*_TRUST` variables) accept `auto`: a stored pin wins, else standard CA
  validation for the sync server, else pin the first sighting. A first-use pin
  is logged and surfaced as a non-blocking notice.
- IPv6 on the local network: the data-plane listener binds dual-stack, mDNS
  advertises ULA/global IPv6 addresses alongside IPv4, and the record only
  carries the families the listener actually bound (an IPv4-only host or network
  keeps working unchanged).

### Changed

- A removed **directory** keeps the bytes of the files it held: the tree is parked
  in the trash (up to 1000 files / 1 GiB per removal, with the remainder logged)
  before it is deleted, so a folder a peer removed is as recoverable as a single
  file.
- A deletion applied from a real-time event, or by the p2p master on a peer's
  request, parks its bytes in the same trash as a cycle deletion; the sweep also
  drops a record whose payload is gone.
- The serving role runs its own maintenance pass (partial files, minted deletions, merge
  bases, `.hsynckeep`) at role start and on every resync.
- A compressed download is streamed instead of staged, so the 256 MB cap and the
  temporary full copy are gone.
- Election: no clock; the leadership term decides between two serving masters, so a
  partition heal has a deterministic winner.
- A host that cannot vouch for its clock publishes no modification time until it can,
  and a conflict whose timestamps differ by less than the measurement error is resolved
  by content.
### Fixed

- A read-only file is made writable before it is moved into the trash and back,
  so Windows (which refuses to rename such a file) can restore it too.
- A folder emptied by a peer's deletion no longer comes back: the emptied
  directory is pruned instead of being kept as an intentional empty folder.
- A file deleted while the peer was unreachable is now deleted on the peer
  instead of being downloaded again: the offline-deletion marker inherits the
  peer's clock when the remote copy is byte-identical to the deleted version,
  and the P2P master's `201` carries `version` like the server does, so the
  uploader records the accepted clock as its merge base.
- A slave whose master restarted on a new port reconnects instead of spinning
  the rendezvous loop: a failed endpoint selection now pulls a fresh rendezvous
  sample and waits `RECONNECT_DELAY` (clamped to 1–30 s) before retrying.
- The p2p link status follows the data plane: a slave reports `connected` while
  it syncs (before its control WebSocket opens, and after every reconnect), and
  a serving master reports it while a peer uses its data plane, so a long first
  sync or resync no longer shows "Connecting…" (a lost link, in the desktop)
  with no master/slave role.
- The sending peer always reports transfer progress now: the delta upload
  streams its literal payload with its own samples, and a p2p upload no longer
  depends on the receiver's delivery echo (the control WebSocket opens only after
  the initial sync), so a first-cycle or delta transfer leaves its planned status
  and shows a bar instead of staying at "To sync".
- An in-flight transfer row is closed by a `401`/`403` upload answer, an
  arbitration rejection (`superseded`, `conflict_lost`, `destination_changed`),
  and a rejected received body (checksum, duplicate identity, exception), so no
  row stays in flight until the panel's stale-row re-check.
- An inbound rename that changes only the case of a path now applies reliably on
  case-insensitive filesystems, and an inbound rename whose destination already
  holds local content is left to the causal reconcile instead of overwriting it.
- Overwriting or deleting a Windows Read-only file no longer fails: the
  attribute is cleared before the operation. The delta rebuild and the inbound
  P2P upload commit take the same path.
- A name collision (case- or normal-form-fold) no longer makes a `download-only`
  mirror delete the local file that won it: the remote entry is re-pointed at
  the winner's spelling instead of being dropped.
- A crash between the two steps of a case-only rename no longer loses the file:
  the startup sweep restores the stranded marker instead of deleting it.
- The long-path guard also covers every transient name a transfer writes before
  the atomic commit (`.part`, `.delta.part`, `.delta.part.lit`), and a POSIX
  `EACCES`/`EPERM` is no longer mistaken for a host that cannot create symlinks.
- A persistent name collision is reported once instead of once per sync cycle.
- The P2P slave no longer takes `REMOTE_READONLY_PERMISSIONS` from the
  unauthenticated mDNS TXT record (advisory hint only): the permission
  projection switches on the capability proved in the WSS handshake.
- A `chmod` or a `touch` was silently lost on Linux with watchdog 7 (the
  metadata change arrived as a dedicated `attrib` event the client never read),
  so permissions and mtime converged only at the periodic resync.
- A server or peer `429` no longer drops the file: the transfer is retried,
  honouring the server's `Retry-After` within the per-file retry bound.
- A file re-created at a path that had been deleted is no longer deleted again
  on both peers: its version clock is now seeded from the deletion marker it
  supersedes instead of starting from scratch.
- The file watcher no longer fails to start on Windows and macOS: the
  inotify-only `generate_full_events` argument is no longer passed to the
  platform-native observer, which rejects it.
- Storage detection now works on every platform instead of being skipped off
  Linux: Linux resolves the block device through `/sys/dev/block` (partitions,
  dm/LVM) and walks up through ecryptfs/overlay mounts, Windows asks the storage
  stack for the disk bus type and seek penalty, and macOS reads `diskutil`. A
  synthetic or unreadable device logs at debug and keeps the conservative HDD
  tier, so an SSD/NVMe no longer runs at the HDD concurrency default.
- The startup autotune pass no longer probes `/health` before the server trust
  is resolved, so a self-signed server (the normal deployment) no longer fails
  certificate verification with two warnings at every start; the post-connect
  refresh probes after the pin/CA/`--insecure` policy is applied.
- A `token_required` rendezvous refusal now names the gated bucket and states the
  fix (the node's `bucket=<key>` line must hold the group key derived from the
  shared password, `hsync derive-group-key`) instead of only the raw error code.
- A Windows host with several adapters (Wi-Fi, Ethernet, VPN) now advertises
  every interface instead of only the address the hostname resolves to.
- A relay connection on a dual-stack host no longer stalls on an unreachable
  IPv6 address: the resolved candidates are tried in order and the first that
  completes the TLS handshake is pinned for every later connection.
- Console output stays UTF-8 even when redirected on Windows (Task Scheduler,
  `cmd`), so the log glyphs are no longer lost.
- The stdin command channel works on Windows: a blocking reader thread replaces
  the Proactor pipe read (which fails on a synchronous parent pipe with
  `ERROR_INVALID_PARAMETER`), so `SYNC`/`PREVIEW`/direction/bandwidth commands
  from the desktop are no longer dropped.
- Uploads, deletes and renames send a bucket-relative path with POSIX separators
  on Windows, so the server no longer rejects them with `HSYNC-1002 Invalid path`.
- A certificate-fingerprint probe that fails (server unreachable or no
  certificate) is reported as "could not read the fingerprint" instead of a pin
  mismatch with an empty `sha256:` and a re-pin command for an empty value.
- A transfer that failed after its retries is counted as failed, so the cycle
  reports "completed with N failed transfer(s)" instead of "completed
  successfully".
- With mDNS and several discovery servers configured, the client failed to start
  (`HybridRendezvous` passed a shared `PeerManager` to a fan-out start that
  rejected it); it now starts.
- The hybrid plan (mDNS + a discovery server) logs a single "P2P rendezvous
  started" line instead of two: only the top-level plan announces the start.
- Empty folders no longer read "To sync" on the device that sends them: the
  empty-directory verdict is emitted only for a directory that is empty on both
  sides (`hsync_client/empty_dirs.py::empty_dir_states`), a stale `.hsynckeep` is
  dropped once the directory holds content (`hsync_client/sync.py::prune_sentinel`),
  and the bucket root no longer yields a phantom `.hsynckeep` row.

## [1.1.2] - 2026-11-12

### Added

- A bucket that does not exist is reported as its own error: the message names
  the bucket and the remedy instead of a bare `404`, the sync never retries it,
  and the failure carries `reason="share_missing"` so a front-end can localize
  the remedy.

### Fixed

- A failed preview keeps the missing-bucket reason (`reason="share_missing"`)
  instead of degrading to a generic error.

## [1.1.0] - 2026-11-11

### Added

- The discovery server can be reached over **TLS/TCP** as well as QUIC/UDP:
  `--rendezvous-transport auto|quic|tcp` (`HSYNC_RENDEZVOUS_TRANSPORT`) uses one
  transport when fixed and, on `auto` (the default), races both per endpoint with
  the RFC 8305 stagger (QUIC/UDP preferred); the winning transport is memoized per
  endpoint and a TLS/TCP winner expires so QUIC regains priority on a recovered
  UDP path. The data relay works over TLS/TCP too.
- The p2p master election prefers a node that confirms an externally dialable
  endpoint (`inbound`), and a node defers advertising itself as the serving
  master until the rendezvous answered once, so a relay-only peer no longer
  wins the role and forces the group onto the relay.
- Every P2P data-plane request carries the requester CID in an `X-Client-Id`
  header, so the master keeps per-peer admission buckets separate for relayed
  peers (which all arrive from the loopback address) even on requests whose URL
  has no `cid` query parameter.
- `--no-mdns` (and `HSYNC_MDNS=false`) excludes the local network: peers are
  discovered only through the configured discovery server(s). The desktop
  exposes it as "Exclude the local network".

### Changed

- Dependency refresh: `typer` 0.29, `watchdog` 7.1, `zeroconf` 0.154.
- The p2p slave reconnects at once on a local network change: the WebSocket is
  closed the moment the interface changes and re-opened on the new one, instead
  of blackholing until the keepalive ping times out (tens of seconds).
- The p2p slave WebSocket liveness timeouts (open, ping) follow the autotune
  network class, so a high-RTT mobile path gets a longer, more tolerant set.
- The p2p slave re-selects the master endpoint when the master's advertised
  endpoint changes mid-session (address, port, or candidate list): the WSS is
  closed at once so the loop reconnects to the new endpoint (or the relay)
  instead of dialing the stale address until the keepalive fires.
- The p2p data-relay tunnel multiplexes every loopback connection over one relay
  session (one `relay_connect` per tunnel, private stream framing), so the sync
  cycle no longer spends a pairing token per REST/WebSocket connection.
- A relayed slave caps its concurrent transfers so the multiplexed session's
  flow-control window is not swamped.
- Relayed peers are rate- and in-flight-bucketed by their CID instead of the
  shared loopback address, so one relayed peer cannot starve the others.
- The relay keepalive runs at a third of the node's QUIC idle, so one lost ping
  no longer tears down the tunnel and every transfer with it.
- A multi-homed node advertises one dial candidate per active interface (the
  default-route address first, LAN addresses before globally routable ones,
  container/bridge interfaces skipped) capped at the node's four-endpoint relay
  bound, so a peer on any of its real networks connects directly instead of
  falling back to the relay.
- An mDNS node advertises one A record per active interface, and a discovered
  peer's same-subnet address is promoted to its primary one (with the other
  interfaces kept as dial candidates so the probe falls back to the reachable
  one), so a multi-homed peer sharing a LAN is dialed directly instead of over
  an unreachable interface.
- The relay's transfer-concurrency cap is lifted when a direct path is used
  again, so a CLI/env override or `--no-autotune` no longer keeps the fast path
  throttled to the relay value.
- A newly joined P2P peer appears in the other devices' peer lists within a
  second instead of up to the 30 s rendezvous refresh: a serving peer samples
  the discovery server as soon as it admits the new WebSocket, and the client
  keeps sampling at the sustainable floor while the group is not provably
  complete (a peer that joined after the last sample, or one still unpublished).

### Fixed

- The status channel no longer reports the sync phase as a link state: a cycle
  on a live link publishes `activity=syncing`/`idle` and leaves the link
  `connected`, so the desktop no longer keeps showing "Syncing…" with every
  synced row unverified once a run ends.
- A newly created empty folder keeps its final state on both devices: the p2p
  master now creates the empty-directory sentinel (it runs no sync cycle, so
  nothing else did) and both creation paths report the folder's verdict, so it no
  longer stays "to sync" on the device that created it or "local" on the replica.
- A rendezvous `retry_after` (rate limit or node capacity) is now honoured as a
  wake-up time: the lease loop slept the full refresh interval, so the announced
  retry never happened and a peer stayed undiscovered for up to 30 s longer than
  the node allowed.
- A rendezvous exchange over QUIC no longer waits for the peer's connection drain
  when the per-exchange connection is closed (~95 ms per request on loopback);
  a TLS/TCP connection releases its socket immediately instead of at garbage
  collection.
- The p2p rendezvous republishes its endpoint candidates when the local
  candidate list changes (a new/removed interface), not only when the
  default-route address changes, so the node stops relaying stale candidates.
- A multi-homed node no longer reports its own mDNS record as a cloned device
  identity when the record lists a secondary interface first: the self-record
  check accepts any address the node announces, not only the primary one.
- The relay acceptor drops a frame for a stream it already closed instead of
  re-opening the master's loopback connection mid-stream, which spliced a fresh
  TLS handshake into the old stream (a plausible cause of the master's
  `WRONG_VERSION_NUMBER` TLS errors); a stream whose local connection cannot be
  opened is now closed back to the peer at once.
- An upload that finds the content already on the peer (preflight `complete` or
  a legacy `409`) now emits its terminal transfer event, so the desktop row
  closes instead of staying at the last progress sample.
- The relay acceptor opens the master's local connection only on the first data
  frame, so a connection that carries no bytes no longer reaches its TLS
  listener (no empty-handshake `SSLEOFError`).
- The relay closes its QUIC streams before the connection ends, so the aioquic
  "Cannot send data on a finished stream" teardown noise no longer appears.
- The inventory publisher retries a fresh snapshot after an ACK timeout instead
  of disabling itself, so the master is no longer left awaiting the inventory.
- The election log names the sort-key component that decided it, not the
  winner's highest flag.
- A `startup_time` election tie is broken on the SNTP-corrected clock, so a host
  whose clock runs behind no longer wins it by accident.
- A resumable upload whose preflight offset no longer matches the peer's
  `.part` (a duplicate writer appended bytes, or the partial was already
  renamed) answers `202` with the true offset and keeps the partial — the peer
  re-probes and resumes instead of failing permanently.
- Uploads to the same p2p destination are serialized per path, so a duplicate
  trigger (watcher + full sync) can no longer corrupt the `.part` with two
  concurrent writers.
- A `202` partial on a standard upload is now retried (re-probing the resume
  offset) and ends with a terminal `transfer_failed` when the retries are
  exhausted, instead of leaving the row open.
- A resumed upload anchors its first progress sample at the resuming offset, so
  the desktop no longer reports an impossible burst speed for the bytes already
  on the peer.
- A TLS/TCP rendezvous dial is bounded (10 s connect + handshake), so a node
  that only answers on QUIC/UDP fails fast with a logged `unreachable` instead
  of stalling the lease loop for minutes on the kernel's connect timeout.
- The `<fp>` placeholder in the rendezvous-server trust refusals
  (`--rendezvous-server-fingerprint <fp>`) no longer crashes the colored log
  formatter and silently drops the error: the placeholder reads `FINGERPRINT`
  and the colorizer escapes `<` in every message, so an unpinned server now
  surfaces "Rendezvous … not trusted" loudly instead of failing silently.
