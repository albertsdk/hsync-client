# HSYNC Client — LLM Wiki (Index)

Agent-optimized knowledge base for `hsync-client`, the sync daemon/CLI. It runs in
two mutually exclusive modes — **remote** (talks to an `hsync-server` over
REST+WS) and **LAN** (peer-to-peer master/slave over mDNS + HTTPS/WSS) — sharing
one watcher + transfer core. Built on `hsync-common` (models, protocol, utils).

> End-user / human docs live in [`../docs/`](../docs/README.md)
> (`getting-started`, `getting-started-server`, `getting-started-lan`,
> `usage`, `configuration`, `configuration-server`, `configuration-lan`,
> `sync-modes-server`, `sync-modes-lan`, `server-mode`, `lan-mode`,
> `sync-flow-server`, `sync-flow-lan`, `examples-server`, `examples-lan`,
> `faq-server`, `faq-lan`). This wiki is the **first search surface**:
> start here, follow the `path/to/file.py:line` references to reach code directly.

## Map

| Doc | Scope |
| --- | ----- |
| [architecture.md](architecture.md) | Entry points, the two run modes, config flow, layering, watcher/transfer core |
| [modules/sync-core.md](modules/sync-core.md) | `sync.py`, `transfer.py`, `event_dispatch.py`, `file_event_handler.py`, `event_queue.py` |
| [modules/remote-mode.md](modules/remote-mode.md) | server mode: `remote_websocket.py`, REST via `utils` session, reconnect |
| [modules/lan-mode.md](modules/lan-mode.md) | `lan.py` discovery/election, `lan_server.py`, `lan_websocket.py`, `lan_handler.py`, `pinning.py`, `ssl_utils.py` |
| [modules/cli-config.md](modules/cli-config.md) | `cli.py`, `main_logic.py`, `config.py`, `constants.py`, `validators.py` |
| [modules/autotune.md](modules/autotune.md) | `autotune.py` — adaptive tuning of timeout, retry & concurrency |
| [data-models.md](data-models.md) | reused common models + client-local state stores (sync_state, tombstones, pins) |
| [decisions.md](decisions.md) | pointer to ecosystem ADRs + client-local choices |

## Quick facts

- Package root: `hsync_client/` (`hsync_client/__init__.py:1`).
- Entry points: `main.main` (`main.py:25`) → Typer command `cli.main`
  (`cli.py:101`); console script `cli.run` (`cli.py:674`).
- Orchestrator: `run_sync` (`main_logic.py:529`) — validates, branches to remote or
  LAN, starts the watcher. Logs `hsync_common.runtime.runtime_summary()`
  (container/TTY/cgroup limits) at startup.
- Mode branch: remote → `remote_websocket.remote_events` (`remote_websocket.py:58`);
  LAN → `lan_handler.run_lan_mode` (`lan_handler.py:109`).
- Version SSOT: `HSYNC_VERSION = VERSION` (`constants.py:32`).
- Config: env + CLI merged into the global `CONFIG: HsyncConfig` (`config.py:391`),
  set via `set_config` (`config.py:397`), read via `get_config` (`config.py:422`).
- Reuses `hsync_common.models` directly — there is **no** `hsync_client/models/`.

## Conventions used in this wiki

- Every claim is anchored to code via `path/to/file.py:line`.
- No large code blocks are duplicated; intent is described and the code referenced.
- Kept in sync with `../docs/` per the Dual-Docs rule in `CLAUDE.md`.
