# WD OS5 web UI (`packaging/wd-os5/Apkg/web`)

The WD My Cloud OS5 package ships a small PHP + vanilla-JS web UI served by the
stock Apache, used to configure and control the NAS daemon without SSH. It is
part of `hsync-client` (see `packaging/wd-os5/README.md` for the build/runtime
layout). This page maps the UI files and the client-side command-generation
logic.

## Files

| File | Role |
| ---- | ---- |
| `Apkg/web/index.html` | markup + scoped CSS; all selectors rooted at `#hsync-app` |
| `Apkg/web/app.js` | controller: load/persist config, daemon service, log viewer, i18n, connect-command generator |
| `Apkg/web/config_get.php` | returns the whitelisted `HSYNC_*` keys from `hsync.env` as JSON |
| `Apkg/web/config_set.php` | writes the whitelisted keys back atomically (POSIX-quoted) |
| `Apkg/web/service.php` | start/stop/restart/status of the daemon |
| `Apkg/web/log_get.php` | incremental tail of `hsync.log` |
| `Apkg/web/i18n/{en,it,de,es,fr}.json` | lightweight dictionary; `app.js` `t()` falls back to the key name |

Config flow: `config_get.php` → `applyConfig` (`app.js`) → form fields →
`collectConfig` → `config_set.php`. The daemon is launched by `Apkg/start.sh`,
which translates `hsync.env` into CLI flags (see `../../llm-wiki/modules/cli-config.md`).

## "Connect another device" command generator

`renderConnectCommand()` (`app.js`) builds the `hsync` command to run on a
second device to join the sync group coordinated by the NAS. The command is
shown in a readonly `#hsync-connect-command` textarea (not `<code>`: the WD
dashboard / remotewd injector can strip or hide `code` contents). It is
recomputed live from the form fields (`#HSYNC_MODE`, `#HSYNC_BUCKET`,
`#HSYNC_LAN_GROUP`, `#HSYNC_LAN_KEY`) on `input`/`change` and on mode switches
(`syncModeVisibility`), and re-applied on delayed timeouts so a post-inject
DOM replace cannot leave the field empty.

Scope: **LAN mode only**. In server mode the NAS is a client of a central
`hsync-server`, so the section shows an explanatory note instead of a command.

Only connection-matching fields are emitted — bucket, mode, group, LAN key.
Per-device sync behaviour and the NAS role are omitted: watch is implicit in
LAN mode and master election resolves the remote role automatically. The LAN
shared key is rendered masked (`KEY_MASK`, fixed length) with a reveal toggle
(`keyRevealed`); the Copy button copies the unmasked command via
`navigator.clipboard` with an `execCommand` fallback. Every interpolated value
(bucket, path placeholder, group, key) is POSIX single-quoted via `shellQuote`
so the generated command is safe to paste regardless of its contents (no `$` /
backtick / `"` expansion, no `<`/`>` redirection from the placeholder).

> Human doc: `../../packaging/wd-os5/README.md` → "Connect another device".
> Domain rule: any CLI flag / `HSYNC_*` / i18n change requires the synchronized
> `packaging/wd-os5` update (see `../../CLAUDE.md`).
