# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Log label format normalized to spaces**: multi-word action labels now print
  with spaces (`[NO MASTER]`, `[TO DOWNLOAD]`, `[FINGERPRINT CHANGED]`, …)
  instead of underscores, matching the rest of the label set and the shared
  `hsync_common.labels` SSOT. **Breaking** for log parsers that match the old
  underscored strings; the Android mirror, tests and docs are updated in lockstep.
- **Unified log-label colorization via shared SSOT**: the client's local
  `ActionLabel` enum and `LABEL_STYLES`/`_colorize_labels` helpers have been
  removed in favor of `hsync_common.labels`. Every bracketed log label
  (`[DOWNLOAD]`, `[UPLOAD]`, `[SECURITY]`, `[MASTER CONFLICT]`, `[NO MASTER]`,
  `[AUTH REQUIRED]`, the LAN `[CLIENT *]`/`[LAN *]` tags, …) is now emitted
  through `ActionLabel` and colorized identically on the client and the server.
  The watchdog handlers and the inbound event dispatcher now share a single
  `resolve_label` helper, so `[IGNORE DEL]` (previously emitted as a stray
  `[IGNORE]`) and the LAN-prefixed ignore/delete tags are colorized correctly.
  The `--legend` output lists the full label set.

### Added
- **Auto-tuning of timeout, retry & concurrency**: the client now
  auto-detects the runtime environment (network latency, bandwidth, storage
  type) and derives adaptive defaults for HTTP timeout, retry backoff, and
  transfer concurrency. Probes run at startup and per sync cycle (concurrency
  only); the resolution chain is `CLI > env > autotune > default`. Kill switch:
  `--no-autotune` / `HSYNC_AUTOTUNE=false`. New env vars: `HSYNC_AUTOTUNE`,
  `HSYNC_HTTP_TIMEOUT`, `HSYNC_RECONNECT_DELAY`, `HSYNC_TRANSFER_RETRY_MIN_WAIT`,
  `HSYNC_TRANSFER_RETRY_MAX_WAIT`. New module: `hsync_client/autotune.py`.
- **`install.sh --version latest`**: the installer now accepts `latest` as the
  version value, resolving the newest stable GitHub Release automatically
  (via the `releases/latest` redirect; drafts and pre-releases are excluded),
  so users no longer need to know a release tag in advance. The installation
  guide now points at `releases/latest/download/install.sh` for a
  version-free bootstrap.
- **Installer script** (`install.sh`): a self-contained installer for Linux and
  macOS that detects the host platform, downloads the correct prebuilt binary
  from GitHub Releases, verifies its SHA-256 checksum, installs it to
  `~/.local/share/hsync-client`, and creates a symlink in `~/.local/bin`.
  Published as `install.sh` in every release. Clears the macOS
  quarantine flag automatically.
- **Unified archive naming**: prebuilt binary archives now use the
  `hsync-client-<version>-<target>` prefix (was `hsync-<version>-<target>`),
  matching the Debian package naming and the `hsync-client` project identity.
- **Delta transfer (rsync-style block sync)**: for large files with small
  changes, only the changed blocks are transferred instead of the whole file.
  Delta transfer is attempted automatically when the file is ≥ 256 KB (server
  mode) or ≥ 1 MB (LAN mode), a base hash exists in `.hsyncstate`, and both
  peers support the delta capability. Upload delta flow: GET signatures →
  rolling match → POST apply. Download delta flow: POST signatures → GET data
  → rebuild. Any failure falls back to full transfer transparently.
  Configurable via `HSYNC_DELTA_ENABLED` (default `true`),
  `HSYNC_DELTA_MIN_FILE_SIZE`, and `HSYNC_DELTA_MIN_FILE_SIZE_LAN`.
  CLI flags: `--no-delta`, `--delta-min-file-size`, `--delta-min-file-size-lan`.
- **TOCTOU re-hash protection**: for files smaller than 10 MB, the delta upload
  path now performs a full SHA-256 re-hash after the rolling scan to catch
  content changes that mtime+size alone miss (e.g., rapid edits within the same
  second). If the file changed during the scan, the delta is aborted and the
  caller falls back to a full transfer.
