# HSYNC Client — Data models & state

The client owns **no wire models**: file/dir/action/tombstone/version types are
imported from `hsync-common` (SSOT, see
[common data-models](https://github.com/albertobasso/hsync-common/blob/main/llm-wiki/data-models.md)). This page maps
the *client-local* persisted state and how it wraps the common stores.

## Reused common models (do not redefine)

| Type | Source | Used by |
| ---- | ------ | ------- |
| `File` | `hsync_common.models.file` | `sync.py`, `transfer.py` (metadata, transfers) |
| `Dir` / `is_hidden_dir` | `hsync_common.models.dir` | `sync.py` (tree scan) |
| `SyncAction` / `SyncActionName` | `hsync_common.models.sync_action` | `get_sync_actions` (`sync.py:684`) |
| `TombstoneEntry` / `TombstoneStore` / `tombstone_key` | `hsync_common.models.tombstone` | `tombstones.py` |
| `SyncStateStore` | `hsync_common.models.version` | `sync_state.py` |
| `FileVersion` / `VersionInfo` / `SyncDecision` / `resolve_three_way` / `parse_version_info` / `merge_vclocks` / `tick_vclock` | `hsync_common.models.version` | `sync.py`, `transfer.py`, `arbitration.py` (causal three-way merge) |
| `relpath_in_bucket` | `hsync_common.models.tombstone` | `sync_state.py`, `tombstones.py` |

## Client-local persisted state (per bucket)

Two hidden side-cars at the bucket root; both keyed by bucket dir (not singletons)
to keep tests isolated.

| Side-car | Filename const | Wrapper | Purpose |
| -------- | -------------- | ------- | ------- |
| Sync state | `HSYNCSTATE_FILENAME` (`.hsyncstate`) | `sync_state.py` → `SyncStateStore` | per-path `base_hash` + vector-clock lineage for causal three-way merge (ADR-0022; no wall-clock trust) |
| Tombstones | `HSYNCTOMBSTONES_FILENAME` (`.hsynctombstones`) | `tombstones.py` → `TombstoneStore` | per-peer deletion markers, gossiped across mastership changes |

### sync_state.py (merge base)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `get_sync_state_store` | `sync_state.py:48` | cached per-bucket store (`_stores`, `_lock` RLock) |
| `get_base_hash` | `:60` | read agreed base hash for a path |
| `record_base` | `:69` | persist base after a successful sync |
| `local_version_for_compare` | `:133` | pure (no mutation) effective version for the causal matrix: ticks the stored clock by this node's cid when content differs from the recorded base |
| `merge_and_record` | `:211` | record agreed version with a merged clock (convergence D11) |
| `upload_version_fields` | `:202` | SSOT `(vclock_json, version_id)` wire form for upload + delta-apply, shared by `transfer.py` and `delta_transfer.py` |
| `note_local_edit` | `:173` | persist a self-recorded local edit (anti-aliasing invariant). **Deliberately NOT wired into the watcher**: a persistent pre-broadcast self-record would advance `base_hash` to the just-edited content, and if the subsequent upload fails the next reconcile would hit the base fast-path (`local == base` ⇒ download) and silently overwrite the local edit. The anti-aliasing guarantee is delivered by the **pure** `local_version_for_compare` tick at compare/upload time plus the post-success `record_base`/`merge_and_record` (upload accepted ⇒ store reflects the advertised clock) |
| `get_vclock` | `:201` | read the stored vclock for a path |
| `clear_base` | `:88` | drop base on delete |
| `retain` | `:96` | GC bases not in the current path set |
| `flush` | `:122` | single flush per sync (writes `.hsyncstate` atomically); test reset via `_stores.clear()` |

Writes are RLock-guarded because transfers record their base from the parallel
transfer pool; persistence is decoupled from mutation (one flush per sync).
The per-bucket store cache (`_stores`) is keyed by bucket dir so tests that
switch `LOCAL_BUCKET` stay isolated.

### tombstones.py (deletion markers)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `get_tombstone_store` | `tombstones.py:43` | cached store; `load()` + `prune()` on first use (TTL via `get_tombstone_ttl_days`) |
| `record_deletion` | `:55` | add marker (path, sha256, ts) and persist |
| `clear_deletion` | `:71` | remove a marker (resurrection) |
| `merge_payload` | `:82` | LWW-merge master/peer markers (gossip) |
| `get_payload` | `:92` | union to serve when this peer is master |

Tombstone semantics (LWW merge, TTL, hashed keys) are SSOT in
`hsync_common.models.tombstone` — see ADR 0003. The per-bucket store cache
(`_stores`) is keyed by bucket dir, matching `sync_state`.

Tombstone semantics (LWW merge, TTL, hashed keys) are SSOT in
`hsync_common.models.tombstone` — see ADR 0003.

## Runtime config models

`HsyncConfig` / `ApiEndpoints` TypedDicts and the global `CONFIG`
(`config.py:294`/`:275`/`:391`) hold the merged runtime settings; see
[cli-config.md](modules/cli-config.md). `EnvSettings` (`config.py:47`) is the
pydantic-settings layer over `HSYNC_*` vars.

> Human-facing config reference: `../docs/configuration.md`.
