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
| `SyncAction` / `SyncActionName` | `hsync_common.models.sync_action` | `get_sync_actions` (`hsync_client/sync.py::get_sync_actions`) |
| `TombstoneEntry` / `TombstoneStore` / `tombstone_key` | `hsync_common.models.tombstone` | `tombstones.py` |
| `TrashEntry` / `trash_root` / `parse_trash_entry` / `is_safe_relpath` | `hsync_common.models.trash` | `trash.py` (store), `hsync trash list --json` (front-ends) |
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

`hsync state reset` (CLI: `hsync_client/cli.py::state_reset`) forgets **only** the merge bases
and deletion markers of one folder. The module resolves the target DB read-only
and holds the folder's `InstanceLock` for the whole operation, so a live sync
makes it refuse instead of clearing state a running process keeps writing.

| Symbol | Location | Role |
| ------ | -------- | ---- |
| `ResetReport` | `hsync_client/state_reset.py::ResetReport` | outcome (`folder`, `folder_id`, `db_path`, `found`, `versions_deleted`, `tombstones_deleted`, `quarantined`); serialized by `--json` — the field names are the desktop's contract (`hsync-desktop/hsync_desktop/engine/cli.py::reset_sync_state` parses them) |
| `resolve_state_db` | `hsync_client/state_reset.py::resolve_state_db` | `(folder_id, db_path)` or `None`. Mirrors the read-only decisions of `state_backend.resolve_bucket_folder` (marker → `index.db` re-anchor → duplicate-marker copy gets no state) but **never** creates a `.hsyncfolder`, an index row or an empty DB. Honours `HSYNC_STATE_DB` |
| `reset_folder_state` | `hsync_client/state_reset.py::reset_folder_state` | `InstanceLock.acquire(folder)` → `StateDb.reset_folder(folder_id)` (common, one transaction) → release. `quarantine=True` renames an unreadable DB to `<db>.corrupt-<ts>` (with its `-wal`/`-shm`) instead of raising |
| `hsync_client/cli.py::state_reset` | `hsync_client/cli.py::state_reset` | thin shell: `--folder` required, exit `ExitCode.FATAL` on lock/invalid path/DB error, `setup_logging(quiet=True)` so the lock's DEBUG lines stay off stderr |

Why it is safe for the data: with no merge base the cycle mints **no** tombstone
(the mint loop iterates `sync_state.iter_merge_bases()`), and `delete_remote`
requires a dominating tombstone clock — so a reset can never delete a file. The
cost is a full scan/hash of the folder; identical files stay `in_sync` (hash
first) and are re-recorded, while deletions performed before the reset can be
undone because their markers were forgotten.


### sync_state.py (merge base)

| Symbol | Location | Role |
| ------ | -------- | ---- |
| `get_sync_state_store` | `hsync_client/sync_state.py::get_sync_state_store` | cached per-folder-id store (`_stores`, `_lock` RLock) |
| `get_base_hash` | `hsync_client/sync_state.py::get_base_hash` | read agreed base hash for a path |
| `record_base` | `hsync_client/sync_state.py::record_base` | persist base after a successful sync |
| `_corrected_mtime_of` | `hsync_client/sync_state.py::_corrected_mtime_of` | corrected mtime of the on-disk file through `hsync_common.clock.correct_mtime` (ADR-0023); 0 when unreadable |
| `local_version_for_compare` | `hsync_client/sync_state.py::local_version_for_compare` | pure (no mutation) effective version for the causal matrix: ticks the stored clock by this node's cid when content differs from the recorded base; carries `corrected_mtime` on the fresh/ticked copy |
| `merge_and_record` | `hsync_client/sync_state.py::merge_and_record` | record agreed version with a merged clock (convergence invariant, ADR-0022 §5); records the remote corrected mtime, falling back to the disk file's when unknown |
| `upload_version_fields` | `hsync_client/sync_state.py::upload_version_fields` | SSOT `(vclock_json, version_id, corrected_mtime)` 3-tuple wire form for upload + delta-apply, shared by `transfer.py` and `delta_transfer.py` |
| `note_local_edit` | `hsync_client/sync_state.py::note_local_edit` | persist a self-recorded local edit (anti-aliasing invariant), including the corrected mtime. **Deliberately NOT wired into the watcher**: a persistent pre-broadcast self-record would advance `base_hash` to the just-edited content, and if the subsequent upload fails the next reconcile would hit the base fast-path (`local == base` ⇒ download) and silently overwrite the local edit. The anti-aliasing guarantee is delivered by the **pure** `local_version_for_compare` tick at compare/upload time plus the post-success `record_base`/`merge_and_record` (upload accepted ⇒ store reflects the advertised clock) |
| `get_vclock` | `hsync_client/sync_state.py::get_vclock` | read the stored vclock for a path |
| `clear_base` | `hsync_client/sync_state.py::clear_base` | drop base on delete |
| `retain` | `hsync_client/sync_state.py::retain` | GC bases not in the current path set |
| `flush` | `hsync_client/sync_state.py::flush` | single flush per sync (one SQLite transaction); test reset via `_stores.clear()` |