- **Runtime environment awareness**: at startup the client logs a one-line
  summary of the detected runtime environment (container runtime, cgroup
  memory/CPU limits, TTY state) via `hsync_common.runtime.runtime_summary()`.
  Colored output now also honors the standard [`NO_COLOR`](https://no-color.org)
  environment variable, in addition to `--no-color` and the automatic non-TTY
  detection. In LAN mode, running inside a container logs a warning that mDNS
  peer discovery cannot cross container bridge/NAT networking and suggests
  host networking (`docker run --network host`).

### Changed
- **CLI flag rename**: `--max-download-size` is renamed to
  `--max-inbound-size`, and the config key `MAX_DOWNLOAD_SIZE` to
  `MAX_INBOUND_SIZE` (env `HSYNC_MAX_DOWNLOAD_SIZE_MB` →
  `HSYNC_MAX_INBOUND_SIZE_MB`). The old name was misleading: the cap applies
  to **both** downloads and LAN-master received uploads (the anti
  decompression-bomb bound in `inbound_size_cap`), not just downloads. The
  new name is direction-neutral, consistent with the `inbound_size_cap`
  helper and the "Max inbound size" label already used in the WD README.

### Fixed
- **Spurious "Cannot delete"/"Not found" warnings on LAN delete echo**: when a
  slave deleted a directory locally and notified the master, the master
  broadcast the resulting `deleted` events (for the directory and its
  `.hsynckeep` sentinel) back to the originator. Echo suppression missed
  because the master's `mark_inbound_origin` cid gate is a security measure
  that only propagates the originator's CID on authenticated (`LAN_KEY`-set)
  requests, and because `rmtree` removes a directory's children without
  individually marking them. The slave then ran `_apply_delete` on paths it had
  already removed, emitting WARNING noise for a no-op. An inbound `deleted`
  event for a path already absent locally is now logged at DEBUG (benign echo),
  mirroring the rename 404-echo degradation and the master's "File already
  deleted" success branch. The deletion itself always succeeded (HTTP 200); no
  data was lost.
- **Streaming upload of binary files**: the LAN master's multipart parser
  rejected uploads with "multipart header line too long" for any file whose
  body exceeded 8 KB (mp3, flac, …). The parser's `_read_line` checked the
  ENTIRE buffer size against the 8 KB header-line cap, but a single
  `_read_more` reads 64 KB at once — so even valid short CRLF-terminated
  header lines in a 64 KB buffer triggered the false rejection. The cap is
  now checked only while no CRLF is in the buffer (i.e. a single
  unterminated line is genuinely too long), not after a chunk read that
  happens to contain multiple valid lines plus lookahead.
- **Upload 4xx loguru crash**: the response body of a server 4xx rejection
  (e.g. `{"error": "..."}`) contains curly braces that loguru interprets as
  format placeholders, crashing the log handler with `KeyError`. The body
  and exception message are now escaped via `escape_loguru_markup` before
  logging.
  401/403/409) now logs the response body (e.g. "Checksum mismatch", "Invalid
  upload: ...") and raises `PermanentUploadError` instead of calling
  `raise_for_status()`, which previously discarded the body and made the
  rejection undiagnosable. `PermanentUploadError` is not a `RequestException`,
  so the transfer retry decorator no longer retries a deterministic 4xx three
  times (it now fires exactly once). The `transfer_failed` UI event message
  includes the server's diagnostic body.
- **LAN master log label**: the master-side file-ready broadcast log line now
  reads `[LAN MODIFY] "<path>" (closed|modified)` instead of the opaque
  `[LAN FILE] "<path>"`. The new label matches the `modified` event actually
  broadcast (and the `[LAN MODIFY]` label already used for directory modify
  events), and the `(closed)`/`(modified)` trigger suffix distinguishes the
  two `IN_CLOSE_WRITE` fires a GNOME/gvfs safe-save produces. Logging-only;
  no change to what is broadcast to slaves.
- **Editor safe-save handling**: when an editor safe-saves (writes to a temp
  file like `.goutputstream-*` then atomically renames it over the final
  file), the watcher now reframes the rename as a content `modified` of the
  destination (logged `[LAN MODIFY] "<path>" (safe-save)`) instead of
  broadcasting a misleading `renamed` event. A two-stage content-aware dedup
  in `_process_file_ready` suppresses spurious broadcasts: (1) a stat pre-check
  `(mtime, size)` skips without hashing when the file hasn't changed (e.g.
  an editor open/close with no write), and (2) a hash compare skips when the
  content matches the last broadcast even if mtime was touched. This replaces
  the previous time-based debounce, which was counterproductive in the
  no-op-close-before-safe-save scenario (it suppressed the real rename while
  keeping the false-positive close). The hash is already computed by
  `File.get`, so the comparison adds zero I/O cost. The `.goutputstream-*`
  pattern is now in the built-in `DEFAULT_IGNORE_PATTERNS` (hsync-common) so
  GNOME GIO temp files are never synced.

### Security
- **Permanent download failure skip-set**: a download that fails
  permanently (e.g. declared size exceeds the configured cap) now raises
  `PermanentDownloadError`, which the sync caller catches, records in a
  per-cycle skip-set (keyed by `filepath|sha256`), and emits a
  `transfer_failed` UI event. The file is no longer retried on every resync
  tick within the same cycle. Also fixed a latent `NameError` (the exception
  was raised but never imported in `transfer.py`) and an `IndentationError`
  on the compressed-download branch.
- **LAN master thread cap**: the LAN master HTTP server now caps
  concurrent request threads with a `BoundedSemaphore` (default 32) instead
  of spawning an unbounded thread per connection, preventing FD/memory
  exhaustion under a flood of peer requests. The compressed-download branch
  now falls back to a sane absolute cap when `MAX_DOWNLOAD_SIZE` is not
  configured.
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
- **Non-interactive run detection**: a run is now considered unattended when no
  interactive terminal is attached (container without `-t`, systemd, cron,
  pipes), not only when `--yes` is passed. Two consequences: the deletion-risk
  confirmation fails fast with a clear error and exit code 1 instead of
  blocking on an unreadable prompt (previously it cancelled silently with exit
  0 via `EOFError`); and the LAN write policy now requires `HSYNC_LAN_KEY` for
  write-capable sync in such runs even without `--yes`.
