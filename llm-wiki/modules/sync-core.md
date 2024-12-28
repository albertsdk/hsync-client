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
| `_execute_parallel_transfers` | `sync.py:267` | run downloads+uploads via `asyncio.Semaphore` (`MAX_CONCURRENT_TRANSFERS`, `constants.py:90`) |
| `_mass_deletion_blocked` | `sync.py:94` | §12 guard: pause when deletions exceed threshold/percent or empty master |
| `update_file` / `update_dir` | `sync.py:996` / `:957` | apply inbound metadata; reject unsafe remote paths |
| `ensure_hsynckeep_files` | `sync.py:326` | keep empty dirs via sentinel |
| `_cleanup_partial_files` | `sync.py:62` | drop stale `.part` files |

Tombstone-vs-local decision: `_tombstone_deletes_local` (`sync.py:653`,
`MTIME_TOLERANCE` `:650`).

## transfer.py — file I/O over HTTP(S)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `download_file` / `_download_file_attempt` | `transfer.py:270` / `:316` | retry wrapper + one attempt (raises transient → retry) |
| `upload_file` / `_upload_file_attempt` | `transfer.py:697` / `:770` | retry wrapper + one attempt; resumable via `.part` + offset |
| `_get_upload_status` | `transfer.py:598` | query server for resume offset |
| `is_file_stable` / `_wait_for_stable_file` | `transfer.py:542` / `:635` | size+mtime stability gate; fast path on `from_closed` |
| `_reschedule_upload` | `transfer.py:671` | §15: defer torn-file uploads (bounded) |
| `_build_multipart_streaming` / `_SizedStreamingBody` | `transfer.py:154` / `:131` | streaming multipart |
| `_ThrottledFileReader` | `transfer.py:101` | bandwidth throttle on upload reads |
| `_record_synced_base` | `transfer.py:80` | persist merge base after success (sync_state) |
| `_emit_transfer_failed` | `transfer.py:75` | emit `transfer_failed` UI event on exhaustion |
| `_local_dest_path` | `transfer.py:242` | confine remote path to `LOCAL_BUCKET` via `resolve_within` (returns `None` on unsafe input) |
| `_open_partial_no_follow` | `transfer.py:257` | `.part` writes with `O_NOFOLLOW` where supported (anti-TOCTOU) |

Compressed downloads stream through `decompress_stream` with a hard output cap
(anti decompression-bomb, `transfer.py:448`); the payload is never fully
buffered in memory. The cap is `min(declared size, MAX_DOWNLOAD_SIZE)`
(`utils.inbound_size_cap` — the single implementation shared with LAN-master
received uploads): the declared size alone is not a
safe bound because a hostile server/master can declare an arbitrary size and
expand a bomb up to it on disk before the SHA-256 check discards the content.
The same cap also bounds uncompressed bodies, so an over-cap download (hostile
or simply larger than the configured maximum) is aborted and the `.part`
dropped. `MAX_DOWNLOAD_SIZE` defaults to 10 GB (`--max-download-size`,
mirroring the server's `max_upload_size`; `0` = declared size only). Wire-supplied permissions go through
`utils.set_permissions` (`utils.py:492`) which strips setuid/setgid/sticky bits
(`sanitize_remote_permissions`).

Transfers are serialized per local path via `utils.path_lock` (`utils.py:411`, §14).
Retry policy: `retry.retry_on_transient_transfer_error` (`retry.py:105`); params
from `constants.py:99`–`:101`.

## event_dispatch.py — apply inbound events

`apply_inbound_event` (`event_dispatch.py:124`) routes a remote event dict to
`_apply_add_or_modify` (`:158`), `_apply_rename` (`:188`), `_apply_delete` (`:242`,
locked via `_remove_locked:48`), `_apply_symlink` (`:295`). Echo suppression uses
the per-event `EventDispatchContext` (`:62`) marking (`_mark:93`/`_unmark:98`).
Path resolution: `_resolve_local_path` (`:178`).

**Inbound path security gate** (`:139`): every src/dst path is validated with
`hsync_common.path_validation.validate_remote_relpath` before dispatch —
traversal, absolute paths, backslashes and internal engine files
(`.hsync*`/`*.part`) are dropped, logged `[SECURITY]` and surfaced as a
`security_rejected` UI event (`_reject_unsafe:103`). When a payload carries
both `filepath` and `path`, *every* present key is validated
(`hsync_common.wire_relpaths`, SSOT shared with the server relay gate) so a
malicious second key cannot smuggle past the gate.
Delete/rename/symlink targets are additionally **confined** with
`resolve_within` (`_confined_local:113`): lexical safety alone cannot stop a
symlink *inside* the bucket (e.g. `docs -> /home/user`) from redirecting the
operation outside it. `_apply_symlink` also validates the link target with
`is_safe_symlink_target`; `update_dir`/`update_file` (`sync.py`) apply the
same confinement before mkdir/metadata writes.

## file_event_handler.py — watchdog adapter

`BaseFileEventHandler` (`file_event_handler.py:49`) maps watchdog callbacks to the
transfer pipeline. Linux uses `on_closed` (`SUPPORTS_ON_CLOSED`, `:43`); other
platforms debounce `on_modified` (`MODIFIED_DEBOUNCE_INTERVAL`, `:46`). `on_closed`
threads `from_closed=True` into the stability fast path. Subclasses implement
`_on_file_ready` (remote/LAN-master variants).

## event_queue.py — outbound WS event retries

`EventQueue` (`event_queue.py:77`) buffers `QueuedEvent` (`:50`) and retries
sending over the WS with exponential backoff (`MAX_RETRIES` `:43`, `INITIAL/MAX/_
MULTIPLIER` `:44`–`:46`). `WebSocketProtocol` (`:31`) is the minimal send interface.

> Human-facing: event processing + stability + parallel transfers in
> `../docs/architecture.md`; modes in `../docs/sync-modes.md`.
