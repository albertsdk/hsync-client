# hsync-client — Sync core

Mode-agnostic engine: list/diff/transfer + the watcher pipeline. Shared by remote
and LAN modes.

## sync.py — reconcile loop

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `sync` | `sync.py:132` | one full reconcile cycle (lan_mode flag) |
| `get_files_local` | `sync.py:452` | scan local tree (parallel, adaptive workers `_get_optimal_workers:409`) |
| `get_files_remote` | `sync.py:515` | fetch remote file list |
| `get_tombstones_remote` / `push_tombstones_remote` | `sync.py:566` / `:615` | tombstone exchange |
| `get_sync_actions` | `sync.py:690` | diff → `SyncAction`s; applies `--sync-direction` filter; skips unsafe/internal listing entries (`validate_remote_relpath`) |
| `_compare_files` | `sync.py:820` | per-file decision (hash/mtime + base via sync_state) |
| `_save_conflict_copy` | `sync.py:905` | write a conflict copy on three-way conflict |
| `_execute_parallel_transfers` | `sync.py:267` | run downloads+uploads via `asyncio.Semaphore` (`MAX_CONCURRENT_TRANSFERS`, `constants.py:90`); catches `PermanentDownloadError` |
| `_mass_deletion_blocked` | `sync.py:94` | §12 guard: pause when deletions exceed threshold/percent or empty master |
| `_permanent_download_failures` / `_reset_permanent_failures` / `_is_permanently_failed` | `sync.py:58` | H8 per-cycle skip-set of permanently-failed downloads (keyed `filepath\|sha256`); cleared at the start of each cycle |
| `update_file` / `update_dir` | `sync.py:996` / `:957` | apply inbound metadata; reject unsafe remote paths |
| `ensure_hsynckeep_files` | `sync.py:326` | keep empty dirs via sentinel |
| `_cleanup_partial_files` | `sync.py:62` | drop stale `.part` files |

Tombstone-vs-local decision: `_tombstone_deletes_local` (`sync.py:653`,
`MTIME_TOLERANCE` `:650`).

## delta_transfer.py — rsync-style block sync

Delta transfer reduces bandwidth for large files with small changes by transferring
only changed blocks. Integrated transparently into `transfer.py` upload/download paths.

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `upload_delta` | `delta_transfer.py:45` | GET signatures → rolling match → POST apply; fallback to full upload on failure |
| `download_delta` | `delta_transfer.py:120` | POST signatures → GET data → rebuild; fallback to full download on failure |
| `should_use_delta` | `delta_transfer.py:25` | gate: size threshold, base hash exists, capability check, direction logic |

**Delta duration logging**: the `[DELTA ↑]`/`[DELTA ↓]` success logs include
the wall-clock duration via `format_duration(..., subsecond=True)` — e.g.
`[DELTA ↑] "/path" 512 KB literal (saved 1.0 MB) in 120ms`.