Writes are RLock-guarded because transfers record their base from the parallel
transfer pool; persistence is decoupled from mutation (one flush per sync).
The store cache (`_stores`) is keyed by folder UUID so tests that switch
`LOCAL_BUCKET` stay isolated (a new directory gets a new marker).

### trash.py (sync trash)

| Symbol | Location | Role |
| ------ | -------- | ---- |
| `TrashStore` | `hsync_client/trash.py::TrashStore` | one folder's view of the device-local store: `move` (park a file the cycle is deleting), `entries`, `restore`, `restore_all` (every entry, newest first), `empty`, `prune`/`prune_if_due`. `restore` and `restore_all` share one `_preflight` refusal (`payload-missing`, `payload-corrupt`, `download-only`, `destination-exists` unless forced) |
| `store_for_folder` | `hsync_client/trash.py::store_for_folder` | the store of one folder, or `None` when it has none to extend; refuses a root inside the synced folder (reported once per process). Shared by the cycle and the realtime delete handlers |
| `delete_local_file` | `hsync_client/trash.py::delete_local_file` | park the bytes, then remove: the single removal used by the cycle, `event_dispatch` and the p2p master. Engine artifacts and `.hsynckeep` are unlinked without an entry |
| `delete_local_tree` | `hsync_client/trash.py::delete_local_tree` | park the files of a directory tree before the tree is removed (bounded by `TREE_TRASH_MAX_FILES` / `TREE_TRASH_MAX_BYTES`), so a folder a peer deleted is as recoverable as a single file; symlinks are left to the removal and the remainder is logged |
| `stats_store` | `hsync_client/trash.py::stats_store` | summarize the whole root (`StoreStats`: entries, bytes, oldest, budget, folders) for `hsync trash stats`; walks the store, never the state DB |
| `empty_store` | `hsync_client/trash.py::empty_store` | empty a whole trash root (one folder's part is `TrashStore.empty`); used by `hsync trash empty --all`, since the store keeps one directory per share. `dry_run` renders the same counts without deleting |
| `prune_store` | `hsync_client/trash.py::prune_store` | the sweep over a whole trash root (one budget for every folder); discards records whose payload is missing or its size differs. `TrashStore.prune` delegates, and `hsync trash prune` calls it without a store |
| `trash_settings` | `hsync_client/trash.py::trash_settings` | resolve `TRASH_ENABLED` / `TRASH_DIR` / `TRASH_RETENTION_DAYS` / `TRASH_MAX_SIZE` from `CONFIG` |
| `TrashSettings.budget` | `hsync_client/trash.py::TrashSettings.budget` | effective byte budget: explicit `--trash-max-size`, else a share of the hosting filesystem with a floor |
| `folder_id_of` | `hsync_client/trash.py::folder_id_of` | read `.hsyncfolder` read-only (a folder never synced has no trash) |
| `MoveResult` / `RestoreReport` / `RestoreAllReport` / `EmptyReport` / `PruneReport` | `hsync_client/trash.py` | outcome records; `RestoreReport.status` is the machine-readable verdict (`restored`, `no-entry`, `destination-exists`, `download-only`, `payload-missing`, `payload-corrupt`, `failed`) and `RestoreReport.replaced` says a forced restore parked the copy it replaced. `RestoreAllReport` counts one batch (`restored`/`skipped`/`failed`, `bytes`, `attempted`) and maps each entry left behind to its verdict in `problems`; only `UNRECOVERABLE_RESTORE` (`payload-missing`, `payload-corrupt`, `failed`) counts as a failure, so a path that merely reappeared does not fail the command |

The policy values this module applies are **declared in hsync-common, not here**:
`TREE_TRASH_MAX_FILES`, `TREE_TRASH_MAX_BYTES`, `TRASH_SWEEP_INTERVAL_SECONDS` and
`TRASH_UNRECOVERABLE_RESTORE` are imported from `hsync_common.models.trash`, which
publishes them as the `trash-contract` fixture that the Android store asserts
against. The sweep rule itself is the contract's too: `prune_store` walks the
tree and applies `plan_trash_sweep` (`hsync_common/models/trash.py::plan_trash_sweep`)
instead of re-deriving which entries expire or get evicted.
`tests/test_trash_contract.py` pins the two sides together, replays the
fixture's `prune_cases` through `prune_store` (including the unusable entry
directories it must discard), and checks that the record `move` writes carries
exactly the contract's keys.

