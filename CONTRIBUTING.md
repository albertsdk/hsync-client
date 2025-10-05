# Contributing to hsync-client

Thanks for your interest in contributing! This guide covers the development
workflow for the HSYNC command-line client.

## Prerequisites

- Python `>=3.12,<3.15` (lint/CI target: **3.13**)
- [Poetry](https://python-poetry.org/) for dependency management
- A checkout of [`hsync-common`](../hsync-common) as a **sibling directory**
  (declared as a Poetry path dependency)

## Setup

```bash
poetry install
```

## Building the Binary

```bash
./compile.sh            # build standalone binary (PyInstaller)
./compile.sh --clean    # clean artifacts and rebuild
# output: dist/<os>-<arch>/hsync
```

## Code Style

This project uses [ruff](https://docs.astral.sh/ruff/) for linting and
formatting (line length 100, target `py313`), enforced via pre-commit hooks.

```bash
poetry run ruff check --fix .
poetry run ruff format .
```

## Pre-commit Workflow

Pre-commit hooks run ruff (and an end-of-file fixer) on commit. When a hook
auto-fixes files, the commit aborts because files changed — **this is expected,
not a failure**. Re-stage and commit again:

```bash
poetry run ruff check --fix .
poetry run ruff format .
git add -A
git commit -m "type(scope): message"
# if hooks modified files and the commit aborted, re-stage and retry:
git add -A
git commit -m "type(scope): message"
```

## Tests

```bash
# Run the full test suite
poetry run pytest

# Run a specific test file
poetry run pytest tests/test_sync_local.py

# Run with coverage report
poetry run pytest --cov=hsync_client --cov-report=term-missing

# Run a test matching a keyword
poetry run pytest -k "conflict"
```

If the project configures mypy (see `pyproject.toml`), also run
`poetry run mypy .`.

### Test structure

All tests live at the project root under `tests/`. The suite is organised by
area; each file name starts with `test_` and groups related functionality.

| Area | Key files | What they cover |
|------|-----------|-----------------|
| **Sync engine** | `test_sync_local.py`, `test_sync_remote.py`, `test_sync_update.py`, `test_sync_workers.py`, `test_sync_permanent_failures.py` | File comparison, action generation, parallel transfers, permanent-failure skip-set |
| **Transfer** | `test_transfer_download.py`, `test_transfer_upload.py`, `test_transfer_status.py` | Upload/download, resumable transfers, status tracking |
| **WebSocket / events** | `test_websocket_events.py`, `test_events.py`, `test_event_queue.py`, `test_event_dispatch_*.py` | WS event processing, event queue, inbound dispatch (delete/rename/symlink) |
| **LAN mode** | `test_lan_peer.py`, `test_lan_peer_manager.py`, `test_lan_handler_advanced.py`, `test_lan_sync.py`, `test_lan_server_*.py`, `test_lan_websocket_*.py`, `test_lan_utils.py` | Discovery, election, master server, slave client, WSS, security, thread cap |
| **Security** | `test_security_inbound.py`, `test_pinning.py`, `test_ssl_utils.py` | Inbound path validation, TOFU pinning, SSL utils |
| **Config / CLI** | `test_config.py`, `test_cli.py`, `test_exceptions.py` | Config loading, CLI parsing (inline Typer validation), exception hierarchy |
| **Delta transfer** | `test_delta_transfer.py` | rsync-style block sync |
| **Models** | `test_models_*.py` | Common model wrappers (file, dir, event, sync_action, symlink) |
| **Other** | `test_compression.py`, `test_edge_cases.py`, `test_stdin_commands.py`, `test_logger.py`, `test_coverage_gaps.py`, `test_file_event_handler_base.py`, `test_main_logic_*.py`, `test_utils.py`, `test_retry.py` | Compression, edge cases, stdin protocol, logging, handler base, orchestrator, utils, retry |
| **Shared** | `tests/conftest.py` | Shared fixtures: temp directories, mock server, env vars |

### Writing new tests

1. Use `tmp_path` fixture from pytest for temporary directories
2. Mock the server with `responses` or `httpx` test fixtures
3. LAN mode tests may need `pytest-asyncio` for async discovery
4. Avoid tests that require a real HSYNC Server — use mocks

### Cross-project testing

The client depends on `hsync-common` models. When you change a model in
`hsync-common`, run the client's test suite to verify nothing breaks:

```bash
cd ../hsync-common && poetry run pytest && cd ../hsync-client && poetry run pytest
```

## Release Process

Releases are **tag-driven**: pushing a `vX.Y.Z` tag triggers the
`release.yml` GitHub Actions workflow, which gates on lint+test, then builds
native binaries on per-OS matrix runners (`linux-amd64`, `linux-arm64`,
`darwin-amd64`, `darwin-arm64`, `windows-amd64`) plus the Linux-only
`deb-all`/`wd-os5-all` packages, and publishes them with `SHA256SUMS` to the
GitHub Release. macOS binaries are unsigned (documented `xattr` workaround in
`docs/installation.md`).

### Version bump

Use the wrapper script to bump the version across `pyproject.toml` and
`hsync_client/__init__.py` in one step:

```bash
./bump-version.sh 1.2.3
```

The script delegates to the centralized Python version bumper in
`hsync-common/scripts/dev/bump-python-version.sh`.

### Build binaries (local / manual)

The release workflow builds everything automatically. To build locally for
testing or for a non-release platform:

```bash
# Build for the current platform
./compile.sh

# Build all targets (requires Docker)
./compile.sh --target deb-all       # .deb for amd64, arm64, armhf
./compile.sh --target wd-os5-all    # .bin for all WD OS5 models
```

### Create a release

1. Ensure the working tree is clean and on `main`.
2. Bump the version (see [Version bump](#version-bump)) and commit the change.
3. Tag the commit and push the tag:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

4. The `release.yml` workflow runs automatically: it builds all assets,
   generates `SHA256SUMS`, and creates the GitHub Release with
   auto-generated release notes.
5. Verify the release on the
   [Releases page](https://github.com/albertobasso/hsync-client/releases).

> You can also trigger a build-only run (no tag, no Release) via
> `workflow_dispatch` on the Actions tab — useful for testing build changes
> without cutting a release.

### Post-release checklist

- [ ] All platform binaries and `SHA256SUMS` are uploaded to the release
- [ ] `.deb` packages are tested on a clean Debian/Ubuntu install
- [ ] Release notes are published on GitHub
- [ ] `docs/configuration.md` is up to date with any new options
- [ ] `docs/usage.md` reflects any CLI changes

## Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):
`type(scope): summary` (`feat`, `fix`, `docs`, `refactor`, `test`, `chore`, …).

## Pull Requests

- Keep changes focused and covered by tests.
- When changing CLI options, update the reference in
  `docs/configuration.md` (and the overview in `docs/usage.md`).
- Ensure `ruff` and `pytest` pass locally before opening a PR.
