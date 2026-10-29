# HSYNC Client — Data models & state

The client owns **no wire models**: file/dir/action/tombstone/version types are
imported from `hsync-common` (SSOT, see
[common data-models](https://github.com/albertsdk/hsync-common/blob/main/llm-wiki/data-models.md)). This page maps
the *client-local* persisted state and how it wraps the common stores.

## Reused common models (do not redefine)

| Type | Source | Used by |
| ---- | ------ | ------- |
| `File` | `hsync_common.models.file` | `sync.py`, `transfer.py` (metadata, transfers) |
| `Dir` / `is_hidden_dir` | `hsync_common.models.dir` | `sync.py` (tree scan) |
| `SyncAction` / `SyncActionName` | `hsync_common.models.sync_action` | `get_sync_actions` (`sync.py::get_sync_actions`) |
| `TombstoneEntry` / `TombstoneStore` / `tombstone_key` | `hsync_common.models.tombstone` | `tombstones.py` |
| `SyncStateStore` | `hsync_common.models.version` | `sync_state.py` |
| `FileVersion` / `VersionInfo` / `SyncDecision` / `resolve_three_way` / `parse_version_info` / `merge_vclocks` / `tick_vclock` | `hsync_common.models.version` | `sync.py`, `transfer.py`, `arbitration.py` (causal three-way merge) |
| `relpath_in_bucket` | `hsync_common.models.tombstone` | `sync_state.py`, `tombstones.py` |

## Client-local persisted state (per folder UUID)

Causal versions and tombstones live in one SQLite DB **per folder**, under the
device dir (`<device_dir>/syncstate/<uuid>.db`; `HSYNC_STATE_DB` collapses this
to a single DB), keyed by the `.hsyncfolder` UUID (ADR-0025). A per-device
`index.db` maps `canonical_path → UUID` for re-anchoring. The marker is the only
engine file at the bucket root.

| Store | Location | Wrapper | Purpose |
| ----- | -------- | ------- | ------- |
| Sync state | per-folder DB table `versions` | `sync_state.py` → `SyncStateStore` | per-path `base_hash` + vector-clock lineage for causal three-way merge (ADR-0022; no wall-clock trust) |
| Tombstones | per-folder DB table `tombstones` | `tombstones.py` → `TombstoneStore` | per-peer deletion markers, gossiped across mastership changes |
| Folder id | `.hsyncfolder` at bucket root | `state_backend.resolve_bucket_folder` | UUIDv4 that re-anchors state across rename/move |

### state_reset.py (sync-state reset)

`hsync state reset` (CLI: `cli.py::state_reset`) forgets **only** the merge bases
and deletion markers of one folder. The module resolves the target DB read-only
and holds the folder's `InstanceLock` for the whole operation, so a live sync
makes it refuse instead of clearing state a running process keeps writing.

| Symbol | Location | Role |
| ------ | -------- | ---- |
| `ResetReport` | `state_reset.py::ResetReport` | outcome (`folder`, `folder_id`, `db_path`, `found`, `versions_deleted`, `tombstones_deleted`, `quarantined`); serialized by `--json` — the field names are the desktop's contract (`hsync-desktop/hsync_desktop/engine/cli.py::reset_sync_state` parses them) |
| `resolve_state_db` | `state_reset.py::resolve_state_db` | `(folder_id, db_path)` or `None`. Mirrors the read-only decisions of `state_backend.resolve_bucket_folder` (marker → `index.db` re-anchor → duplicate-marker copy gets no state) but **never** creates a `.hsyncfolder`, an index row or an empty DB. Honours `HSYNC_STATE_DB` |
| `reset_folder_state` | `state_reset.py::reset_folder_state` | `InstanceLock.acquire(folder)` → `StateDb.reset_folder(folder_id)` (common, one transaction) → release. `quarantine=True` renames an unreadable DB to `<db>.corrupt-<ts>` (with its `-wal`/`-shm`) instead of raising |
| `cli.py::state_reset` | `cli.py::state_reset` | thin shell: `--folder` required, exit `ExitCode.FATAL` on lock/invalid path/DB error, `setup_logging(quiet=True)` so the lock's DEBUG lines stay off stderr |

Why it is safe for the data: with no merge base the cycle mints **no** tombstone
(the mint loop iterates `sync_state.iter_merge_bases()`), and `delete_remote`
requires a dominating tombstone clock — so a reset can never delete a file. The
cost is a full scan/hash of the folder; identical files stay `in_sync` (hash
first) and are re-recorded, while deletions performed before the reset can be
undone because their markers were forgotten.


### sync_state.py (merge base)

| Symbol | Location | Role |
| ------ | -------- | ---- |
| `get_sync_state_store` | `sync_state.py::get_sync_state_store` | cached per-folder-id store (`_stores`, `_lock` RLock) |
| `get_base_hash` | `sync_state.py::get_base_hash` | read agreed base hash for a path |
| `record_base` | `sync_state.py::record_base` | persist base after a successful sync |
| `_corrected_mtime_of` | `sync_state.py::_corrected_mtime_of` | corrected mtime of the on-disk file through `hsync_common.clock.correct_mtime` (ADR-0023); 0 when unreadable |
| `local_version_for_compare` | `sync_state.py::local_version_for_compare` | pure (no mutation) effective version for the causal matrix: ticks the stored clock by this node's cid when content differs from the recorded base; carries `corrected_mtime` on the fresh/ticked copy |
| `merge_and_record` | `sync_state.py::merge_and_record` | record agreed version with a merged clock (convergence invariant, ADR-0022 §5); records the remote corrected mtime, falling back to the disk file's when unknown |
| `upload_version_fields` | `sync_state.py::upload_version_fields` | SSOT `(vclock_json, version_id, corrected_mtime)` 3-tuple wire form for upload + delta-apply, shared by `transfer.py` and `delta_transfer.py` |
| `note_local_edit` | `sync_state.py::note_local_edit` | persist a self-recorded local edit (anti-aliasing invariant), including the corrected mtime. **Deliberately NOT wired into the watcher**: a persistent pre-broadcast self-record would advance `base_hash` to the just-edited content, and if the subsequent upload fails the next reconcile would hit the base fast-path (`local == base` ⇒ download) and silently overwrite the local edit. The anti-aliasing guarantee is delivered by the **pure** `local_version_for_compare` tick at compare/upload time plus the post-success `record_base`/`merge_and_record` (upload accepted ⇒ store reflects the advertised clock) |
| `get_vclock` | `sync_state.py::get_vclock` | read the stored vclock for a path |
| `clear_base` | `sync_state.py::clear_base` | drop base on delete |
| `retain` | `sync_state.py::retain` | GC bases not in the current path set |
| `flush` | `sync_state.py::flush` | single flush per sync (one SQLite transaction); test reset via `_stores.clear()` |

Writes are RLock-guarded because transfers record their base from the parallel
transfer pool; persistence is decoupled from mutation (one flush per sync).
The store cache (`_stores`) is keyed by folder UUID so tests that switch
`LOCAL_BUCKET` stay isolated (a new directory gets a new marker).

### tombstones.py (deletion markers)

| Symbol | Location | Role |
| ------ | -------- | ---- |
| `get_tombstone_store` | `tombstones.py::get_tombstone_store` | cached store; `load()` + `prune()` on first use (TTL via `get_tombstone_ttl_days`) |
| `record_deletion` | `tombstones.py::record_deletion` | add causal marker (path, sha256, origin cid, inherited vclock) and persist |
| `clear_deletion` | `tombstones.py::clear_deletion` | remove a marker (path re-created) |
| `merge_payload` | `tombstones.py::merge_payload` | causal-merge master/peer markers (dominance + content tie-break, gossip) |
| `get_payload` | `tombstones.py::get_payload` | union to serve when this peer is master |
| `deletion_marker_snapshot` | `tombstones.py::deletion_marker_snapshot` | opaque `(version_id, vclock)` snapshot of the marker for an absolute path (`None` when absent): every mint, merge and clear changes it |
| `destination_mutation_since` | `tombstones.py::destination_mutation_since` | commit gate: true when *abs_path* was removed or replaced since a snapshot — delete/rename-away via the presence check, delete-and-recreate via the marker-snapshot comparison |

Tombstone semantics (causal merge, TTL storage GC, hashed keys) are SSOT in
`hsync_common.models.tombstone` — see ADR 0003. The store cache (`_stores`)
is keyed by folder UUID, matching `sync_state`.

A marker is minted by every path that removes a name from the tree
(`file_event_handler.py::BaseFileEventHandler`): a delete (including the files
folded under a deleted directory), a move out of the tree, an in-tree **file
rename**, and every file vacated by an in-tree **directory rename**
(`file_event_handler.py::BaseFileEventHandler._mark_dir_rename_vacated` —
directories themselves never carry tombstones). The marker's clock starts from
the path's stored version, or from this node's first tick when it only ever
served the path (`p2p_websocket.py::_record_deletion_marker`), and is ticked
once, so a peer that downloaded the advertised version gets a dominating marker
and removes the old path instead of re-uploading it.

Because the watcher mints a marker *immediately* on a local delete or rename,
`destination_mutation_since` is the pre-commit re-validation of a transfer's
final move: `transfer.py::_download_from_source_attempt` and
`p2p_delta.py::handle_delta_apply` snapshot the destination (`existed_at_start`
+ marker) before the body streams and re-check it right before `os.replace`.
A destination deleted, renamed away or deleted-and-recreated while the transfer
ran is therefore never resurrected or clobbered by the commit; a marker already
present at the snapshot and left unchanged does not block the commit (the plan
may legitimately override it, e.g. a peer re-created the file).

## Runtime config models

`HsyncConfig` / `ApiEndpoints` TypedDicts and the global `CONFIG`
(`config.py::HsyncConfig`/`config.py::ApiEndpoints`/`config.py::CONFIG`) hold the merged runtime settings; see
[cli-config.md](modules/cli-config.md). `EnvSettings` (`config.py::EnvSettings`) is the
pydantic-settings layer over `HSYNC_*` vars.

> Human-facing config reference: `../docs/configuration.md`.