Layout and the entry contract are SSOT in `hsync_common.models.trash`
(`TrashEntry`, `trash_root`, `entry_payload_path`, `parse_trash_entry`) — see
ADR 0056. The store never opens the per-folder state DB: listing and restoring
work while a sync runs, and the state DB keeps its single writer. A restore is
therefore a plain file write, and the engine's existing delete-vs-recreate rule
(`hsync_client/sync_state.py::note_local_edit` minting a clock past the marker)
is what makes the file propagate again.

### tombstones.py (deletion markers)

| Symbol | Location | Role |
| ------ | -------- | ---- |
| `get_tombstone_store` | `hsync_client/tombstones.py::get_tombstone_store` | cached store; `load()` + `prune()` on first use (TTL via `get_tombstone_ttl_days`) |
| `record_deletion` | `hsync_client/tombstones.py::record_deletion` | add causal marker (path, sha256, origin cid, inherited vclock) and persist |
| `clear_deletion` | `hsync_client/tombstones.py::clear_deletion` | remove a marker (path re-created) |
| `merge_payload` | `hsync_client/tombstones.py::merge_payload` | causal-merge master/peer markers (dominance + content tie-break, gossip) |
| `get_payload` | `hsync_client/tombstones.py::get_payload` | union to serve when this peer is master |
| `deletion_marker_snapshot` | `hsync_client/tombstones.py::deletion_marker_snapshot` | opaque `(version_id, vclock)` snapshot of the marker for an absolute path (`None` when absent): every mint, merge and clear changes it |
| `destination_mutation_since` | `hsync_client/tombstones.py::destination_mutation_since` | commit gate: true when *abs_path* was removed or replaced since a snapshot — delete/rename-away via the presence check, delete-and-recreate via the marker-snapshot comparison |

Tombstone semantics (causal merge, TTL storage GC, hashed keys) are SSOT in
`hsync_common.models.tombstone` — see ADR 0003. The store cache (`_stores`)
is keyed by folder UUID, matching `sync_state`.

A marker is minted by every path that removes a name from the tree
(`hsync_client/file_event_handler.py::BaseFileEventHandler`): a delete (including the files
folded under a deleted directory), a move out of the tree, an in-tree **file
rename**, and every file vacated by an in-tree **directory rename**
(`hsync_client/file_event_handler.py::BaseFileEventHandler._mark_dir_rename_vacated` —
directories themselves never carry tombstones). The marker's clock starts from
the path's stored version, or from this node's first tick when it only ever
served the path (`hsync_client/p2p_websocket.py::_record_deletion_marker`), and is ticked
once, so a peer that downloaded the advertised version gets a dominating marker
and removes the old path instead of re-uploading it.

Because the watcher mints a marker *immediately* on a local delete or rename,
`destination_mutation_since` is the pre-commit re-validation of a transfer's
final move: `hsync_client/transfer.py::_prepare_download_target` snapshots the destination
(`existed_at_start` + marker) before the body streams and
`hsync_client/transfer.py::_stream_download_body` re-checks it right before `os.replace`;
`hsync_client/p2p_delta.py::handle_delta_apply` does the same for delta payloads.
A destination deleted, renamed away or deleted-and-recreated while the transfer
ran is therefore never resurrected or clobbered by the commit; a marker already
present at the snapshot and left unchanged does not block the commit (the plan
may legitimately override it, e.g. a peer re-created the file).

**A directory name carries no marker**, so a removed folder is protected by the
event contract instead of a clock: a `modified` never materialises a directory,
and a materialising event landing inside an in-flight recursive delete is
dropped (see [sync-core.md](modules/sync-core.md#event_dispatchpy--apply-inbound-events)).
The window itself is a transient module-level registry
(`hsync-common/hsync_common/deletions.py::begin_tree_delete`) that is never persisted: it
lives exactly as long as the removal it guards.

## Runtime config models

`HsyncConfig` / `ApiEndpoints` TypedDicts and the global `CONFIG`
(`hsync_client/config.py::HsyncConfig`/`hsync_client/config.py::ApiEndpoints`/`hsync_client/config.py::CONFIG`) hold the merged runtime settings; see
[cli-config.md](modules/cli-config.md). `EnvSettings` (`hsync_client/config.py::EnvSettings`) is the
pydantic-settings layer over `HSYNC_*` vars.

> Human-facing config reference: `../docs/configuration.md`.
