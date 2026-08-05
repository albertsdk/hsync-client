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
