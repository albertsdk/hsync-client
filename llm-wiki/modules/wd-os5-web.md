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

The p2p section exposes the bootnode field (`HSYNC_BOOTNODE`, a `HOST[:PORT]`
endpoint) and the UPnP toggle (`HSYNC_UPNP_ENABLED`); both are whitelisted in
`config_get.php` / `config_set.php` (with endpoint/boolean validation) and
carried by `install.sh` / exported by `start.sh`. The connect-command builder
prefixes `HSYNC_BOOTNODE='<host[:port]>'` so the other device joins the same
group through the bootnode (mDNS stays active on the same client). The
`HSYNC_MODE` value is `p2p` (not `lan`): `start.sh` maps it to the `p2p`
subcommand, and UI + PHP validation agree.
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
`#HSYNC_P2P_GROUP`, `#HSYNC_P2P_KEY`) on `input`/`change`/`keyup` — delegated
on `document` so they survive dashboard node replacement (`app.js::boot`) — and
on mode switches (`syncModeVisibility`), and re-applied on delayed timeouts so
a post-inject DOM replace cannot leave the field empty. After a successful
**Save** the command is rebuilt from the persisted configuration
(`applyConfig(await loadConfig())` + `renderConnectCommand()`, `app.js::applyConfig`):
`config_set.php` normalizes inputs (invalid bucket → fallback, booleans →
`true`/`false`, invalid numerics → empty), so the section always reflects what
`hsync.env` actually contains, not just the pre-save form values.

Scope: **p2p mode only**. In server mode the NAS is a client of a central
`hsync-server`, so the section shows an explanatory note instead of a command.

Only connection-matching fields are emitted — bucket, mode, group, p2p key.
Per-device sync behaviour and the NAS role are omitted: continuous mode is
the default in both p2p and server mode, and master election resolves the
remote role automatically. The p2p
shared key is rendered masked (`KEY_MASK`, fixed length) with a reveal toggle
(`keyRevealed`); the Copy button copies the unmasked command via
`navigator.clipboard` with an `execCommand` fallback. Every interpolated value
(bucket, path placeholder, group, key) is POSIX single-quoted via `shellQuote`
so the generated command is safe to paste regardless of its contents (no `$` /
backtick / `"` expansion, no `<`/`>` redirection from the placeholder).

> Human doc: `../../packaging/wd-os5/README.md` → "Connect another device".
> Domain rule: any CLI flag / `HSYNC_*` / i18n change requires the synchronized
> `packaging/wd-os5` update (see `../../AGENTS.md`).