Delta transfer is **enabled by default** (`HSYNC_DELTA_ENABLED=true`). Size thresholds:
≥ 256 KB server mode, ≥ 1 MB LAN mode (configurable via `HSYNC_DELTA_MIN_FILE_SIZE`
and `HSYNC_DELTA_MIN_FILE_SIZE_LAN`). Falls back to full transfer on any failure
(network error, hash mismatch, peer doesn't support delta).

**TOCTOU protection**: After the rolling scan, the client re-checks mtime+size. For
files smaller than 10 MB (`DELTA_TOCTOU_REHASH_THRESHOLD`), it also does a full SHA-256
re-hash to catch content changes that mtime+size alone miss (e.g., rapid edits within
the same second). If the file changed during the scan, the delta is aborted and the
caller falls back to a full transfer.

## transfer.py — file I/O over HTTP(S)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `download_file` / `_download_file_attempt` | `transfer.py:270` / `:316` | retry wrapper + one attempt (raises transient → retry); delta download attempted first via `delta_transfer.download_delta` |
| `upload_file` / `_upload_file_attempt` | `transfer.py:697` / `:770` | retry wrapper + one attempt; resumable via `.part` + offset; delta upload attempted first via `delta_transfer.upload_delta` |
| `_get_upload_status` | `transfer.py:598` | query server for resume offset |
| `is_file_stable` / `_wait_for_stable_file` | `transfer.py:542` / `:635` | size+mtime stability gate; fast path on `from_closed` |
| `_reschedule_upload` | `transfer.py:671` | §15: defer torn-file uploads (bounded) |
| `_build_multipart_streaming` / `_SizedStreamingBody` | `transfer.py:154` / `:131` | streaming multipart |
| `_ThrottledFileReader` | `transfer.py:101` | bandwidth throttle on upload reads |
| `_record_synced_base` | `transfer.py:80` | persist merge base after success (sync_state) |
| `_emit_transfer_failed` | `transfer.py:75` | emit `transfer_failed` UI event on exhaustion |
| `_local_dest_path` | `transfer.py:242` | confine remote path to `LOCAL_BUCKET` via `resolve_within` (returns `None` on unsafe input) |
| `_open_partial_no_follow` | `transfer.py:257` | `.part` writes with `O_NOFOLLOW` where supported (anti-TOCTOU) |

**Transfer duration logging**: every `[DOWNLOAD]`/`[UPLOAD]` success log
includes the per-attempt wall-clock duration via
`format_duration(monotonic() - t0, subsecond=True)` — e.g.
`[DOWNLOAD] "/path" 1.5 MB in 320ms`. The `t0` is captured at the start of
`_download_file_attempt` / `_upload_file_attempt` (covers the delta-fallback
path too: a failed delta attempt's time is included in the full-transfer
duration).

Compressed downloads stream through `decompress_stream` with a hard output cap
(anti decompression-bomb, `transfer.py:448`); the payload is never fully
buffered in memory. The cap is `min(declared size, MAX_INBOUND_SIZE)`
(`utils.inbound_size_cap` — the single implementation shared with LAN-master
received uploads): the declared size alone is not a
safe bound because a hostile server/master can declare an arbitrary size and
expand a bomb up to it on disk before the SHA-256 check discards the content.
The same cap also bounds uncompressed bodies, so an over-cap download (hostile
or simply larger than the configured maximum) is aborted and the `.part`
dropped. `MAX_INBOUND_SIZE` defaults to 10 GB (`--max-inbound-size`,
mirroring the server's `max_upload_size`; `0` = declared size only). Wire-supplied permissions go through
`utils.set_permissions` (`utils.py:492`) which strips setuid/setgid/sticky bits
(`sanitize_remote_permissions`).

Transfers are serialized per local path via `utils.path_lock` (`utils.py:411`, §14).
Retry policy: `retry.retry_on_transient_transfer_error` (`retry.py:105`); params
from `constants.py:99`–`:101`.

## event_dispatch.py — apply inbound events

`apply_inbound_event` (`event_dispatch.py:148`) routes a remote event dict to
`_apply_add_or_modify` (`:194`, also receives `event_name` to log `[REMOTE CREATE]` vs
`[REMOTE MODIFY]` for the directory branch — the log is gated on `update_dir` returning
non-`None`, so a security-rejected directory is not pre-logged as created), `_apply_rename` (`:245`), `_apply_delete` (`:319`,
locked via `_remove_locked:55`), `_apply_symlink` (`:379`). Echo suppression uses
the per-event `EventDispatchContext` (`:68`) marking (`_mark:138`/`_unmark:143`).
Path resolution: `_resolve_local_path` (`:235`).

**Inbound directory dedup** (`_apply_add_or_modify` + `_DirDedup`): the master
broadcasts a burst of near-identical `added`/`modified` directory events per
`mkdir` (its watcher fires several `IN_ATTRIB` ~1ms apart). Because `update_dir`
returns the `Dir` even for a no-op (existing dir, unchanged permissions), each
duplicate would be re-logged as a fresh `[REMOTE CREATE]`/`[REMOTE MODIFY]`.
A per-connection `_DirDedup` cache (`dir path -> permissions`, threaded through
`EventDispatchContext.dir_dedup`, default per-context) suppresses the repeat:
it is keyed on the **absolute** local path (matching the delete/rename
invalidation), records before acting so a concurrently-dispatched sibling is
caught, and is invalidated on `deleted` (`_apply_delete`) and `renamed`
(`_rename_path`) so a re-created directory with the same permissions is applied
fresh. A genuine change carries different permissions and is never skipped; a
security-rejected path (`update_dir` → `None`) is never recorded.

**Benign-echo degradation** (`_apply_delete`, dir branch `:293` / file branch
`:312`): when an inbound `deleted` event targets a path already absent locally,
the delete has nothing to act on (the originator removed it, a peer's delete
converged after a local deletion, or the path never existed here). This is logged
at **DEBUG**, not WARNING — mirroring the master's "File already deleted" success
branch (`lan_server.py:_handle_delete`) and the rename 404-echo degradation
(`_rename_on_master`). WARNINGs here were noise because echo suppression can miss
when the master has no `LAN_KEY` set (the `mark_inbound_origin` cid gate is a
security measure) or when a directory's children are removed by `rmtree` without
being individually marked.

**Inbound path security gate** (`:139`): every src/dst path is validated with
`hsync_common.path_validation.validate_remote_relpath` before dispatch —
traversal, absolute paths, backslashes and internal engine files
(`.hsync*`/`*.part`) are dropped, logged with `ActionLabel.SECURITY` and
surfaced as a `security_rejected` UI event (`_reject_unsafe:103`). When a
payload carries both `filepath` and `path`, *every* present key is validated
(`hsync_common.wire_relpaths`, SSOT shared with the server relay gate) so a
malicious second key cannot smuggle past the gate.
Delete/rename/symlink targets are additionally **confined** with
`resolve_within` (`_confined_local:113`): lexical safety alone cannot stop a
symlink *inside* the bucket (e.g. `docs -> /home/user`) from redirecting the
operation outside it. `_apply_symlink` also validates the link target with
`is_safe_symlink_target`; `update_dir`/`update_file` (`sync.py`) apply the
same confinement before mkdir/metadata writes.

**Log labels** (`_tag:107`): every bracketed label in this module is resolved
through `hsync_common.labels.resolve_label` (the SSOT shared with
`BaseFileEventHandler._label`), so the watchdog handlers and the inbound
dispatcher emit identical, colorizable tags. Inbound events are always
peer-caused, so the prefix is the module constant `INBOUND_PREFIX = "REMOTE "`
(`:101`) and every dispatch label is `REMOTE_*` (`[REMOTE RENAME]`,
`[REMOTE DELETE]`, `[REMOTE SECURITY]`, `[REMOTE ECHO]`, …). There is no
project-local label table, and `EventDispatchContext` carries no `log_prefix`
field (removed as dead configuration — both inbound contexts would share the
same value).

## file_event_handler.py — watchdog adapter

`BaseFileEventHandler` (`file_event_handler.py:54`) maps watchdog callbacks to the
transfer pipeline. Linux uses `on_closed` (`SUPPORTS_ON_CLOSED`, `:43`); other
platforms fall back to `on_modified`. Subclasses implement `_on_file_ready`
(remote/LAN-master variants). `_label` (`:60`) resolves bracketed log labels
through `hsync_common.labels.resolve_label` with an **origin-aware prefix**:
`"REMOTE "` when `_origin_cid(src, dst)` attributes the event to a peer (the
master's inbound-origin map), `"LOCAL "` otherwise. The static `_LOG_PREFIX`
class attribute no longer exists. `RemoteFileEventHandler` never overrides
`_origin_cid`, so its lines are always `LOCAL_*`; on the LAN master the same
hook produces `REMOTE_*` for peer-caused events, so the master's watchdog
output is colorized identically to the slave's inbound dispatcher. The `[MOVE]`
debug trace (`:324`) is deliberately unprefixed (internal trace, not the
propagated event).

**Safe-save handling** (`on_moved` file branch, `:244`): when an editor
safe-saves (writes to a temp like `.goutputstream-*` or `*.tmp` then atomically
renames over the final file), the temp is hidden (`DEFAULT_IGNORE_PATTERNS`)
so the rename is reframed as a content `modified` of the destination via
`_process_file_ready(dest, trigger="safe-save")` — not a user-initiated
`renamed`.

**Content-aware dedup** (`_process_file_ready`, `:177`): a two-stage filter
suppresses spurious broadcasts without a naive time-based debounce (which was
counterproductive: it suppressed the real safe-save rename while keeping the
preceding no-op close):

1. **Stat pre-check** — compares `(mtime, size)` with the last broadcast for
   the path (`_last_broadcast_stat`). If identical, skips without hashing
   (catches an editor open/close with no write; cost: one `stat()`).
2. **Hash compare** — if the stat changed, computes the SHA-256 (via
   `File.get`, which the code already did) and compares with
   `_last_broadcast_hash`. If the content is identical (e.g. mtime touched
   but bytes unchanged), skips the broadcast. Only a genuine content change
   propagates.

This correctly handles the no-op-close-before-safe-save scenario: the first
close (content unchanged) is skipped at stage 2, and the subsequent rename
(content changed) is broadcast. The hash is already computed by `File.get`,
so the comparison adds zero I/O cost.

**Delete invalidates the dedup cache** (`on_deleted`, `file_event_handler.py`):
after broadcasting `deleted`, the handler pops the path from both
`_last_broadcast_stat` and `_last_broadcast_hash`. Without this, a re-created
file sharing `mtime_ns` + `size` (and even `sha256`) with its pre-deletion
version would be silently skipped at stage 1 as "Skip unchanged (stat)",
dropping the create → delete → **re-create** third event so peers never
receive the re-creation.

**Rename invalidates the dedup cache for the source path** (`on_moved`,
file branch, `file_event_handler.py`): a visible→visible file rename pops
`src_path` from both dedup dicts after building the `renamed` event. Without
this, a file later re-created at the original source path with identical
stat+hash would be skipped at stage 1 the same way as the delete case.
Directories carry no content hash so the invalidation is file-only.

**Directory modify dedup** (`on_modified`, directory branch,
`file_event_handler.py`): inotify fires `IN_ATTRIB` on a directory whenever a
child is created, written, renamed or removed, so a single file save can
emit several identical directory `MODIFY` lines. The directory branch applies
the same stat pre-check as the file stage-1 filter (`(mtime_ns, size)` against
`_last_broadcast_stat`) and suppresses the duplicate broadcast + log line.
The stat is recorded **before** the broadcast (not after): `mkdir` emits a
burst of `IN_ATTRIB` ~1ms apart and the second can be dispatched while
`Dir.get` + broadcast of the first is still in flight, so recording up front
lets the pre-check catch the sibling immediately. A later genuine content
change updates the directory mtime and is never skipped. The log carries a
`(dir)` annotation to distinguish these directory-level metadata events from
file content changes.

## event_queue.py — outbound WS event retries

`EventQueue` (`event_queue.py:77`) buffers `QueuedEvent` (`:50`) and retries
sending over the WS with exponential backoff (`MAX_RETRIES` `:43`, `INITIAL/MAX/_
MULTIPLIER` `:44`–`:46`). `WebSocketProtocol` (`:31`) is the minimal send interface.

> Human-facing: event processing + stability + parallel transfers in
> `../docs/architecture.md`; modes in `../docs/sync-modes.md`.
