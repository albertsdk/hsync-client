# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Fixed `KeyError: "'HTTP_TIMEOUT'"` raised by loguru while formatting the
  LAN autotune summary message. The autotune updates dict was interpolated
  into an f-string, leaving `{'HTTP_TIMEOUT': ...}` in the message, which
  loguru's callable formatter then parsed as a format placeholder. The dict
  is now serialized and escaped via `safe_json_for_log` before logging
  (`hsync_client/lan_handler.py`).
- Fixed silent drop of the re-creation event in a create → delete →
  re-create sequence of the same file (same path, same content, preserved
  mtime). The per-path content-dedup cache (`_last_broadcast_stat` /
  `_last_broadcast_hash`) was not invalidated on deletion, so the master
  skipped the third event as "Skip unchanged (stat)" and peers never
  received the re-creation. `on_deleted` now pops the path from both dedup
  dicts before recording the tombstone (`hsync_client/file_event_handler.py`).
  The same invalidation is now applied in `on_moved` for the source path of
  a visible→visible file rename, so a re-creation at the original path with
  identical stat+hash is not skipped either.
- Fixed missing log line for inbound directory creation/modification. When the
  master broadcast an `added`/`modified` event for a directory, the slave
  created the directory via `update_dir` but emitted no `[CREATE]`/`[MODIFY]`
  log line (the file branch was already covered by `download_file`'s
  `[DOWNLOAD]` line). `_apply_add_or_modify` now logs the directory event
  explicitly after `update_dir` succeeds, so the slave's log shows the
  directory operation and not only the subsequent `.hsynckeep` sentinel
  upload. The log is gated on `update_dir`'s return value so a
  security-rejected directory (symlink escape) is not misleadingly logged as
  created before the `[SECURITY]` rejection line
  (`hsync_client/event_dispatch.py`).
- Reduced log noise from duplicate directory `MODIFY` lines. inotify fires
  `IN_ATTRIB` on a directory whenever a child is created, written, renamed
  or removed, so a single file save could emit several identical
  `[LAN MODIFY]` lines for the parent directory. The directory branch of
  `on_modified` now applies the same stat-based dedup as the file stage-1
  filter (`(mtime_ns, size)` against `_last_broadcast_stat`) and annotates
  the surviving lines with `(dir)` to distinguish them from file content
  changes (`hsync_client/file_event_handler.py`).
