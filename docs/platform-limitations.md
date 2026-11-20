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
| Case sensitivity | case-sensitive | two names that differ only by case cannot coexist, and names that differ only by Unicode normal form (NFC vs NFD) collide on every filesystem (the wire and version keys are NFC): an existing local file always wins, otherwise the lexicographically smallest relative path is kept and the others are skipped with a warning |
| Hidden files | dotfiles / `.hsyncignore` patterns | same pattern-based rules; the Windows hidden attribute is not synchronised |
| Timestamps | nanosecond mtime | mtime drives change detection; FAT/exFAT's 2-second granularity can hide a same-size edit until the content changes |
| LAN addresses | IPv4 and IPv6 (ULA/global) advertised over mDNS; IPv6 link-local is not (no portable scope) | same; a dual-stack listener serves both families, while an IPv4-only fallback advertises only A records |
| State & identity files | POSIX `0700`/`0600` under `~/.local/state/hsync` | `chmod` is a no-op (only the Read-only attribute); the files rely on the user-profile ACL (`%USERPROFILE%`) |
| TLS trust | the platform's default CA store | the platform's default CA store; certificate pinning and `--ca-cert` are the portable path |

## Permission model

A device whose filesystem cannot represent full POSIX modes advertises the
`readonly_permissions` capability. Peers then compare permission changes on the
portable bit (writable vs read-only) instead of the full mode string, so a POSIX
`600` and a Windows canonical `644` never bounce back and forth. The transition
between writable and read-only still propagates in both directions. Directories
always stay traversable on such a host (a non-writable directory mode cannot be
represented), so directory permission divergence is skipped rather than
converged when either side is lossy.

## Upgrading an existing install

The bucket-relative identity of a path is separator-independent and NFC
normalized, so two builds that recorded the same file under different spellings
(pre-Windows backslashes, or a macOS NFD name) derive different state keys. A
`hsync state reset` is the clean recommendation for an install that predates the
current build: it re-records the identity of every file, at the cost of one full
re-hash. The following cases need it:

- a Windows install created by an earlier build (backslash-keyed entries);
- a Linux or Windows install whose files include a **non-NFC** name (typically a
  tree created on macOS, where names are NFD): the version and tombstone keys
  change, so history recorded under the old key is not matched.

## File names

Server and direct mode share one rule: a name must be storable **identically on
every device**. `hsync_common.path_validation.is_valid_filename` is the upload
policy the server applies, and `validate_remote_relpath` is the same rule for
the paths the scan and the wire carry: no `< > : " / \ | ? *` or control
characters, no trailing dot or space, no reserved names (`CON`, `PRN`, `AUX`,
`NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`, with or without an extension), components
under 255 bytes, and no `.`/`..`.

An entry outside it is **left out of the synchronisation and reported** — the scan
warns once and names the first few paths — never renamed: a rename on one side
would stop the copies from agreeing. Rename it locally (or add an ignore rule) and
the next scan picks it up. The server applies the same rule to every uploaded
filename and refuses it (`400`, `HSYNC-1007`) instead of storing a mangled name.

Names differ by case as well as by letters. The index is case-sensitive, so
devices on a case-sensitive filesystem keep `foo` and `Foo` as two files. A device
whose filesystem folds case can hold only one per group, and the group converges
on a spelling every peer computes identically: when the bucket has a single
spelling, the local file is **renamed to it** if only the case differs — the same
file on that filesystem, so no content moves. When the bucket really holds two
different files that differ only by case (created on a case-sensitive device),
this device cannot hold both and nothing is renamed, overwritten or deleted: the
pair is reported in the problems badge until one is renamed on a device that
supports both. A capitalisation-only rename is a normal rename and propagates.
