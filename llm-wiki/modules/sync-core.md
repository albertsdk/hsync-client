# HSYNC Client — Sync core

Mode-agnostic engine: list/diff/transfer + the watcher pipeline. Shared by remote
and LAN modes.

## sync.py — reconcile loop

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `sync` | `sync.py:263` | one full reconcile cycle (lan_mode flag) |
| `log_initial_sync_start` | `sync.py:263` (above `sync`) | emits the colored `[INITIAL SYNC]` phase marker; called by the three initial-sync entry points (`lan_handler._run_as_slave`, `remote_websocket.remote_connect`, `main_logic` one-shot) right before the first `sync()` of a run |
| `periodic_resync` | `sync.py:105` | shared RESYNC loop for remote + LAN slave; re-raises `CancelledError` |
| `get_files_local` | `sync.py:863` | scan local tree (parallel, adaptive workers `_get_optimal_workers:816`); rejects file-symlinks whose target escapes `LOCAL_BUCKET` via `is_safe_symlink_target` (security: prevents exfiltrating out-of-bucket targets such as `secret -> /etc/passwd`). `os.walk` (followlinks=False) never descends into symlinked dirs, so an in-bucket dir-symlink pointing outside is not traversed. `_get_optimal_workers` bounds the `ThreadPoolExecutor` by the cgroup CPU quota **and**, when a cgroup memory limit is detected (containers), by `get_memory_limit_bytes` / `WORKER_MEMORY_BUDGET_BYTES` (8 MiB/worker); on bare metal the memory probe is `None` and only the CPU quota applies |
| `get_files_remote` | `sync.py:661` | fetch remote file list |
| `get_tombstones_remote` / `push_tombstones_remote` | `sync.py:950` / `:998` | tombstone exchange |
| offline-deletion tombstone minting | `sync.py`, within `sync()` | a merge-base entry missing from the local filesystem is tombstoned (deleted while offline, or below the watcher's radar in a one-shot run). The existence check uses the ignore-agnostic `_existing_file_paths` (`sync.py`) snapshot only — it enumerates every real file bypassing the active `IgnoreMatcher`, so a file hidden by `.hsyncignore` is never mistaken for a deletion ("exclude protects, it does not delete", rsync `--exclude`, Syncthing `.stignore`); it is a superset of the syncable listing for on-disk files, so the syncable `local_keys` set is redundant and was dropped to fix a mid-scan race (a file deleted while `get_files_remote` retries bridge a transiently-unreachable master would otherwise survive in the stale listing and be resurrected). The same snapshot keeps the merge base alive across `retain`, so re-including the file later reconciles with full causal history instead of a false conflict. Paths are normalized via `relpath_in_bucket` before hashing (absolute-path hashes never match the relative-keyed store) |
| `get_sync_actions` | `sync.py:1140` | diff → `SyncAction`s; applies `--sync-direction` filter; skips unsafe/internal listing entries (`validate_remote_relpath`); `persist_state=False` (preview/dry-run) skips `merge_and_record` so `syncstate.db` is never mutated. **Symlink entries** (`"type": "symlink"`, local or remote listing) are routed to `SyncActionName.upload_symlink` / `download_symlink` — the pair (path,target) is replicated STRUCTURALLY, never as File content (regola 1); direction filters treat them as uploads/downloads |
| remote-only tombstone proofs | `sync.py:1470` | the remote-only branch decides **download vs `delete_remote`** on positive causal proof only: `_tombstone_clock_dominates(entry, remote_vclock)` (`sync.py:1161`) — the tombstone clock dominating the remote advertised version proves the remote copy is a stale ancestor (ADR-0003). Content equality is **not** a delete proof: a file re-created byte-for-byte identically (an empty file being the trivial case — every empty file shares the same SHA-256) is an intentional re-creation and is downloaded (add-wins, mirroring Syncthing's modification-beats-deletion policy). A marker without a valid causal clock never deletes. `delete_remote` is executed best-effort per cycle (`sync.py`) and retried by the next reconcile because the tombstone stays in the local store |
| `_parse_advertised_version` | `sync.py:1108` | shared wire `version` parser (vclock + corrected mtime) used by the remote-only branch, the two-sided compare, and `update_file` — third use extracted per the Rule of Three |
| `_compare_files` | `sync.py:1388` | per-file decision via `resolve_three_way` (ADR-0022/0023): content identity → causal dominance → **corrected_mtime** tie-break on true concurrency → SHA-256 if mtime missing or tied. Raw wall clock is never consulted |
| `_save_conflict_copy` | `sync.py:1228` | write a conflict copy on `conflict_download` (remote won the causal tie-break); resolves bucket-relative action paths against `LOCAL_BUCKET` before reading/preserving the local file (raw relative paths were silently skipped) |
| `_execute_parallel_transfers` | `sync.py:669` | run downloads+uploads via `asyncio.Semaphore` (`MAX_CONCURRENT_TRANSFERS`, `constants.py:90`); catches `PermanentDownloadError`. Threads come from a **process-wide persistent executor** (`_get_transfer_executor:649`, resizable, lazy singleton from the shared `hsync_client.pools.get_singleton_executor`): worker threads — and their per-thread `requests.Session` keep-alive connections — survive across sync cycles instead of being rebuilt (fresh TLS handshake) at every cycle |
| `_mass_deletion_blocked` | `sync.py:134` | §12 guard: pause when deletions exceed threshold/percent or empty master |
| `_permanent_download_failures` / `_reset_permanent_failures` / `_is_permanently_failed` | `sync.py:79` | H8 per-cycle skip-set of permanently-failed downloads (keyed `filepath\|sha256`); cleared at the start of each cycle |
| `update_file` / `update_dir` | `sync.py:1159` / `:1111` | apply inbound metadata; reject unsafe remote paths |
| `ensure_hsynckeep_files` | `sync.py:445` | keep empty dirs via sentinel |
| `_cleanup_partial_files` | `sync.py:102` | drop stale `.part` files |
| `incremental_vacuum_if_due` | `state_maintenance.py:38` | end-of-cycle compaction of `syncstate.db` (ADR-0025), called by `sync()` after `sync_state.flush()`; throttled to `INCREMENTAL_VACUUM_INTERVAL_S` and bounded to `INCREMENTAL_VACUUM_PAGES` free pages. A full `VACUUM` runs once at process exit via `vacuum_on_shutdown` (`state_maintenance.py:53`), wired in `main_logic.run_sync`'s `finally` |

Tombstone-vs-local decision: `_tombstone_deletes_local` (`sync.py:799`,
`MTIME_TOLERANCE` `:796`). `MTIME_TOLERANCE` is used **only** by the
tombstone resurrection guard — content comparison is causal (vector clocks)
and never consults the wall clock.

## Causal conflict resolution (ADR-0022)

`_compare_files` calls `hsync_common.models.version.resolve_three_way` with
hashes, vclocks, `base_hash`, and `local_corrected_mtime` /
`remote_corrected_mtime` (ADR-0023):

| Decision | Meaning | Action |
| -------- | ------- | ------ |
| `in_sync` | content identical | nothing |
| `upload` | local causally ahead (or remote at base) | upload |
| `download` | remote causally ahead (or local at base) | download |
| `conflict_upload` | concurrent; local wins corrected_mtime (SHA-256 fallback) | upload (receiver preserves its loser) |
| `conflict_download` | concurrent; remote wins tie-break | preserve local + download |

The local vclock is resolved **purely** (no store mutation) via
`sync_state.local_version_for_compare` (ticks the stored clock by this node's
cid when the content differs from the recorded base). Persistence happens only
after a successful sync (`merge_and_record` on `in_sync`, `_record_synced_base`
on transfer). `get_sync_actions(persist_state=False)` skips the
`merge_and_record` step so a dry-run/preview never mutates `syncstate.db`.

No node is authoritative: the server and the elected LAN master are transport
hubs. Upload arbitration mirrors `hsync_server.arbitration` (client side in
`hsync_client/arbitration.py`); the tie-break is deterministic by highest
SHA-256 so every node converges without a central authority.

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
| `download_file` / `_download_file_attempt` | `transfer.py:280` / `:326` | retry wrapper + one attempt (raises transient → retry); delta download attempted first via `delta_transfer.download_delta`; the up-to-date shortcut is content-aware (equal/newer local mtime is trusted only when the expected hash matches — required to deliver the master's version after a preserved conflict). The checksum is computed **incrementally while streaming** (a resumed download ingests the on-disk `.part` prefix into the hasher first): no second full read of the partial file after the write |
| `upload_file` / `_upload_file_attempt` | `transfer.py:804` / `:912` | retry wrapper + one attempt; resumable via `.part` + offset; delta upload attempted first via `delta_transfer.upload_delta`. The compressed branch **stream-compresses to a temp file** (`_compress_to_temp_file` → `hsync_common.compress_file_to_file`) and streams it as a regular file part: neither the source nor the zstd frame is ever fully resident in RAM (the old implementation read the whole file into memory before sending) |
| `_get_upload_status` | `transfer.py:680` | query server for resume offset |
| `is_file_stable` / `_file_already_settled` / `_wait_for_stable_file` | `transfer.py` | size+mtime stability gate: `from_closed` → 0.3s re-check; conservative path skips the 5s poll when mtime is already older than `STABILITY_DELAY` (reconcile / atomic rename); otherwise poll until `MAX_WAIT_TIMEOUT` |
| `_reschedule_upload` | `transfer.py:764` | §15: defer torn-file uploads (bounded) |
| `_build_multipart_streaming` / `_SizedStreamingBody` | `transfer.py:172` / `:149` | streaming multipart |
| `_ThrottledFileReader` | `transfer.py:119` | bandwidth throttle on upload reads |
| `_record_synced_base` | `transfer.py:101` | persist merge base after success (sync_state); resolves relative action paths to absolute before recording (raw relative paths were silent no-ops that degraded every later cycle to the mtime heuristic); accepts the merged `vclock` to record (convergence D11). **Durability**: for real-time transfers (watcher uploads / inbound event downloads, detected via `is_sync_cycle_active()` being False) the merge base is flushed to `syncstate.db` immediately, so a process kill cannot lose it and resurrect a file deleted while offline; during a full reconcile cycle the record stays in memory and is flushed once by the single end-of-cycle `sync_state.flush()` (batch, avoids per-file rewrites of the whole version map) |
| `_handle_upload_conflict_response` | `transfer.py` | handle 409 upload responses: `superseded`/`conflict_lost` → download needed (next cycle); legacy "already present" → record base as in-sync |
| `_upload_version_fields` / `_resolve_local_vclock` | `transfer.py` | resolve the effective local `vclock`/`version_id` for the upload form and merge-base recording (delegates to `sync_state.upload_version_fields`) |
| `_emit_transfer_failed` | `transfer.py:93` | emit `transfer_failed` UI event on exhaustion |
| `_local_dest_path` | `transfer.py:265` | confine remote path to `LOCAL_BUCKET` via `resolve_within` (returns `None` on unsafe input) |
| `_open_partial_no_follow` | `utils.py:379` | `.part` writes with `O_NOFOLLOW` where supported (anti-TOCTOU) |
| escaping-symlink upload guard | `upload_file` `transfer.py` | refuses (before any hashing/read) a symlink whose target escapes `LOCAL_BUCKET` via `is_safe_symlink_target` (realtime watch/LAN path; the reconcile routes links structurally instead — see symlink replication below). Emits `security_rejected` |
| `upload_symlink` / `download_symlink` / `materialize_symlink_local` | `transfer.py` (symlink section) | **Structural replication**: `upload_symlink` POSTs `(path, target)` to `/symlinks` (central server AND LAN master — same surface, transport-agnostic); an ABSOLUTE local target is relativized against the link's parent via `hsync_common.wire_symlink_target` (rsync `munge` semantics: the sender's absolute paths must never reach peers; escaping targets → security_rejected). `download_symlink` → `materialize_symlink_local` recreates the LINK locally (confined `resolve_within(..., resolve_final=False)`, target validated `is_safe_symlink_target`), replacing an existing file/link but refusing a non-empty directory. `upload_file` is never used for links in the reconcile path |

After `_wait_for_stable_file` succeeds, `upload_file` snapshots
`(size, st_mtime_ns)` before the wait and compares it after. If both match and
the `File` already carries a digest (watcher / reconcile scan), that digest is
reused. Otherwise it rehashes with `compute_sha256` — `File.get`'s cache is
keyed on `int(mtime)+size` and would miss a same-second same-size rewrite.

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

`apply_inbound_event` (`event_dispatch.py:228`) routes a remote event dict to
`_apply_add_or_modify` (`:274`, also receives `event_name` to log `[REMOTE CREATE]` vs
`[REMOTE MODIFY]` for the directory branch — the log is gated on `update_dir` returning
non-`None`, so a security-rejected directory is not pre-logged as created), `_apply_rename` (`:369`), `_apply_delete` (`:449`,
locked via `_remove_locked:67`), `_apply_symlink` (`:523`). Echo suppression uses
the `EventDispatchContext` (`:121`) marking (`_mark:218`/`_unmark:223`).
Path resolution: `_resolve_local_path` (`:334`).

**Inbound directory dedup** (`_apply_add_or_modify` + `_DirDedup`): the master
broadcasts a burst of near-identical `added`/`modified` directory events per
`mkdir` (its watcher fires several `IN_ATTRIB` ~1ms apart). There are two
suppression layers, both keyed on the **absolute** local path:

1. **Converged no-op degradation**: before the apply, the local directory is
   snapshotted (`Dir.get`); if it already exists with the same permissions the
   event carries (or the event carries no permissions to adjust), `update_dir`
   mutates nothing and the event degrades to a DEBUG `Already converged` line,
   with **no** `transfer` UI event. This makes the echo loopback of the node's
   own `POST /dirs` (visible when no `LAN_KEY` enables CID echo suppression)
   truthful instead of a fake INFO `[REMOTE CREATE]`. A directory that did not
   exist before is a real creation and stays INFO.
2. **`_DirDedup` cache** (`dir path -> permissions`, threaded through
   `EventDispatchContext.dir_dedup`, default per-context): suppresses the
   repeat of a genuine apply — it is keyed on the **absolute** local path
   (matching the delete/rename invalidation), records before acting so a
   concurrently-dispatched sibling is caught, and is invalidated on `deleted`
   (`_apply_delete`) and `renamed` (`_rename_path`) so a re-created directory
   with the same permissions is applied fresh. A genuine change carries
   different permissions and is never skipped; a security-rejected path
   (`update_dir` → `None`) is never recorded or degraded.

The LAN WebSocket loop builds **one context per connection** via
`_build_lan_dispatch_ctx` (`lan_websocket.py`) and passes it to every
`_handle_lan_event` call; the context-scoped `dir_dedup` factory guarantees the
cache lives for the whole connection. Remote mode reuses a module-level
`REMOTE_DISPATCH_CTX` (`remote_websocket.py:249`).

**Symlink echo marking + idempotency** (`_apply_symlink`,
`event_dispatch.py:523`): the mutation section (unlink/rmdir cleanup +
`os_symlink`) is wrapped in `_mark(ctx, local)`/`finally: _unmark(ctx, local)`
— placed *after* mkdir of the parent and *after* all validations, so a rejected
(unsafe) path is never marked. Without the mark, a LAN peer materialising an
inbound link re-broadcasts its own `deleted`/`created` watcher events to the
master (its `_receiving_files` echo filter never sees the path), which
broadcasts back: an infinite ping-pong loop across peers. On top of the mark,
the function is **idempotent**: a link already present with the SAME target
(`readlink == target`) is a converged echo and is skipped before any mutation,
so no watcher events fire at all. This breaks the loop even when inotify
delivers the `deleted`/`created` pair after the mark/unmark window (the window
only covers the instant syscall, while watchdog events arrive later). A
different target — or a missing link — is a real change and goes through. In
remote mode the `REMOTE_DISPATCH_CTX` has no marking callbacks, so
`_mark`/`_unmark` are no-ops (the central server never echoes an event back to
its sender via `ConnectionManager.send_message`).

**Benign-echo degradation** (`_apply_delete`, dir branch `:482` / file branch
`:510`): when an inbound `deleted` event targets a path already absent locally,
the delete has nothing to act on (the originator removed it, a peer's delete
converged after a local deletion, or the path never existed here). This is logged
at **DEBUG**, not WARNING — mirroring the master's "File already deleted" success
branch (`lan_server.py:_handle_delete`) and the rename 404-echo degradation
(`_rename_on_master`). WARNINGs here were noise because echo suppression can miss
when the master has no `LAN_KEY` set (the `mark_inbound_origin` cid gate is a
security measure) or when a directory's children are removed by `rmtree` without
being individually marked.

**Recursive inbound delete echo** (`_apply_delete` dir branch,
`event_dispatch.py`): before `rmtree`, the confined absolute root path is
passed to `EventDispatchContext.record_inbound_delete` (new callback; `None`
in remote mode). `_mark`/`_unmark` only cover the tree root, so the applying
node's own watcher still fires `deleted` per descendant removed by `rmtree`:
the LAN slave caches the root with a 10 s TTL (`record_inbound_delete` /
`is_inbound_delete_echo` in `lan_websocket.py`, exact + descendant match with
the same `/`-suffix guard as the rename echo) and its `on_deleted` drops those
echoes instead of re-sending them to the master (where each would 404 as a
benign echo). Remote mode never installs the callback — the central server
never echoes an event back to its sender.

**Inbound path security gate** (`:228`): every src/dst path is validated with
`hsync_common.path_validation.validate_remote_relpath` before dispatch —
traversal, absolute paths, backslashes and internal engine files
(`.hsync*`/`*.part`) are dropped, logged with `ActionLabel.SECURITY` and
surfaced as a `security_rejected` UI event (`_reject_unsafe:207`). When a
payload carries both `filepath` and `path`, *every* present key is validated
(`hsync_common.wire_relpaths`, SSOT shared with the server relay gate) so a
malicious second key cannot smuggle past the gate.
Delete/rename/symlink targets are additionally **confined** with
`resolve_within` (`_confined_local:177`): lexical safety alone cannot stop a
symlink *inside* the bucket (e.g. `docs -> /home/user`) from redirecting the
operation outside it. Delete/rename/symlink-materialisation use
`resolve_final=False` (the final component is NOT dereferenced): deleting or
replacing an existing link acts on the LINK entry — a dangling link is a real
directory entry and must be removable (`_path_present` = `is_symlink() or
exists()`). `_apply_symlink` also validates the link target with
`is_safe_symlink_target`; `update_dir`/`update_file` (`sync.py`) apply the
full-resolve confinement before mkdir/metadata writes.

**Log labels** (`_tag:166`): every bracketed label in this module is resolved
through `hsync_common.labels.resolve_label` (the SSOT shared with
`BaseFileEventHandler._label`), so the watchdog handlers and the inbound
dispatcher emit identical, colorizable tags. Inbound events are always
peer-caused, so the prefix is the module constant `INBOUND_PREFIX = "REMOTE "`
(`:163`) and every dispatch label is `REMOTE_*` (`[REMOTE RENAME]`,
`[REMOTE DELETE]`, `[REMOTE SECURITY]`, `[REMOTE ECHO]`, …). There is no
project-local label table, and `EventDispatchContext` carries no `log_prefix`
field (removed as dead configuration — both inbound contexts would share the
same value).

## file_event_handler.py — watchdog adapter

**Watcher lifecycle (limits, Linux)** — every watcher in the client (LAN master
`lan_websocket.py:514`, LAN slave `:839`, remote continuous
`remote_websocket.py:203`, `.hsyncignore` hot-reload `main_logic.py:458`) is
owned by a `WatcherManager` (`watcher.py:105`), which records every `(handler,
path, recursive)` registration so a backend swap can replay the full watch set
onto a fresh observer. On an inotify-limit `OSError` (`EMFILE` instances /
`ENOSPC` watches) or a descriptor-exhaustion `EMFILE`/`ENFILE`
(`watcher.watcher_oserror_class`, `watcher.py:82`), the manager logs the
parsable `[WATCHER FALLBACK]` / `[FD EXHAUSTED]` line and **downgrades to a
`PollingObserver`** (no inotify resources, higher CPU, ~1 s event latency)
instead of blocking realtime sync. A limit hit at runtime (adding a watch for a
new directory, `WatcherManager.schedule_directory` `:166`) is detected inline,
the single watch is skipped (RESYNC reconciles it) and the downgrade is
performed at a safe point by a supervisor thread. Swaps are adopted only while
the manager is not `stop()`ed: an observer whose build raced a `stop()` is torn
down instead, so no live watcher can outlive `stop()` (and a swap failure —
e.g. polling defeated by true descriptor exhaustion — keeps the current backend
and retries at the grown cadence). While degraded, the supervisor periodically
probes inotify and restores it (`[WATCHER RESTORE]`) once the limits free up;
failed probes and downgrade retries grow the same exponential backoff
(`WATCHER_UPGRADE_PROBE_*`, `constants.py`), and a successful upgrade drops a
stale downgrade request. Only if the polling fallback also fails does the manager
log `[WATCHER ERROR]` and raise `exceptions.WatcherStartError` (carrying the
raw kernel error in `.original`), which the retry policies treat specially
because a kernel limit never clears at the normal cadence: the LAN slave
(`lan_handler._run_as_slave`)
and the LAN master (tearing down the half-started WSS server,
`lan_handler._run_as_master`) sleep `WATCHER_START_RETRY_DELAY_S` (60 s,
`constants.py`) before re-entering their loops; remote mode reuses its
exponential backoff with a dedicated log line. User-facing diagnosis/remedies:
`docs/troubleshooting.md`; parsable log rows in `hsync-common/docs/log-labels.md`
(`[WATCHER FALLBACK]`, `[WATCHER RESTORE]`, `[FD EXHAUSTED]`, client-only).

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

**Safe-save handling** (`on_moved` file branch, `:341`): when an editor
safe-saves (writes to a temp like `.goutputstream-*` or `*.tmp` then atomically
renames over the final file), the temp is hidden (`DEFAULT_IGNORE_PATTERNS`)
so the rename is reframed as a content `modified` of the destination via
`_process_file_ready(dest, trigger="safe-save")` — not a user-initiated
`renamed`.

**Symlink broadcast guard** (`on_created`, `file_event_handler.py:192`): the
shared watchdog handler (remote + LAN master) refuses to broadcast a `symlink`
event whose target escapes `LOCAL_BUCKET` via `is_safe_symlink_target` — an
escaping link such as `secret -> /etc/passwd` must never reach peers (the
server/master would re-create the out-of-bucket link, and a LAN slave that
materialises links as files would copy the target's content). In-bucket
symlinks are broadcast normally.

**Content-aware dedup** (`_process_file_ready`, `:271`): a two-stage filter
suppresses spurious broadcasts without a naive time-based debounce (which was
counterproductive: it suppressed the real safe-save rename while keeping the
preceding no-op close):

1. **Stat pre-check** — compares `(mtime, size, mode)` with the last broadcast
   for the path (`_last_broadcast_stat`, `_stat_key` masks `st_mode` to the
   permission bits `0o7777`). If identical, skips without hashing (catches an
   editor open/close with no write; cost: one `stat()`).
2. **Hash compare** — if the stat changed, computes the SHA-256 (via
   `File.get`, which the code already did) and compares with
   `_last_broadcast_hash`. If the content is identical (e.g. mtime touched
   but bytes unchanged), skips the broadcast. Only a genuine content change
   propagates.

**Permission-only changes (chmod)** propagate as a normal `modified` change
(no special policy), driven by the mode half of the stat key:

- **Linux (inotify)**: a `chmod` fires only `IN_ATTRIB` (no `IN_CLOSE_WRITE`),
  so `on_modified` calls `_process_permission_only` (`file_event_handler.py`),
  which refreshes the cached mode and, on a real change, invokes the transport
  hook `_on_permissions_changed(file, path)`. Content bytes are NEVER read from
  this path (a concurrent partial write would be torn). The mode baseline lives
  in the dedicated `_last_mode` map — deliberately SEPARATE from
  `_last_broadcast_stat`: writing the first-observed stat there poisoned stage 1
  of `_process_file_ready` (a fast single-syscall write reaches its final size
  before the handler runs, so `on_closed` saw the same stat and skipped the
  file as "unchanged", silently losing the upload); `_last_mode` is kept in
  step with the last *propagated* content in `_record_broadcast` and
  invalidated on rename/delete/eviction.
- **Non-inotify platforms**: the same mode comparison lives in stage 2 of
  `_process_file_ready` — a hash match with a different mode triggers the hook
  instead of the skip.
- **Transports**: the Remote handler uploads the file with
  `permissions_only=True`; when the upload-status precheck reports
  `complete`, the `PATCH …/files/permissions` endpoint is tried first
  (`patch_permissions_remote` / `_patch_permissions_only`, `transfer.py`):
  the mode travels alone, NO file bytes cross the wire. When the peer does
  not expose the endpoint (older version), the legacy same-SHA full-upload
  branch is the fallback. The LAN master broadcasts `modified` so slaves
  converge via `update_file`. The LAN slave handler
  (`LANSlaveFileEventHandler.on_modified` Linux branch) keeps its own
  `_last_mode` map and uploads on mode change so the master re-applies the
  permissions to itself and re-broadcasts.
- **One-shot reconcile** (`get_sync_actions`, `sync.py`): when content hashes
  agree but permissions differ, the `nothing` action is reclassified —
  `upload` in bidirectional/upload-only (local permissions win) or local
  `set_permissions(remote)` in download-only (remote wins). The permission
  mutation is gated on `persist_state` so a dry-run never chmods anything.

**Directory permission-only changes (chmod on a dir)** propagate through the
directory metadata surface (directories carry no content hash and never
appear in the `/files` listing):

- **Remote / LAN-master watcher** (`BaseFileEventHandler.on_modified`,
  directory branch, `file_event_handler.py`): a directory `chmod` fires only
  `IN_ATTRIB`, which watchdog delivers as a directory `modified` event. The
  branch detects a mode-only change — `st_mode` changed but `st_mtime` and
  `st_size` unchanged — and logs `[PERMISSION] mode change: "<path>" (dir)`
  (with the `(CID:)` suffix when peer-attributed on the master) instead of
  `[LOCAL/REMOTE MODIFY]`. The `modified` event with the `Dir` (including the
  new `permissions`) is broadcast unchanged. This mirrors the file-chmod path
  (`_process_permission_only`, logged `[PERMISSION] mode change`) and the LAN
  slave's directory-chmod line.
- **LAN slave watcher** (Linux IN_ATTRIB): the slave keeps a `_last_dir_mode`
  map and, on a mode change, pushes the directory through `POST /dirs` with
  `permissions` set (`_propagate_dir_permission_change`, `lan_websocket.py`);
  the master applies the mode (owner rwx floor) and its watcher re-broadcasts
  the change.
- **Inbound events**: `update_dir` (`sync.py`) applies the remote mode with
  the owner rwx floor via `set_permissions_on_dir` (`utils.py`) — a peer mode
  like `000` can never lock the receiver out of its own storage.
- **One-shot reconcile (download-only)**: `converge_directory_permissions`
  (`sync.py`) compares the dedicated `GET /dirs` listing (exposed by both the
  server and the LAN master) against the local tree and applies the remote
  mode where it diverges. Runs only inside `sync()`, never in previews.

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
`file_event_handler.py`): a visible→visible rename pops `src_path` from both
dedup dicts — the file branch after building the `renamed` event, the
directory branch before logging it. Without this, a path later re-created at
the original source location with an identical stat (and hash) would be
skipped at stage 1 the same way as the delete case. Renaming preserves the
directory's mtime, so the directory invalidation is what lets an undone
rename (dir back to its old path) re-broadcast instead of being
stat-suppressed by the directory-create dedup.

**Delete buffering dedup** (`on_deleted`, `file_event_handler.py`): the
coalescing buffer rejects an identical `(path, is_directory)` pair already
pending in the same window. A directory with its own watch reports its
deletion twice (`IN_DELETE` from the parent's watch and `IN_DELETE_SELF` from
its own watch on the same path); before this guard the flush emitted two
identical `[LOCAL DELETE]` lines and two identical `deleted` broadcasts. The
LAN slave's own buffer (`lan_websocket.py`) applies the same dedup before
sending the single recursive DELETE to the master.

**Directory modify dedup** (`on_modified`, directory branch,
`file_event_handler.py`): inotify fires `IN_ATTRIB` on a directory whenever a
child is created, written, renamed or removed, so a single file save can
emit several identical directory `MODIFY` lines. The directory branch applies
the same stat pre-check as the file stage-1 filter (`(mtime_ns, size, mode)`
against `_last_broadcast_stat`) and suppresses the duplicate broadcast + log
line. The stat is recorded **before** the broadcast (not after): `mkdir` emits
a burst of `IN_ATTRIB` ~1ms apart and the second can be dispatched while
`Dir.get` + broadcast of the first is still in flight, so recording up front
lets the pre-check catch the sibling immediately. A later genuine content
change updates the directory mtime and is never skipped. The log carries a
`(dir)` annotation to distinguish these directory-level metadata events from
file content changes.

**Directory-modify time window** (same branch): the stat check alone cannot
collapse the burst of a recursive operation — each child create/remove bumps
the directory mtime, so every `IN_ATTRIB` has a *distinct* `(mtime_ns, size,
mode)` and passes the stat check. A second guard folds a follow-up `MODIFY`
for the same path within `DIR_MODIFY_WINDOW` (0.1s, `file_event_handler.py`)
**only when the mode is unchanged**: pure mtime/size churn (an inbound
`rmtree` applied on the receiving side, a deep download, a `mkdir` burst) is
noise, while a `chmod` (mode change) is the one meaningful directory signal
and always falls through to broadcast. When it does, the branch detects a
mode-only change (`st_mode` changed, `st_mtime`/`st_size` unchanged) and logs
`[PERMISSION] mode change: "<path>" (dir)` instead of `[LOCAL/REMOTE MODIFY]`
(see "Directory permission-only changes" above). The timestamp lives in a
parallel `_last_broadcast_ts` map, written in `_record_broadcast` and
evicted/invalidated in lockstep with `_last_broadcast_stat`/`_last_broadcast_hash`.

**Directory create dedup** (`on_created`, directory branch,
`file_event_handler.py`): mirrors the directory-modify dedup. Watchdog emits
a synthetic `DirCreatedEvent` for children already present whenever a watch
is scheduled on a fresh directory (the `observer.schedule` below), so a
chain of mkdirs can fire two `DirCreatedEvent` for the same directory within
milliseconds. The branch applies the same `(mtime_ns, size)` stat pre-check
against `_last_broadcast_stat` and records before logging/broadcasting, so
the duplicate is suppressed (one `[LOCAL/REMOTE CREATE]` line + one broadcast
per directory). The delete flush and the directory-rename branch both
invalidate the entry (`_flush_pending_deletes` / `on_moved`), so a
re-created directory with an identical stat is never skipped. If the
directory is already gone at event time (`stat()` raises), the event is
noise and is dropped.

**Recursive-delete MODIFY suppression** (same branch): before the stat
pre-check, two guard clauses drop the `IN_ATTRIB` noise of an `rmtree` —
(a) `_is_covered_by_pending_delete` (`file_event_handler.py`): the path
equals or descends from a directory already buffered in `_pending_deletes`
(snapshot under `_delete_lock`, because the flush timer drains it from
another thread); (b) `stat()` raising OSError: the directory is already gone.
During `rm -rf` the removed ancestors are buffered before the surrounding
MODIFY burst is delivered, so both guards suppress the spurious per-child
MODIFY lines that mtime changes would otherwise keep "fresh". A path inside
the coalescing window but NOT covered by a delete (e.g. a sibling dir) still
broadcasts normally.

## event_queue.py — outbound WS event retries

`EventQueue` (`event_queue.py:111`) buffers `QueuedEvent` (`:86`) and retries
sending over the WS with exponential backoff (`MAX_RETRIES` `:53`, `INITIAL/MAX/_
MULTIPLIER` `:54`–`:56`). `WebSocketProtocol` (`:41`) is the minimal send interface.

`_send_event` (`event_queue.py:157`) serializes **before** sending: a payload
that cannot be JSON-serialized (`TypeError`/`ValueError` from `dumps`) is a
**permanent** failure — retries would re-serialize the same broken payload and
stall the queue with head-of-line blocking — so the event is dropped immediately
(terminal outcome) and the next event proceeds; the affected file is picked up
by the next full sync. Transient `send` failures are retried as before.

`_await_event` (`:194`) races `queue.get()` against `_stop_event`. Both waiters
are always cancelled-and-awaited via `_await_cancelled` (`:59`), including when
`process()` itself is cancelled in the same tick as `stop()` (`remote_events`
finally). Leaving a `Queue.get()` task pending and dropping the queue used to
log `Task was destroyed but it is pending!` during later GC (e.g. a large
upload).

> Human-facing: event processing + stability + parallel transfers in
> `../../docs/internal/architecture.md`; modes in `../../docs/sync-modes-server.md` and `../../docs/sync-modes-lan.md`.
