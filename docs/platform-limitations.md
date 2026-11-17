# Platform limits

Synchronisation behaves the same on Linux, Windows and macOS for file content,
names and deletions. Filesystem metadata that a platform cannot represent is
mapped explicitly instead of silently diverging, and the few cases that cannot be
represented at all are skipped with a warning in the log.

| Area | Linux / macOS | Windows |
|---|---|---|
| File names | any POSIX name | names that Windows cannot store (reserved names, `< > : " / \| ? *`, control characters, trailing dots/spaces) are rejected on every platform, so a bucket stays portable |
| Permissions | POSIX modes (`600`, `644`, `755`, …) synchronise as-is | only the owner-write bit is representable: it maps to the **Read-only** attribute. Windows reports canonical `644`/`755` (or `444`/`555` when read-only) and applies an incoming mode as read-only only; group/other bits are not represented |
| ACLs | not synchronised | not synchronised |
| Symlinks | created as symlinks | require Windows Developer Mode (or elevation); without it each link is skipped with one warning |
| Long paths | no total-path limit (per-component limit only) | an absolute path longer than 259 characters is skipped with a warning |
| Case sensitivity | case-sensitive | two names that differ only by case cannot coexist: an existing local file always wins, otherwise the lexicographically smallest remote path is kept and the others are skipped with a warning |
| Hidden files | dotfiles / `.hsyncignore` patterns | same pattern-based rules; the Windows hidden attribute is not synchronised |
| Timestamps | nanosecond mtime | mtime drives change detection; FAT/exFAT's 2-second granularity can hide a same-size edit until the content changes |

## Permission model

A device whose filesystem cannot represent full POSIX modes advertises the
`readonly_permissions` capability. Peers then compare permission changes on the
portable bit (writable vs read-only) instead of the full mode string, so a POSIX
`600` and a Windows canonical `644` never bounce back and forth. The transition
between writable and read-only still propagates in both directions.

## Upgrading an existing Windows install

A Windows installation created by an earlier build may hold local state entries
keyed with backslashes. Those entries are ignored by the current build and the
next synchronisation records the correct keys; a one-shot `hsync state reset`
is the clean recommendation for such an install.
