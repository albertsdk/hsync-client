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

> New decisions affecting multiple repos: add the ADR in `hsync-common` first,
> then map it here.
