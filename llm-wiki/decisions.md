# hsync-client — Decisions

Ecosystem ADRs are SSOT in `hsync-common`
([decisions index](../../hsync-common/llm-wiki/decisions.md) →
`hsync-common/docs/adr/`). This page maps each ADR to the **client code** that
implements it, plus client-only operational rules.

## ADR → client code

| ADR | Client implementation |
| --- | --------------------- |
| [0001 server-vs-lan-modes](../../hsync-common/docs/adr/0001-server-vs-lan-modes.md) | `run_sync` branch (`main_logic.py:459`); remote stack ([remote-mode.md](modules/remote-mode.md)) vs LAN stack ([lan-mode.md](modules/lan-mode.md)) |
| [0002 lan-master-election](../../hsync-common/docs/adr/0002-lan-master-election.md) | `PeerManager._elect_master` (`lan.py:129`) → calls common `elect_master` |
| [0003 tombstone-gossip](../../hsync-common/docs/adr/0003-tombstone-gossip.md) | `tombstones.py` per-peer store + `merge_payload`; exchange in `sync.py:566`/`:615` |
| [0004 desktop-subprocess-contract](../../hsync-common/docs/adr/0004-desktop-subprocess-contract.md) | `events.py` (`EventEmitter`), `--capabilities-json` (`cli.py:76`) |
| [0005 protocol-versioning](../../hsync-common/docs/adr/0005-protocol-versioning.md) | WS handshake (`remote_websocket.py:275`); REST `X-Protocol-Version` (`utils.py`) — hard-fail on mismatch |
| [0006 master-authoritative-conflict-model](../../hsync-common/docs/adr/0006-master-authoritative-conflict-model.md) | `_compare_files` + base via `sync_state.py`; `_save_conflict_copy` (`sync.py:905`) |

LAN transport/security rationale (TOFU pinning, LAN-key policy, threat model) is
in [`LAN_PROTOCOL.md`](../../hsync-common/docs/LAN_PROTOCOL.md) and maps to
`pinning.py` + `ssl_utils.py` ([lan-mode.md](modules/lan-mode.md)).

## Client-only operational rules

- **No protocol forking**: never redefine versions/constants/models locally;
  import from `hsync_common.*`. New wire fields land in common first.
- **No wall-clock trust for direction**: reconciliation uses the persisted
  `base_hash` (`sync_state.py`), not mtimes, to pick direction / detect conflict.
  Mtime is only a tolerance hint (`MTIME_TOLERANCE`, `sync.py:650`).
- **Mandatory TLS in LAN mode**: HTTPS/WSS only; no plaintext fallback
  (`ssl_utils.py`). Master identity is a persistent pinned cert.
- **Write-policy gate**: non-interactive write-capable LAN sync requires a LAN key
  (`_check_lan_write_policy`, `main_logic.py:344`).
- **Mass-deletion guard (§12)**: bulk deletes pause the cycle
  (`_mass_deletion_blocked`, `sync.py:94`) unless explicitly allowed.
- **Per-path serialization (§14)**: all transfers take `utils.path_lock`
  (`utils.py:411`) to avoid concurrent writes to the same file.
- **Packaging lock-step**: any `HSYNC_*` flag/default change requires the
  synchronized `packaging/wd-os5` update (see `CLAUDE.md`).
- **Containerized mksapkg**: `mksapkg-OS5` runs inside the
  `hsync-wdos5-mksapkg` image (`packaging/wd-os5/Dockerfile.mksapkg`) via
  `_wdos5_run_mksapkg` (`compile.sh:947`), not on the host — the prebuilt tool
  needs `libxml2.so.2` + OpenSSL 1.1.1 `bf-cbc`, absent on modern hosts. The
  former host `openssl bf-cbc` shim was removed.
- **Host-uid container builds**: containerized build steps drop to the host
  `uid:gid` so artifacts under `build/`/`dist/` stay caller-owned — otherwise
  root-owned output breaks the `rm -rf build` cleanup of a later native build
  (`cleanup_temp_files`). Covers WD OS5 (`_wdos5_run_mksapkg`; PyInstaller
  `_run_pyinstaller_in_docker` (`compile.sh:1000`)) and `deb-all`'s in-container
  PyInstaller (`compile.sh:742`, with `HOME=/tmp` for the poetry/PyInstaller
  caches). The `deb-all` `dpkg-deb` step stays **root** (so the `.deb`'s
  internal `/usr`,`/etc` are owned `root:root`) and instead `chown`s the
  host-mounted stage + produced `.deb` back to the caller afterwards
  (`compile.sh:849`); the archive's captured ownership is unaffected.
- **Tag-driven release** (`.github/workflows/release.yml`): pushing a `vX.Y.Z`
  tag gates on lint+test, then builds native binaries on per-OS matrix runners
  (`linux-amd64`, `linux-arm64`, `darwin-amd64`, `darwin-arm64`, `windows-amd64`
  — PyInstaller cannot cross-compile) plus the Linux-only `deb-all`/`wd-os5-all`
  packages, and publishes them with `SHA256SUMS` to the GitHub Release. macOS
  binaries are unsigned (documented `xattr` workaround in `docs/installation.md`).
- **Windows-portable `compile.sh`**: PyInstaller path is OS-aware
  (`.venv/Scripts` vs `.venv/bin`, `compile.sh:92`) and `--strip` is omitted on
  Windows (`STRIP_FLAG`, `compile.sh:99`) since binutils is unavailable there.

> New decisions affecting multiple repos: add the ADR in `hsync-common` first,
> then map it here.
