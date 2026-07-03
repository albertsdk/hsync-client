# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
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
