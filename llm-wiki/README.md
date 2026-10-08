# HSYNC Client — LLM Wiki (Index)

Agent-optimized knowledge base for `hsync-client`, the sync daemon/CLI. It runs in
two mutually exclusive modes — **remote** (talks to an `hsync-server` over
REST+WS) and **p2p** (peer-to-peer master/slave; mDNS or bootnode discovery over
HTTPS/WSS) — sharing
one watcher + transfer core. Built on `hsync-common` (models, protocol, utils).

> End-user / human docs live in [`../docs/`](../docs/README.md)
> (`getting-started`, `getting-started-server`, `getting-started-p2p`,
> `usage`, `configuration`, `configuration-server`, `configuration-p2p`,
> `sync-modes-server`, `sync-modes-p2p`, `server-mode`, `p2p-mode`,
> `sync-flow-server`, `sync-flow-p2p`, `examples-server`, `examples-p2p`,
> `faq-server`, `faq-p2p`, `internal/testing`,
> `internal/stress-test-plan`). This wiki is the
> **first search surface**:
> start here, follow the `path::symbol` references to reach code directly.

## Map

| Doc | Scope |
| --- | ----- |
| [architecture.md](architecture.md) | Entry points, the two run modes, config flow, layering, watcher/transfer core |
| [modules/sync-core.md](modules/sync-core.md) | `watcher.py`, `sync.py`, `transfer.py`, `event_dispatch.py`, `file_event_handler.py`, `event_queue.py` |
| [modules/remote-mode.md](modules/remote-mode.md) | server mode: `remote_websocket.py`, REST via `utils` session, `server_trust.py` server TLS trust, reconnect |
| [modules/p2p-mode.md](modules/p2p-mode.md) | `p2p_peers.py` + `p2p_discovery.py` interface + `p2p_discovery_mdns.py` / `p2p_discovery_hybrid.py` (mDNS + bootnode over one shared manager) / `boot_client.py` + `stun.py` (bootnode plan) discovery/election, `p2p_server.py`, `p2p_websocket.py`, `p2p_handler.py`, `pinning.py`, `known_hosts.py` + `bootnode_trust.py` + `server_trust.py` (trust stores), `trust_messages.py` (shared SSH-style trust prose), `ssl_utils.py`, `identity.py` (device identity + instance lock) |
| [modules/cli-config.md](modules/cli-config.md) | `cli.py`, `main_logic.py`, `config.py`, `constants.py` |
| [modules/wd-os5-web.md](modules/wd-os5-web.md) | WD My Cloud OS5 web UI (`packaging/wd-os5/Apkg/web`) + connect-command generator |
| [modules/autotune.md](modules/autotune.md) | `autotune.py` — adaptive tuning of timeout, retry & concurrency |
| [modules/benchmarks.md](modules/benchmarks.md) | L1 micro-benchmark suite + L2 loopback p2p peer-download bench (`bench/`): tiers, scenario→code anchors, methodology, findings |
| [data-models.md](data-models.md) | reused common models + client-local state stores (sync_state, tombstones, pins) |
| [decisions.md](decisions.md) | pointer to ecosystem ADRs + client-local choices |

## Quick facts

- Package root: `hsync_client/` (`hsync_client/__init__.py`).
- Entry points: `main.main` (`main.py::main`) → Typer command `cli.main`
  (`cli.py::main`); console script `cli.run` (`cli.py::run`). `main` calls
  `multiprocessing.freeze_support()` first, then imports the CLI lazily (see
  `decisions.md`: PyInstaller × loguru `enqueue=True` helper re-exec).
- Orchestrator: `run_sync` (`main_logic.py::run_sync`) — validates, branches to remote or
  p2p, starts the watcher. Logs `hsync_common.runtime.runtime_summary()`
  (container/TTY/cgroup limits) at startup.
- Mode branch: remote → `remote_websocket.remote_events` (`remote_websocket.py::remote_events`);
  p2p → `p2p_handler.run_p2p_mode` (`p2p_handler.py::run_p2p_mode`).
- Version SSOT: `HSYNC_VERSION` re-exported from `hsync_common.constants`
  (`hsync_common/constants.py::HSYNC_VERSION`); `CLIENT_NAME = "hsync-client"` (`constants.py::CLIENT_NAME`) feeds the
  `X-Client-Name` REST header build in `main_logic.py`.
- Config: env + CLI merged into the global `CONFIG: HsyncConfig` (`config.py::CONFIG`),
  set via `set_config` (`config.py::set_config`); modules read `CONFIG` directly.
- Reuses `hsync_common.models` directly — there is **no** `hsync_client/models/`.

## Conventions used in this wiki

- Every claim is anchored to code via `path::symbol`.
- No large code blocks are duplicated; intent is described and the code referenced.
- Kept in sync with `../docs/` per the Dual-Docs rule in `AGENTS.md`.
