# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- The p2p data-relay bridge no longer drops a long unidirectional transfer:
  the idle watchdog is counted per session (any direction), not per direction,
  so a slow upload whose reply arrives only at the end is no longer killed
  mid-flight.
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
- A relayed HTTPS connection no longer dies before its first byte: the
  per-connection relay handler unpacked three values from the node probe
  instead of the four it answers, so the loopback endpoint answered an SSL EOF
  and a relayed sync could never start.
