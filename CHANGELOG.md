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