- **Deps**: Updated runtime and development dependencies: typer 0.27.0,
  zeroconf 0.150.0, aiofiles 25.1.0 (via hsync-common), cffi 2.1.0,
  charset-normalizer 3.4.9, coverage 7.15.2, filelock 3.31.2, librt 0.13.0,
  mypy 2.3.0, platformdirs 4.10.1, ruff 0.15.22, virtualenv 21.6.1.
  Constraint ranges for `typer` and `zeroconf` widened accordingly.
- **Docs**: Note in the user documentation (`README.md`, `faq.md`) that the
  desktop GUI (`hsync-desktop`) and Android app (`hsync-android`) are still
  under active development and not yet ready for production use.
- **Docs**: The `README.md` Quick Start now shows both run modes side by side:
  the existing Server Mode command and a new LAN Mode one-liner
  (`hsync -b <bucket> -p <path> --lan-mode`), so the peer-to-peer path — the
  simplest to try, since it needs no server or API key — is visible on the
  project's landing page instead of only in `docs/`.
- **Docs**: Expanded the client FAQ (`docs/faq.md`) with six new entries:
  basic concepts (bucket, server/client, master/slave roles), why the periodic
  resync exists and why the file-system watcher can miss events, whether
  hidden files and directories are synced by default, running multiple hsync
  instances on the same machine, how to update to a new version, and what the
  `.hsyncstate` / `.hsynctombstones` side-car files are.
- **Docs**: Added a "Key terms" glossary table to `docs/getting-started.md`
  (bucket, Server/LAN Mode, master/slave, watch mode, side-cars) so the core
  vocabulary is defined in one place before the first-run walkthrough.
- **Docs**: The `README.md` Quick Start and `docs/installation.md` now show a
  one-command quick install (`curl -fsSL … | bash`) as the primary path, with
  the verified multi-step download as an alternative.
- **Release**: The installer asset is now published as `install.sh` (was
  `install-<version>.sh`) since the version is already in the release URL
  path. The `release.yml` workflow and all documentation URLs have been
  updated accordingly.
- **Docs**: Updated `CONTRIBUTING.md` — the test-structure table now reflects
  the actual 50+ test files organised by area (sync, transfer, WebSocket,
  LAN, security, config, delta, models), and the release process documents
  the tag-driven `release.yml` workflow (push `vX.Y.Z` → CI auto-build +
  publish) and the `bump-version.sh` wrapper instead of the old manual
  upload steps.
- **Perf**: `_record_synced_base()` no longer calls `flush()` after every
  transfer. The merged sync-state is now persisted only once at the end of
  the sync cycle (in `sync_state.flush()`), avoiding redundant per-file
  rewrites of the entire `.hsyncstate` side-car. This significantly reduces
  disk I/O when syncing many small files.
- **Docs**: Corrected and completed the user documentation tables in
  `docs/configuration.md`, `docs/usage.md`, `docs/sync-flow.md`,
  `docs/faq.md`, `docs/debian-package.md` and `docs/internal/developer.md`:
  added the missing `HSYNC_TOMBSTONE_TTL_DAYS` env var and `--no-autotune`
  CLI flag, fixed the auto-tuned defaults for `--timeout`/`--retry-delay`,
  added the missing log labels (`[CONFLICT]`, `[GUARD]`, `[TOMBSTONE]`,
  `[SYNCED]`, `[TO DOWNLOAD]`, `[TO UPLOAD]`, `[TO DELETE]`, `[TO RENAME]`,
  `[CLIENT NEW]`, `[LAN FILE]`), corrected the retry parameters in the
  sync-flow diagram (3 attempts, 10 s max), fixed the `HSYNC_RECONNECT_DELAY`
  env var name, corrected the stdin command names (`SET_SYNC_DIRECTION`,
  `SYNC`, `PREVIEW`, `SET_BANDWIDTH`), fixed the editor swap pattern
  (`.*.sw?`), and clarified that `HSYNC_LAN_HTTPS` is always on and not
  configurable.

### Fixed
- **LAN slave uploads**: eliminate duplicate `[CLIENT UP]` / `[UPLOAD]` for a
  single file creation and avoid uploading half-written files. A non-empty
  regular file observed at `on_created` is no longer uploaded immediately;
  its upload is deferred for a short window (`STABILITY_DELAY_ON_CLOSED`)
  so `on_closed` (`IN_CLOSE_WRITE`, the reliable fd-closed trigger) can
  cancel it and take precedence with the fast stability re-check. The
  deferred timer is the fallback for files that arrive already complete with
  no writer fd (atomic rename from outside the watch tree, hard-links,
  `mv` from a sibling directory). `on_deleted` / `on_moved(src)` / shutdown
  cancel pending timers so stale uploads never fire.
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
