# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- **Inbound path validation**: every path received from a remote peer
  (central server events, LAN master events, remote listings) is validated
  before any local filesystem operation: traversal (`..`), absolute paths,
  drive letters, backslashes, forbidden/control characters and internal
  engine files (`.hsyncignore`, `.hsyncstate`, `.hsynctombstones`, `*.part`)
  are rejected, logged and surfaced as the new `security_rejected` UI event.
  Symlink events also require the target to stay inside the bucket
- **LAN master confinement**: download/delete/rename/upload endpoints now
  validate and confine peer-supplied paths with
  `resolve()` + `relative_to()` instead of `startswith`, closing the
  sibling-directory bypass (e.g. bucket `/data/sync` vs `/data/sync-evil`)
- **Decompression-bomb protection**: compressed downloads are
  stream-decompressed straight to disk capped at the declared file size (the
  payload is never fully buffered in memory); LAN-master compressed uploads
  abort when the decompressed bytes exceed the declared `size`
- **Absolute inbound size cap (`--max-download-size`)**: the declared size
  alone was not a safe bound — a hostile server/master could declare an
  arbitrary size and expand a decompression bomb up to it on disk before the
  SHA-256 check discarded the content. Downloads and received LAN uploads are
  now capped at `min(declared size, MAX_DOWNLOAD_SIZE)`; uncompressed bodies
  and LAN uploads with no declared size are capped at `MAX_DOWNLOAD_SIZE`
  outright. Defaults to 10 GB (mirroring the server's `max_upload_size`);
  `0` restores the declared-size-only behaviour. The cap policy is a single
  shared implementation (`utils.inbound_size_cap`) for downloads and
  LAN-master uploads alike
- **Upload integrity on the LAN master**: the `sha256sum` form field is
  mandatory and verified before the atomic rename, so corrupt or
  attacker-controlled content is never committed nor propagated to slaves
- **Permissions hardening**: setuid/setgid/sticky bits are stripped from
  peer-supplied permission strings before being applied locally
- **Tombstone gossip hardening**: oversized POST bodies are rejected, and the
  store validation now lives in `hsync-common` (sha256-hex keys, future
  timestamps beyond 300 s rejected, 100 000-entry cap)
- **Wire-supplied mtimes** are clamped to `now + 5 min` on the LAN master
  (conflict-engineering mitigation); WebSocket connections declare an
  explicit 4 MiB `max_size`
- **Inbound event gate validates every path key**: an event payload carrying
  both `filepath` and `path` is now rejected unless *both* validate
  (previously only the first present key was checked)
- **LAN master echo-suppression CID format-validated**: the `cid` on
  upload/delete/rename is honoured only when it passes `is_valid_cid` and the
  request is authenticated (`LAN_KEY` set)
- **Constant-time checksum comparisons**: SHA-256 integrity checks now use
  `hmac.compare_digest` (download verification, LAN-master upload check);
  wire-supplied digests are shape-validated first (`is_sha256_hex`), since
  `compare_digest` raises `TypeError` on non-ASCII input
- **Event application confined against in-bucket symlink escapes**: inbound
  delete/rename/symlink events (and remote-triggered mkdir/metadata updates)
  now resolve targets with `resolve_within`, so a symlink *inside* the bucket
  (e.g. `docs -> /home/user`) can no longer redirect a remote-triggered
  delete/rename/symlink/create outside it — the same confinement downloads
  already had
- **LAN multipart parser memory caps**: form-field values (64 KiB) and
  part-header lines (8 KiB) are now hard-capped — previously a single giant
  "field" was buffered unboundedly in master RAM
- **LAN `.part` uploads opened with `O_NOFOLLOW`** (shared helper in
  `utils.py`, same as downloads): a pre-existing/raced `.part` symlink can no
  longer redirect a peer's streamed upload outside the bucket
- **Robust reconciliation against hostile listings/events**: one malformed
  remote listing entry (bogus `size`/mtime types, unreadable stat) no longer
  stalls the whole sync cycle — the entry is skipped for comparison but
  still counts as remotely present, so it can never trigger a spurious
  delete (download-only) or re-upload; one malformed server event no longer
  kills the remote WebSocket loop (per-message isolation, as the LAN loop
  already had); garbage resumable `offset` values from the server are
  clamped back to a fresh upload
- **Pin store written with `0600` from creation**: the TOFU pin file no
  longer has a window where trusted-peer fingerprints are world-readable

### Removed
- **LAN master**: dead `_parse_multipart` helper (superseded by the streaming
  multipart parser)
- **Utils**: duplicated `is_valid_filename` (use the `hsync-common` SSOT)

### Changed
- **Deps**: Updated runtime and development dependencies: typer 0.27.0,
  zeroconf 0.150.0, aiofiles 25.1.0 (via hsync-common), cffi 2.1.0,
  charset-normalizer 3.4.9, coverage 7.15.2, filelock 3.31.2, librt 0.13.0,
  mypy 2.3.0, platformdirs 4.10.1, ruff 0.15.22, virtualenv 21.6.1.
  Constraint ranges for `typer` and `zeroconf` widened accordingly.
- **Docs**: Note in the user documentation (`README.md`, `faq.md`) that the
  desktop GUI (`hsync-desktop`) and Android app (`hsync-android`) are still
  under active development and not yet ready for production use.
- **Perf**: `_record_synced_base()` no longer calls `flush()` after every
  transfer. The merged sync-state is now persisted only once at the end of
  the sync cycle (in `sync_state.flush()`), avoiding redundant per-file
  rewrites of the entire `.hsyncstate` side-car. This significantly reduces
  disk I/O when syncing many small files.

### Fixed
- **Build**: Fix CI "Build .deb and WD OS5 packages" failure in the CPython
  source-compile step where compilation aborted with
  `error: 'PY_TIME_T_MIN' undeclared` in `Python/pytime.c`. Under qemu-user
  emulation the CPython `configure` runtime probe for `sizeof(time_t)` is
  unreliable and could leave `SIZEOF_TIME_T` empty in `pyconfig.h`, so neither
  `PY_TIME_T_MIN`/`PY_TIME_T_MAX` branch was defined. Both builder images now
  measure `sizeof(time_t)` with a small target-native C program and seed
  `ac_cv_sizeof_time_t` into configure's cache, and log the resulting
  `SIZEOF_TIME_T` for diagnostics.
- **Build**: Fix an earlier CI "Build .deb and WD OS5 packages" failure in the
  CPython source-compile step. The deprecated `--with-system-ffi` configure
  flag (removed in Python 3.12) has been dropped, `make -j$(nproc)` is now
  capped at `-j4` to avoid OOM kills under qemu-user emulation on GitHub
  Actions runners (arm64/armhf), and the python.org tarball download is
  retried up to 3 times to survive transient CDN failures. The same
  `make -j4` cap is applied to the static OpenSSL 3 build in the WD OS5
  builder image.

## [1.0.0] — 2025-12-xx

### Added
- Initial release: bidirectional file synchronization client
- Server Mode: connect to a central HSYNC Server via REST + WebSocket
- LAN Mode (P2P): peer-to-peer sync with mDNS discovery and master election
- Real-time file system monitoring (watchdog)
- Parallel file transfers (configurable concurrency)
- SHA-256-based file integrity verification
- Zstandard (zstd) transfer compression
- Symbolic link synchronization support
- `.hsyncignore` (gitignore-style) filtering
- Bandwidth limiting (token bucket)
- Compilable to standalone binary (PyInstaller)
- Prebuilt binaries for Linux (amd64, arm64), macOS (amd64, arm64), Windows (amd64)
- `.deb` package with systemd service
- WD My Cloud OS5 `.bin` installer
- Runtime stdin command protocol (direction change, resync, exit)
- Three-way merge conflict resolution with `.hsyncstate` version store
- Mass deletion guard
- Event queue with exponential backoff retry
- Comprehensive documentation

[1.0.0]: https://github.com/albertobasso/hsync-client/releases/tag/v1.0.0
