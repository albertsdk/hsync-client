# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Startup import-surface guard for the CLI fast paths.

Pins the invariant behind the startup optimization: importing ``hsync_client.cli``
(what ``hsync --version``/``--help``/``--help-symbols`` do) must **not** pull the
sync engine or its heavy dependencies. Each check runs in a fresh subprocess so
the pytest runner's own ``sys.modules`` cannot mask a regression.

These are white-box guards over the import surface, not black-box behaviour
tests; the public output is already pinned by ``tests/test_cli.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from hsync_common.constants import HSYNC_VERSION

# Modules the CLI fast paths must not import. ``asyncio`` and
# ``hsync_common.models`` are the deep stdlib/shared costs that used to be pulled
# transitively by ``hsync_common.protocol.capabilities`` (via ``hashing``/
# ``peer_download``).
FORBIDDEN = [
    "watchdog",
    "zeroconf",
    "websockets",
    "zstandard",
    "cryptography",
    "loguru",
    "requests",
    "asyncio",
    "hsync_common.models",
]

# Mode-specific stacks that ``import hsync_client.main_logic`` must not pull: the
# P2P stack (zeroconf) and the remote WebSocket stack (websockets) are imported
# lazily inside ``run_sync`` / ``_run_with_stdin_commands``. The shared engine
# (requests, loguru, ...) is legitimately loaded at import time.
FORBIDDEN_MAIN_LOGIC = ["zeroconf", "websockets"]


def _clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("HSYNC_")}
    env["NO_COLOR"] = "1"
    return env


def _loaded_forbidden_modules(import_stmt: str, forbidden: list[str]) -> list[str]:
    """Return the ``forbidden`` modules present after running ``import_stmt``."""
    code = (
        "import sys\n"
        f"{import_stmt}\n"
        f"forbidden = {forbidden!r}\n"
        "loaded = [m for m in forbidden if m in sys.modules]\n"
        "print('\\n'.join(sorted(loaded)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    assert proc.returncode == 0, proc.stderr
    return [line for line in proc.stdout.splitlines() if line]


def test_import_cli_leaves_sync_engine_unimported() -> None:
    """The CLI module import must not load the sync engine or its heavy deps."""
    assert _loaded_forbidden_modules("import hsync_client.cli", FORBIDDEN) == []


def test_import_main_logic_leaves_mode_stacks_unimported() -> None:
    """Importing main_logic must not pull the P2P or remote WebSocket stacks."""
    assert _loaded_forbidden_modules("import hsync_client.main_logic", FORBIDDEN_MAIN_LOGIC) == []


def _run_main(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``main()`` with ``argv`` and report which heavy modules it loaded.

    The loaded modules are written to stderr so stdout stays the real program
    output under test.
    """
    code = (
        "import sys\n"
        f"sys.argv = {argv!r}\n"
        "from hsync_client.main import main\n"
        "main()\n"
        "loaded = [m for m in ('typer', 'hsync_client.cli') if m in sys.modules]\n"
        "print(','.join(loaded), file=sys.stderr)\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )


def test_version_fast_path_answers_without_importing_the_cli() -> None:
    """--version must print and exit without the CLI framework or the CLI module.

    The Desktop polls ``--version`` at startup, so the flag is answered in
    ``main`` before ``hsync_client.cli`` (Typer + every option module) is
    imported; the loaded-module probe pins that.
    """
    proc = _run_main(["hsync", "--version"])

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"hsync {HSYNC_VERSION}\n"
    assert proc.stderr.strip() == "", f"fast path imported heavy modules: {proc.stderr}"


def test_help_symbols_fast_path_answers_without_importing_the_cli() -> None:
    """--help-symbols prints the legend without importing the CLI framework."""
    proc = _run_main(["hsync", "--help-symbols"])

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("Log legend:")
    assert proc.stderr.strip() == "", f"fast path imported heavy modules: {proc.stderr}"


def test_module_invocation_matches_the_version_ssot() -> None:
    """``python -m hsync_client --version`` reports the version SSOT verbatim."""
    proc = subprocess.run(
        [sys.executable, "-m", "hsync_client", "--version"],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"hsync {HSYNC_VERSION}\n"


def test_module_invocation_prints_the_legend() -> None:
    """``python -m hsync_client --help-symbols`` still prints the legend."""
    proc = subprocess.run(
        [sys.executable, "-m", "hsync_client", "--help-symbols"],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("Log legend:")


def test_capabilities_json_lazy_import_emits_document() -> None:
    """--capabilities-json still builds the document via its lazy import."""
    from hsync_common.protocol.capabilities import CAPABILITIES_SCHEMA_VERSION

    proc = subprocess.run(
        [sys.executable, "-m", "hsync_client", "--capabilities-json"],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["schema_version"] == CAPABILITIES_SCHEMA_VERSION
    assert isinstance(doc["subcommands"], list)
