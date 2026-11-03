# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The p2p master election prefers a node that confirms an externally dialable
  endpoint (`inbound`), and a node defers advertising itself as the serving
  master until the rendezvous answered once, so a relay-only peer no longer
  wins the role and forces the group onto the relay.

### Changed

- The p2p data-relay tunnel multiplexes every loopback connection over one relay
  session (one `relay_connect` per tunnel, private stream framing), so the sync
  cycle no longer spends a pairing token per REST/WebSocket connection.
- A relayed slave caps its concurrent transfers so the multiplexed session's
  flow-control window is not swamped.
- The relay keepalive runs at a third of the node's QUIC idle, so one lost ping
  no longer tears down the tunnel and every transfer with it.

### Fixed

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
