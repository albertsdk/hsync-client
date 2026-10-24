# WD OS5 web UI (`packaging/wd-os5/Apkg/web`)

The WD My Cloud OS5 package ships a PHP + vanilla-JS web UI served by the stock
Apache, used to configure and control the NAS daemon without SSH. It exposes the
same options as `hsync-desktop` (`hsync_desktop/config.py` is the reference for
defaults and wording); the NAS-specific additions are the explicit pin/TOFU
fields and the dangerous opt-ins, which the desktop handles with dialogs.
See `packaging/wd-os5/README.md` for the build/runtime layout.

## Files

| File | Role |
| ---- | ---- |
| `Apkg/web/index.html` | markup + scoped CSS; every selector rooted at `#hsync-app` |
| `Apkg/web/app.js` | controller: load/persist config, service, log viewer, ignore editor, trust manager, probes, i18n, connect-command generator |
| `Apkg/web/env_lib.php` | runtime paths, POSIX `.env` parser, shared-folder resolver, binary runner, JSON reply, atomic private write |
| `Apkg/web/config_get.php` | returns the whitelisted `HSYNC_*` keys from `hsync.env` as JSON |
| `Apkg/web/config_set.php` | validates + writes the whitelisted keys atomically (ranges via `clamp_int`) |
| `Apkg/web/hsyncignore_get.php` / `_set.php` | read/replace `<HSYNC_PATH>/.hsyncignore` (hot-reloaded by the client) |
| `Apkg/web/trust.php` | `hsync trust <kind> list|add|remove` for bootnode/server/peer |
| `Apkg/web/state_reset.php` | stop → `hsync state reset --folder … --quarantine --json` → start |
| `Apkg/web/test.php` | reachability + fingerprint via `hsync trust <kind> fingerprint` |
| `Apkg/web/ca_cert_upload.php` | stores a PEM as `config/ca.pem` (the `HSYNC_CA_CERT` path) |
| `Apkg/web/service.php` | start/stop/restart/status of the daemon |
| `Apkg/web/log_get.php` | incremental tail of `hsync.log` |
| `Apkg/web/i18n/{en,it,de,es,fr,pt,zh,ru,nl,pl,ja}.json` | dictionary; `app.js` `t()` falls back to the key name |

Config flow: `config_get.php` → `applyConfig` (`app.js`) → form fields →
`collectConfig` → `config_set.php`. The daemon is launched by `Apkg/start.sh`,
which translates `hsync.env` into CLI flags (see
[cli-config.md](cli-config.md)); the NAS-local keys (`HSYNC_SYNC_DIRECTION`,
`HSYNC_STABILITY_DELAY`, `HSYNC_MAX_WAIT_TIMEOUT`, `HSYNC_FILTER_PATH`,
`HSYNC_IGNORE_PATTERNS`, `HSYNC_MASS_DELETE_*`, `HSYNC_MAX_CONCURRENT_TRANSFERS`,
`HSYNC_P2P_PORT`, `HSYNC_PREFER_MASTER`, `HSYNC_NEVER_MASTER`, `HSYNC_INSECURE`,
`HSYNC_CA_CERT`, `HSYNC_QUIET`, `HSYNC_DRY_RUN`) have no client environment
counterpart and exist only in the launcher.

## Invariants

- `config_get.php`, `config_set.php` and the form share one key set
  (`tests/test_wd_os5_web.py::test_php_whitelists_match_the_form`); only the
  per-file retry waits are whitelisted without a field.
- Every numeric field is clamped to the range the engine accepts
  (`config_set.php::clamp_int`), so a saved file is always launchable.
- The English dictionary equals the inline `data-i18n` fallback text
  (`test_inline_labels_match_the_dictionary`), and every locale carries the same
  keys.
- `start.sh` never passes an empty argument to a numeric flag and disables the
  periodic check explicitly with `HSYNC_RESYNC=0`.

## "Connect another device" command generator

`renderConnectCommand()` (`app.js`) builds the `hsync` command to run on a
second device to join the sync group coordinated by the NAS. The command is
shown in a readonly `#hsync-connect-command` textarea (not `<code>`: the WD
dashboard / remotewd injector can strip or hide `code` contents). It is
recomputed live from the form fields (`#HSYNC_BUCKET`, `#HSYNC_P2P_GROUP`,
`#HSYNC_P2P_KEY`, bootnode fields) on `input`/`change`/`keyup` — delegated on
`document` so they survive dashboard node replacement (`app.js::boot`) — and on
mode switches (`syncModeVisibility`). After a successful **Save** the command is
rebuilt from the persisted configuration (`applyConfig(await loadConfig())`), so
the section always reflects what `hsync.env` actually contains.

Scope: **p2p mode only**. In server mode the NAS is a client of a central
`hsync-server`, so the section shows an explanatory note instead of a command.
Only connection-matching fields are emitted; per-device sync behaviour and the
role are omitted because election resolves them. The shared password is rendered
masked (`KEY_MASK`, fixed length) with a reveal toggle (`keyRevealed`); the Copy
button copies the unmasked command via `navigator.clipboard` with an
`execCommand` fallback. Every interpolated value is POSIX single-quoted via
`shellQuote`, so the command is safe to paste regardless of its contents.

> Human doc: `../../packaging/wd-os5/README.md`.
> Domain rule: any CLI flag / `HSYNC_*` / i18n change requires the synchronized
> `packaging/wd-os5` update (see `../../AGENTS.md`).
