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
poetry run pytest tests/test_sync.py

# Run with coverage report
poetry run pytest --cov=hsync_client --cov-report=term-missing

# Run a test matching a keyword
poetry run pytest -k "conflict"
```

If the project configures mypy (see `pyproject.toml`), also run
`poetry run mypy .`.

### Test structure

| Directory | Purpose |
|-----------|---------|
| `tests/` | All test files live at the project root |
| `tests/conftest.py` | Shared fixtures: temp directories, mock server, env vars |
| `tests/test_sync.py` | Sync engine logic: file comparison, action generation |
| `tests/test_transfer.py` | Upload/download operations |
| `tests/test_websocket.py` | WebSocket handler and event processing |
| `tests/test_lan.py` | LAN mode: discovery, master election, pairing |
| `tests/test_config.py` | Configuration loading and CLI arg parsing |
| `tests/test_compression.py` | Zstandard compression/decompression |
| `tests/test_ignore.py` | `.hsyncignore` pattern matching |

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

### Version bump

Update the version in `pyproject.toml` and `hsync_client/__init__.py`.

### Build binaries

```bash
# Build for the current platform
./compile.sh

# Build all targets (requires Docker)
./compile.sh --target deb-all       # .deb for amd64, arm64, armhf
./compile.sh --target wd-os5-all    # .bin for all WD OS5 models
```

### Create GitHub Release

1. Go to [Releases](https://github.com/albertobasso/hsync-client/releases)
2. Click "Draft a new release"
3. Tag: `v1.0.1`
4. Title: `v1.0.1`
5. Upload the built artifacts (binaries, `.deb`, `.bin` files)
6. Click "Generate release notes" for the auto-generated changelog
7. Publish

### Post-release checklist

- [ ] All platform binaries are uploaded to the release
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
