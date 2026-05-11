# HSYNC Client — Decisions

Ecosystem ADRs are SSOT in `hsync-common`
([decisions index](https://github.com/albertsdk/hsync-common/blob/main/llm-wiki/decisions.md) →
`hsync-common/docs/internal/adr/`). This page maps each ADR to the **client code** that
implements it, plus client-only operational rules.

## ADR → client code

| ADR | Client implementation |
| --- | --------------------- |
| [0001 server-vs-lan-modes](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0001-server-vs-lan-modes.md) | `run_sync` branch (`main_logic.py:539`); remote stack ([remote-mode.md](modules/remote-mode.md)) vs LAN stack ([lan-mode.md](modules/lan-mode.md)) |
| [0002 lan-master-election](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0002-lan-master-election.md) | `PeerManager._elect_master` (`lan.py:129`) → calls common `elect_master` |
| [0003 tombstone-gossip](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0003-tombstone-gossip.md) | `tombstones.py` per-peer store + `merge_payload`; exchange in `sync.py:566`/`:615` |
| [0004 desktop-subprocess-contract](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0004-desktop-subprocess-contract.md) | `events.py` (`EventEmitter`), `--capabilities-json` (`cli.py:109`) |
| [0005 protocol-versioning](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0005-protocol-versioning.md) | WS handshake (`remote_websocket.py:170`); REST `X-Protocol-Version` (`utils.py`) — hard-fail on mismatch |
| [0017 lan-slave-outbound-event-reliability](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0017-lan-slave-outbound-event-reliability.md) | `LANSlaveFileEventHandler` defer-to-close uploads (`_schedule_create_upload`/`_cancel_create_upload`) + `record_inbound_rename`/`is_inbound_rename_echo` 404 echo cache ([lan-mode.md](modules/lan-mode.md)) |
| [0022 causal-vector-clock-conflict-model](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0022-causal-vector-clock-conflict-model.md) | `_compare_files` (`sync.py:989`) resolves via `resolve_three_way` with vclocks; `_save_conflict_copy` (`sync.py:1228`)on `conflict_download`; upload arbitration mirrored in `arbitration.py` (client) / `hsync_server.arbitration` (server. |
| [0023 corrected-mtime-tie-break](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0023-corrected-mtime-tie-break.md) | `timesync.py` (server RTT probe in `main_logic.py`, background NTP multi-host probe via `start_ntp_clock_offset_probe` called from `lan_handler.run_lan_mode`; `HSYNC_NTP_HOSTS` CSV with `host[:port]` entries); `sync_state._corrected_mtime_of`/`upload_version_fields` 3-tuple (`sync_state.py:170`/`:202`) feeds `corrected_mtime` to upload + delta-apply (`transfer.py:1044`, `delta_transfer.py:84`) and to the mirror master arbitration (`arbitration.py`, `lan_server.py:1406`); `_compare_files` passes corrected mtimes to `resolve_three_way` (`sync.py:1290`). |
| [0025 sqlite-external-state-store](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0025-sqlite-external-state-store.md) | `state_backend.resolve_bucket_folder`; `sync_state.py` / `tombstones.py` (folder UUID + `syncstate.db`); `HSYNC_STATE_DIR` |

LAN transport/security rationale (TOFU pinning, LAN-key policy, threat model) is
in [`lan-protocol.md`](https://github.com/albertsdk/hsync-common/blob/main/docs/lan-protocol.md) and maps to
`pinning.py` + `ssl_utils.py` ([lan-mode.md](modules/lan-mode.md)).

## Client-only operational rules
- **Continuous/daemon mode is the default in both topologies**: the client
  runs indefinitely, retries an unreachable server forever (`run_sync`
  connectivity gate is non-fatal in continuous mode; `remote_connect`
  (`remote_websocket.py`) loops forever with full-jitter exponential
  backoff), and LAN discovery waits indefinitely by default
  (`DISCOVERY_TIMEOUT_DEFAULT = 0`).
  Continuous is the default and `--once` is the single
  one-shot selector in both topologies (in LAN mode it runs one slave sync
  cycle and exits; the master role stays continuous). Rationale: sync
  daemons (Syncthing, Nextcloud/Dropbox clients) never exit on a transient
  connectivity loss; schedulers want the explicit one-shot + exit-code
  contract.

- **One device identity per installation (Syncthing model)**: LAN mode identifies
  with the persistent device ID from `identity.py` (anchored in
  `HSYNC_DEVICE_DIR`), never a per-session random CID; remote mode keeps random
  session CIDs (echo suppression only — the API key is the identity there).
  Consequence: two instances on one host syncing the same bucket are the *same
  device* and cannot discover each other — distinct devices need distinct
  `HSYNC_DEVICE_DIR`s.
- **One process per synced directory**: `InstanceLock` (`identity.py`) refuses a
  second process on the same local path (store races otherwise); the
  flock lives outside the bucket and can never go stale.
- **No protocol forking**: never redefine versions/constants/models locally;
  import from `hsync_common.*`. New wire fields land in common first.
- **No wall-clock trust for direction**: reconciliation uses the causal
  vector-clock matrix (`resolve_three_way`, `sync_state.py`), not mtimes, to
  pick direction / detect conflict. Deletion resurrection is causal too: a
  tombstone whose clock **dominates** the local clock is proof the local copy
  is a stale ancestor (ADR-0003); a concurrent local edit is an intentional
  re-creation and is kept. The wall clock is never consulted.
- **Chronological tie-break on true divergence (ADR-0023)**: a concurrent
  conflict is decided by the offset-corrected `corrected_mtime` advertised
  with the version metadata, with the deterministic SHA-256 fallback when a
  side lacks the field. Offline (no NTP answer) ⇒ offset 0 ⇒ hash fallback;
  sync never blocks.
- **Mandatory TLS in LAN mode**: HTTPS/WSS only; no plaintext fallback
  (`ssl_utils.py`). Master identity is a persistent pinned cert.
- **Write-policy gate**: non-interactive write-capable LAN sync requires a LAN key
  (`_check_lan_write_policy`, `main_logic.py:379`).
- **Mass-deletion guard (§12)**: bulk deletes pause the cycle
  (`_mass_deletion_blocked`, `sync.py:94`) unless explicitly allowed.
- **Per-path serialization (§14)**: all transfers take `utils.path_lock`
  (`utils.py:411`) to avoid concurrent writes to the same file.
- **Failover via periodic staleness sweep**: a hard-crashed master sends no
  mDNS goodbye and no further announce, so election is also re-run on every
  discovery wake and on a slave WebSocket drop
  (`PeerManager.reconcile_stale_peers`, `lan.py:430`), which demotes any peer
  whose heartbeat is older than `HSYNC_PEER_TIMEOUT`. The peer timeout must be
  ≥ 2× `HSYNC_HEARTBEAT_INTERVAL` (enforced in `config.EnvSettings`) so a
  single missed announce never flaps a live peer; the survivor only demotes a
  master whose record is already stale, so a local-link blip never splits the
  group. The deterministic election alone decides the role.
- **Packaging lock-step**: any `HSYNC_*` flag/default change requires the
  synchronized `packaging/wd-os5` update (see `CLAUDE.md`).
- **Containerized mksapkg**: `mksapkg-OS5` runs inside the
  `hsync-wdos5-mksapkg` image (`packaging/wd-os5/Dockerfile.mksapkg`) via
  `_wdos5_run_mksapkg` (`compile.sh:947`), not on the host — the prebuilt tool
  needs `libxml2.so.2` + OpenSSL 1.1.1 `bf-cbc`, absent on modern hosts.
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
- **`install.sh --version latest`**: the installer resolves the latest stable
  release by following the `github.com/<repo>/releases/latest` redirect with
  `curl -fsSIL -w %{url_effective}` (no GitHub API, no rate limit; drafts and
  pre-releases are excluded by GitHub), validates the `vX.Y.Z` tag, then proceeds
  exactly as for a pinned version (`install.sh`, `resolve_latest_version`).
  User docs point at `releases/latest/download/install.sh` so the bootstrap
  needs no version either.
- **Windows-portable `compile.sh`**: PyInstaller path is OS-aware
  (`.venv/Scripts` vs `.venv/bin`, `compile.sh:92`) and `--strip` is omitted on
  Windows (`STRIP_FLAG`, `compile.sh:99`) since binutils is unavailable there.

> New decisions affecting multiple repos: add the ADR in `hsync-common` first,
> then map it here.
