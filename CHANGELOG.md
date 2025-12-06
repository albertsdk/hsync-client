# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `--once` CLI flag: explicit form of the default one-shot behaviour
  (incompatible with `-w`; in LAN mode use `--no-lan-persistent`).
- Documented one-shot exit-code contract (`hsync_client.exitcodes`): `0` OK,
  `1` fatal, `2` transfer errors, `3` mass-deletion guard tripped,
  `4` conflicts resolved, `5` LAN no-peer. One-shot runs now map the cycle
  verdict (`SyncCycleResult`) onto these codes instead of always exiting `0`.
- Failed-transfer counting inside each sync cycle (transient and permanent),
  feeding the exit-code mapping and future diagnostics.
- `docs/scheduling.md`: cron and systemd timer integration guide with
  env-file secrets, alerting examples, and the exit-code table.

### Changed

- LAN one-shot without peers now exits with code `5` (previously `0`/`1`
  depending on the path).
- A cycle that resolved causal conflicts (created `*_conflicted_copy_*`
  files) now exits with code `4`.
