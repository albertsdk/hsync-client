# HSYNC Client — Decisions

Ecosystem ADRs are SSOT in `hsync-common`
([decisions index](https://github.com/albertsdk/hsync-common/blob/main/llm-wiki/decisions.md) →
`hsync-common/docs/internal/adr/`). This page maps each ADR to the **client code** that
implements it, plus client-only operational rules.

## ADR → client code

| ADR | Client implementation |
| --- | --------------------- |
| [0001 server-vs-p2p-modes](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0001-server-vs-p2p-modes.md) | `run_sync` branch (`main_logic.py::run_sync`); remote stack ([remote-mode.md](modules/remote-mode.md)) vs P2P stack ([p2p-mode.md](modules/p2p-mode.md)) |
| [0002 p2p-master-election](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0002-p2p-master-election.md) | `PeerManager._elect_master` (`p2p_peers.py::PeerManager._elect_master`) → calls common `elect_master` |
| [0003 tombstone-gossip](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0003-tombstone-gossip.md) | `tombstones.py` per-peer store + `merge_payload`; exchange in `sync.py::sync` |
| [0004 desktop-subprocess-contract](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0004-desktop-subprocess-contract.md) | `events.py` (`EventEmitter`), `--capabilities-json` (`cli.py::capabilities_json_callback`) |
| [0005 protocol-versioning](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0005-protocol-versioning.md) | WS handshake (`remote_websocket.py::remote_connect`); REST `X-Protocol-Version` (`utils/http.py`) — hard-fail on mismatch |
| [0017 p2p-slave-outbound-event-reliability](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0017-p2p-slave-outbound-event-reliability.md) | `P2PSlaveFileEventHandler` defer-to-close uploads (`_schedule_create_upload`/`_cancel_create_upload`) + `record_inbound_rename`/`is_inbound_rename_echo` 404 echo cache ([p2p-mode.md](modules/p2p-mode.md)) |
| [0022 causal-vector-clock-conflict-model](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0022-causal-vector-clock-conflict-model.md) | `_compare_files` (`sync.py::_compare_files`) resolves via `resolve_three_way` with vclocks; `_save_conflict_copy` (`sync.py::_save_conflict_copy`)on `conflict_download`; upload arbitration mirrored in `arbitration.py` (client) / `hsync_server.arbitration` (server. |
| [0023 corrected-mtime-tie-break](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0023-corrected-mtime-tie-break.md) | `timesync.py` (server RTT probe in `main_logic.py`, background NTP multi-host probe via `start_ntp_clock_offset_probe` called from `p2p_handler.run_p2p_mode`; `HSYNC_NTP_HOSTS` CSV with `host[:port]` entries); `sync_state._corrected_mtime_of`/`upload_version_fields` 3-tuple (`sync_state.py::_corrected_mtime_of`/`sync_state.py::upload_version_fields`) feeds `corrected_mtime` to upload + delta-apply (`transfer.py::_upload_version_fields`, `delta_transfer.py::_resolve_delta_version_fields`) and to the mirror master arbitration (`arbitration.py::arbitrate_upload`, `p2p_server.py::P2PServer`); `_compare_files` passes corrected mtimes to `resolve_three_way` (`sync.py::_compare_files`). |
| [0025 sqlite-external-state-store](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0025-sqlite-external-state-store.md) | `state_backend.resolve_bucket_folder`; `sync_state.py` / `tombstones.py` (folder UUID + per-folder DB under the device dir); `HSYNC_STATE_DIR` / `HSYNC_STATE_DB` |
| [0030 p2p-seeder-capacity-advertisement](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/0030-p2p-seeder-capacity-advertisement.md) | Seeder advertisement (`p2p_websocket.py` `p2p_websocket_client` handshake), master session storage (`p2p_inventory.py` `begin_session(serve_slots=…)`/`session_serve_slots`), candidate echo (`p2p_sources.py` `P2PSourceService`), requester admission + busy-as-scheduling (`p2p_sources.py` `acquire_slot`/`release_slot`, `transfer.py` `_parse_retry_after`), seeder `Retry-After` (`p2p_server.py`) |

p2p transport/security rationale (TOFU pinning, p2p-key policy, threat model) is
in [`p2p-protocol.md`](https://github.com/albertsdk/hsync-common/blob/main/docs/p2p-protocol.md) and maps to
`pinning.py` + `ssl_utils.py` ([p2p-mode.md](modules/p2p-mode.md)).

## Client-only operational rules
- **Continuous/daemon mode is the default in both topologies**: the client
  runs indefinitely, retries an unreachable server forever (`run_sync`
  connectivity gate is non-fatal in continuous mode; `remote_connect`
  (`remote_websocket.py`) loops forever with full-jitter exponential
  backoff), and p2p discovery waits indefinitely by default
  (`DISCOVERY_TIMEOUT_DEFAULT = 0`).
  Continuous is the default and `--once` is the single
  one-shot selector in both topologies (in p2p mode it runs one slave sync
  cycle and exits; the master role stays continuous **while it has live
  peers**). The finite discovery window ends a one-shot run even when the
  local role was resolved as MASTER at discovery start: with no live peer the
  discovery loop exits with `ExitCode.P2P_NO_PEER` (5)
  (`exitcodes.py::ExitCode`) instead of serving as a peer-less master, so a
  scheduled `--once` run always terminates unless `--discovery-timeout 0`
  explicitly waits forever. Rationale: sync
  daemons (Syncthing, Nextcloud/Dropbox clients) never exit on a transient
  connectivity loss; schedulers want the explicit one-shot + exit-code
  contract.

- **A retired server role never answers a pooled connection (role handover)**:
  the slave seeder and the master share the client's single listener, so on a
  role change `stop_serving()` retires the role — the listener refuses any
  request (503) while no role is served and the publish gate closes — while
  `P2PWebSocketServer.stop()` closes the live control-plane clients. Rationale: a
  keep-alive connection established while the process was a seeder survives the
  handover and would otherwise be served by the retired seeder handler — a
  master download (`filename`+`cid`, no `sha256`) was rejected as an "Invalid
  seeder query" (400) and failover never resumed sync.
  Regression: `tests/test_p2p_server_keepalive.py::TestRoleHandover` (unit) and
  `tests/test_bb_p2p_advanced.py::TestMasterFailover::test_master_death_reelects_successor` (black-box).

- **A node that lost the election while starting up yields instead of claiming
  readiness**: `_run_as_master` advertises `server_ready` only after
  re-checking `peer_manager.is_master()`; if a peer won during the startup
  window (typically a `--force-master` peer that joined after this node
  self-elected but before its watcher/WebSocket server were up) it returns
  early and the outer loop's yield path retires the role and revokes the
  incumbency. Rationale: `server_ready` out-ranks `force_master` in the shared
  election, so an unconditional advertisement made the loser the incumbent
  again — two nodes served the same bucket, and a later crash-failover could
  never converge on a single successor because the survivor had never stopped
  serving (and the double server broke the "exactly one successor" invariant).
  A node that is *already* serving never re-enters that block, so the
  "incumbent keeps the role" rule is unchanged.
  Regression:
  `tests/test_p2p_handler_advanced.py::TestRunAsMaster::test_master_lost_while_starting_up_does_not_advertise_readiness` (unit) and
  `tests/test_bb_p2p_advanced.py::TestMasterFailover::test_quiescent_slaves_failover_to_one_successor` (black-box).

- **The P2P transport is a single TLS port (HTTPS REST + WSS)**: the master runs
  one `aiohttp` listener (`P2PServer`) with a bounded executor for the blocking
  data plane, and the WSS control plane is the canonical
  `/api/v1/buckets/{bucket}/events/{cid}` route on the same port. The realtime
  channel is therefore bound atomically with the data plane, so a master never
  advertises readiness without a live event channel. Regression:
  `tests/test_p2p_single_port.py`.

- **Guard abort diagnostics are synchronous**: the loguru sinks are
  `enqueue=True`, so a message written immediately before `raise SystemExit`
  can be lost (the queue drains after the process/stream teardown marks the
  sink closed). The deletion-risk guard therefore also echoes its full
  diagnostic through the synchronous stderr channel, the same channel the CLI
  uses for other fatal errors. Regression:
  `tests/test_bb_p2p_security.py::TestLanDeletionRiskGuard::test_download_only_without_yes_aborts_in_noninteractive`.

- **One device identity per installation (Syncthing model)**: p2p mode identifies
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
- **Mandatory TLS in p2p mode**: HTTPS/WSS only; no plaintext fallback
  (`ssl_utils.py`). Master identity is a persistent pinned cert.
- **Write-policy gate**: non-interactive write-capable p2p sync requires a p2p key
  (`_check_p2p_write_policy`, `main_logic.py::_check_p2p_write_policy`).
- **Mass-deletion guard (§12)**: bulk deletes pause the cycle
  (`_mass_deletion_blocked`, `sync.py::_mass_deletion_blocked`) unless explicitly allowed.
- **Per-path serialization (§14)**: all transfers take `utils.path_lock`
  (`utils/paths.py::path_lock`) to avoid concurrent writes to the same file.
- **Failover via periodic staleness sweep**: a hard-crashed master sends no
  mDNS goodbye and no further announce, so election is also re-run on every
  discovery wake and on a slave WebSocket drop
  (`PeerManager.reconcile_stale_peers`, `p2p_peers.py::PeerManager.reconcile_stale_peers`), which demotes any peer
  whose heartbeat is older than `HSYNC_PEER_TIMEOUT`. The peer timeout must be
  ≥ 2× `HSYNC_HEARTBEAT_INTERVAL` (enforced in `config.EnvSettings`) so a
  single missed announce never flaps a live peer; the survivor only demotes a
  master whose record is already stale, so a local-link blip never splits the
  group. The deterministic election alone decides the role.
- **Packaging lock-step**: any `HSYNC_*` flag/default change requires the
  synchronized `packaging/wd-os5` update (see `AGENTS.md`).
- **Containerized mksapkg**: `mksapkg-OS5` runs inside the
  `hsync-wdos5-mksapkg` image (`packaging/wd-os5/Dockerfile.mksapkg`) via
  `_wdos5_run_mksapkg` (`compile.sh::_wdos5_run_mksapkg`), not on the host — the prebuilt tool
  needs `libxml2.so.2` + OpenSSL 1.1.1 `bf-cbc`, absent on modern hosts.
- **Host-uid container builds**: containerized build steps drop to the host
  `uid:gid` so artifacts under `build/`/`dist/` stay caller-owned — otherwise
  root-owned output breaks the `rm -rf build` cleanup of a later native build
  (`cleanup_temp_files`). Covers WD OS5 (`_wdos5_run_mksapkg`; PyInstaller
  `_run_pyinstaller_in_docker` (`compile.sh::_run_pyinstaller_in_docker`)) and `deb-all`'s in-container
  PyInstaller (`compile.sh::build_deb_all`, with `HOME=/tmp` for the poetry/PyInstaller
  caches). The `deb-all` `dpkg-deb` step stays **root** (so the `.deb`'s
  internal `/usr`,`/etc` are owned `root:root`) and instead `chown`s the
  host-mounted stage + produced `.deb` back to the caller afterwards
  (`compile.sh::build_deb_all`); the archive's captured ownership is unaffected.
- **Container builds install the `build` group by exclusion**: `deb-all`/`rpi`
  use `poetry install --no-root --without dev`, which keeps the `build` group
  (PyInstaller) — correct as written, and now protected by
  `tests/test_compile_script.py`. Switching to an `--only` list is a silent trap
  under Poetry 2.x (2.3.2, pinned in the builder image): `--only` is exhaustive
  and a co-present `--with` is ignored, so `--only main --with build` exits 0
  while installing no PyInstaller. That form silently broke `rpi`/`deb-all` in
  hsync-bootnode, hsync-server and hsync-desktop.
- **Tag-driven release** (`.github/workflows/release.yml`): pushing a `vX.Y.Z`
  tag gates on lint+test, then builds native binaries on per-OS matrix runners
  (`linux-amd64`, `linux-arm64`, `darwin-amd64`, `darwin-arm64`, `windows-amd64`
  — PyInstaller cannot cross-compile) plus the Linux-only
  `deb-all`/`rpm-all`/`wd-os5-all` packages, and publishes them with
  `SHA256SUMS` to the GitHub Release. macOS
  binaries are unsigned (documented `xattr` workaround in `docs/getting-started-binary.md`).
- **Fixed-name `latest` aliases**: the `release` job copies each versioned asset
  to a stable name (`hsync-client-linux-amd64.tar.gz`, `hsync-client-amd64.deb`,
  `hsync-client-x86_64.rpm`, `hsync-client-windows-amd64-setup.exe`, …) so the
  docs link `releases/latest/download/<name>` without a version (`RELEASES.md`
  §5.8); pinned URLs keep the version. Guarded by
  `tests/test_compile_script.py` (docs ↔ workflow consistency).
- **`install.sh --version latest`**: the installer resolves the latest stable
  release by following the `github.com/<repo>/releases/latest` redirect with
  `curl -fsSIL -w %{url_effective}` (no GitHub API, no rate limit; drafts and
  pre-releases are excluded by GitHub), validates the `vX.Y.Z` tag, then proceeds
  exactly as for a pinned version (`install.sh`, `resolve_latest_version`).
  User docs point at `releases/latest/download/install.sh` so the bootstrap
  needs no version either.
- **RPM packaging (`rpm`/`rpm-all`)**: `compile.sh --target rpm` (host, needs
  `rpmbuild`) and `rpm-all` (Docker: x86_64 + aarch64,
  `packaging/rpm/Dockerfile.builder` on Rocky Linux 9) build a `.rpm` from the
  same onedir payload as the `.deb`, reusing `packaging/deb/` (man page, unit,
  conffile) via `packaging/rpm/` (`.spec`). `_semver_to_rpm` shares the Debian
  `-`→`~` mapping (RPM ≥ 4.10 also sorts `~` before everything), and `rpmbuild`
  runs with an absolute `_topdir` plus a writable `_dbpath`. An RPM erase keeps
  the data and the `_hsync` user (there is no `purge` equivalent).
- **Windows installer (Inno Setup)**: `packaging/windows/hsync-client.iss`, built
  in CI on `windows-latest` (`choco install innosetup imagemagick`; the app PNG
  is converted to a multi-size `.ico`). Per-user install (no admin) under
  `%LOCALAPPDATA%\Programs\HSYNC`; appends `{app}` to the user `PATH` and removes
  it on uninstall. The portable `.zip` remains the no-install alternative.
- **Windows-portable `compile.sh`**: PyInstaller path is OS-aware
  (`.venv/Scripts` vs `.venv/bin`, `compile.sh`) and `--strip` is omitted on
  Windows (`STRIP_FLAG`, `compile.sh::STRIP_FLAG`) since binutils is unavailable there.
- **Raspberry Pi target (`rpi`)**: `compile.sh --target rpi` cross-builds the
  onefile executable for Raspberry Pi OS 32-bit (armhf/armv7) and 64-bit
  (arm64/aarch64) via Docker + qemu, reusing `packaging/deb/Dockerfile.builder`
  as the cross-build image (Python 3.12 + Poetry + the armhf Rust/OpenSSL
  toolchain), and writing `dist/rpi/<arch>/hsync`. The onefile PyInstaller flags
  are shared between the host build and the container build through
  `_onefile_pyi_flags` (`compile.sh::_onefile_pyi_flags`) so the two cannot drift. The
  cross-build images pin their build inputs: the `qemu-user-static` binfmt image
  is referenced by digest (it runs `--privileged`) and the Rust toolchain is
  installed from a SHA256-verified, version-pinned `rustup-init` instead of
  piping the remote `https://sh.rustup.rs` script to a shell. The builder base
  is the official multi-arch `python:3.12-bookworm` image (prebuilt CPython
  built `--enable-shared`) rather than a CPython build from source: this keeps
  the CPython build out of qemu-user emulation, where the host's
  `qemu-aarch64` 7.2.0 crashed (`cc1` SIGSEGV) compiling `Modules/readline`.
  The WD OS5 (Debian Buster) builder stays separate and still compiles Python
  from source.
- **PyInstaller × multiprocessing (`freeze_support`)**: `main.main`
  (`main.py::main`) calls `multiprocessing.freeze_support()` before importing the
  CLI. The loguru sinks use `enqueue=True` (`logger.py::setup_logging`), which makes
  the stdlib start a `multiprocessing` helper (the resource tracker) by
  re-executing `sys.executable` — the frozen `hsync` — with the interpreter flags
  plus `-c 'from multiprocessing.resource_tracker import main; …'`. Without the
  `freeze_support()` call that child booted the Typer app and died with
  `No such option: -B` on stderr (confusing, and the tracker never ran);
  PyInstaller's `pyi_rth_multiprocessing` runtime hook needs this call to divert
  the helper. Outside a frozen build it is a no-op, so dev/`python -m` runs are
  unaffected.

> New decisions affecting multiple repos: add the ADR in `hsync-common` first,
> then map it here.

- **Bootnode TLS trust (ADR-0034, `hsync-common`)**: the `boot/1` node is
  authenticated pre-write. `BootnodeClient` gained `fingerprints`/`tofu`
  (replacing the unusable `ca_cert`); the pin/TOFU decision is the shared
  `hsync_common.protocol.pairing` SSOT (`parse_fingerprint`/`fingerprint_matches`/
  `FingerprintMismatch`). `bootnode_trust.py` is a `{host:port} → current` TOFU
  store (keys are the bracketed `[IPv6]:port` composite from
  `hsync_common.endpoint.format_authority`) sharing the `_trust_store.JsonStore` lock/atomicity base with
  `pinning.PinStore` (extracted on its second use — Rule of Three). A mismatch is
  fatal (`BootnodeFingerprintMismatch`, lease loop stops); `--ca-cert` is refused
  in `hsync p2p`.

- **Bootnode discovery cadence has its own budget**: the `boot/1` plan uses
  `BOOTNODE_REFRESH_INTERVAL` (`hsync_client.constants`, default `30`, spec
  ``boot-1.md``: "client refresh every 30 s"; env `HSYNC_BOOTNODE_REFRESH_INTERVAL`,
  range 1–300) to drive the lease loop's register/refresh/query, distinct from the
  mDNS `HEARTBEAT_INTERVAL` (5 s). `BootnodeDiscovery.wait_for_discovery` is
  event-based (`PeerManager.wait_for_change`, like mDNS) and never polls `query`
  itself; a refused `query` arms a `retry_after` backoff. Rationale: the node
  rate-limits `query` to 5/min per (IP, bucket) and `register`/`refresh` to
  10/min (`hsync-bootnode/python/src/hsync_bootnode/handler.py::BootHandler`), and this cadence keeps the client inside those
  limits instead of burning the burst and being `rate_limited` for the rest of
  the window, which would starve real discovery.
  `HSYNC_BOOTNODE_REFRESH_INTERVAL` is documented in
  `docs/configuration-p2p.md` and the man page; it is **not** part of the WD OS5
  web config surface (which never exposed the heartbeat).

- **A fatal discovery failure aborts `hsync p2p`**: a
  `BootnodeFingerprintMismatch` is terminal — the lease loop records it and
  `wait_for_discovery` raises the new plan-agnostic `DiscoveryFatalError`
  (`p2p_discovery.py`), which `run_p2p_mode` maps to `ExitCode.FATAL` (exit 1),
  so the client never waits for peers that can never be discovered. The mDNS plan
  never raises it; the core still does not branch on the plan (it catches a
  discovery-layer error). See `hsync-bootnode/docs/faq.md` and `docs/faq-p2p.md`
  for the operator/client recovery steps (stable `--identity`,
  `--bootnode-fingerprint`, clearing the pin).

- **One log line per boot/1 lease cycle**: aioquic's per-frame QUIC/TLS INFO
  records (`Retrying with token`, `Negotiated protocol version`, `ALPN
  negotiated protocol`, `Connection close sent`) are pinned to WARNING in
  `logger.setup_logging` (DEBUG with `--debug`), and `_lease_loop` emits a single
  INFO summary per successful cycle
  (`Bootnode <host>:<port>: <action> ok (N peer(s))`). Rationale: four library
  lines per exchange, repeated every cycle, drowned the client's own output
  without adding actionable information; the library detail stays available with
  `--debug`.

- **SSH-like bootnode trust (ADR-0035)**: the default policy is `ask` — on a
  terminal the client prints the unknown bootnode fingerprint and confirms it
  once (like `ssh`), remembering it in `bootnode_known_hosts.json`; without a
  terminal it fails closed. The policy is the shared SSOT
  `hsync_common.protocol.tls_trust` (`TrustPolicy` + `evaluate_tls_trust`),
  consumed by `BootnodeClient` (`policy`/`prompt` fields; `BootnodeNotTrusted` is
  fatal in the lease loop like a pin mismatch) and mirrored by the Android client
  via the `p2p-bootnode-trust-scenarios` fixture. `--bootnode-trust
  {ask,accept-new,strict}` and `HSYNC_BOOTNODE_TRUST` select the policy;
  `--bootnode-tofu` maps to `accept-new`, `--bootnode-fingerprint` to `strict` and
  `--insecure` to `none`. The store (`bootnode_known_hosts.json`) keeps
  `{current, updated_at, first_seen, source}` and is managed with
  `hsync trust bootnode {list,show,add,remove,fingerprint}` (`fingerprint` reads a
  node's certificate without trusting it). The prompt is built by
  `hsync_client/trust_prompt.py` and only prompts on a terminal under policy
  `ask`; non-interactive acceptance is expressed with `accept-new` (TOFU).

- **Discovery elects the local role at start**: each plan calls
  `PeerManager.elect()` (`p2p_peers.py`) once when discovery starts. The election
  previously ran only on peer add/remove, a local readiness change and the stale
  sweep, so a node with **no peers yet** never resolved its role: a lone
  `--force-master` node (the documented single-device master,
  `docs/getting-started-p2p.md`) never advertised `server_ready`, and a joining
  slave looped on "Waiting for master … to start server" forever. Evaluating the
  local candidate up front makes a lone eligible node claim mastership
  immediately, so the slave discovers the ready master and connects.

- **The master watcher propagates a plain ``touch`` (inotify)**: on Linux a
  ``utime`` fires only ``IN_ATTRIB`` (no close), so the base
  ``_process_permission_only`` (used by the P2P master and the server-mode client,
  while the P2P slave has its own handler) now tracks the mtime alongside the mode
  and dispatches ``_on_metadata_changed`` when the content hash is unchanged,
  guarded so a mid-write ``IN_MODIFY`` burst is not mistaken for a touch. Without
  it the master never detected an mtime-only edit and never broadcast it, so a
  ``touch`` propagated only client→master (the slave's
  ``_propagate_metadata_change``): the two directions are now symmetric
  (``touch`` on either side converges the other's mtime without transferring
  bytes).
