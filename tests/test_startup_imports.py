# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

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

# Modules the fast paths must not import. ``asyncio`` and ``hsync_common.models``
# are the deep stdlib/shared costs that used to be pulled transitively by
# ``hsync_common.protocol.capabilities`` (via ``hashing``/``peer_download``).
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


def _clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("HSYNC_")}
    env["NO_COLOR"] = "1"
    return env


def _loaded_forbidden_modules() -> list[str]:
    """Return the forbidden modules present after ``import hsync_client.cli``."""
    code = (
        "import sys\n"
        "import hsync_client.cli\n"
        f"forbidden = {FORBIDDEN!r}\n"
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
    assert _loaded_forbidden_modules() == []


def test_capabilities_json_lazy_import_emits_document() -> None:
    """--capabilities-json still builds the document via its lazy import."""
    proc = subprocess.run(
        [sys.executable, "-m", "hsync_client", "--capabilities-json"],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["schema_version"] == 2
    assert isinstance(doc["subcommands"], list)
