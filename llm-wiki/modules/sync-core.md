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
| `get_sync_actions` | `sync.py:684` | diff → `SyncAction`s; applies `--sync-direction` filter |
| `_compare_files` | `sync.py:820` | per-file decision (hash/mtime + base via sync_state) |
| `_save_conflict_copy` | `sync.py:905` | write a conflict copy on three-way conflict |
| `_execute_parallel_transfers` | `sync.py:267` | run downloads+uploads via `asyncio.Semaphore` (`MAX_CONCURRENT_TRANSFERS`, `constants.py:90`) |
| `_mass_deletion_blocked` | `sync.py:94` | §12 guard: pause when deletions exceed threshold/percent or empty master |
| `update_file` / `update_dir` | `sync.py:976` / `:942` | apply inbound metadata |
| `ensure_hsynckeep_files` | `sync.py:326` | keep empty dirs via sentinel |
| `_cleanup_partial_files` | `sync.py:62` | drop stale `.part` files |

Tombstone-vs-local decision: `_tombstone_deletes_local` (`sync.py:653`,
`MTIME_TOLERANCE` `:650`).

## transfer.py — file I/O over HTTP(S)

| Symbol | Line | Role |
| ------ | ---- | ---- |
| `download_file` / `_download_file_attempt` | `transfer.py:248` / `:281` | retry wrapper + one attempt (raises transient → retry) |
| `upload_file` / `_upload_file_attempt` | `transfer.py:665` / `:735` | retry wrapper + one attempt; resumable via `.part` + offset |
| `_get_upload_status` | `transfer.py:554` | query server for resume offset |
| `is_file_stable` / `_wait_for_stable_file` | `transfer.py:498` / `:591` | size+mtime stability gate; fast path on `from_closed` |
| `_reschedule_upload` | `transfer.py:627` | §15: defer torn-file uploads (bounded) |
| `_build_multipart_streaming` / `_SizedStreamingBody` | `transfer.py:148` / `:125` | streaming multipart |
| `_ThrottledFileReader` | `transfer.py:95` | bandwidth throttle on upload reads |
| `_record_synced_base` | `transfer.py:77` | persist merge base after success (sync_state) |
| `_emit_transfer_failed` | `transfer.py:72` | emit `transfer_failed` UI event on exhaustion |
| `_local_dest_path` | `transfer.py:236` | resolve local target (used with `path_lock`) |

Transfers are serialized per local path via `utils.path_lock` (`utils.py:411`, §14).
Retry policy: `retry.retry_on_transient_transfer_error` (`retry.py:105`); params
from `constants.py:99`–`:101`.

## event_dispatch.py — apply inbound events

`apply_inbound_event` (`event_dispatch.py:103`) routes a remote event dict to
`_apply_add_or_modify` (`:137`), `_apply_rename` (`:167`), `_apply_delete` (`:221`,
locked via `_remove_locked:48`), `_apply_symlink` (`:263`). Echo suppression uses
the per-event `EventDispatchContext` (`:62`) marking (`_mark:93`/`_unmark:98`).
Path resolution: `_resolve_local_path` (`:157`).

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
