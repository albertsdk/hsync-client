# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- A `chmod` or a `touch` was silently lost on Linux with watchdog 7 (the
  metadata change arrived as a dedicated `attrib` event the client never read),
  so permissions and mtime converged only at the periodic resync.
- A server or peer `429` no longer drops the file: the transfer is retried,
  honouring the server's `Retry-After` within the per-file retry bound.

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
